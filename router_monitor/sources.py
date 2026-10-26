"""Read-only Router discovery and metric dialects."""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import quote

from .config import MonitorConfig, RouterType
from .http import HttpClient
from .metrics import metric_samples


@dataclass
class Registry:
    kind: str
    workers: list[dict]
    available: bool
    # Gateway deployments have no directly scrapeable worker endpoint.
    metrics: dict[str, list[dict]] = field(default_factory=dict)
    scope: str = "workers"


def json_object(text: str):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def detect_router(samples: list[dict]) -> str:
    names = {sample["name"] for sample in samples}
    for kind, prefixes in (
        ("sglang", ("smg_",)),
        ("litellm", ("litellm_",)),
        ("dynamo", ("dynamo_",)),
        ("envoy", ("envoy_",)),
        ("nginx", ("nginx_", "nginxplus_")),
        ("haproxy", ("haproxy_",)),
        ("traefik", ("traefik_",)),
        ("kong", ("kong_",)),
        ("ray", ("ray_serve_",)),
        ("vllm", ("vllm:",)),
    ):
        if any(name.startswith(prefixes) for name in names):
            return kind
    return "generic"


class RouterSource:
    def __init__(self, config: MonitorConfig, http: HttpClient):
        self.config = config
        self.http = http
        self.metrics_url = config.prom_url
        self.kind = config.router_type.value

    def read_metrics(self) -> list[dict]:
        # The configured URL is authoritative. Only default locations fall back.
        urls = [self.metrics_url]
        if not self.config.metrics_endpoint:
            urls.extend((f"{self.config.router_url}/metrics", f"{self.config.admin_endpoint}/stats/prometheus"))
            if self.kind in {"auto", "sglang"}:
                urls.append(self.config.separate_prom_url)
        for url in dict.fromkeys(urls):
            text = self.http.fetch(url)
            if not text or text.lstrip().startswith(("<", "{", "[")):
                continue
            try:
                samples = metric_samples(text)
                if samples:
                    self.metrics_url = url
                    return samples
            except ValueError:
                continue
        return []

    def registry(self, raw: str, samples: list[dict]) -> Registry:
        data = json_object(raw)
        workers = data.get("workers") if isinstance(data, dict) else data
        is_sglang = (
            isinstance(workers, list)
            and all(isinstance(worker, dict) and {"url", "id", "is_healthy"} <= worker.keys() for worker in workers)
            and (isinstance(data, dict) or bool(workers) or self.kind == "sglang")
        )
        if self.config.router_type == RouterType.auto:
            detected = "sglang" if is_sglang else detect_router(samples)
            if detected != "generic":
                self.kind = detected
        kind = self.kind if self.kind != "auto" else "generic"
        if kind == "sglang":
            result = Registry(kind, workers if is_sglang else [], is_sglang)
        elif kind == "litellm":
            result = self._litellm(samples)
        elif kind == "vllm":
            result = self._vllm(samples)
        elif kind == "envoy":
            result = self._envoy()
        elif kind in {"nginx", "traefik"}:
            result = self._upstreams(kind, samples)
        elif kind == "dynamo":
            result = self._dynamo(samples)
        else:
            result = Registry(kind, [], False)
        if kind == "generic" and self.config.router_type == RouterType.auto:
            candidate = self._litellm(samples)
            if candidate.available:
                result = candidate
                self.kind = candidate.kind
        known = {worker["url"].split("@")[0] for worker in result.workers}
        if result.scope == "deployments" and self.config.worker_urls:
            result.workers.clear()
            result.metrics.clear()
            result.scope = "workers"
        for url in self.config.worker_urls:
            if url not in known:
                result.workers.append(
                    {"id": url, "url": url, "is_healthy": None, "managed": False, "rank_health_known": False}
                )
        if self.config.worker_urls:
            result.available = True
        return result

    def _vllm(self, samples: list[dict]) -> Registry:
        hosts = {}
        for sample in samples:
            url = sample["labels"].get("server")
            if not url or not url.startswith(("http://", "https://")):
                continue
            worker = hosts.setdefault(url, {"id": url, "url": url, "is_healthy": None})
            if sample["name"] == "vllm:healthy_pods_total":
                worker["is_healthy"] = sample["value"] > 0
            elif sample["name"] == "vllm:num_requests_running":
                worker["load"] = sample["value"]
        # A directly exposed vLLM engine is also useful as a one-worker fleet.
        if not hosts and any(s["name"].startswith("vllm:") for s in samples):
            url = self.config.router_url
            hosts[url] = {"id": url, "url": url, "is_healthy": None}
        return Registry("vllm", list(hosts.values()), bool(samples))

    def _upstreams(self, kind: str, samples: list[dict]) -> Registry:
        metric, label = {
            "nginx": ("nginxplus_upstream_server_state", "server"),
            "traefik": ("traefik_service_server_up", "url"),
        }[kind]
        hosts = {}
        for sample in samples:
            if sample["name"] != metric:
                continue
            address = sample["labels"].get(label, "")
            if not address or address.startswith("unix:"):
                continue
            url = address if address.startswith(("http://", "https://")) else "http://" + address
            hosts[url] = {"id": url, "url": url, "is_healthy": sample["value"] == 1}
        return Registry(kind, list(hosts.values()), bool(hosts))

    def _dynamo(self, samples: list[dict]) -> Registry:
        groups = defaultdict(list)
        for sample in samples:
            model = sample["labels"].get("model")
            if model and sample["name"].startswith("dynamo_frontend_"):
                groups[model].append(sample)
        workers, metrics = [], {}
        for model, observed in groups.items():
            url = "dynamo://" + quote(model, safe="")
            workers.append({"id": model, "url": url, "model_id": model, "is_healthy": None})
            metrics[url] = observed
        return Registry("dynamo", workers, bool(groups), metrics, "deployments")

    def _envoy(self) -> Registry:
        data = json_object(self.http.fetch(f"{self.config.admin_endpoint}/clusters?format=json"))
        if not isinstance(data, dict) or "cluster_statuses" not in data:
            return Registry("envoy", [], False)
        hosts = {}
        for cluster in data["cluster_statuses"]:
            for host in cluster.get("host_statuses", []):
                socket = host.get("address", {}).get("socket_address", {})
                address, port = socket.get("address"), socket.get("port_value")
                if not address or port is None:
                    continue
                address = f"[{address}]" if ":" in address else address
                url = f"http://{address}:{port}"
                health = host.get("health_status", {})
                failed = any(value is True for key, value in health.items() if key.startswith(("failed_", "excluded_")))
                eds = health.get("eds_health_status")
                healthy = None
                if failed or eds == "UNHEALTHY":
                    healthy = False
                elif eds == "HEALTHY":
                    healthy = True
                hosts[url] = {"id": url, "url": url, "is_healthy": healthy}
        return Registry("envoy", list(hosts.values()), True)

    def _litellm(self, samples: list[dict]) -> Registry:
        # Do not call /health: LiteLLM may issue billable inference probes there.
        data = json_object(self.http.fetch(f"{self.config.router_url}/model/info"))
        models = data.get("data") if isinstance(data, dict) else None
        registry_ok = (
            isinstance(models, list)
            and (bool(models) or self.kind == "litellm")
            and all(isinstance(m, dict) and "model_name" in m for m in models)
        )
        deployments = {}
        for model in models if registry_ok else []:
            model_id = str(model.get("model_info", {}).get("id", model["model_name"]))
            deployments[model_id] = model["model_name"]
        grouped = defaultdict(list)
        has_ids = bool(deployments) or any(s["labels"].get("model_id") for s in samples)
        for sample in samples:
            if not sample["name"].startswith("litellm_"):
                continue
            labels = sample["labels"]
            model_id = labels.get("model_id")
            model = labels.get("requested_model") or labels.get("model_group") or labels.get("model")
            if model_id:
                key = model_id
            elif model and not has_ids:
                key = model
            else:
                continue  # Never duplicate a model-wide series across deployments.
            if key not in deployments:
                deployments[key] = model or labels.get("litellm_model_name") or key
            grouped[key].append(sample)
        workers, metrics = [], {}
        for model_id, model in deployments.items():
            url = "litellm://" + quote(model_id, safe="")
            observed = grouped[model_id]
            states = [s["value"] for s in observed if s["name"] == "litellm_deployment_state"]
            workers.append(
                {"id": model_id, "url": url, "model_id": model, "is_healthy": max(states) == 0 if states else None}
            )
            metrics[url] = observed
        return Registry("litellm", workers, registry_ok or bool(deployments), metrics, "deployments")


def request_metric(kind: str) -> str | None:
    return {
        "sglang": "smg_http_requests_total",
        "litellm": "litellm_proxy_total_requests_metric_total",
        "nginx": "nginx_http_requests_total",
        "haproxy": "haproxy_frontend_http_requests_total",
        "envoy": "envoy_http_downstream_rq_total",
        "traefik": "traefik_service_requests_total",
        "kong": "kong_http_requests_total",
        "ray": "ray_serve_num_http_requests_total",
        "dynamo": "dynamo_frontend_requests_total",
    }.get(kind)


def normalize_inference(samples: list[dict], engine: str) -> list[dict]:
    """Map equivalent meanings only; unsupported signals remain absent."""
    if engine == "sglang":
        return samples
    aliases = {
        "vllm:num_requests_running": "num_running_reqs",
        "vllm:num_requests_waiting": "num_queue_reqs",
        "vllm:kv_cache_usage_perc": "token_usage",
        "vllm:gpu_cache_usage_perc": "token_usage",
        "vllm:generation_tokens_total": "generation_tokens_total",
        "vllm:prefix_cache_hits_total": "cached_tokens_total",
        "vllm:prefix_cache_queries_total": "prompt_tokens_total",
        "litellm_output_tokens_metric_total": "generation_tokens_total",
        "litellm_input_tokens_metric_total": "prompt_tokens_total",
        "litellm_input_cached_tokens_metric_total": "cached_tokens_total",
        "litellm_proxy_total_requests_metric_total": "requests_total",
        "litellm_proxy_failed_requests_metric_total": "errors_total",
        "dynamo_frontend_output_tokens_total": "generation_tokens_total",
    }
    histograms = {
        "vllm:time_to_first_token_seconds": "time_to_first_token_seconds",
        "vllm:inter_token_latency_seconds": "inter_token_latency_seconds",
        "litellm_llm_api_time_to_first_token_metric": "time_to_first_token_seconds",
        "dynamo_frontend_time_to_first_token_seconds": "time_to_first_token_seconds",
        "dynamo_frontend_inter_token_latency_seconds": "inter_token_latency_seconds",
    }
    prefix = {"vllm": "vllm:", "litellm": "litellm_", "dynamo": "dynamo_"}[engine]
    result = []
    names = {sample["name"] for sample in samples}
    for sample in samples:
        name = sample["name"]
        if name == "vllm:gpu_cache_usage_perc" and "vllm:kv_cache_usage_perc" in names:
            continue
        if name != "process_start_time_seconds" and not name.startswith(prefix):
            continue
        canonical = aliases.get(name)
        for source, target in histograms.items():
            if name.startswith(source + "_"):
                canonical = target + name[len(source) :]
                break
        if canonical:
            labels = dict(sample["labels"])
            if engine == "vllm":
                labels["dp_rank"] = labels.get("engine", labels.get("engine_id", labels.get("dp_rank", "0")))
            result.append({"name": "sglang:" + canonical, "labels": labels, "value": sample["value"]})
        elif name == "process_start_time_seconds":
            result.append(sample)
    return result
