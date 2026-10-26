"""Fetch and aggregate a worker host's DP and TP metrics."""

import json
import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .http import HttpClient
from .metrics import CounterWindow, MovingAverage, TokenDeltaWindow, gauge_values, histogram_quantile, metric_samples
from .sources import normalize_inference

logger = logging.getLogger(__name__)


class WorkerMetricsCollector:
    def __init__(self, interval: float, api_key: str = ""):
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._http = HttpClient(token=api_key)
        self._model_cache: dict[str, str] = {}
        self._counter_window = CounterWindow()
        self._throughput = MovingAverage(interval)
        self._hit_token_window = TokenDeltaWindow(60)

    def get_model(self, worker_url: str, *, refresh: bool = False) -> str:
        if refresh or worker_url not in self._model_cache:
            names = [m["id"] for m in json.loads(self._http.fetch(f"{worker_url}/v1/models")).get("data", [])]
            if not names:
                raise ValueError("no models")
            self._model_cache[worker_url] = names[0]
        return self._model_cache[worker_url]

    def _fetch_detail(self, base_url: str) -> dict:
        """Fetch and aggregate one host's metrics."""
        text = self._http.fetch(f"{base_url}/metrics", timeout=1)
        if not text:
            return {"metrics_ok": False, "endpoint_errors": {"metrics": "Unavailable or empty response"}}

        samples = metric_samples(text)
        engine = "vllm" if any(s["name"].startswith("vllm:") for s in samples) else "sglang"
        detail = self.parse_detail(base_url, samples, engine)
        if engine == "sglang":
            detail.update(self._fetch_loads(base_url))
        return detail

    def parse_detail(self, base_url: str, samples: list[dict], engine: str) -> dict:
        samples = normalize_inference(samples, engine)
        per_rank: dict[str, dict] = defaultdict(dict)
        for name, metric, scale in [
            ("running", "sglang:num_running_reqs", 1),
            ("gen_tps", "sglang:gen_throughput", 1),
            ("waiting", "sglang:num_queue_reqs", 1),
            ("kv_pct", "sglang:token_usage", 100),
            ("batch_hit_pct", "sglang:cache_hit_rate", 100),
            ("spec_accept_len", "sglang:spec_accept_length", 1),
            ("swa_pct", "sglang:swa_token_usage", 100),
            ("queue_latency_s", "sglang:avg_request_queue_latency", 1),
        ]:
            for rank, value in gauge_values(metric, samples).items():
                if value >= 0 and (name not in {"kv_pct", "swa_pct", "batch_hit_pct"} or value <= 1):
                    per_rank[rank][name] = value * scale
        for rank, metrics in per_rank.items():
            if "gen_tps" in metrics:
                metrics["gen_tps"] = self._throughput.observe(f"{base_url}@{rank}", metrics["gen_tps"])

        detail: dict = {
            "metrics_ok": any(s["name"].startswith("sglang:") for s in samples),
            "counter_window": self._counter_window.observe(base_url, samples),
            "endpoint_errors": {},
        }
        # Health gauges may be labelled by TP/PP rather than DP. Reduce the
        # original samples so distinct series never overwrite the implicit rank 0.
        for key, metric, reducer, scale, maximum in [
            ("health_level", "sglang:engine_health_level", max, 1, None),
            ("success_pct", "sglang:req_success_rate", min, 100, 1),
        ]:
            observed = [
                sample
                for sample in samples
                if sample["name"] == metric and sample["value"] >= 0 and (maximum is None or sample["value"] <= maximum)
            ]
            if observed:
                detail[key] = reducer(sample["value"] for sample in observed) * scale
            for sample in observed:
                rank = sample["labels"].get("dp_rank")
                if rank is not None:
                    value = sample["value"] * scale
                    per_rank[rank][key] = reducer(per_rank[rank].get(key, value), value)

        window = detail["counter_window"]
        if "gen_tps" in window:
            detail["gen_tps"] = self._throughput.observe(base_url, window["gen_tps"])
        if "success_pct" in window:
            detail["success_pct"] = window["success_pct"]

        cached = sum(sample["value"] for sample in samples if sample["name"] == "sglang:cached_tokens_total")
        prompt = sum(sample["value"] for sample in samples if sample["name"] == "sglang:prompt_tokens_total")
        has_cache = any(s["name"] == "sglang:cached_tokens_total" for s in samples)
        hit_tokens = self._hit_token_window.observe(base_url, cached, prompt) if has_cache else None
        if hit_tokens is not None:
            detail["hit_cached_tokens"], detail["hit_prompt_tokens"] = hit_tokens
            detail["hit_pct"] = hit_tokens[0] / hit_tokens[1] * 100

        ttft = histogram_quantile(samples, "time_to_first_token_seconds", 0.5)
        if ttft is not None:
            detail["ttft_p50_ms"] = ttft * 1000

        ranks = sorted(per_rank, key=lambda rank: int(rank) if rank.isdigit() else rank)
        for name, aggregate in [
            ("running", sum),
            ("waiting", sum),
            ("gen_tps", sum),
            ("kv_pct", max),
            ("spec_accept_len", lambda values: sum(values) / len(values)),
            ("swa_pct", max),
            ("queue_latency_s", max),
        ]:
            values = [per_rank[rank][name] for rank in ranks if name in per_rank[rank]]
            if values:
                detail[name] = aggregate(values)
        # DP-attention exposes one cache-hit gauge per rank.  Keep those only when
        # there is more than one rank; ordinary workers use the token-weighted
        # 60-second aggregate above.
        hit_ranks = [per_rank[rank].get("batch_hit_pct") for rank in ranks]
        if len(hit_ranks) > 1 and any(value is not None for value in hit_ranks):
            detail["hit_ranks"] = hit_ranks
        detail["dp_metrics"] = (
            [{"rank": rank, **per_rank[rank]} for rank in ranks] if engine in {"sglang", "vllm"} else []
        )
        return detail

    def _fetch_loads(self, base_url: str) -> dict:
        """Keep incomplete or invalid DP load coverage separate from metric values."""
        detail = {"endpoint_errors": {}}
        raw_loads = self._http.fetch(f"{base_url}/v1/loads", timeout=1)
        try:
            loads = json.loads(raw_loads).get("loads", []) if raw_loads else []
            valid = [row for row in loads if isinstance(row, dict) and type(row.get("dp_rank")) is int]
            ids = [row["dp_rank"] for row in valid]
            if not valid or len(valid) != len(loads) or sorted(ids) != list(range(len(ids))):
                raise ValueError("Missing or duplicate DP ranks")
            detail["dp_loads"] = [
                {
                    key: value
                    for key, value in row.items()
                    if key in {"dp_rank", "num_running_reqs", "num_waiting_reqs", "num_waiting_uncached_tokens"}
                    and type(value) in (int, float)
                    and math.isfinite(value)
                    and value >= 0
                }
                for row in valid
            ]
            prefill = [row.get("num_waiting_uncached_tokens") for row in detail["dp_loads"]]
            if all(value is not None for value in prefill):
                detail["prefill_tokens"] = sum(prefill)
        except (ValueError, TypeError, AttributeError):
            detail["endpoint_errors"]["loads"] = "Not available; prefill coverage is unknown"

        return detail

    def collect(self, worker_urls: list[str]) -> dict[str, dict]:
        futures = {self._pool.submit(self._fetch_detail, url): url for url in worker_urls}
        details = {}
        for future in as_completed(futures):
            url = futures[future]
            try:
                details[url] = future.result()
            except Exception:
                logger.warning("Failed to collect worker metrics from %s", url, exc_info=True)
        return details

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._http.close()
