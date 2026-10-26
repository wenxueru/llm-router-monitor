"""Optional worker keepalive and registration cleanup."""

import logging
import random
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import httpx

from .config import MonitorConfig
from .http import HttpClient
from .state import SnapshotStore

logger = logging.getLogger(__name__)


class KeepaliveWarmer:
    def __init__(self, config: MonitorConfig, store: SnapshotStore):
        self.config = config
        self.store = store
        self._http = HttpClient(token=config.worker_api_key)
        self._pool = ThreadPoolExecutor(max_workers=32)
        self._round = 0

    def idle_workers(self) -> dict[str, str]:
        snapshot = self.store.snapshot()
        if snapshot.get("router_type", "sglang") != "sglang":
            return {}
        active = snapshot.get("active_pw", {})
        idle = {row["url"] for row in snapshot.get("worker_rows", []) if row.get("running") == 0 and row.get("waiting") == 0}
        return {
            url: model
            for model, urls in snapshot.get("by_model", {}).items()
            if model != "unknown" and (not self.config.keepalive_model or model == self.config.keepalive_model)
            for url in urls
            if url in idle and url in active and active[url] <= 0
        }

    def ping(self, target: tuple[str, str, str]) -> bool:
        url, model, tag = target
        started = time.monotonic()
        words = random.randint(self.config.keepalive_prompt_words // 2, self.config.keepalive_prompt_words)
        prompt = f"Continue this sequence ({tag}): " + " ".join(
            random.choices(("amber", "birch", "cloud", "delta", "elm", "fern", "green", "hill"), k=words)
        )
        try:
            response = self._http.request_json(
                f"{url}/v1/chat/completions",
                method="POST",
                payload={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": self.config.keepalive_max_tokens,
                    "temperature": 0,
                },
                timeout=40,
            )
            return bool(response.get("choices"))
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
            logger.warning(
                "Keepalive failed: worker=%s request=%s elapsed=%.1fs error=%s: %s",
                url,
                tag,
                time.monotonic() - started,
                type(error).__name__,
                error,
            )
            return False

    def run_round(self) -> dict[str, bool]:
        self._round += 1
        targets = [
            (url, model, f"keepalive-{self._round}-{index}")
            for url, model in self.idle_workers().items()
            for index in range(random.randint(1, self.config.keepalive_concurrency))
        ]
        results = {}
        for (url, _, _), ok in zip(targets, self._pool.map(self.ping, targets)):
            results[url] = results.get(url, True) and ok
        return results

    def run(self, stop: threading.Event) -> None:
        backoff = self.config.keepalive_interval
        try:
            while not stop.is_set():
                results = self.run_round()
                backoff = (
                    self.config.keepalive_interval
                    if all(results.values())
                    else min(backoff * 2, self.config.keepalive_max_interval)
                )
                stop.wait(backoff)
        finally:
            self.close()

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._http.close()


class AutoUnregister:
    """Opt-in cleanup of mismatched models or persistently unreachable hosts."""

    def __init__(self, router_url: str, *, model_mismatch: bool = True, unavailable_after: float = 0, api_key: str = ""):
        self.router_url = router_url
        self.model_mismatch = model_mismatch
        self.unavailable_after = unavailable_after
        self._unavailable_since = {}
        self._http = HttpClient(token=api_key, origins=(router_url,))
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._pending = {}

    def reconcile(self, workers: list[dict], by_model: dict[str, list[str]]) -> None:
        hosts = defaultdict(list)
        for worker in workers:
            hosts[worker["url"].split("@")[0]].append(worker)
        observed = {url: model for model, urls in by_model.items() if model != "unknown" for url in urls}
        self._pending = {url: state for url, state in self._pending.items() if url in hosts}
        self._unavailable_since = {url: state for url, state in self._unavailable_since.items() if url in hosts}
        now = time.monotonic()
        for url, ranks in hosts.items():
            ids = tuple(rank["id"] for rank in ranks)
            expired = self._unavailable_expired(url, ranks, ids, now)
            actual = observed.get(url)
            mismatch = (
                self.model_mismatch
                and actual is not None
                and any(rank.get("model_id") and rank["model_id"] != actual for rank in ranks)
            )
            if not mismatch and not expired:
                continue
            previous = self._pending.get(url)
            if previous is not None:
                previous_ids, future = previous
                if not future.done():
                    continue
                same_registration = previous_ids == ids
                if same_registration and future.result() and mismatch:
                    continue
                del self._pending[url]
                if same_registration and not mismatch:
                    # Accepted removals can leave registrations behind; retry after another grace period.
                    self._unavailable_since[url] = (ids, now)
                    continue
            reason = (
                f"serving model {actual} differs from registration"
                if mismatch
                else f"all ranks unhealthy for {self.unavailable_after:g}s and host unreachable"
            )
            self._pending[url] = (ids, self._pool.submit(self._unregister, url, ranks, reason, probe=not mismatch))

    def _unavailable_expired(self, url: str, ranks: list[dict], ids: tuple, now: float) -> bool:
        if not self.unavailable_after or any(rank.get("is_healthy") is not False or rank.get("load", 0) > 0 for rank in ranks):
            self._unavailable_since.pop(url, None)
            return False
        previous_ids, since = self._unavailable_since.get(url, (ids, now))
        if ids != previous_ids:
            since = now
        self._unavailable_since[url] = (ids, since)
        return now - since >= self.unavailable_after

    def _unregister(self, url: str, ranks: list[dict], reason: str, *, probe: bool = False) -> bool:
        if probe:
            try:
                self._http.request_json(f"{url}/v1/models", method="GET", timeout=2)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
            except (httpx.HTTPError, httpx.InvalidURL, ValueError):
                # An HTTP response or a read timeout does not prove the host is gone.
                return False
            else:
                return False
        try:
            worker_id = ranks[0]["id"]
            if any("@" in rank["url"] for rank in ranks):
                # DP-aware builds require a host-level registration before deletion.
                response = self._http.request_json(f"{self.router_url}/workers", method="POST", payload={"url": url})
                worker_id = response.get("worker_id") or worker_id
            self._http.request_json(f"{self.router_url}/workers/{worker_id}", method="DELETE")
            logger.info("Requested deregistration of %s: %s", url, reason)
            return True
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            logger.warning("Failed to unregister %s", url, exc_info=True)
            return False

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._http.close()
