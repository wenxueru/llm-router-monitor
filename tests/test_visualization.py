"""Cover missing measurements, physical worker grouping, and complete DP coverage."""

import unittest

from router_monitor.visualization import build_visualization


class VisualizationTests(unittest.TestCase):
    def test_partial_dp_loads_and_unhealthy_hosts_are_visible(self):
        registry = [
            {"url": "http://a@0", "is_healthy": True, "load": 1},
            {"url": "http://a@1", "is_healthy": False, "load": 0},
            {"url": "http://b", "is_healthy": False, "load": 0},
        ]
        snapshot = {
            "by_model": {"m": ["http://a"]},
            "details": {
                "http://a": {
                    "metrics_ok": True,
                    "running": 0,
                    "waiting": 2,
                    "dp_loads": [{"dp_rank": 0}],
                    "prefill_tokens": 100,
                    "counter_window": {"ttft_mean_ms": 100, "ttft_mean_ms_count": 2},
                },
            },
        }
        result = build_visualization(snapshot, registry)
        summary = result["summary"]
        self.assertEqual(len(result["worker_rows"]), 2)
        self.assertIsNone(summary["prefill_tokens"])
        self.assertEqual(summary["running"], 0)
        self.assertEqual(summary["unhealthy_workers"], 2)
        self.assertEqual(summary["coverage"]["running"], 1)
        self.assertIsNone(summary["balance"]["max_mean"])
        self.assertEqual(summary["ttft_mean_ms"], 100)
        self.assertIsNone(summary["itl_ms"])

    def test_model_trends_share_aggregation_without_mixing_models_or_missing_data(self):
        registry = [
            {"url": f"http://{host}", "is_healthy": True, "model_id": model}
            for host, model in [("a1", "a"), ("a2", "a"), ("b", "b"), ("missing", "missing")]
        ]
        snapshot = {
            "by_model": {"a": ["http://a1", "http://a2"], "b": ["http://b"], "missing": ["http://missing"]},
            "details": {
                "http://a1": {
                    "metrics_ok": True,
                    "running": 1,
                    "waiting": 0,
                    "gen_tps": 100,
                    "kv_pct": 20,
                    "swa_pct": 5,
                    "success_pct": 100,
                    "hit_cached_tokens": 10,
                    "hit_prompt_tokens": 100,
                    "counter_window": {"ttft_mean_ms": 100, "ttft_mean_ms_count": 1, "abort_rate": 0.5},
                    "dp_metrics": [{"batch_hit_pct": 80}],
                },
                "http://a2": {
                    "metrics_ok": True,
                    "running": 3,
                    "waiting": 2,
                    "gen_tps": 300,
                    "kv_pct": 70,
                    "swa_pct": 10,
                    "success_pct": 80,
                    "hit_cached_tokens": 270,
                    "hit_prompt_tokens": 300,
                    "counter_window": {"ttft_mean_ms": 400, "ttft_mean_ms_count": 3, "abort_rate": 1},
                    "dp_metrics": [{"batch_hit_pct": 40}],
                },
                "http://b": {
                    "metrics_ok": True,
                    "running": 10,
                    "waiting": 5,
                    "gen_tps": 900,
                    "counter_window": {"ttft_mean_ms": 900, "ttft_mean_ms_count": 100},
                },
            },
        }
        result = build_visualization(snapshot, registry)
        a, b, missing = (result["by_model_stats"][name] for name in ("a", "b", "missing"))
        self.assertAlmostEqual(result["summary"]["ttft_mean_ms"], (100 + 400 * 3 + 900 * 100) / 104)
        self.assertEqual(result["summary"]["hit_pct"], 70)
        self.assertEqual(a["running"], 4)
        self.assertEqual(a["waiting"], 2)
        self.assertEqual(a["tps"], 400)
        self.assertEqual(a["ttft_mean_ms"], 325)
        self.assertEqual(a["abort_rate"], 1.5)
        self.assertEqual(a["hit_pct"], 70)
        self.assertEqual(a["batch_hit_min_pct"], 40)
        self.assertEqual(a["kv_pct"], 70)
        self.assertEqual(a["swa_pct"], 10)
        self.assertEqual(a["success_pct"], 80)
        self.assertEqual(b["running"], 10)
        self.assertEqual(b["ttft_mean_ms"], 900)
        for field in ("running", "tps", "ttft_mean_ms", "hit_pct", "abort_rate"):
            self.assertIsNone(missing[field], field)

    def test_lost_registry_does_not_look_like_an_empty_healthy_fleet(self):
        result = build_visualization({"by_model": {}, "details": {}, "registry_ok": False}, [])
        self.assertIsNone(result["summary"]["workers"])
        self.assertIsNone(result["summary"]["unhealthy_workers"])

    def test_complete_loads_supply_request_counts_when_gauges_are_missing(self):
        registry = [{"url": "http://a@0", "is_healthy": True}]
        snapshot = {
            "by_model": {},
            "details": {
                "http://a": {
                    "metrics_ok": True,
                    "dp_loads": [{"dp_rank": 0, "num_running_reqs": 3, "num_waiting_reqs": 2}],
                }
            },
        }
        result = build_visualization(snapshot, registry)
        self.assertEqual(result["summary"]["running"], 3)
        self.assertEqual(result["summary"]["waiting"], 2)
