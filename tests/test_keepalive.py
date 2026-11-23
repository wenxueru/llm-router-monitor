"""Exercise keepalive scheduling, HTTP requests, and cancellation."""

import asyncio
import json
import threading
import time
import unittest
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from router_monitor.collection.sources import RouterSource
from router_monitor.config import MonitorConfig
from router_monitor.maintenance.keepalive import KeepaliveControl, KeepaliveWarmer, _TrafficWindow, _wait_for_poll
from router_monitor.state import SnapshotStore


def publish_workers(store, workers, *, running=0):
    store.publish({
        "collected_at": 0,
        "by_model_stats": {},
        "router_type": "sglang",
        "registry_ok": True,
        "by_model": {"chosen": workers},
        "active_pw": dict.fromkeys(workers, running),
        "worker_rows": [{"url": url, "running": running, "waiting": 0} for url in workers],
    })


class KeepaliveControlTests(unittest.TestCase):
    def test_applying_current_settings_restarts_a_failed_warmer(self):
        control = KeepaliveControl(MonitorConfig(host="router", keepalive_unconditional=True), SnapshotStore())
        threads = Queue()
        fail = threading.Event()
        self.addCleanup(control.close)

        def fail_once(_, stop):
            restarting = fail.is_set()
            threads.put(threading.current_thread())
            if restarting:
                stop.wait(2)
                return
            fail.wait(2)
            raise RuntimeError("worker scheduler failed")

        with (
            patch.object(KeepaliveWarmer, "run", fail_once),
            self.assertLogs("router_monitor.maintenance.keepalive", level="ERROR"),
        ):
            control.start()
            thread = threads.get(timeout=2)
            fail.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            control.update(control.settings())
            self.assertIsNot(threads.get(timeout=2), thread)


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_interrupts_a_long_poll_interval(self):
        stop = threading.Event()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        await asyncio.wait_for(_wait_for_poll(stop, time.monotonic() + 60), timeout=1)
        self.assertTrue(stop.is_set())

    def test_adaptive_uses_per_worker_budget_for_eligible_workers(self):
        config = MonitorConfig(host="router", keepalive_model="chosen", keepalive_concurrency=3)
        store = SnapshotStore()
        warmer = KeepaliveWarmer(config, store)
        snapshot = {
            "router_type": "sglang",
            "by_model": {"chosen": ["first", "second", "unknown"], "other": ["other"]},
            "active_pw": {"first": 0, "second": 0, "other": 0},
            "worker_rows": [{"url": url, "running": 0, "waiting": 0} for url in ("first", "second", "unknown", "other")],
        }
        self.assertEqual(
            list(warmer.request_slots(snapshot)),
            [
                ("first", "chosen", 0),
                ("second", "chosen", 0),
                ("first", "chosen", 1),
                ("second", "chosen", 1),
                ("first", "chosen", 2),
                ("second", "chosen", 2),
            ],
        )
        snapshot["worker_rows"][-1]["waiting"] = 1
        self.assertEqual(list(warmer.request_slots(snapshot)), [])
        snapshot["worker_rows"][-1]["waiting"] = 0
        snapshot["by_model"] = {"other": ["other"]}
        self.assertEqual(list(warmer.request_slots(snapshot)), [])

    async def test_worker_payload_and_adaptive_round_backoff(self):
        config = MonitorConfig(
            host="router",
            interval=60,
            worker_api_key="worker-key",
            keepalive_interval=2,
            keepalive_concurrency=1,
            keepalive_min_tokens=80,
            keepalive_max_tokens=128,
        )
        store = SnapshotStore()
        publish_workers(store, ["http://worker"])
        warmer = KeepaliveWarmer(config, store)
        stop = threading.Event()
        attempts = []
        now = time.monotonic()
        client_class = httpx.AsyncClient

        async def advance(_stop, deadline):
            nonlocal now
            now = deadline
            await asyncio.sleep(0)

        def worker(request):
            body = json.loads(request.content)
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer worker-key")
            self.assertEqual((body["model"], body["stream"], body["min_tokens"]), ("chosen", True, 80))
            self.assertIn(body["max_tokens"], range(80, 129))
            attempts.append(now)
            if len(attempts) == 4:
                stop.set()
            data = 'data: {"choices": [{}]}\n\n' if len(attempts) > 2 else ""
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=data + "data: [DONE]\n\n")

        with (
            patch("router_monitor.maintenance.keepalive.time", SimpleNamespace(monotonic=lambda: now)),
            patch("router_monitor.maintenance.keepalive._wait_for_poll", advance),
            patch(
                "router_monitor.maintenance.keepalive.httpx.AsyncClient",
                side_effect=lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(worker)),
            ) as create_client,
        ):
            await asyncio.wait_for(warmer._run(stop, lambda: 0), 3)
        self.assertIsNone(create_client.call_args.kwargs["limits"].max_connections)
        self.assertEqual(len(attempts), 4)
        first, second, third = (b - a for a, b in zip(attempts, attempts[1:]))
        self.assertGreater(second, first)
        self.assertLess(third, first)

    async def test_unconditional_refills_each_request_and_tracks_worker_changes(self):
        config = MonitorConfig(
            host="router",
            keepalive_unconditional=True,
            keepalive_interval=0.5,
            keepalive_concurrency=2,
        )
        store = SnapshotStore()
        publish_workers(store, ["http://first", "http://second"], running=10)
        warmer = KeepaliveWarmer(config, store)
        stop = threading.Event()
        opened = asyncio.Queue()
        loop = asyncio.get_running_loop()

        async def worker(request):
            finished = loop.create_future()
            opened.put_nowait((request.url.host, finished))
            ok = await finished
            data = 'data: {"choices": [{}]}\n\n' if ok else ""
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=data + "data: [DONE]\n\n")

        async def next_request(host):
            target, finished = await asyncio.wait_for(opened.get(), 2)
            self.assertEqual(target, host)
            return finished

        async with httpx.AsyncClient(transport=httpx.MockTransport(worker)) as client:
            task = asyncio.create_task(warmer._maintain_concurrency(client, stop))
            try:
                first = []
                second = []
                for _ in range(4):
                    host, finished = await asyncio.wait_for(opened.get(), 2)
                    (first if host == "first" else second).append(finished)
                self.assertEqual((len(first), len(second)), (2, 2))

                publish_workers(store, ["http://second", "http://first"], running=10)
                second[0].set_result(True)
                replacement = await next_request("second")
                self.assertTrue(all(not request.done() for request in first))
                self.assertEqual(store.snapshot()["keepalive"]["inflight"], 4)

                failed_at = loop.time()
                first[0].set_result(False)
                replacement.set_result(True)
                replacement = await next_request("second")
                retried = await next_request("first")
                self.assertGreaterEqual(loop.time() - failed_at, config.keepalive_interval)

                publish_workers(store, ["http://second", "http://third"], running=10)
                added = [await next_request("third"), await next_request("third")]
                self.assertTrue(first[1].cancelled() and retried.cancelled())
                self.assertFalse(replacement.done() or second[1].done())
                self.assertEqual(store.snapshot()["keepalive"]["inflight"], 4)
            finally:
                stop.set()
                await asyncio.wait_for(task, 2)
        self.assertTrue(all(request.cancelled() for request in [replacement, second[1], *added]))

    async def test_adaptive_cancellation_resume_and_hot_update(self):
        opened = closed = 0
        last_opened = 0.0

        async def stream(reader, writer):
            nonlocal opened, closed, last_opened
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":")[1]) for line in head.lower().splitlines() if line.startswith(b"content-length:")
                )
                await reader.readexactly(length)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
                    b'data: {"choices": [{}]}\n\n'
                )
                await writer.drain()
                last_opened = time.monotonic()
                opened += 1
                await reader.read()
                closed += 1
            finally:
                writer.close()
                await writer.wait_closed()

        async def until(predicate):
            async def poll():
                while not predicate():
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(poll(), 3)

        live = 0
        unavailable = False

        def samples():
            if unavailable:
                return []
            return [
                {"name": "smg_http_requests_total", "labels": {"method": "GET", "path": "/workers"}, "value": opened},
                {
                    "name": "smg_http_requests_total",
                    "labels": {"method": "POST", "path": "/v1/chat/completions"},
                    "value": live,
                },
            ]

        config = MonitorConfig(
            host="router",
            keepalive=True,
            keepalive_concurrency=2,
            keepalive_traffic_poll_interval=0.1,
        )
        server = await asyncio.start_server(stream, "127.0.0.1", 0)
        async with server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            store = SnapshotStore()
            publish_workers(store, [url])
            control = KeepaliveControl(config, store)
            quiet = 0.7

            def paused(status):
                return store.snapshot().get("keepalive", {}).get("status") == status

            with (
                patch.object(RouterSource, "read_metrics", side_effect=samples),
                patch.object(_TrafficWindow, "quiet_seconds", quiet),
            ):
                control.start()
                try:
                    await until(lambda: opened == 2)
                    active_at = time.monotonic()
                    live += 1
                    await until(lambda: closed == 2 and paused("paused_for_traffic"))
                    self.assertEqual(store.snapshot()["keepalive"]["cancelled"], 2)
                    control.update(control.settings().model_copy(update={"keepalive_prompt_words": 128}))
                    await until(lambda: opened == 4)
                    self.assertGreaterEqual(last_opened - active_at, quiet)
                    unavailable = True
                    await until(lambda: closed == 4 and paused("waiting_for_router_metrics"))
                    live += 1
                    unavailable = False
                    await until(lambda: paused("paused_for_traffic"))
                    self.assertEqual(opened, 4)
                    await until(lambda: opened == 6)
                finally:
                    await asyncio.to_thread(control.close)
                    await until(lambda: closed == opened)
