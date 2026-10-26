"""Check packaged dashboard assets and configuration compatibility."""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import typer
from click import unstyle
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from router_monitor import router
from router_monitor.cli import app
from router_monitor.config import MonitorConfig, RouterType
from router_monitor.dashboard import WebDashboard
from router_monitor.state import SnapshotStore


class ConfigTests(unittest.TestCase):
    def invoke(self, args=()):
        with patch("router_monitor.cli.MonitorApplication") as application:
            result = CliRunner().invoke(app, list(args))
            self.assertEqual(result.exit_code, 0, result.output)
            application.return_value.run.assert_called_once()
            return application.call_args.args[0]

    def test_environment_defaults_and_cli_overrides(self):
        with patch.dict(os.environ, {"PROM_PORT": "30000", "INTERVAL": "5", "KEEPALIVE_MAX_TOKENS": "128"}, clear=True):
            config = self.invoke(["--host", "192.0.2.1", "--interval", "1", "--keepalive-max-tokens", "256"])
        self.assertEqual(config.prom_port, 30000)
        self.assertEqual(config.interval, 1)
        self.assertEqual(config.host, "192.0.2.1")
        self.assertEqual(config.keepalive_max_tokens, 256)
        with patch.dict(os.environ, {}, clear=True):
            default = self.invoke()
            self.assertEqual(default.host, "127.0.0.1")
            self.assertEqual(default.router_type, RouterType.auto)
            self.assertEqual(default.history_window, 1440)
            self.assertFalse(default.keepalive)
            self.assertEqual(default.keepalive_max_tokens, 64)
            self.assertFalse(default.auto_unregister)
            self.assertEqual(default.unregister_unavailable_after, 1800)
            self.assertEqual(self.invoke(["--unregister-unavailable-after", "0"]).unregister_unavailable_after, 0)
            cleanup = self.invoke(["--unregister-unavailable-after", "600"])
            self.assertEqual(cleanup.unregister_unavailable_after, 600)
            self.assertFalse(cleanup.auto_unregister)
            for flags, expected in [
                (["--keepalive"], (True, False)),
                (["--auto-unregister"], (False, True)),
                (["--keepalive", "--auto-unregister"], (True, True)),
            ]:
                with self.subTest(flags=flags):
                    config = self.invoke(flags)
                    self.assertEqual((config.keepalive, config.auto_unregister), expected)

    def test_router_url_precedence(self):
        cases = [
            ([], "http://environment:4000"),
            (["--router-url", "http://option:4000"], "http://option:4000"),
            (["https://router.example:8443/"], "https://router.example:8443"),
            (["http://positional:8000", "--router-url", "http://option:4000"], "http://positional:8000"),
        ]
        with patch.dict(os.environ, {"ROUTER_URL": "http://environment:4000"}, clear=True):
            for args, expected in cases:
                with self.subTest(args=args):
                    self.assertEqual(self.invoke(args).router_url, expected)

    def test_router_connection_options_and_auto_default(self):
        with patch.dict(os.environ, {"ROUTER_API_KEY": "router-secret", "WORKER_API_KEY": "worker-secret"}, clear=True):
            config = self.invoke(
                [
                    "--router-url",
                    "https://router.example",
                    "--metrics-url",
                    "https://metrics.example/metrics",
                    "--router-type",
                    "litellm",
                    "--worker",
                    "http://worker-a:8000/",
                    "--worker",
                    "http://worker-b:8000",
                ]
            )
        self.assertEqual(config.router_type, RouterType.litellm)
        self.assertEqual(config.router_url, "https://router.example")
        self.assertEqual(config.prom_url, "https://metrics.example/metrics")
        self.assertEqual(config.worker_urls, ("http://worker-a:8000", "http://worker-b:8000"))
        self.assertEqual(config.api_key, "router-secret")
        self.assertEqual(config.worker_api_key, "worker-secret")
        self.assertNotIn("secret", repr(config))

    def test_help_and_invalid_values_do_not_start_the_monitor(self):
        with patch("router_monitor.cli.MonitorApplication") as application:
            result = CliRunner().invoke(app, ["--help"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("--keepalive", unstyle(result.output))
            self.assertIn("MASTER_HOST", unstyle(result.output))
            for args in [
                ["--port", "65536"],
                ["--interval", "0"],
                ["--keepalive-concurrency", "0"],
                ["--unregister-unavailable-after", "-1"],
            ]:
                with self.subTest(args=args):
                    result = CliRunner().invoke(app, args)
                    self.assertEqual(result.exit_code, 2, result.output)
            result = CliRunner().invoke(app, [], env={"INTERVAL": "invalid"})
            self.assertEqual(result.exit_code, 2, result.output)
            application.assert_not_called()

    def test_router_cli_help_and_environment_overrides(self):
        router_app = typer.Typer()
        router_app.command()(router.main)
        result = CliRunner().invoke(router_app, ["--help"])
        self.assertEqual(result.exit_code, 0)
        launch = Mock()
        modules = {
            "sglang_router": ModuleType("sglang_router"),
            "sglang_router.launch_router": SimpleNamespace(launch_router=launch),
            "sglang_router.router_args": SimpleNamespace(RouterArgs=lambda **kwargs: kwargs),
        }
        with patch.dict(sys.modules, modules):
            result = CliRunner().invoke(
                router_app,
                ["--port", "2740", "--no-dp-aware"],
                env={"ROUTER_PORT": "2731", "ROUTER_PROMETHEUS_PORT": "30001"},
            )
        self.assertEqual(result.exit_code, 0, result.output)
        settings = launch.call_args.args[0]
        self.assertEqual(settings["port"], 2740)
        self.assertEqual(settings["prometheus_port"], 30001)
        self.assertFalse(settings["dp_aware"])


class DashboardTests(unittest.TestCase):
    def test_single_file_dashboard_and_configuration(self):
        config = MonitorConfig(host="192.0.2.1", interval=5, history_window=10)
        app = WebDashboard(config, SnapshotStore()).create_app()
        with TestClient(app) as client:
            html = client.get("/")
            self.assertEqual(html.status_code, 200)
            self.assertIn("intervalMs: 5000", html.text)
            self.assertIn("collectionSeconds: 5", html.text)
            self.assertIn("historyWindowMinutes: 10", html.text)
            self.assertIn("Last 10 min", html.text)
            self.assertEqual(html.headers["cache-control"], "no-store")
            self.assertEqual(html.headers["content-encoding"], "gzip")
            self.assertIn('id="echarts-vendor"', html.text)
            self.assertIn("The Apache Software Foundation", html.text)
            self.assertNotIn('src="http', html.text)
            self.assertEqual(client.get("/data").json()["by_model"], {})
            self.assertEqual(client.get("/history").json(), [])

    def test_template_replacement_is_visible_without_recreating_the_app(self):
        config = MonitorConfig(host="127.0.0.1")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = root / "templates/dashboard.html"
            template.parent.mkdir()
            template.write_text("first INTERVAL_MS HISTORY_LABEL")
            with patch("router_monitor.dashboard.files", return_value=root):
                app = WebDashboard(config, SnapshotStore()).create_app()
            with TestClient(app) as client:
                self.assertEqual(client.get("/").text, "first 2000 1440 min")
                template.write_text("second COLLECTION_S WIN_DEFAULT_MIN")
                self.assertEqual(client.get("/").text, "second 2.0 1440")
