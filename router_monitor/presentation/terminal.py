"""Terminal presentation with Rich tables and live rendering."""

from contextlib import contextmanager

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ..config import MonitorConfig


def _number(value, spec: str, suffix: str = "") -> str:
    return f"{value:{spec}}{suffix}" if value is not None else "—"


def _cache_hit(detail: dict) -> str:
    if "hit_ranks" in detail:
        return "/".join(_number(value, ".0f", "%") for value in detail["hit_ranks"])
    return _number(detail.get("hit_pct"), ".0f", "%")


def render_terminal(snapshot: dict, config: MonitorConfig) -> Group:
    panels = [
        Text(f"Router Monitor  {snapshot['ts']}", style="bold cyan"),
        Text(snapshot["router_url"], style="cyan"),
    ]
    if not snapshot["prom_ok"]:
        panels.append(Text("Router metrics unavailable", style="bold red"))
    else:
        active = snapshot["total_active"]
        rps = snapshot["rps"]
        traffic = f"  {_number(rps, '.1f', ' req/s')}" if rps is not None else ""
        panels.append(Text(f"Active: {active if active is not None else '—'}{traffic}", style="bold"))
        for model, workers in sorted(snapshot["by_model"].items()):
            table = Table(title=Text(f"{model} ({len(workers)} workers)"), title_justify="left")
            table.add_column("Worker", no_wrap=True)
            for heading in ("Run", "Wait", "KV%", "TPS", "TTFT", "Accept", "Hit"):
                table.add_column(heading, justify="right")
            for worker in workers:
                detail = snapshot["details"].get(worker, {})
                table.add_row(
                    Text(worker.removeprefix("http://").removeprefix("https://")),
                    _number(detail.get("running"), ".1f"),
                    _number(detail.get("waiting"), ".0f"),
                    _number(detail.get("kv_pct"), ".0f", "%"),
                    _number(detail.get("gen_tps"), ".0f"),
                    _number(detail.get("ttft_p50_ms"), ".0f", "ms"),
                    _number(detail.get("spec_accept_len"), ".2f"),
                    _cache_hit(detail),
                )
            panels.append(table)

        aged = sorted(
            ((label, count) for label, count in snapshot["age"].items() if count),
            key=lambda item: float(item[0].split("~")[0]),
        )
        if aged:
            table = Table(title="In-flight age", title_justify="left")
            table.add_column("Age")
            table.add_column("Requests", justify="right")
            for label, count in aged:
                table.add_row(Text(label), str(count))
            panels.append(table)

    web_url = f"  |  Web → http://0.0.0.0:{config.web_port}" if config.web_port else ""
    panels.append(Text(f"Refresh {config.interval}s  |  Ctrl+C to exit{web_url}", style="dim"))
    return Group(*panels)


@contextmanager
def terminal_updates(config: MonitorConfig):
    console = Console()
    if console.is_terminal:
        with Live(console=console, auto_refresh=False, redirect_stdout=False, redirect_stderr=False) as live:
            yield lambda snapshot: live.update(render_terminal(snapshot, config), refresh=True)
    else:
        yield lambda snapshot: console.print(render_terminal(snapshot, config))
