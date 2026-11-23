"""SGLang worker registration cleanup."""

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..collection.sources import worker_host
from ..http import HttpClient

logger = logging.getLogger(__name__)


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

    def reconcile(self, workers: list[dict], observed_models: dict[str, str]) -> None:
        hosts = defaultdict(list)
        for worker in workers:
            hosts[worker_host(worker["url"])].append(worker)
        self._pending = {url: state for url, state in self._pending.items() if url in hosts}
        self._unavailable_since = {url: state for url, state in self._unavailable_since.items() if url in hosts}
        now = time.monotonic()
        for url, ranks in hosts.items():
            ids = tuple(rank["id"] for rank in ranks)
            expired = self._unavailable_expired(url, ranks, ids, now)
            actual = observed_models.get(url)
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
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
                pass
            except (httpx.HTTPError, httpx.InvalidURL, ValueError):
                # Other HTTP failures do not establish that the host is unavailable.
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
