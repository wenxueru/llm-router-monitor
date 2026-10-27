"""Monitor command-line interface and application lifecycle."""

import logging
import threading
from typing import Annotated

import typer

from .collectors import RouterCollector
from .config import MonitorConfig, RouterType
from .dashboard import WebDashboard, render_terminal
from .maintenance import KeepaliveWarmer
from .state import SnapshotStore

logger = logging.getLogger(__name__)

app = typer.Typer(
    help="Monitor LLM Routers, worker health, and inference metrics.",
    pretty_exceptions_show_locals=False,
)


class MonitorApplication:
    def __init__(self, config: MonitorConfig):
        self.config = config
        self.collector = RouterCollector(config)
        self.store = SnapshotStore()
        self.warmer = KeepaliveWarmer(config, self.store) if config.keepalive else None
        self._stop = threading.Event()

    def _collect_loop(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = self.collector.collect()
                self.store.publish(snapshot)
                render_terminal(snapshot, self.config)
            except Exception:
                logger.exception("Monitor collection cycle failed")
            self._stop.wait(self.config.interval)

    def run(self) -> None:
        warmer_thread = None
        collection_thread = None
        try:
            if self.warmer is not None:
                warmer_thread = threading.Thread(target=self.warmer.run, args=(self._stop,), daemon=True)
                warmer_thread.start()
            if self.config.web_port == 0:
                self._collect_loop()
            else:
                collection_thread = threading.Thread(target=self._collect_loop, daemon=True)
                collection_thread.start()
                WebDashboard(self.config, self.store).run()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            if collection_thread is not None:
                collection_thread.join()
            self.collector.close()
            if warmer_thread is not None:
                warmer_thread.join()


@app.command()
def monitor(
    url: Annotated[
        str | None,
        typer.Argument(help="Router base URL; overrides --router-url and ROUTER_URL."),
    ] = None,
    host: Annotated[
        str, typer.Option(envvar="MASTER_HOST", help="Router host.", rich_help_panel="Connection")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Router API port.", rich_help_panel="Connection")] = 2730,
    prom_port: Annotated[
        int, typer.Option(envvar="PROM_PORT", min=1, max=65535, help="Prometheus port.", rich_help_panel="Connection")
    ] = 29000,
    router_url: Annotated[
        str, typer.Option(envvar="ROUTER_URL", help="Router base URL, including scheme.", rich_help_panel="Connection")
    ] = "",
    metrics_url: Annotated[
        str,
        typer.Option(
            envvar="METRICS_URL",
            help="Prometheus scrape URL; overrides default discovery.",
            rich_help_panel="Connection",
        ),
    ] = "",
    admin_url: Annotated[
        str,
        typer.Option(
            envvar="ROUTER_ADMIN_URL", help="Separate admin base URL (e.g. Envoy).", rich_help_panel="Connection"
        ),
    ] = "",
    router_type: Annotated[
        RouterType, typer.Option(help="Router dialect; auto detects exposed interfaces.", rich_help_panel="Connection")
    ] = RouterType.auto,
    worker: Annotated[
        list[str] | None,
        typer.Option(
            "--worker", help="Worker base URL; repeat for proxies without discovery.", rich_help_panel="Connection"
        ),
    ] = None,
    api_key: Annotated[
        str,
        typer.Option(
            envvar="ROUTER_API_KEY",
            help="Bearer token for configured Router/admin/metrics origins only.",
            rich_help_panel="Connection",
        ),
    ] = "",
    worker_api_key: Annotated[
        str,
        typer.Option(
            envvar="WORKER_API_KEY", help="Separate Bearer token for worker endpoints.", rich_help_panel="Connection"
        ),
    ] = "",
    interval: Annotated[
        float,
        typer.Option(envvar="INTERVAL", min=0.1, help="Collection interval in seconds.", rich_help_panel="Dashboard"),
    ] = 2,
    history_window: Annotated[
        float, typer.Option(min=1, help="Initial history window in minutes.", rich_help_panel="Dashboard")
    ] = 720,
    web_port: Annotated[
        int, typer.Option(min=0, max=65535, help="Dashboard port; 0 for terminal only.", rich_help_panel="Dashboard")
    ] = 8899,
    keepalive: Annotated[
        bool,
        typer.Option(
            help="Send inference requests to idle SGLang-registered workers.", rich_help_panel="Worker maintenance"
        ),
    ] = False,
    auto_unregister: Annotated[
        bool,
        typer.Option(
            help="Unregister SGLang workers whose model differs from registration.",
            rich_help_panel="Worker maintenance",
        ),
    ] = False,
    unregister_unavailable_after: Annotated[
        float,
        typer.Option(
            min=0,
            help="SGLang only: unregister unreachable workers after this many unhealthy seconds; 0 disables cleanup.",
            rich_help_panel="Worker maintenance",
        ),
    ] = 1800,
    keepalive_model: Annotated[
        str,
        typer.Option(
            envvar="KEEPALIVE_MODEL", help="Limit keepalive to one model.", rich_help_panel="Keepalive tuning"
        ),
    ] = "",
    keepalive_interval: Annotated[
        float,
        typer.Option(
            envvar="KEEPALIVE_INTERVAL", min=0.1, help="Round interval in seconds.", rich_help_panel="Keepalive tuning"
        ),
    ] = 5,
    keepalive_max_interval: Annotated[
        float,
        typer.Option(
            envvar="KEEPALIVE_MAX_INTERVAL",
            min=0.1,
            help="Maximum retry backoff in seconds.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = 60,
    keepalive_max_tokens: Annotated[
        int,
        typer.Option(
            envvar="KEEPALIVE_MAX_TOKENS",
            min=1,
            help="Generated tokens per request.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = 64,
    keepalive_concurrency: Annotated[
        int,
        typer.Option(
            envvar="KEEPALIVE_CONCURRENCY",
            min=1,
            help="Maximum requests per idle worker per round.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = 8,
    keepalive_prompt_words: Annotated[
        int,
        typer.Option(
            envvar="KEEPALIVE_PROMPT_WORDS",
            min=1,
            help="Maximum prompt length in words.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = 512,
):
    """Start the dashboard. Only SGLang registrations support automatic cleanup."""
    config = MonitorConfig(
        host=host,
        port=port,
        prom_port=prom_port,
        endpoint=url or router_url,
        metrics_endpoint=metrics_url,
        admin_url=admin_url,
        router_type=router_type,
        worker_urls=tuple(url.rstrip("/") for url in worker or []),
        api_key=api_key,
        worker_api_key=worker_api_key,
        interval=interval,
        history_window=history_window,
        web_port=web_port,
        keepalive=keepalive,
        auto_unregister=auto_unregister,
        unregister_unavailable_after=unregister_unavailable_after,
        keepalive_model=keepalive_model,
        keepalive_interval=keepalive_interval,
        keepalive_max_interval=keepalive_max_interval,
        keepalive_max_tokens=keepalive_max_tokens,
        keepalive_concurrency=keepalive_concurrency,
        keepalive_prompt_words=keepalive_prompt_words,
    )
    MonitorApplication(config).run()


def main():
    app()
