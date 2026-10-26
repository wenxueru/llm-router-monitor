"""Exercise real HTTP collection through snapshot storage and web responses."""

import json
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, call, patch

import httpx
from fastapi.testclient import TestClient

from router_monitor.collectors import RouterCollector
from router_monitor.config import MonitorConfig
from router_monitor.dashboard import WebDashboard, render_terminal
from router_monitor.http import HttpClient
from router_monitor.maintenance import AutoUnregister, KeepaliveWarmer
from router_monitor.state import SnapshotStore


class PipelineTests(unittest.TestCase):
    def test_router_worker_collection_and_dashboard(self):
        counters = {"cached": 100, "prompt": 200, "requests": 10}
        mutations = []
        connection_ports = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                connection_ports.append(self.client_address[1])
                if self.path == "/workers":
                    base = f"http://127.0.0.1:{self.server.server_port}"
                    body = json.dumps(
                        {
                            "workers": [
                                {
                                    "id": "a",
                                    "url": base + "@0",
                                    "load": 1,
                                    "is_healthy": True,
                                    "model_id": "registered-model",
                                },
                                {
                                    "id": "b",
                                    "url": base + "@1",
                                    "load": 2,
                                    "is_healthy": True,
                                    "model_id": "registered-model",
                                },
                            ]
                        }
                    )
                elif self.path == "/v1/models":
                    body = json.dumps({"data": [{"id": "test"}]})
                elif self.path == "/v1/loads":
                    body = json.dumps(
                        {
                            "loads": [
                                {"dp_rank": 0, "num_waiting_uncached_tokens": 100},
                                {"dp_rank": 1, "num_waiting_uncached_tokens": 200},
                            ]
                        }
                    )
                elif self.path == "/metrics":
                    body = (
                        f'smg_http_requests_total{{route="chat"}} {counters["requests"]}\n'
                        'smg_http_requests_total{route="health"} 3\n'
                        'smg_http_inflight_request_age_count{le="5",gt="0"} 3\n'
                        'sglang:num_running_reqs{dp_rank="0"} 2\n'
                        'sglang:num_running_reqs{dp_rank="1"} 3\n'
                        'sglang:gen_throughput{dp_rank="0"} 100\n'
                        'sglang:gen_throughput{dp_rank="1"} 200\n'
                        'sglang:cache_hit_rate{dp_rank="0"} 0.7\n'
                        'sglang:cache_hit_rate{dp_rank="1"} 0.9\n'
                        f"sglang:cached_tokens_total{{}} {counters['cached']}\n"
                        f"sglang:prompt_tokens_total{{}} {counters['prompt']}\n"
                    )
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                mutations.append(("POST", self.path, payload))
                self.send_response(200)
                self.send_header("Content-Length", str(len(b'{"worker_id": "host-id"}')))
                self.end_headers()
                self.wfile.write(b'{"worker_id": "host-id"}')

            def do_DELETE(self):
                mutations.append(("DELETE", self.path, None))
                self.send_response(200)
                self.send_header("Content-Length", "7")
                self.end_headers()
                self.wfile.write(b"removed")

            def log_message(self, *_args):
                return

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            config = MonitorConfig(host="127.0.0.1", port=server.server_port, prom_port=server.server_port)
            collector = RouterCollector(config)
            try:
                first = collector.collect()
                self.assertIsNone(first["rps"])
                with patch("builtins.print") as output:
                    render_terminal(first, config)
                self.assertIn("test", output.call_args.args[0])
                counters.update(cached=110, prompt=220, requests=20)
                second = collector.collect()
                self.assertEqual(list(second["by_model"]), ["test"])
                self.assertEqual(second["summary"]["workers"], 1)
                self.assertEqual(second["total_active"], 3)
                self.assertEqual(second["age"], {"0~5s": 3})
                self.assertGreater(second["rps"], 0)
                self.assertEqual(second["summary"]["tps"], 300)
                self.assertEqual(second["summary"]["hit_pct"], 50)
                self.assertEqual(second["summary"]["prefill_tokens"], 300)
                self.assertEqual(second["summary"]["healthy_ranks"], 2)
                self.assertEqual(second["summary"]["coverage"]["running"], 1)
                self.assertEqual(second["worker_rows"][0]["ranks"], 2)
                detail = next(iter(second["details"].values()))
                self.assertEqual(detail["running"], 5)
                self.assertEqual(detail["hit_ranks"], [70, 90])
                store = SnapshotStore()
                store.publish(first)
                store.publish(second)
                with TestClient(WebDashboard(config, store).create_app()) as client:
                    self.assertEqual(client.get("/data").json()["summary"]["tps"], 300)
                    history = client.get("/history?n=1").json()
                    self.assertEqual(len(history), 1)
                    self.assertEqual(history[0]["at"], second["collected_at"])
                    self.assertEqual(history[0]["by_model"]["test"]["running"], 5)
                    self.assertEqual(history[0]["by_model"]["test"]["tps"], 300)
                    self.assertEqual(history[0]["by_model"]["test"]["hit_pct"], 50)
                self.assertEqual(mutations, [])
                self.assertLess(len(set(connection_ports)), len(connection_ports))
                cleanup_only = RouterCollector(replace(config, unregister_unavailable_after=60))
                try:
                    cleanup_only.collect()
                    cleanup_only.collect()
                finally:
                    cleanup_only.close()
                self.assertEqual(mutations, [])
                enabled = RouterCollector(replace(config, auto_unregister=True))
                try:
                    enabled.collect()
                    enabled.collect()
                finally:
                    enabled.close()
                self.assertEqual(
                    mutations,
                    [
                        ("POST", "/workers", {"url": f"http://127.0.0.1:{server.server_port}"}),
                        ("DELETE", "/workers/host-id", None),
                    ],
                )
            finally:
                collector.close()
                server.shutdown()
                thread.join(timeout=5)

    def test_keepalive_filters_idle_workers_uses_their_model_and_backs_off(self):
        store = Mock(spec=SnapshotStore)
        store.snapshot.return_value = {
            "by_model": {
                "chosen": ["http://idle", "http://busy", "http://direct", "http://queued", "http://unknown"],
                "other": ["http://other"],
            },
            "active_pw": {"http://idle": 0, "http://busy": 2, "http://direct": 0, "http://queued": 0},
            "worker_rows": [
                {"url": "http://idle", "running": 0, "waiting": 0},
                {"url": "http://busy", "running": 0, "waiting": 0},
                {"url": "http://direct", "running": 3, "waiting": 0},
                {"url": "http://queued", "running": 0, "waiting": 2},
                {"url": "http://unknown", "running": None, "waiting": None},
                {"url": "http://other", "running": 0, "waiting": 0},
            ],
        }
        config = MonitorConfig(host="router", keepalive=True, keepalive_model="chosen", keepalive_max_tokens=128)
        warmer = KeepaliveWarmer(config, store)
        try:
            self.assertEqual(warmer.idle_workers(), {"http://idle": "chosen"})
            with patch.object(warmer._http, "request_json", return_value={"choices": [{}]}) as send:
                self.assertEqual(warmer.run_round(), {"http://idle": True})
            for args in send.call_args_list:
                self.assertEqual(args.args, ("http://idle/v1/chat/completions",))
                self.assertEqual(args.kwargs["method"], "POST")
                self.assertEqual(args.kwargs["payload"]["model"], "chosen")
                self.assertEqual(args.kwargs["payload"]["max_tokens"], 128)

            with (
                patch.object(warmer, "ping", side_effect=[True, False]),
                patch("router_monitor.maintenance.random.randint", return_value=2),
            ):
                self.assertEqual(warmer.run_round(), {"http://idle": False})

            stop = Mock(spec=threading.Event)
            stop.is_set.side_effect = [False, False, False, True]
            with patch.object(warmer, "run_round", side_effect=[{"idle": False}, {"idle": False}, {"idle": True}]):
                warmer.run(stop)
            self.assertEqual(stop.wait.call_args_list, [call(10), call(20), call(5)])
        finally:
            warmer.close()

    def test_unavailable_cleanup_waits_and_keeps_responding_partial_or_busy_workers(self):
        probes, mutations = [], []

        def handle(request):
            host = request.url.host
            if request.method == "GET":
                probes.append(host)
                if host == "gone":
                    raise httpx.ConnectError("refused", request=request)
                if host == "slow":
                    raise httpx.ReadTimeout("busy", request=request)
                if host == "error":
                    return httpx.Response(503)
                return httpx.Response(200, json={"data": [{"id": "actual"}]})
            mutations.append((request.method, str(request.url)))
            return httpx.Response(200, json={"worker_id": "host-id"})

        cleanup = AutoUnregister("http://router", model_mismatch=False, unavailable_after=60)
        cleanup._http.close()
        cleanup._http = HttpClient(transport=httpx.MockTransport(handle))
        workers = [
            {"id": host, "url": f"http://{host}", "is_healthy": False, "load": 0, "model_id": "registered"}
            for host in ("gone", "responding", "slow", "error", "busy", "partial")
        ]
        workers[-2]["load"] = 1
        workers.append({"id": "healthy-rank", "url": "http://partial@1", "is_healthy": True})
        workers[0]["url"] += "@0"

        def reconcile(now):
            with patch("router_monitor.maintenance.time.monotonic", return_value=now):
                cleanup.reconcile(workers, {"actual": ["http://responding"]})
                for _, future in cleanup._pending.values():
                    future.result(timeout=2)

        try:
            reconcile(0)
            reconcile(59)
            self.assertEqual(probes, [])
            reconcile(60)
            self.assertCountEqual(probes, ["gone", "responding", "slow", "error"])
            self.assertEqual(
                mutations, [("POST", "http://router/workers"), ("DELETE", "http://router/workers/host-id")]
            )
            reconcile(61)
            reconcile(120)
            self.assertEqual(len(probes), 4)
            reconcile(121)
            self.assertCountEqual(probes, ["gone", "responding", "slow", "error"] * 2)
            self.assertEqual(len(mutations), 4)
        finally:
            cleanup.close()

    def test_unavailable_cleanup_restarts_grace_after_recovery_registration_or_registry_gap(self):
        cases = ("recovery", "registration", "registry_gap")
        for case in cases:
            with self.subTest(case=case):
                cleanup = AutoUnregister("http://router", model_mismatch=False, unavailable_after=60)
                rank = {"id": "old", "url": "http://gone", "is_healthy": False}
                try:
                    with patch.object(cleanup, "_unregister", return_value=True) as remove:

                        def collect(now, workers):
                            with patch("router_monitor.maintenance.time.monotonic", return_value=now):
                                cleanup.reconcile(workers, {})
                                for _, future in cleanup._pending.values():
                                    future.result(timeout=2)

                        collect(0, [rank])
                        if case == "recovery":
                            rank["is_healthy"] = True
                            collect(30, [rank])
                            rank["is_healthy"] = False
                        elif case == "registration":
                            rank["id"] = "new"
                        else:
                            collect(30, [])
                        collect(40, [rank])
                        collect(99, [rank])
                        remove.assert_not_called()
                        collect(100, [rank])
                        remove.assert_called_once()
                        self.assertTrue(remove.call_args.kwargs["probe"])
                finally:
                    cleanup.close()

    def test_http_status_timeout_and_json_requests(self):
        def handle(request):
            if request.url.path == "/timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if request.url.path == "/error":
                return httpx.Response(503)
            return httpx.Response(200, json={"payload": json.loads(request.content)})

        client = HttpClient(transport=httpx.MockTransport(handle))
        try:
            self.assertEqual(client.fetch("http://worker/error"), "")
            self.assertEqual(client.fetch("http://worker/timeout"), "")
            with self.assertRaises(httpx.HTTPStatusError):
                client.request_json("http://worker/error", method="POST", payload={})
            self.assertEqual(
                client.request_json("http://worker/echo", method="POST", payload={"model": "test"}),
                {"payload": {"model": "test"}},
            )
        finally:
            client.close()
