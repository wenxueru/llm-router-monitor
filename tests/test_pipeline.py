"""Exercise real HTTP collection through snapshot storage and web responses."""

import json
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from rich.console import Console

from router_monitor.collection.collector import RouterCollector
from router_monitor.config import MonitorConfig
from router_monitor.http import HttpClient
from router_monitor.maintenance.registration import AutoUnregister
from router_monitor.presentation.dashboard import create_dashboard
from router_monitor.presentation.terminal import render_terminal
from router_monitor.state import SnapshotStore
from router_monitor.target import RouterTarget


class PipelineTests(unittest.TestCase):
    def test_registry_fallback_does_not_count_as_worker_discovery(self):
        collector = RouterCollector(MonitorConfig(host="router", auto_unregister=True))
        ranks = [
            {"url": "http://worker@0", "model_id": "registered", "is_healthy": True},
            {"url": "http://worker@1", "model_id": "registered", "is_healthy": False},
        ]
        try:
            with patch.object(collector._worker_metrics, "get_model", return_value=None):
                with self.assertLogs("router_monitor.collection.collector", level="WARNING"):
                    grouped, observed = collector._group_by_model(ranks, "sglang")
                self.assertEqual(grouped, {"registered": ["http://worker"]})
                self.assertEqual(observed, {})
                ranks[1]["model_id"] = "different"
                with self.assertLogs("router_monitor.collection.collector", level="WARNING"):
                    grouped, _ = collector._group_by_model(ranks, "sglang")
                self.assertEqual(grouped, {"unknown": ["http://worker"]})
            with patch.object(collector._worker_metrics, "get_model", return_value="actual"):
                grouped, observed = collector._group_by_model(ranks, "sglang")
            self.assertEqual(grouped, {"actual": ["http://worker"]})
            self.assertEqual(observed, {"http://worker": "actual"})
        finally:
            collector.close()

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
                    body = json.dumps({
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
                    })
                elif self.path == "/v1/models":
                    body = json.dumps({"data": [{"id": "test"}]})
                elif self.path == "/v1/loads":
                    body = json.dumps({
                        "loads": [
                            {"dp_rank": 0, "num_waiting_uncached_tokens": 100},
                            {"dp_rank": 1, "num_waiting_uncached_tokens": 200},
                        ]
                    })
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
                output = StringIO()
                Console(file=output, width=120).print(render_terminal(first, config))
                self.assertIn("test", output.getvalue())
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
                store = SnapshotStore(history_limit=1)
                store.publish(first)
                store.publish(second)
                with TestClient(create_dashboard({"": RouterTarget(config, store)})) as client:
                    self.assertEqual(client.get("/data").json()["summary"]["tps"], 300)
                    history = client.get("/history?n=1").json()
                    self.assertEqual(len(history), 1)
                    self.assertEqual(client.get("/history").json(), history)
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
                if host == "disconnected":
                    raise httpx.RemoteProtocolError("disconnected", request=request)
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
            for host in ("gone", "responding", "slow", "disconnected", "error", "busy", "partial")
        ]
        workers[-2]["load"] = 1
        workers.append({"id": "healthy-rank", "url": "http://partial@1", "is_healthy": True})
        workers[0]["url"] += "@0"

        def reconcile(now):
            with patch("router_monitor.maintenance.registration.time.monotonic", return_value=now):
                cleanup.reconcile(workers, {"http://responding": "actual"})
                for _, future in cleanup._pending.values():
                    future.result(timeout=2)

        try:
            reconcile(0)
            reconcile(59)
            self.assertEqual(probes, [])
            expected_probes = ["gone", "responding", "slow", "disconnected", "error"]
            expected_mutations = [
                ("POST", "http://router/workers"),
                ("DELETE", "http://router/workers/host-id"),
                ("DELETE", "http://router/workers/slow"),
                ("DELETE", "http://router/workers/disconnected"),
            ]
            for now, attempts in [(60, 1), (61, 1), (120, 1), (121, 2)]:
                with self.subTest(now=now):
                    reconcile(now)
                    self.assertCountEqual(probes, expected_probes * attempts)
                    self.assertCountEqual(mutations, expected_mutations * attempts)
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
                            with patch("router_monitor.maintenance.registration.time.monotonic", return_value=now):
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
