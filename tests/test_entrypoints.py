"""Check packaged dashboard assets and configuration compatibility."""

import json
import os
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import typer
from click import unstyle
from fastapi.testclient import TestClient
from jinja2 import FileSystemLoader
from typer.testing import CliRunner

from router_monitor import router
from router_monitor.app import MonitorApplication
from router_monitor.cli import app
from router_monitor.config import MonitorConfig, PrometheusHistoryConfig, RouterType
from router_monitor.maintenance.keepalive import KeepaliveWarmer
from router_monitor.presentation.dashboard import create_dashboard
from router_monitor.state import SnapshotStore
from router_monitor.target import RouterTarget


class ConfigTests(unittest.TestCase):
    def invoke(self, args=()):
        with patch("router_monitor.cli.MonitorApplication") as application:
            result = CliRunner().invoke(app, list(args))
            self.assertEqual(result.exit_code, 0, result.output)
            application.return_value.run.assert_called_once()
            return application.call_args.args[0][""]

    def test_named_router_config_cli_and_invalid_input(self):
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            path = Path(temporary) / "routers.json"
            entries = {
                "inference": {
                    "endpoint": "http://engine:4000",
                    "interval": 3,
                    "keepalive_unconditional": True,
                    "keepalive_min_tokens": 32,
                    "keepalive_traffic_poll_interval": 0.25,
                    "keepalive_concurrency": 20,
                    "keepalive_total_concurrency": 1,
                },
                "proxy": {"endpoint": "http://proxy:8080", "router_type": "nginx", "worker_urls": ["http://worker/"]},
            }
            path.write_text(json.dumps(entries))
            with patch("router_monitor.cli.MonitorApplication") as application:
                result = CliRunner().invoke(app, ["--router-config", str(path), "--history-window", "90"])
                self.assertEqual(result.exit_code, 0, result.output)
                application.return_value.run.assert_called_once()
                targets = application.call_args.args[0]
                self.assertEqual(set(targets), {"inference", "proxy"})
                self.assertEqual(targets["inference"].router_url, "http://engine:4000")
                self.assertEqual(targets["inference"].interval, 3)
                self.assertTrue(targets["inference"].keepalive_unconditional)
                self.assertEqual(targets["inference"].keepalive_min_tokens, 32)
                self.assertEqual(targets["inference"].keepalive_traffic_poll_interval, 0.25)
                self.assertEqual(targets["inference"].keepalive_concurrency, 20)
                self.assertEqual(targets["proxy"].router_type, RouterType.nginx)
                self.assertEqual(targets["proxy"].worker_urls, ("http://worker",))
                for target in targets.values():
                    self.assertEqual(target.history_window, 90)
                    self.assertEqual(target.unregister_unavailable_after, 0)
            for content, extra in [
                (json.dumps(entries), ["--web-port", "0"]),
                ("{}", []),
                ('{"bad": {"endpoint": "file:///tmp/router"}}', []),
                ('{"bad": {"endpoint": "http://router", "typo": 1}}', []),
                ('{"bad": {"endpoint": "http://router", "keepalive": "true"}}', []),
                ('{"bad": {"endpoint": "http://router", "keepalive_min_tokens": 65}}', []),
                ('{"bad": {"endpoint": "http://router", "interval": NaN}}', []),
                ('{"bad": {"endpoint": "http://router", "api_key": ["hidden-token"]}}', []),
                ("invalid json", []),
            ]:
                with (
                    self.subTest(content=content, extra=extra),
                    patch("router_monitor.cli.MonitorApplication") as application,
                ):
                    path.write_text(content)
                    result = CliRunner().invoke(app, ["--router-config", str(path), *extra])
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertNotIn("hidden-token", result.output)
                    application.assert_not_called()

    def test_environment_defaults_and_cli_overrides(self):
        with patch.dict(os.environ, {"PROM_PORT": "30000", "INTERVAL": "5", "KEEPALIVE_MAX_TOKENS": "128"}, clear=True):
            config = self.invoke([
                "--host",
                "192.0.2.1",
                "--interval",
                "1",
                "--keepalive-max-tokens",
                "256",
                "--keepalive-concurrency",
                "16",
                "--keepalive-total-concurrency",
                "1",
            ])
        self.assertEqual(config.prom_port, 30000)
        self.assertEqual(config.interval, 1)
        self.assertEqual(config.host, "192.0.2.1")
        self.assertEqual(config.keepalive_max_tokens, 256)
        self.assertEqual(config.keepalive_concurrency, 16)
        with patch.dict(os.environ, {}, clear=True):
            default = self.invoke()
            self.assertEqual(default.host, "127.0.0.1")
            self.assertEqual(default.router_type, RouterType.auto)
            self.assertEqual(default.history_window, 120)
            self.assertEqual(default.keepalive_mode, "off")
            self.assertEqual(default.keepalive_max_tokens, 64)
            self.assertFalse(default.auto_unregister)
            self.assertEqual(default.unregister_unavailable_after, 1800)
            self.assertEqual(self.invoke(["--unregister-unavailable-after", "0"]).unregister_unavailable_after, 0)
            cleanup = self.invoke(["--unregister-unavailable-after", "600"])
            self.assertEqual(cleanup.unregister_unavailable_after, 600)
            self.assertFalse(cleanup.auto_unregister)
            for flags, mode, auto_unregister in [
                (["--keepalive"], "adaptive", False),
                (["--auto-unregister"], "off", True),
                (["--keepalive", "--auto-unregister"], "adaptive", True),
                (["--keepalive-unconditional"], "unconditional", False),
                (["--keepalive", "--keepalive-unconditional"], "unconditional", False),
            ]:
                with self.subTest(flags=flags):
                    config = self.invoke(flags)
                    self.assertEqual((config.keepalive_mode, config.auto_unregister), (mode, auto_unregister))

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
            config = self.invoke([
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
            ])
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
                ["--keepalive-traffic-poll-interval", "0.05"],
                ["--keepalive-traffic-poll-interval", "nan"],
                ["--keepalive-min-tokens", "65"],
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


class ApplicationTests(unittest.TestCase):
    def test_terminal_and_web_run_and_close_the_same_target_services(self):
        for port in (0, 8899):
            with self.subTest(port=port):
                config = MonitorConfig(host="router", web_port=port, interval=0.1, keepalive_unconditional=True)
                snapshot = {"collected_at": 1, "by_model_stats": {}, "router_type": "sglang"}
                published, warming, stopped = Event(), Event(), Event()
                collecting, stopped_during_collection = Event(), Event()

                def collect():
                    if port and published.is_set():
                        collecting.set()
                        if stopped.wait(2):
                            stopped_during_collection.set()
                    return snapshot

                def update(data):
                    self.assertEqual(data, snapshot)
                    published.set()
                    if not port:
                        self.assertTrue(warming.wait(2))
                        raise KeyboardInterrupt

                def warm(_, stop):
                    warming.set()
                    if stop.wait(2):
                        stopped.set()

                def serve(dashboard, **options):
                    self.assertEqual(options["port"], port)
                    self.assertTrue(published.wait(2))
                    self.assertTrue(warming.wait(2))
                    with TestClient(dashboard) as client:
                        self.assertEqual(client.get("/data").json()["collected_at"], 1)
                        self.assertEqual(client.get("/settings/keepalive").json()["settings"]["mode"], "unconditional")
                    self.assertTrue(collecting.wait(2))

                with (
                    patch("router_monitor.target.RouterCollector") as collector,
                    patch("router_monitor.app.terminal_updates", return_value=nullcontext(update)),
                    patch.object(KeepaliveWarmer, "run", side_effect=warm, autospec=True) as warmer,
                    patch("uvicorn.run", side_effect=serve) as web,
                ):
                    collector.return_value.collect.side_effect = collect
                    MonitorApplication({"": config}).run()
                    self.assertEqual(web.call_count, bool(port))
                    warmer.assert_called_once()
                    collector.return_value.close.assert_called_once()
                    self.assertTrue(stopped.is_set())
                    if port:
                        self.assertTrue(stopped_during_collection.is_set())


class DashboardTests(unittest.TestCase):
    def test_keepalive_settings_apply_to_one_router_and_replace_its_warmer(self):
        target = RouterTarget(MonitorConfig(host="router"))
        other = RouterTarget(MonitorConfig(host="other"))
        self.addCleanup(target.keepalive.close)
        self.addCleanup(other.keepalive.close)
        app = create_dashboard({"other": other, "router & test": target})
        sessions = Queue()

        def run(warmer, stop):
            concurrency = warmer.config.keepalive_concurrency
            sessions.put(("started", concurrency, warmer.config.keepalive_unconditional))
            stop.wait(5)
            sessions.put(("stopped", concurrency))

        path = "/settings/keepalive"
        scope = {"router": "router & test"}
        with (
            patch.object(KeepaliveWarmer, "run", new=run),
            TestClient(app, base_url="http://localhost:8899", client=("127.0.0.1", 50000)) as client,
        ):
            initial = client.get(path, params=scope).json()
            self.assertTrue(initial["editable"])
            self.assertNotIn("keepalive_total_concurrency", initial["settings"])
            settings = {**initial["settings"], "mode": "unconditional", "keepalive_concurrency": 96}
            response = client.put(path, params=scope, json={**settings, "keepalive_total_concurrency": 1})
            self.assertEqual(response.json()["settings"], settings)
            self.assertEqual(sessions.get(timeout=2), ("started", 96, True))
            self.assertEqual(client.get(path).json()["settings"]["mode"], "off")

            smaller = {**settings, "mode": "adaptive", "keepalive_concurrency": 32}
            self.assertEqual(client.put(path, params=scope, json=smaller).status_code, 200)
            self.assertEqual(sessions.get(timeout=2), ("stopped", 96))
            self.assertEqual(sessions.get(timeout=2), ("started", 32, False))
            invalid = {**smaller, "keepalive_min_tokens": 100, "keepalive_max_tokens": 64}
            self.assertEqual(client.put(path, params=scope, json=invalid).status_code, 422)
            response = client.put(path, params=scope, json=settings, headers={"Origin": "http://other.test"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(client.get(path, params=scope).json()["settings"], smaller)

            target.store.publish({"collected_at": 1, "by_model_stats": {}, "router_type": "nginx"})
            self.assertTrue(client.get(path, params=scope).json()["editable"])
            self.assertEqual(client.put(path, params=scope, json=settings).status_code, 403)
            disabled = {**smaller, "mode": "off"}
            response = client.put(path, params=scope, json=disabled)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["editable"])
            self.assertEqual(sessions.get(timeout=2), ("stopped", 32))
        restarted = create_dashboard({"other": RouterTarget(other.config), "router & test": RouterTarget(target.config)})
        with TestClient(restarted, base_url="http://monitor.test", client=("192.0.2.5", 50000)) as remote:
            self.assertFalse(remote.get(path, params=scope).json()["editable"])
            self.assertEqual(remote.put(path, params=scope, json=settings).status_code, 403)
            self.assertEqual(remote.get(path, params=scope).json()["settings"], initial["settings"])

    def test_named_router_dashboard_isolates_data_history_and_settings(self):
        first = MonitorConfig(host="first", interval=2, history_window=60)
        second = MonitorConfig(host="second", interval=7, history_window=120)
        first_store, second_store = SnapshotStore(), SnapshotStore()
        for store, model, at in [
            (first_store, "first-model", 1),
            (first_store, "first-model", 2),
            (second_store, "second-model", 3),
        ]:
            store.publish({"collected_at": at, "by_model_stats": {model: {"running": at}}})
        name = 'second & "quoted"'
        app = create_dashboard({"first": RouterTarget(first, first_store), name: RouterTarget(second, second_store)})
        with TestClient(app) as client:
            self.assertEqual(client.get("/data").json()["by_model_stats"], {"first-model": {"running": 2}})
            for limit in (1, 0, -1, sys.maxsize + 1):
                with self.subTest(limit=limit):
                    expected = [{"at": at, "by_model": {"first-model": {"running": at}}} for at in (1, 2)]
                    self.assertEqual(
                        client.get("/history", params={"n": limit}).json(), expected[-1:] if limit == 1 else expected
                    )
            self.assertEqual(
                client.get("/data", params={"router": name}).json()["by_model_stats"], {"second-model": {"running": 3}}
            )
            self.assertEqual(client.get("/history", params={"router": name}).json(), second_store.history())
            html = client.get("/", params={"router": name}).text
            self.assertIn("collectionSeconds: 7", html)
            self.assertIn("historyWindowMinutes: 120", html)
            self.assertIn('value="second &amp; &#34;quoted&#34;" selected', html)
            for route in ("/", "/data", "/history"):
                self.assertEqual(client.get(route, params={"router": "missing"}).status_code, 404)

    def test_prometheus_history_merges_stored_and_recent_samples(self):
        config = MonitorConfig(host="router")
        store = SnapshotStore()
        store.publish({"collected_at": 13.1234, "by_model_stats": {"m": {"running": 0, "waiting": None}}})
        history = PrometheusHistoryConfig("http://prometheus/monitoring", job="example-monitor")
        name = 'router "A"'
        app = create_dashboard({name: RouterTarget(config, store)}, history_config=history)
        requests = []

        def respond(request):
            requests.append(request)
            if len(requests) == 2:
                return httpx.Response(503)
            series = [
                {"metric": {"__name__": "router_monitor_sample"}, "values": [[10, "1"], [11, "1"], [13.123, "1"]]},
                {"metric": {"__name__": "router_monitor_model_running", "model": "m"}, "values": [[10, "7"], [13.123, "4"]]},
                {"metric": {"__name__": "router_monitor_model_waiting", "model": "m"}, "values": [[10, "NaN"], [11, "+Inf"]]},
            ]
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": series}})

        with TestClient(app) as client:
            app.state.history.http.close()
            app.state.history.http = httpx.Client(base_url=history.url, transport=httpx.MockTransport(respond))
            result = client.get("/history", params={"router": name, "n": 3})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(
                result.json(),
                [
                    {"at": 10, "by_model": {"m": {"running": 7}}},
                    {"at": 11, "by_model": {}},
                    {"at": 13.1234, "by_model": {"m": {"running": 0, "waiting": None}}},
                ],
            )
            self.assertEqual(requests[0].url.path, "/monitoring/api/v1/query")
            query = requests[0].url.params["query"]
            self.assertIn('job="example-monitor"', query)
            self.assertIn("router=" + json.dumps(name), query)
            self.assertEqual(client.get("/history", params={"router": name}).status_code, 503)
            self.assertEqual(client.get("/data", params={"router": name}).status_code, 200)
            metrics = client.get("/metrics").text
            labels = 'model="m",router=' + json.dumps(name)
            self.assertIn(f"router_monitor_model_running{{{labels}}} 0.0 13123", metrics)
            self.assertNotIn("router_monitor_model_waiting{", metrics)

    def test_single_file_dashboard_and_configuration(self):
        config = MonitorConfig(host="192.0.2.1", interval=5, history_window=10)
        app = create_dashboard({"": RouterTarget(config)})
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
            template.write_text('first {{ interval_ms }} {{ "%g"|format(history_window) }} min')
            with patch("router_monitor.presentation.dashboard.PackageLoader", return_value=FileSystemLoader(template.parent)):
                app = create_dashboard({"": RouterTarget(config)})
            with TestClient(app) as client:
                self.assertEqual(client.get("/").text, "first 2000 120 min")
                template.write_text('second {{ collection_seconds }} {{ "%g"|format(history_window) }}')
                self.assertEqual(client.get("/").text, "second 2.0 120")
