"""Schedule synthetic SGLang requests with optional traffic preemption."""

import asyncio
import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterator

import httpx

from ..collection.sources import RouterSource
from ..config import KeepaliveSettings, MonitorConfig
from ..state import SnapshotStore

logger = logging.getLogger(__name__)


def _keepalive_prompt(tag: str, word_limit: int) -> str:
    words = random.randint(max(1, word_limit // 8), word_limit)
    task = random.choice((
        "Summarize these observations for a teammate taking over the shift.",
        "Suggest a troubleshooting plan and explain which checks to run first.",
        "Draft a short status update, separating observations from hypotheses.",
        "Compare two plausible explanations and identify what evidence would distinguish them.",
    ))
    notes = []
    index = 1
    while len(notes) < words:
        component = random.choice(("indexer", "gateway", "queue", "cache", "scheduler"))
        symptom = random.choice(("delayed responses", "repeated retries", "uneven load", "normal throughput"))
        notes.extend(f"Check {index}: the {component} reported {symptom}.".split())
        index += 1
    context = " ".join(notes[:words])
    return f"{task}\n\nSynthetic observations ({tag}):\n{context}"


class KeepaliveControl:
    """Apply live settings between warmer runs, after the old requests stop."""

    def __init__(self, config: MonitorConfig, store: SnapshotStore):
        self._config = config
        self._store = store
        self._traffic = _TrafficWindow(config.interval)
        self._lock = threading.Lock()
        self._closed = False
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            self._start_if_enabled()

    def settings(self) -> KeepaliveSettings:
        with self._lock:
            config = self._config
        return KeepaliveSettings.from_config(config)

    def update(self, settings: KeepaliveSettings) -> None:
        with self._lock:
            config = settings.apply(self._config)
            if config != self._config:
                self._config = config
                if self._stop is not None:
                    self._stop.set()
            self._start_if_enabled()

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            if self._stop is not None:
                self._stop.set()

    def close(self) -> None:
        self.stop()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join()

    def _start_if_enabled(self) -> None:
        if not self._closed and self._thread is None and self._config.keepalive_mode != "off":
            thread = self._thread = threading.Thread(target=self._run, daemon=True)
            thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                config = self._config
                if self._closed or config.keepalive_mode == "off":
                    self._thread = self._stop = None
                    return
                self._stop = stop = threading.Event()
            try:
                KeepaliveWarmer(config, self._store, traffic=self._traffic).run(stop)
            except Exception:
                logger.exception("Keepalive scheduler stopped for %s", config.router_url)
            finally:
                self._store.clear_keepalive()
            with self._lock:
                if config is self._config:
                    self._thread = self._stop = None
                    return


class KeepaliveWarmer:
    def __init__(self, config: MonitorConfig, store: SnapshotStore, *, traffic: "_TrafficWindow | None" = None):
        self.config = config
        self.store = store
        self._traffic = traffic

    async def ping(self, client: httpx.AsyncClient, url: str, model: str, tag: str) -> bool:
        started = time.monotonic()
        token_limit = self.config.keepalive_max_tokens
        min_tokens = self.config.keepalive_min_tokens
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": _keepalive_prompt(tag, self.config.keepalive_prompt_words)}],
            "max_tokens": random.randint(max(1, token_limit // 4, min_tokens), token_limit),
            "temperature": random.choice((0.2, 0.5, 0.8, 1.0)),
            "stream": True,
        }
        if min_tokens:
            payload["min_tokens"] = min_tokens
        try:
            async with client.stream("POST", f"{url}/v1/chat/completions", json=payload, timeout=40) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";", 1)[0] != "text/event-stream":
                    raise ValueError("Expected a keepalive SSE response")
                generated = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        return generated
                    event = json.loads(data)
                    if not isinstance(event, dict) or event.get("error"):
                        raise ValueError("Worker reported a keepalive error")
                    generated = generated or bool(event.get("choices"))
                raise ValueError("Keepalive SSE ended before [DONE]")
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

    def request_slots(self, snapshot: dict) -> Iterator[tuple[str, str, int]]:
        if snapshot.get("router_type") != "sglang":
            return
        idle = None
        if not self.config.keepalive_unconditional:
            idle = set()
            active = snapshot["active_pw"]
            for row in snapshot["worker_rows"]:
                running, waiting = row.get("running"), row.get("waiting")
                # Worker load also covers streams whose Router response headers already went out.
                if (running or 0) > 0 or (waiting or 0) > 0:
                    return
                url = row["url"]
                if running == waiting == 0 and url in active and active[url] <= 0:
                    idle.add(url)
        model_filter = self.config.keepalive_model
        workers = [
            (url, model)
            for model, urls in snapshot["by_model"].items()
            if model != "unknown" and (not model_filter or model == model_filter)
            for url in urls
            if idle is None or url in idle
        ]
        if not workers:
            return
        for slot in range(self.config.keepalive_concurrency):
            for url, model in workers:
                yield url, model, slot

    def run(self, stop: threading.Event) -> None:
        if self.config.keepalive_unconditional:
            asyncio.run(self._run(stop))
            return
        source = RouterSource(self.config)
        try:
            asyncio.run(self._run(stop, source.read_inference_requests))
        finally:
            source.close()

    async def _run(self, stop: threading.Event, read_requests: Callable[[], float | None] | None = None) -> None:
        headers = {"Authorization": f"Bearer {self.config.worker_api_key}"} if self.config.worker_api_key else None
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=20)
        async with httpx.AsyncClient(headers=headers, limits=limits, follow_redirects=True) as client:
            if read_requests is None:
                await self._maintain_concurrency(client, stop)
            else:
                await self._schedule_adaptive(client, stop, read_requests)

    async def _maintain_concurrency(self, client: httpx.AsyncClient, stop: threading.Event) -> None:
        tasks: dict[tuple[str, str, int], asyncio.Task[bool]] = {}
        retry_at: dict[tuple[str, str, int], float] = {}
        slots: set[tuple[str, str, int]] = set()
        snapshot = None
        dispatched = cancelled = 0
        last_report = None
        try:
            while not stop.is_set():
                current, _ = self.store.observation()
                if current is not snapshot:
                    snapshot = current
                    slots = set(self.request_slots(snapshot))
                    removed = [tasks.pop(slot) for slot in tasks.keys() - slots]
                    if removed:
                        cancelled += await _cancel_requests(removed)
                    for slot in retry_at.keys() - slots:
                        del retry_at[slot]
                    if stop.is_set():
                        break

                now = time.monotonic()
                for slot, task in tuple(tasks.items()):
                    if task.done():
                        del tasks[slot]
                        if not task.result():
                            retry_at[slot] = now + self.config.keepalive_interval
                for slot in slots:
                    if slot in tasks or now < retry_at.get(slot, now):
                        continue
                    retry_at.pop(slot, None)
                    dispatched += 1
                    url, model, _ = slot
                    tasks[slot] = asyncio.create_task(self.ping(client, url, model, f"keepalive-{dispatched}"))

                status = "ready" if slots else "waiting_for_workers"
                report = ("warming" if tasks else status, len(tasks), dispatched, cancelled)
                if report != last_report:
                    self.store.publish_keepalive(*report)
                    last_report = report
                timeout = min(0.25, min(retry_at.values(), default=now + 0.25) - now)
                if tasks:
                    await asyncio.wait(tasks.values(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(timeout)
        finally:
            await _cancel_requests(list(tasks.values()))

    async def _schedule_adaptive(
        self, client: httpx.AsyncClient, stop: threading.Event, read_requests: Callable[[], float | None]
    ) -> None:
        base_delay = self.config.keepalive_interval
        poll_interval = self.config.keepalive_traffic_poll_interval
        traffic = self._traffic if self._traffic is not None else _TrafficWindow(self.config.interval)
        tasks: list[asyncio.Task[bool]] = []
        backoff, next_round = base_delay, 0.0
        dispatched = cancelled = 0
        last_report = None
        try:
            while not stop.is_set():
                started = time.monotonic()
                requests = await asyncio.to_thread(read_requests)
                if stop.is_set():
                    break
                now = time.monotonic()
                snapshot, published_at = self.store.observation()
                pause = traffic.pause_reason(requests, snapshot, published_at, now)
                status = pause or "ready"
                inflight = sum(not task.done() for task in tasks)
                if pause:
                    if tasks:
                        cancelled += await _cancel_requests(tasks)
                        tasks.clear()
                        inflight = 0
                elif tasks and not inflight:
                    ok = all(await asyncio.gather(*tasks))
                    backoff = base_delay if ok else min(backoff * 2, self.config.keepalive_max_interval)
                    next_round = now + backoff
                    tasks.clear()
                elif not tasks and now >= next_round:
                    tasks = [
                        asyncio.create_task(self.ping(client, url, model, f"keepalive-{index}"))
                        for index, (url, model, _) in enumerate(self.request_slots(snapshot), dispatched + 1)
                    ]
                    inflight = len(tasks)
                    dispatched += inflight
                    if not tasks:
                        status = "waiting_for_idle_workers"
                report = ("warming" if inflight else status, inflight, dispatched, cancelled)
                if report != last_report:
                    self.store.publish_keepalive(*report)
                    last_report = report
                await _wait_for_poll(stop, started + poll_interval)
        finally:
            if tasks:
                await _cancel_requests(tasks)


class _TrafficWindow:
    quiet_seconds = 5.0

    def __init__(self, collection_interval: float):
        self.previous_requests: float | None = None
        self.quiet_until = 0.0
        self.max_age = max(5, 3 * collection_interval)

    def pause_reason(self, requests: float | None, snapshot: dict, published_at: float | None, now: float) -> str | None:
        previous = self.previous_requests
        if requests is not None:
            self.previous_requests = requests
        changed = requests is not None and previous is not None and previous != requests
        if changed or any(load > 0 for load in snapshot.get("active_pw", {}).values()):
            self.quiet_until = now + self.quiet_seconds
        if requests is None or previous is None:
            return "waiting_for_router_metrics"
        if now < self.quiet_until:
            return "paused_for_traffic"
        if not snapshot.get("registry_ok") or published_at is None or now - published_at > self.max_age:
            return "waiting_for_worker_metrics"
        return None


async def _cancel_requests(tasks: list[asyncio.Task[bool]]) -> int:
    cancelled = sum(task.cancel() for task in tasks)
    await asyncio.gather(*tasks, return_exceptions=True)
    return cancelled


async def _wait_for_poll(stop: threading.Event, deadline: float) -> None:
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.25))
