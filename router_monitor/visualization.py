"""Derive dashboard views from one collection; never make extra network requests."""

from collections import Counter, defaultdict


def _worker_row(url: str, workers: list[dict], model: str | None, detail: dict) -> dict:
    rank_health_known = all(worker.get("rank_health_known", True) for worker in workers)
    healthy = sum(worker.get("is_healthy") is True for worker in workers)
    expected = len(workers)
    registry_failed = any(worker.get("is_healthy") is False for worker in workers)
    registry_unknown = any(worker.get("is_healthy") is None for worker in workers)
    loads = detail.get("dp_loads", [])
    complete_loads = bool(loads) and (not rank_health_known or len(loads) == expected)
    prefill = detail.get("prefill_tokens") if complete_loads else None
    covered = bool(detail.get("metrics_ok"))
    health = detail.get("health_level")
    success = detail.get("success_pct")
    engine_unhealthy = health is not None and health > 0
    requests_unhealthy = health is None and success is not None and success < 90
    if registry_failed or engine_unhealthy or requests_unhealthy:
        status = "unhealthy"
    elif not covered or registry_unknown and health is None:
        status = "unknown"
    else:
        status = "healthy"
    row = {
        "url": url,
        "model": model or next((w["model_id"] for w in workers if w.get("model_id")), "unknown"),
        "healthy_ranks": healthy if rank_health_known else None,
        "ranks": expected if rank_health_known else None,
        "registry_failed": registry_failed,
        "status": status,
        "metrics_ok": covered,
        "prefill_tokens": prefill,
        "dp_metrics": detail.get("dp_metrics", []),
        "dp_loads": loads,
        "counter_window": detail.get("counter_window", {}),
        "endpoint_errors": detail.get("endpoint_errors", {}),
    }
    for key in (
        "running",
        "waiting",
        "kv_pct",
        "swa_pct",
        "gen_tps",
        "hit_pct",
        "hit_ranks",
        "ttft_p50_ms",
        "spec_accept_len",
        "queue_latency_s",
        "health_level",
        "success_pct",
        "hit_cached_tokens",
        "hit_prompt_tokens",
    ):
        row[key] = detail.get(key)
    if complete_loads:
        for key, source in [("running", "num_running_reqs"), ("waiting", "num_waiting_reqs")]:
            values = [rank.get(source) for rank in loads]
            if values and all(type(value) is int and value >= 0 for value in values):
                row[key] = sum(values)
    if not covered and not row["endpoint_errors"]:
        row["endpoint_errors"] = {"metrics": "No healthy rank or no current metrics"}
    return row


def _summarize_rows(rows: list[dict]) -> dict:
    coverage = {}

    def aggregate(key, reducer=sum):
        values = [row[key] for row in rows if row.get(key) is not None]
        coverage[key] = len(values)
        return reducer(values) if values else None

    def window_value(key, weighted=False):
        observed = [row["counter_window"] for row in rows if row["counter_window"].get(key) is not None]
        coverage[key] = len(observed)
        if not observed:
            return None
        if weighted:
            count_key = key + "_count"
            count = sum(window[count_key] for window in observed)
            return sum(window[key] * window[count_key] for window in observed) / count if count else None
        return sum(window[key] for window in observed)

    cached = sum(row["hit_cached_tokens"] for row in rows if row["hit_cached_tokens"] is not None)
    prompt = sum(row["hit_prompt_tokens"] for row in rows if row["hit_prompt_tokens"] is not None)
    statuses = Counter(row["status"] for row in rows)
    summary = {
        "workers": len(rows),
        "healthy_workers": statuses["healthy"],
        "unhealthy_workers": statuses["unhealthy"],
        "unknown_workers": statuses["unknown"],
        "metrics_workers": sum(row["metrics_ok"] for row in rows),
        "ranks": sum(row["ranks"] for row in rows) if all(row["ranks"] is not None for row in rows) else None,
        "healthy_ranks": sum(row["healthy_ranks"] for row in rows)
        if all(row["healthy_ranks"] is not None for row in rows)
        else None,
        "health_missing_workers": sum(row["health_level"] is None for row in rows),
        "running": aggregate("running"),
        "waiting": aggregate("waiting"),
        "tps": aggregate("gen_tps"),
        "kv_pct": aggregate("kv_pct", max),
        "swa_pct": aggregate("swa_pct", max),
        "success_pct": aggregate("success_pct", min),
        "prefill_tokens": aggregate("prefill_tokens"),
        "ttft_mean_ms": window_value("ttft_mean_ms", weighted=True),
        "itl_ms": window_value("itl_ms", weighted=True),
        "retract_rate": window_value("retract_rate"),
        "abort_rate": window_value("abort_rate"),
        "hit_pct": cached / prompt * 100 if prompt else None,
    }
    batch_hits = [dp["batch_hit_pct"] for row in rows for dp in row["dp_metrics"] if "batch_hit_pct" in dp]
    summary["batch_hit_min_pct"] = min(batch_hits) if batch_hits else None
    running = [row["running"] for row in rows if row["running"] is not None]
    running_total = summary["running"]
    running_maximum = max(running) if running else None
    summary["balance"] = {
        "idle": sum(value == 0 for value in running),
        "minimum": min(running) if running else None,
        "maximum": running_maximum,
        "max_mean": running_maximum / (running_total / len(running))
        if running and len(running) == len(rows) and running_total
        else None,
    }
    summary["coverage"] = coverage
    return summary


def build_visualization(snapshot: dict, registry: list[dict]) -> dict:
    hosts = defaultdict(list)
    models = {url: model for model, urls in snapshot["by_model"].items() for url in urls}
    for worker in registry:
        hosts[worker.get("url", "").split("@")[0]].append(worker)
    rows = [
        _worker_row(url, workers, models.get(url), snapshot["details"].get(url, {}))
        for url, workers in sorted(hosts.items())
    ]
    summary = _summarize_rows(rows)
    if not snapshot.get("registry_ok", True):
        for key in (
            "workers",
            "healthy_workers",
            "unhealthy_workers",
            "unknown_workers",
            "metrics_workers",
            "ranks",
            "healthy_ranks",
            "health_missing_workers",
        ):
            summary[key] = None
    groups = defaultdict(list)
    for row in rows:
        groups[row["model"]].append(row)
    by_model_stats = {}
    for model in snapshot["by_model"]:
        group = groups[model]
        model_summary = _summarize_rows(group)
        by_model_stats[model] = model_summary
    return {"worker_rows": rows, "summary": summary, "by_model_stats": by_model_stats}
