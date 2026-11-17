"""Export monitor observations and retrieve their original samples from Prometheus."""

import json
import math

import httpx
from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily

from .config import PrometheusHistoryConfig
from .state import RouterTarget

_MODEL_PREFIX = "router_monitor_model_"
_SAMPLE = "router_monitor_sample"
_TREND_KEYS = (
    "tps",
    "running",
    "waiting",
    "ttft_mean_ms",
    "itl_ms",
    "prefill_tokens",
    "retract_rate",
    "abort_rate",
    "kv_pct",
    "swa_pct",
    "unhealthy_workers",
    "success_pct",
    "health_missing_workers",
    "hit_pct",
    "batch_hit_min_pct",
    "metrics_workers",
)


class MonitorMetrics:
    def __init__(self, targets: dict[str, RouterTarget]):
        self.targets = targets

    def collect(self):
        samples = GaugeMetricFamily(_SAMPLE, "A completed monitor collection", labels=["router"])
        metrics = {
            key: GaugeMetricFamily(_MODEL_PREFIX + key, f"Model {key}", labels=["router", "model"]) for key in _TREND_KEYS
        }
        for router, target in self.targets.items():
            point = target.store.latest_sample()
            if point is None:
                continue
            at = point["at"]
            samples.add_metric([router], 1, timestamp=at)
            for model, values in point["by_model"].items():
                for key, metric in metrics.items():
                    value = values.get(key)
                    if value is not None and math.isfinite(value):
                        metric.add_metric([router, model], value, timestamp=at)
        yield samples
        yield from metrics.values()


def metrics_registry(targets: dict[str, RouterTarget]) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(MonitorMetrics(targets))
    return registry


class PrometheusHistory:
    def __init__(self, config: PrometheusHistoryConfig):
        self.config = config
        self.http = httpx.Client(base_url=config.url.rstrip("/") + "/", timeout=5)

    def history(self, router: str, interval: float, limit: int, recent: list[dict]) -> list[dict]:
        cap = self.config.lookback_hours * 3600
        seconds = cap
        if 0 < limit < cap / interval:
            seconds = min(cap, max(300, math.ceil(limit * interval)))
        labels = f"job={json.dumps(self.config.job, ensure_ascii=False)},router={json.dumps(router, ensure_ascii=False)}"
        query = f'{{__name__=~"{_SAMPLE}|{_MODEL_PREFIX}.+",{labels}}}[{seconds}s]'
        # A range vector preserves original timestamps and missing samples.
        response = self.http.get("api/v1/query", params={"query": query})
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success" or payload["data"]["resultType"] != "matrix":
            raise ValueError("Unexpected Prometheus history response.")
        points = {}
        for series in payload["data"]["result"]:
            labels = series["metric"]
            metric = labels["__name__"]
            key = metric.removeprefix(_MODEL_PREFIX)
            for at, value in series["values"]:
                point = points.setdefault(round(at * 1000), {"at": at, "by_model": {}})
                if metric != _SAMPLE and math.isfinite(number := float(value)):
                    point["by_model"].setdefault(labels["model"], {})[key] = number

        # Prometheus stores timestamps at millisecond precision.
        for point in recent:
            points[int(point["at"] * 1000)] = point
        history = [points[at] for at in sorted(points)]
        return history if limit <= 0 else history[-limit:]

    def close(self) -> None:
        self.http.close()
