"""Regression coverage for cumulative counters and Prometheus histograms."""

import unittest
from unittest.mock import patch

from router_monitor.metrics import CounterWindow, TokenDeltaWindow, gauge_values, histogram_quantile, metric_samples
from router_monitor.worker import WorkerMetricsCollector


class MetricsTests(unittest.TestCase):
    def test_token_window_handles_counter_resets_and_expiry(self):
        window = TokenDeltaWindow(60)
        self.assertIsNone(window.observe("worker", 10, 20, now=0))
        self.assertEqual(window.observe("worker", 20, 60, now=1), (10, 40))
        self.assertEqual(window.observe("worker", 30, 80, now=2), (20, 60))
        self.assertIsNone(window.observe("worker", 1, 2, now=3))
        self.assertEqual(window.observe("worker", 5, 10, now=4), (4, 8))
        self.assertIsNone(window.observe("worker", 9, 20, now=65))

    def test_gauges_support_rank_labels_and_unlabelled_metrics(self):
        text = 'sglang:gen_throughput{model="x",dp_rank="1"} 1.2e2\nsglang:gen_throughput{dp_rank="0",model="x"} 80\n'
        self.assertEqual(gauge_values("sglang:gen_throughput", metric_samples(text)), {"1": 120, "0": 80})
        self.assertEqual(gauge_values("sglang:token_usage", metric_samples("sglang:token_usage 0.5\n")), {"0": 0.5})

    def test_parser_handles_metadata_escaped_labels_and_nonfinite_values(self):
        text = (
            "# HELP sglang:requests Request count\n"
            "# TYPE sglang:requests counter\n"
            r'sglang:requests_total{model="a,b\"c\\d",dp_rank="0"} 1.2e2 123'
            '\nsglang:requests_total{dp_rank="1"} +Inf\n'
            'sglang:requests_total{dp_rank="2"} NaN\n'
            'sglang:requests_total{dp_rank="3"} 1e9999\n'
        )
        samples = metric_samples(text)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["name"], "sglang:requests_total")
        self.assertEqual(samples[0]["value"], 120)
        self.assertEqual(samples[0]["labels"]["model"], 'a,b"c\\d')

    def test_histogram_uses_cumulative_buckets_once(self):
        text = (
            'sglang:latency_bucket{le="0.1"} 2\n'
            'sglang:latency_bucket{le="0.2"} 4\n'
            'sglang:latency_bucket{le="+Inf"} 4\n'
            "sglang:latency_count{} 4\n"
        )
        self.assertAlmostEqual(histogram_quantile(metric_samples(text), "latency", 0.75), 0.15)


class CounterWindowTests(unittest.TestCase):
    @staticmethod
    def samples(total, count=2, label="a"):
        return metric_samples(
            f'sglang:time_to_first_token_seconds_sum{{model="{label}"}} {total}\n'
            f'sglang:time_to_first_token_seconds_count{{model="{label}"}} {count}\n'
            f"sglang:total_retracted_reqs {count}\n"
        )

    def test_first_sample_reset_gap_and_series_change_stay_missing(self):
        window = CounterWindow()
        self.assertEqual(window.observe("x", self.samples(1), now=0), {})
        result = window.observe("x", self.samples(2, 4), now=2)
        self.assertEqual(result["ttft_mean_ms"], 500)
        self.assertEqual(result["retract_rate"], 1)
        self.assertNotIn("ttft_mean_ms", window.observe("x", self.samples(1, 2), now=4))
        self.assertEqual(window.observe("x", self.samples(2, 4), now=70), {})
        self.assertNotIn("ttft_mean_ms", window.observe("x", self.samples(3, 6, "b"), now=72))
        restart = metric_samples("process_start_time_seconds 10\n")
        self.assertEqual(window.observe("x", self.samples(4, 8, "b") + restart, now=74), {})
        self.assertEqual(window.observe("x", self.samples(5, 10, "b") + restart, now=76)["ttft_mean_ms"], 500)
        duplicate = self.samples(6, 12, "b") + restart
        duplicate.append(duplicate[0])
        self.assertNotIn("ttft_mean_ms", window.observe("x", duplicate, now=78))
        self.assertNotIn("ttft_mean_ms", window.observe("x", self.samples(7, 14, "b") + restart, now=80))


class WorkerHealthTests(unittest.TestCase):
    def collect(self, metrics):
        collector = WorkerMetricsCollector(2)
        self.addCleanup(collector.close)
        with patch.object(
            collector._http,
            "fetch",
            side_effect=lambda url, **_kw: metrics if url.endswith("/metrics") else "",
        ):
            return collector._fetch_detail("http://worker")

    def test_tp_labelled_health_reports_worst_value_and_lowest_success(self):
        detail = self.collect(
            'sglang:engine_health_level{tp_rank="4",pp_rank="0"} 93.6\n'
            'sglang:engine_health_level{tp_rank="0",pp_rank="0"} 91\n'
            'sglang:req_success_rate{tp_rank="4",pp_rank="0"} 0.636\n'
            'sglang:req_success_rate{tp_rank="0",pp_rank="0"} 0.93\n'
            'sglang:req_success_rate{tp_rank="1",pp_rank="0"} -1\n'
            'sglang:gen_throughput{dp_rank="0"} 100\n'
        )
        self.assertEqual(detail["health_level"], 93.6)
        self.assertAlmostEqual(detail["success_pct"], 63.6)
        self.assertNotIn("health_level", detail["dp_metrics"][0])
        self.assertNotIn("success_pct", detail["dp_metrics"][0])

    def test_explicit_dp_health_reduces_multiple_tp_series_per_dp(self):
        detail = self.collect(
            'sglang:engine_health_level{dp_rank="0",tp_rank="0"} 0\n'
            'sglang:engine_health_level{dp_rank="0",tp_rank="1"} 92\n'
            'sglang:req_success_rate{dp_rank="0",tp_rank="0"} 1\n'
            'sglang:req_success_rate{dp_rank="0",tp_rank="1"} 0.8\n'
        )
        self.assertEqual(detail["dp_metrics"][0]["health_level"], 92)
        self.assertEqual(detail["dp_metrics"][0]["success_pct"], 80)
