"""Router dialects, credential isolation, and real collection behavior."""

import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import httpx

from router_monitor.collectors import RouterCollector
from router_monitor.config import MonitorConfig
from router_monitor.http import HttpClient
from router_monitor.metrics import metric_samples
from router_monitor.sources import RouterSource
from router_monitor.worker import WorkerMetricsCollector


@contextmanager
def collector_for(config, handle):
    collector = RouterCollector(config)
    collector._http.close()
    collector._http = HttpClient(
        transport=httpx.MockTransport(handle),
        token=config.api_key,
        origins=(config.router_url, config.admin_endpoint, config.prom_url),
    )
    collector._source.http = collector._http
    collector._worker_metrics._http.close()
    collector._worker_metrics._http = HttpClient(transport=httpx.MockTransport(handle), token=config.worker_api_key)
    try:
        yield collector
    finally:
        collector.close()


class SourceTests(unittest.TestCase):
    def test_detection_and_explicit_workers_across_proxy_families(self):
        for kind, metric in [
            ("nginx", "nginx_http_requests_total"),
            ("haproxy", "haproxy_frontend_http_requests_total"),
            ("envoy", "envoy_http_downstream_rq_total"),
            ("traefik", "traefik_service_requests_total"),
            ("kong", "kong_http_requests_total"),
            ("ray", "ray_serve_num_http_requests_total"),
        ]:
            with self.subTest(kind=kind):
                calls = []
                count = [10]

                def handle(request):
                    calls.append(request)
                    self.assertEqual(request.method, "GET")
                    if request.url.host == "router" and request.url.path == "/metrics":
                        return httpx.Response(200, text=f"{metric} {count[0]}\n")
                    if request.url.host == "worker" and request.url.path == "/v1/models":
                        self.assertNotIn("authorization", request.headers)
                        return httpx.Response(200, json={"data": [{"id": "model-a"}]})
                    if request.url.host == "worker" and request.url.path == "/metrics":
                        return httpx.Response(200, text='vllm:num_requests_running{engine="0"} 3\n')
                    return httpx.Response(404)

                config = MonitorConfig(
                    host="router",
                    endpoint="http://router",
                    worker_urls=("http://worker",),
                    auto_unregister=True,
                    api_key="router-secret",
                )
                with collector_for(config, handle) as collector:
                    with patch("time.monotonic", return_value=100):
                        first = collector.collect()
                    count[0] = 30
                    with patch("time.monotonic", return_value=102):
                        second = collector.collect()
                    self.assertIsNone(first["rps"])
                    self.assertEqual(second["router_type"], kind)
                    self.assertEqual(second["rps"], 10)
                    self.assertEqual(second["by_model_stats"]["model-a"]["running"], 3)
                    row = second["worker_rows"][0]
                    self.assertEqual(row["status"], "unknown")
                    self.assertIsNone(row["healthy_ranks"])
                    self.assertIsNone(row["kv_pct"])
                    self.assertFalse(second["capabilities"]["unregister"])
                    self.assertIsNone(collector._registration)
                self.assertTrue(all(r.url.path != "/v1/loads" for r in calls))

    def test_litellm_deployments_use_proxy_metrics_without_provider_requests(self):
        counter = [0]
        calls = []

        def handle(request):
            calls.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.host, "router")
            self.assertEqual(request.headers.get("authorization"), "Bearer secret")
            if request.url.path == "/model/info":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "model_name": name,
                                "model_info": {"id": model_id},
                                "litellm_params": {"api_base": "https://provider.example/v1", "api_key": "masked"},
                            }
                            for name, model_id in (("alpha", "a"), ("beta", "b"))
                        ]
                    },
                )
            if request.url.path == "/metrics":
                lines = []
                for model_id in ("a", "b"):
                    labels = f'{{model_id="{model_id}"}}'
                    for metric, value in (
                        ("litellm_deployment_state", 0),
                        ("litellm_output_tokens_metric_total", 1000 + counter[0] * 200),
                        ("litellm_input_tokens_metric_total", 2000 + counter[0] * 400),
                        ("litellm_input_cached_tokens_metric_total", 500 + counter[0] * 100),
                        ("litellm_proxy_total_requests_metric_total", 100 + counter[0] * 20),
                        ("litellm_proxy_failed_requests_metric_total", 10 + counter[0] * 4),
                        ("litellm_llm_api_time_to_first_token_metric_sum", 10 + counter[0] * 2),
                        ("litellm_llm_api_time_to_first_token_metric_count", 20 + counter[0] * 4),
                    ):
                        lines.append(f"{metric}{labels} {value}")
                return httpx.Response(200, text="\n".join(lines) + "\n")
            return httpx.Response(404)

        config = MonitorConfig(host="router", endpoint="http://router", api_key="secret", auto_unregister=True)
        with collector_for(config, handle) as collector:
            with patch("time.monotonic", return_value=100):
                first = collector.collect()
            counter[0] = 1
            with patch("time.monotonic", return_value=102):
                second = collector.collect()
            self.assertEqual(second["router_type"], "litellm")
            self.assertEqual(second["registry_scope"], "deployments")
            self.assertEqual(set(second["by_model"]), {"alpha", "beta"})
            self.assertIsNone(first["summary"]["tps"])
            self.assertEqual(second["summary"]["tps"], 200)
            self.assertEqual(second["summary"]["ttft_mean_ms"], 500)
            self.assertEqual(second["summary"]["hit_pct"], 25)
            self.assertEqual(second["summary"]["success_pct"], 80)
            self.assertTrue(all(row["status"] == "unhealthy" for row in second["worker_rows"]))
            self.assertTrue(all(row["ranks"] is None and not row["dp_metrics"] for row in second["worker_rows"]))
            self.assertNotIn("secret", json.dumps(second))
            self.assertNotIn("provider.example", json.dumps(second))
            self.assertIsNone(collector._registration)
        self.assertNotIn("/health", [request.url.path for request in calls])

    def test_model_label_fallback_and_unknown_endpoints_are_not_false_sglang(self):
        with http_client(lambda request: httpx.Response(404)) as client:
            source = RouterSource(MonitorConfig(host="router"), client)
            registry = source.registry('{"data":[{"id":"generic-model"}]}', [])
            self.assertEqual(registry.kind, "generic")
            self.assertFalse(registry.available)
            samples = metric_samples(
                'litellm_output_tokens_metric_total{model="alpha"} 5\nlitellm_output_tokens_metric_total{model="beta"} 9\n'
            )
            registry = source.registry("", samples)
            self.assertEqual({w["model_id"] for w in registry.workers}, {"alpha", "beta"})
            self.assertEqual(len(registry.metrics), 2)

    def test_vllm_discovery_engine_ranks_and_counter_reset(self):
        clock = [100]
        count = [0]

        def handle(request):
            if request.url.host == "router" and request.url.path == "/metrics":
                return httpx.Response(200, text='vllm:healthy_pods_total{server="http://worker"} 1\n')
            if request.url.host == "worker" and request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "alpha"}]})
            if request.url.host == "worker" and request.url.path == "/metrics":
                lines = []
                for rank in ("0", "1"):
                    labels = f'{{engine="{rank}",model_name="alpha"}}'
                    for name, value in (
                        ("num_requests_running", 2),
                        ("num_requests_waiting", 1),
                        ("kv_cache_usage_perc", 0.25),
                        ("generation_tokens_total", 1000 + count[0] * 100),
                        ("prefix_cache_hits_total", 100 + count[0] * 20),
                        ("prefix_cache_queries_total", 500 + count[0] * 40),
                        ("time_to_first_token_seconds_sum", 10 + count[0] * 2),
                        ("time_to_first_token_seconds_count", 20 + count[0] * 4),
                        ("inter_token_latency_seconds_sum", 5 + count[0] * 0.4),
                        ("inter_token_latency_seconds_count", 50 + count[0] * 20),
                    ):
                        lines.append(f"vllm:{name}{labels} {value}")
                return httpx.Response(200, text="\n".join(lines) + "\n")
            return httpx.Response(404)

        with collector_for(MonitorConfig(host="router", endpoint="http://router"), handle) as collector:
            with patch("time.monotonic", side_effect=lambda: clock[0]):
                first = collector.collect()
                count[0], clock[0] = 1, 102
                second = collector.collect()
                count[0], clock[0] = -1, 104
                reset = collector.collect()
            row = second["worker_rows"][0]
            self.assertEqual(second["router_type"], "vllm")
            self.assertEqual(row["running"], 4)
            self.assertEqual(row["waiting"], 2)
            self.assertEqual(row["kv_pct"], 25)
            self.assertEqual(row["gen_tps"], 100)
            self.assertEqual(row["hit_pct"], 50)
            self.assertEqual(row["counter_window"]["ttft_mean_ms"], 500)
            self.assertAlmostEqual(row["counter_window"]["itl_ms"], 20)
            self.assertEqual([r["rank"] for r in row["dp_metrics"]], ["0", "1"])
            for key in ("gen_tps", "success_pct", "prefill_tokens"):
                self.assertIsNone(first["worker_rows"][0][key])
            self.assertIsNone(reset["worker_rows"][0]["gen_tps"])
            self.assertIsNone(reset["worker_rows"][0]["hit_pct"])

    def test_envoy_and_nginxplus_discovery_preserve_unknown_health(self):
        data = {
            "cluster_statuses": [
                {
                    "host_statuses": [
                        {"address": {"socket_address": {"address": "::1", "port_value": 8000}}, "health_status": {}},
                        {
                            "address": {"socket_address": {"address": "192.0.2.2", "port_value": 8000}},
                            "health_status": {"failed_active_health_check": True},
                        },
                    ]
                }
            ]
        }
        with http_client(lambda request: httpx.Response(200, json=data)) as client:
            source = RouterSource(MonitorConfig(host="router", admin_url="http://admin:9901"), client)
            registry = source.registry("", metric_samples("envoy_server_live 1\n"))
            self.assertEqual(registry.workers[0]["url"], "http://[::1]:8000")
            self.assertIsNone(registry.workers[0]["is_healthy"])
            self.assertIs(registry.workers[1]["is_healthy"], False)
            registry = source.registry(
                "", metric_samples('nginxplus_upstream_server_state{server="worker:8000",upstream="llm"} 1\n')
            )
            self.assertEqual(registry.workers[0]["url"], "http://worker:8000")
            self.assertTrue(registry.workers[0]["is_healthy"])
            registry = source.registry(
                "", metric_samples('traefik_service_server_up{url="https://worker:8443",service="llm"} 1\n')
            )
            self.assertEqual(registry.kind, "traefik")
            self.assertEqual(registry.workers[0]["url"], "https://worker:8443")

    def test_dynamo_model_metrics_and_explicit_worker_override(self):
        samples = metric_samples(
            'dynamo_frontend_output_tokens_total{model="alpha"} 100\n'
            'dynamo_frontend_output_tokens_total{model="beta"} 200\n'
            'dynamo_frontend_time_to_first_token_seconds_sum{model="alpha"} 5\n'
            'dynamo_frontend_time_to_first_token_seconds_count{model="alpha"} 10\n'
            "vllm:num_requests_running 3\n"
        )
        with http_client(lambda request: httpx.Response(404)) as client:
            config = MonitorConfig(host="router")
            source = RouterSource(config, client)
            registry = source.registry("", samples)
            self.assertEqual(registry.kind, "dynamo")
            self.assertEqual(registry.scope, "deployments")
            self.assertEqual(len(registry.metrics), 2)
            source = RouterSource(replace(config, worker_urls=("http://worker",)), client)
            registry = source.registry("", samples)
            self.assertEqual(registry.scope, "workers")
            self.assertEqual(registry.metrics, {})
            self.assertEqual([w["url"] for w in registry.workers], ["http://worker"])

        worker_metrics = WorkerMetricsCollector(2)
        try:
            with patch("time.monotonic", return_value=100):
                first = worker_metrics.parse_detail("dynamo://alpha", samples, "dynamo")
            updated = [{**sample, "value": sample["value"] * 2} for sample in samples]
            with patch("time.monotonic", return_value=102):
                second = worker_metrics.parse_detail("dynamo://alpha", updated, "dynamo")
            self.assertNotIn("gen_tps", first)
            self.assertEqual(second["gen_tps"], 150)
            self.assertEqual(second["counter_window"]["ttft_mean_ms"], 500)
            self.assertEqual(second["dp_metrics"], [])
            self.assertNotIn("running", second)
        finally:
            worker_metrics.close()

    def test_metrics_fallback_override_and_authentication_scope(self):
        calls = []

        def handle(request):
            calls.append(request)
            if request.url.path == "/metrics":
                return httpx.Response(200, text="<html>UI fallback</html>")
            if request.url.path == "/stats/prometheus":
                return httpx.Response(200, text="envoy_server_live 1\n")
            return httpx.Response(404)

        with http_client(handle, token="secret", origins=("http://router",)) as client:
            config = MonitorConfig(host="router", endpoint="http://router")
            source = RouterSource(config, client)
            self.assertEqual(source.read_metrics(), [{"name": "envoy_server_live", "labels": {}, "value": 1}])
            client.fetch("http://other")
            self.assertNotIn("authorization", calls[-1].headers)
            self.assertTrue(all(r.headers.get("authorization") == "Bearer secret" for r in calls[:-1]))
            calls.clear()
            source = RouterSource(replace(config, metrics_endpoint="http://router/metrics"), client)
            self.assertEqual(source.read_metrics(), [])
            self.assertEqual(len(calls), 1)

    def test_registry_loss_resets_cleanup_and_manual_workers_are_unmanaged(self):
        available = [True]

        def handle(request):
            if request.url.path == "/workers" and available[0]:
                return httpx.Response(
                    200, json={"workers": [{"id": "gone", "url": "http://gone", "is_healthy": False, "load": 0}]}
                )
            if request.url.path == "/metrics" and request.url.host == "router":
                return httpx.Response(200, text="smg_http_requests_total 10\n")
            return httpx.Response(503)

        config = MonitorConfig(host="router", endpoint="http://router", worker_urls=("http://manual",))
        with collector_for(config, handle) as collector:
            with patch.object(collector._worker_metrics, "get_model", return_value="alpha"):
                collector.collect()
                self.assertEqual(set(collector._registration._unavailable_since), {"http://gone"})
                available[0] = False
                collector.collect()
                self.assertEqual(collector._registration._unavailable_since, {})


@contextmanager
def http_client(handle, **kwargs):
    client = HttpClient(transport=httpx.MockTransport(handle), **kwargs)
    try:
        yield client
    finally:
        client.close()
