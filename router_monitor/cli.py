"""Monitor command-line options and configuration assembly."""

from pathlib import Path
from typing import Annotated

import typer

from .app import MonitorApplication
from .config import MonitorConfig, PrometheusHistoryConfig, RouterType, load_router_configs, validate_http_endpoint

app = typer.Typer(
    help="Monitor LLM Routers, worker health, and inference metrics.",
    pretty_exceptions_show_locals=False,
)


@app.command()
def monitor(
    url: Annotated[
        str | None,
        typer.Argument(help="Router base URL; overrides --router-url and ROUTER_URL."),
    ] = None,
    host: Annotated[str, typer.Option(envvar="MASTER_HOST", help="Router host.", rich_help_panel="Connection")] = "127.0.0.1",
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
        typer.Option(envvar="ROUTER_ADMIN_URL", help="Separate admin base URL (e.g. Envoy).", rich_help_panel="Connection"),
    ] = "",
    router_type: Annotated[
        RouterType, typer.Option(help="Router dialect; auto detects exposed interfaces.", rich_help_panel="Connection")
    ] = RouterType.auto,
    worker: Annotated[
        list[str] | None,
        typer.Option("--worker", help="Worker base URL; repeat for proxies without discovery.", rich_help_panel="Connection"),
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
        typer.Option(envvar="WORKER_API_KEY", help="Separate Bearer token for worker endpoints.", rich_help_panel="Connection"),
    ] = "",
    interval: Annotated[
        float,
        typer.Option(envvar="INTERVAL", min=0.1, help="Collection interval in seconds.", rich_help_panel="Dashboard"),
    ] = 2,
    history_window: Annotated[
        float, typer.Option(min=1, help="Initial history window in minutes.", rich_help_panel="Dashboard")
    ] = MonitorConfig.history_window,
    web_port: Annotated[
        int, typer.Option(min=0, max=65535, help="Dashboard port; 0 for terminal only.", rich_help_panel="Dashboard")
    ] = 8899,
    keepalive: Annotated[
        bool,
        typer.Option(help="Send inference requests to idle SGLang-registered workers.", rich_help_panel="Worker maintenance"),
    ] = MonitorConfig.keepalive,
    keepalive_unconditional: Annotated[
        bool,
        typer.Option(
            help="Maintain concurrent SGLang keepalive requests regardless of worker load or inference traffic.",
            rich_help_panel="Worker maintenance",
        ),
    ] = MonitorConfig.keepalive_unconditional,
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
        typer.Option(envvar="KEEPALIVE_MODEL", help="Limit keepalive to one model.", rich_help_panel="Keepalive tuning"),
    ] = "",
    keepalive_interval: Annotated[
        float,
        typer.Option(
            envvar="KEEPALIVE_INTERVAL",
            min=0.1,
            help="Seconds between adaptive rounds or before retrying a failed unconditional request.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = MonitorConfig.keepalive_interval,
    keepalive_max_interval: Annotated[
        float,
        typer.Option(
            envvar="KEEPALIVE_MAX_INTERVAL",
            min=0.1,
            help="Maximum retry backoff in seconds in adaptive mode.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = MonitorConfig.keepalive_max_interval,
    keepalive_traffic_poll_interval: Annotated[
        float,
        typer.Option(
            envvar="KEEPALIVE_TRAFFIC_POLL_INTERVAL",
            min=0.1,
            help="Seconds between polls for new Router inference requests.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = MonitorConfig.keepalive_traffic_poll_interval,
    keepalive_min_tokens: Annotated[
        int,
        typer.Option(
            envvar="KEEPALIVE_MIN_TOKENS",
            min=0,
            help="Minimum generated tokens per keepalive request. Zero uses the worker default.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = MonitorConfig.keepalive_min_tokens,
    keepalive_max_tokens: Annotated[
        int,
        typer.Option(
            envvar="KEEPALIVE_MAX_TOKENS",
            min=1,
            help="Maximum generated tokens per keepalive request.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = MonitorConfig.keepalive_max_tokens,
    keepalive_concurrency: Annotated[
        int,
        typer.Option(
            envvar="KEEPALIVE_CONCURRENCY",
            min=1,
            help="Maximum concurrent keepalive requests per worker.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = MonitorConfig.keepalive_concurrency,
    _legacy_keepalive_total_concurrency: Annotated[
        int | None, typer.Option("--keepalive-total-concurrency", hidden=True)
    ] = None,
    keepalive_prompt_words: Annotated[
        int,
        typer.Option(
            envvar="KEEPALIVE_PROMPT_WORDS",
            min=1,
            help="Maximum synthetic context length in words.",
            rich_help_panel="Keepalive tuning",
        ),
    ] = MonitorConfig.keepalive_prompt_words,
    router_config: Annotated[Path | None, typer.Option(help="JSON file of named Router configurations.")] = None,
    history_prometheus_url: Annotated[str, typer.Option(help="Prometheus base URL for persistent dashboard history.")] = "",
    history_prometheus_job: Annotated[str, typer.Option(help="Prometheus scrape job for this monitor.")] = "router-monitor",
    history_prometheus_lookback_hours: Annotated[
        int, typer.Option(min=1, help="Maximum Prometheus history query window in hours.")
    ] = 24,
):
    """Start the dashboard. Only SGLang registrations support automatic cleanup."""
    history = None
    if history_prometheus_url:
        try:
            history_url = validate_http_endpoint(history_prometheus_url)
        except ValueError as error:
            raise typer.BadParameter(str(error), param_hint="--history-prometheus-url") from None
        if not web_port:
            raise typer.BadParameter("Prometheus history requires a dashboard port.", param_hint="--web-port")
        history = PrometheusHistoryConfig(history_url, history_prometheus_job, history_prometheus_lookback_hours)

    if router_config is not None:
        if not web_port:
            raise typer.BadParameter("Named Router configurations require a dashboard port.", param_hint="--web-port")
        try:
            routers = load_router_configs(router_config, history_window=history_window, web_port=web_port)
        except (OSError, ValueError) as error:
            raise typer.BadParameter(str(error), param_hint="--router-config") from error
    else:
        try:
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
                keepalive_unconditional=keepalive_unconditional,
                auto_unregister=auto_unregister,
                unregister_unavailable_after=unregister_unavailable_after,
                keepalive_model=keepalive_model,
                keepalive_interval=keepalive_interval,
                keepalive_max_interval=keepalive_max_interval,
                keepalive_traffic_poll_interval=keepalive_traffic_poll_interval,
                keepalive_min_tokens=keepalive_min_tokens,
                keepalive_max_tokens=keepalive_max_tokens,
                keepalive_concurrency=keepalive_concurrency,
                keepalive_prompt_words=keepalive_prompt_words,
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        routers = {"": config}

    MonitorApplication(routers, history=history).run()


def main():
    app()
