"""Collect Router discovery, traffic, and inference-engine metrics."""

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .config import MonitorConfig
from .http import HttpClient
from .maintenance import AutoUnregister
from .sources import RouterSource, request_metric
from .visualization import build_visualization
from .worker import WorkerMetricsCollector

logger = logging.getLogger(__name__)


class RouterCollector:
    def __init__(self, config: MonitorConfig):
        self.config = config
        self._http = HttpClient(
            token=config.api_key, origins=(config.router_url, config.admin_endpoint, config.prom_url)
        )
        self._source = RouterSource(config, self._http)
        self._io_pool = ThreadPoolExecutor(max_workers=2)
        self._worker_metrics = WorkerMetricsCollector(config.interval, config.worker_api_key)
        self._registration = None
        self._previous_requests = None

    def _router_stats(self, samples: list[dict], active: dict, kind: str):
        now = time.monotonic()
        metric = request_metric(kind)
        if kind == "nginx" and not any(s["name"] == metric for s in samples):
            metric = "nginxplus_http_requests_total"
        requests = {tuple(sorted(s["labels"].items())): s["value"] for s in samples if s["name"] == metric}
        previous = self._previous_requests
        self._previous_requests = (now, kind, requests)
        rps = None
        if (
            previous
            and 0 < now - previous[0] <= 60
            and previous[1] == kind
            and requests
            and requests.keys() == previous[2].keys()
        ):
            deltas = [value - previous[2][key] for key, value in requests.items()]
            if min(deltas) >= 0:
                rps = sum(deltas) / (now - previous[0])
        age = {
            f"{sample['labels']['gt']}~{sample['labels']['le']}s": int(sample["value"])
            for sample in samples
            if sample["name"] == "smg_http_inflight_request_age_count"
        }
        inflight_metric = {
            "litellm": "litellm_in_flight_requests",
            "dynamo": "dynamo_frontend_active_requests",
            "envoy": "envoy_http_downstream_rq_active",
            "ray": "ray_serve_num_ongoing_http_requests",
        }.get(kind)
        inflight = [s["value"] for s in samples if s["name"] == inflight_metric]
        total = None
        if age:
            total = sum(age.values())
        elif inflight:
            total = sum(inflight)
        elif active:
            total = sum(active.values())
        return rps, age, total

    def _group_by_model(self, workers: list[dict], kind: str):
        by_model: dict[str, list[str]] = defaultdict(list)
        hosts = {worker["url"].split("@")[0]: worker for worker in workers if worker.get("is_healthy") is not False}
        for url, worker in hosts.items():
            model = worker.get("model_id") if kind != "sglang" else None
            if not model:
                try:
                    model = self._worker_metrics.get_model(
                        url, refresh=kind == "sglang" and self.config.auto_unregister
                    )
                except Exception:
                    logger.warning("Failed to identify model for %s", url, exc_info=True)
                    model = "unknown"
            by_model[model].append(url)
        return by_model

    def collect(self) -> dict:
        prometheus_future = self._io_pool.submit(self._source.read_metrics)
        registry_future = self._io_pool.submit(self._http.fetch, f"{self.config.router_url}/workers")
        samples, raw_registry = prometheus_future.result(), registry_future.result()
        registry = self._source.registry(raw_registry, samples)
        active = defaultdict(float)
        for worker in registry.workers:
            if worker.get("load") is not None:
                active[worker["url"].split("@")[0]] += worker["load"]
        by_model = self._group_by_model(registry.workers, registry.kind)
        if registry.kind == "sglang" and registry.available:
            if self._registration is None and (
                self.config.auto_unregister or self.config.unregister_unavailable_after > 0
            ):
                self._registration = AutoUnregister(
                    self.config.router_url,
                    model_mismatch=self.config.auto_unregister,
                    unavailable_after=self.config.unregister_unavailable_after,
                    api_key=self.config.api_key,
                )
            if self._registration is not None:
                self._registration.reconcile([w for w in registry.workers if w.get("managed", True)], by_model)
        elif self._registration is not None:
            self._registration.reconcile([], {})
        targets = [url for urls in by_model.values() for url in urls if url not in registry.metrics]
        details = self._worker_metrics.collect(targets)
        for url, deployment_samples in registry.metrics.items():
            details[url] = self._worker_metrics.parse_detail(url, deployment_samples, registry.kind)
        if registry.kind != "sglang":
            for worker in registry.workers:
                worker["rank_health_known"] = False
                # Proxy-side health may be bad while its inference telemetry is present.
                model, url = worker.get("model_id"), worker["url"]
                if model and url in registry.metrics and url not in by_model[model]:
                    by_model[model].append(url)
        rps, age, total_active = self._router_stats(samples, active, registry.kind)
        snapshot = {
            "collected_at": time.time(),
            "registry_ok": registry.available,
            "router_type": registry.kind,
            "registry_scope": registry.scope,
            "router_label": {
                "sglang": "SGLang",
                "vllm": "vLLM",
                "litellm": "LiteLLM",
                "envoy": "Envoy",
                "nginx": "NGINX",
                "haproxy": "HAProxy",
                "traefik": "Traefik",
                "kong": "Kong",
                "ray": "Ray Serve",
                "dynamo": "Dynamo",
                "generic": "Unidentified Router",
            }[registry.kind],
            "capabilities": {
                "unregister": registry.kind == "sglang",
                "discovery": registry.kind in {"sglang", "vllm", "litellm", "dynamo", "envoy"}
                or any(s["name"] in {"nginxplus_upstream_server_state", "traefik_service_server_up"} for s in samples),
            },
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "router_url": self.config.router_url,
            "total_active": total_active,
            "rps": rps,
            "by_model": dict(by_model),
            "active_pw": dict(active),
            "details": details,
            "age": age,
            "prom_ok": bool(samples),
        }
        snapshot.update(build_visualization(snapshot, registry.workers))
        return snapshot

    def close(self) -> None:
        self._io_pool.shutdown(wait=True, cancel_futures=True)
        self._worker_metrics.close()
        if self._registration is not None:
            self._registration.close()
        self._http.close()
