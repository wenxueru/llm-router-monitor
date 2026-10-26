"""SGLang Router entry point."""

from typing import Annotated

import typer


def main(
    port: Annotated[int, typer.Option(envvar="ROUTER_PORT", min=1, max=65535, help="Router API port.")] = 2730,
    policy: Annotated[str, typer.Option(envvar="ROUTER_POLICY", help="Routing policy.")] = "round_robin",
    dp_aware: Annotated[bool, typer.Option(envvar="ROUTER_DP_AWARE", help="Route to individual DP ranks.")] = True,
    prom_port: Annotated[
        int, typer.Option(envvar="ROUTER_PROMETHEUS_PORT", min=1, max=65535, help="Prometheus port.")
    ] = 29000,
    health_check_timeout_secs: Annotated[
        int,
        typer.Option(
            "--health-timeout", envvar="ROUTER_HEALTH_CHECK_TIMEOUT_SECS", min=1, help="Health probe timeout."
        ),
    ] = 30,
    queue_timeout_secs: Annotated[
        int, typer.Option("--queue-timeout", envvar="ROUTER_QUEUE_TIMEOUT_SECS", min=1, help="Queue timeout.")
    ] = 3600,
    request_timeout_secs: Annotated[
        int,
        typer.Option(
            "--request-timeout", envvar="ROUTER_REQUEST_TIMEOUT_SECS", min=1, help="Streaming request timeout."
        ),
    ] = 43200,
):
    """Start SGLang Router; workers register separately. Timeouts are in seconds."""
    from sglang_router.launch_router import launch_router
    from sglang_router.router_args import RouterArgs

    router_args = RouterArgs(
        host="0.0.0.0",
        port=port,
        policy=policy,
        dp_aware=dp_aware,
        prometheus_host="0.0.0.0",
        prometheus_port=prom_port,
        log_level="info",
        cache_threshold=0.3,
        balance_abs_threshold=8,
        balance_rel_threshold=1.5,
        eviction_interval_secs=60,
        max_tree_size=2147483648,
        health_check_interval_secs=15,
        health_check_endpoint="/health_generate",
        health_check_timeout_secs=health_check_timeout_secs,
        enable_igw=True,
        worker_startup_timeout_secs=30,
        queue_size=4096,
        queue_timeout_secs=queue_timeout_secs,
        request_timeout_secs=request_timeout_secs,
    )
    launch_router(router_args)


if __name__ == "__main__":
    typer.run(main)
