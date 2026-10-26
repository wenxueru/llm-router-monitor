"""Prometheus parsing, smoothing, and rolling token statistics."""

import math
import time
from collections import defaultdict, deque

from prometheus_client.parser import text_string_to_metric_families


class MovingAverage:
    """Independent throughput windows for each worker rank."""

    def __init__(self, interval: float, seconds: float = 10):
        self._samples = defaultdict(lambda: deque(maxlen=max(1, round(seconds / interval))))

    def observe(self, key: str, value: float) -> float:
        samples = self._samples[key]
        samples.append(value)
        return sum(samples) / len(samples)


class TokenDeltaWindow:
    """Time-bounded token deltas, aggregated as sum(cached) / sum(prompt)."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self._previous: dict[str, tuple[float, float, float]] = {}
        self._samples: dict[str, deque] = defaultdict(deque)

    def observe(self, key: str, cached: float, prompt: float, now: float | None = None):
        now = time.monotonic() if now is None else now
        samples = self._samples[key]
        cutoff = now - self.window_s
        while samples and samples[0][0] <= cutoff:
            samples.popleft()

        previous = self._previous.get(key)
        self._previous[key] = (now, cached, prompt)
        if previous:
            previous_at, previous_cached, previous_prompt = previous
            cached_delta = cached - previous_cached
            prompt_delta = prompt - previous_prompt
            if now - previous_at > self.window_s or cached_delta < 0 or prompt_delta < 0:
                samples.clear()
            elif prompt_delta > 0:
                samples.append((now, min(cached_delta, prompt_delta), prompt_delta))

        prompt_total = sum(sample[2] for sample in samples)
        if prompt_total <= 0:
            return None
        return sum(sample[1] for sample in samples), prompt_total


def gauge_values(metric: str, samples: list[dict]) -> dict[str, float]:
    """Return one gauge value per DP rank, independent of label order."""
    return {sample["labels"].get("dp_rank", "0"): sample["value"] for sample in samples if sample["name"] == metric}


def histogram_quantile(samples: list[dict], metric: str, q: float):
    buckets: dict[float, float] = defaultdict(float)
    total = 0.0
    for sample in samples:
        if sample["name"] == f"sglang:{metric}_count":
            total += sample["value"]
        elif sample["name"] == f"sglang:{metric}_bucket":
            boundary = sample["labels"]["le"]
            if boundary != "+Inf":
                buckets[float(boundary)] += sample["value"]
    if not total or not buckets:
        return None
    # Buckets are cumulative; combine equal boundaries across series once.
    target = q * total
    previous_boundary = previous_count = 0.0
    for index, (boundary, count) in enumerate(sorted(buckets.items())):
        if count >= target:
            if index == 0 or count <= previous_count:
                return boundary
            return previous_boundary + (boundary - previous_boundary) * (target - previous_count) / (
                count - previous_count
            )
        previous_boundary, previous_count = boundary, count
    return max(buckets)


def metric_samples(text: str) -> list[dict]:
    """Keep finite Prometheus samples with series identity for counter deltas."""
    return [
        {"name": sample.name, "labels": sample.labels, "value": sample.value}
        for family in text_string_to_metric_families(text)
        for sample in family.samples
        if math.isfinite(sample.value)
    ]


class CounterWindow:
    """Window means and event rates; resets, gaps and series changes stay missing."""

    def __init__(self):
        self._previous = {}

    def observe(self, url: str, samples: list[dict], now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        names = {
            "time_to_first_token_seconds_sum",
            "time_to_first_token_seconds_count",
            "inter_token_latency_seconds_sum",
            "inter_token_latency_seconds_count",
            "total_retracted_reqs",
            "num_aborted_requests_total",
            "generation_tokens_total",
            "requests_total",
            "errors_total",
        }
        counters = defaultdict(dict)
        for sample in samples:
            name = sample["name"].removeprefix("sglang:")
            if name in names:
                series = counters[sample["name"]]
                labels = tuple(sorted(sample["labels"].items()))
                series[labels] = sample["value"] if labels not in series else None
        start = next((s["value"] for s in samples if s["name"] == "process_start_time_seconds"), None)
        previous = self._previous.get(url)
        self._previous[url] = (now, start, counters)
        if previous is None or previous[1] != start:
            return {}
        elapsed = now - previous[0]
        if elapsed <= 0 or elapsed > 60:
            return {}

        def delta(name):
            current = counters.get("sglang:" + name, {})
            old = previous[2].get("sglang:" + name, {})
            if not current or current.keys() != old.keys():
                return None, set()
            if any(
                value is None or old[key] is None or value < old[key] or value < 0 for key, value in current.items()
            ):
                return None, set()
            return sum(value - old[key] for key, value in current.items()), current.keys()

        result = {"seconds": elapsed}
        for field, metric, scale in [
            ("ttft_mean_ms", "time_to_first_token_seconds", 1000),
            ("itl_ms", "inter_token_latency_seconds", 1000),
        ]:
            total, sum_labels = delta(metric + "_sum")
            count, count_labels = delta(metric + "_count")
            if sum_labels == count_labels and total is not None and count is not None and count > 0:
                result[field] = total / count * scale
                result[field + "_count"] = count
        for field, metric in [("retract", "total_retracted_reqs"), ("abort", "num_aborted_requests_total")]:
            value, _ = delta(metric)
            if value is not None:
                result[field + "_delta"] = value
                result[field + "_rate"] = value / elapsed
        tokens, _ = delta("generation_tokens_total")
        if tokens is not None:
            result["gen_tps"] = tokens / elapsed
        requests, _ = delta("requests_total")
        errors, _ = delta("errors_total")
        if requests is not None and errors is not None and requests > 0 and 0 <= errors <= requests:
            result["success_pct"] = (1 - errors / requests) * 100
        return result
