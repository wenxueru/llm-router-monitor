"""Web routes and dashboard rendering for configured Router targets."""

from contextlib import asynccontextmanager

import httpx
from jinja2 import Environment, PackageLoader, select_autoescape

from .config import PrometheusHistoryConfig
from .history import PrometheusHistory, metrics_registry
from .state import RouterTarget


def create_dashboard(
    targets: dict[str, RouterTarget],
    *,
    history_config: PrometheusHistoryConfig | None = None,
):
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest

    @asynccontextmanager
    async def lifespan(app):
        backend = PrometheusHistory(history_config) if history_config else None
        app.state.history = backend
        try:
            yield
        finally:
            if backend:
                backend.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    @app.middleware("http")
    async def prevent_stale_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    default_router = next(iter(targets))
    templates = Environment(loader=PackageLoader("router_monitor"), autoescape=select_autoescape())

    def select_target(router: str | None) -> tuple[str, RouterTarget]:
        name = default_router if router is None else router
        try:
            return name, targets[name]
        except KeyError:
            raise HTTPException(404, "Router not found") from None

    @app.get("/", response_class=HTMLResponse)
    def index(router: str | None = None):
        name, target = select_target(router)
        config = target.config
        return templates.get_template("dashboard.html").render(
            routers=targets.keys(),
            selected_router=name,
            interval_ms=int(config.interval * 1000),
            collection_seconds=config.interval,
            history_window=config.history_window,
        )

    @app.get("/data")
    async def data(router: str | None = None):
        _, target = select_target(router)
        return JSONResponse(target.store.snapshot())

    metrics = metrics_registry(targets)

    @app.get("/metrics")
    def export_metrics():
        return Response(generate_latest(metrics), media_type=CONTENT_TYPE_LATEST)

    @app.get("/history")
    def history(n: int = 0, router: str | None = None):
        name, target = select_target(router)
        recent = target.store.history(n)
        if history_config:
            backend = app.state.history
            try:
                return JSONResponse(backend.history(name, target.config.interval, n, recent))
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                raise HTTPException(503, "Prometheus history unavailable") from None
        return JSONResponse(recent)

    return app
