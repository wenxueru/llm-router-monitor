"""Web dashboard and terminal presentation of collected monitor data."""

from importlib.resources import files

from .config import MonitorConfig
from .state import SnapshotStore


class WebDashboard:
    def __init__(self, config: MonitorConfig, store: SnapshotStore):
        self.config = config
        self.store = store

    def create_app(self):
        from fastapi import FastAPI
        from fastapi.middleware.gzip import GZipMiddleware
        from fastapi.responses import HTMLResponse, JSONResponse

        app = FastAPI()
        app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

        @app.middleware("http")
        async def prevent_stale_responses(request, call_next):
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        history_window = self.config.history_window
        history_label = f"{history_window:g} min"
        template = files("router_monitor") / "templates/dashboard.html"
        replacements = {
            "INTERVAL_MS": str(int(self.config.interval * 1000)),
            "COLLECTION_S": str(self.config.interval),
            "WIN_DEFAULT_MIN": str(int(history_window)),
            "HISTORY_LABEL": history_label,
        }

        @app.get("/", response_class=HTMLResponse)
        def index():
            html = template.read_text(encoding="utf-8")
            for key, value in replacements.items():
                html = html.replace(key, value)
            return html

        @app.get("/data")
        async def data():
            return JSONResponse(self.store.snapshot())

        @app.get("/history")
        async def history(n: int = 0):
            return JSONResponse(self.store.history(n))

        return app

    def run(self) -> None:
        import uvicorn

        app = self.create_app()
        config = uvicorn.Config(app, host="0.0.0.0", port=self.config.web_port, log_level="error")
        print(f"[Monitor] Web UI → http://0.0.0.0:{self.config.web_port}")
        uvicorn.Server(config).run()


def _cache_hit(detail: dict) -> str:
    """Show per-rank hit rates for DP attention, otherwise the aggregate rate."""
    if "hit_ranks" in detail:
        return "/".join(f"{value:.0f}%" if value is not None else "-" for value in detail["hit_ranks"])
    return f"{detail['hit_pct']:.0f}%" if "hit_pct" in detail else "-"


def render_terminal(snapshot: dict, config: MonitorConfig) -> None:
    width = 88
    lines = [
        f"\033[1;36m{'═' * width}\033[0m",
        f"\033[1;36m  Router Monitor  {snapshot['ts']}\033[0m",
        f"\033[1;36m  {snapshot['router_url']}\033[0m",
        f"\033[1;36m{'═' * width}\033[0m",
    ]
    if not snapshot["prom_ok"]:
        lines.append("\n\033[1;31m  Prometheus unreachable\033[0m")
    else:
        rps = snapshot["rps"]
        traffic = f"  \033[2m{rps:.1f} req/s\033[0m" if rps is not None and rps > 0.05 else ""
        active = snapshot["total_active"] if snapshot["total_active"] is not None else "-"
        lines.append(f"\n\033[1mActive: \033[1;33m{active}\033[0m{traffic}")

        for model, workers in sorted(snapshot["by_model"].items()):
            lines.extend(
                (
                    f"\n\x1b[1;33m  {model}  \x1b[2m({len(workers)} workers)\x1b[0m",
                    f"  \x1b[1m{'Worker':<22} {'Run':>5} {'Wait':>4}  {'KV%':>4}  {'TPS':>5}  {'TTFT':>7}  {'Accept':>6}  {'Hit':>4}\x1b[0m",
                    "  " + "─" * width,
                )
            )
            for worker in workers:
                detail = snapshot["details"].get(worker, {})
                running = f"{detail['running']:>5.1f}" if "running" in detail else "    -"
                waiting = f"{detail['waiting']:>4.0f}" if "waiting" in detail else "   -"
                kv_pct = f"{detail['kv_pct']:>3.0f}%" if "kv_pct" in detail else "   -"
                hit = _cache_hit(detail)
                tps = f"{detail['gen_tps']:>5.0f}" if "gen_tps" in detail else "     -"
                ttft = f"{detail['ttft_p50_ms']:>5.0f}ms" if "ttft_p50_ms" in detail else "      -"
                accept_len = f"{detail['spec_accept_len']:>6.2f}" if "spec_accept_len" in detail else "     -"
                worker_name = worker.replace("http://", "")
                lines.append(
                    f"  {worker_name:<22}  {running} {waiting}  {kv_pct}  {tps}  {ttft}  {accept_len}  {hit:>4}"
                )

        aged = sorted(
            ((label, count) for label, count in snapshot["age"].items() if count),
            key=lambda item: float(item[0].split("~")[0]),
        )
        if aged:
            lines.append("\n\033[1mIn-flight age:\033[0m")
            for label, count in aged:
                lines.append(f"  {label:<14} {count:>4} req")

    web_url = f"  |  Web → http://0.0.0.0:{config.web_port}" if config.web_port else ""
    lines.append(f"\n\033[2mRefresh {config.interval}s  |  Ctrl+C to exit{web_url}\033[0m")
    print("\033[H\033[2J", end="")
    print("\n".join(lines))
