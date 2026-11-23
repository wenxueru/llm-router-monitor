"""Web routes and dashboard rendering for configured Router targets."""

from contextlib import asynccontextmanager
from ipaddress import ip_address

import httpx
from fastapi import Request
from jinja2 import Environment, PackageLoader, select_autoescape

from ..config import KeepaliveSettings, PrometheusHistoryConfig, RouterType
from ..target import RouterTarget
from .prometheus import PrometheusHistory, metrics_registry


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _can_edit(request: Request) -> bool:
    origin = request.headers.get("origin")
    return (
        request.client is not None
        and _loopback(request.client.host)
        and _loopback(request.url.hostname)
        and (origin is None or origin == f"{request.url.scheme}://{request.url.netloc}")
    )


def _keepalive_supported(target: RouterTarget, snapshot: dict) -> bool:
    return (
        target.config.router_type in (RouterType.auto, RouterType.sglang) and snapshot.get("router_type", "sglang") == "sglang"
    )


def _keepalive_view(settings: KeepaliveSettings, snapshot: dict, *, supported: bool, local: bool) -> dict:
    return {
        "settings": settings,
        "models": [model for model in snapshot.get("by_model", {}) if model != "unknown"],
        "supported": supported,
        "editable": local and (supported or settings.mode != "off"),
    }


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

    @app.get("/settings/keepalive")
    def read_keepalive(request: Request, router: str | None = None):
        _, target = select_target(router)
        snapshot, _ = target.store.observation()
        settings = target.keepalive.settings()
        return _keepalive_view(settings, snapshot, supported=_keepalive_supported(target, snapshot), local=_can_edit(request))

    @app.put("/settings/keepalive")
    def update_keepalive(request: Request, settings: KeepaliveSettings, router: str | None = None):
        _, target = select_target(router)
        snapshot, _ = target.store.observation()
        supported = _keepalive_supported(target, snapshot)
        if not _can_edit(request) or (not supported and settings.mode != "off"):
            raise HTTPException(403, "Keepalive can be edited from a local SGLang dashboard.")
        try:
            target.keepalive.update(settings)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return _keepalive_view(settings, snapshot, supported=supported, local=True)

    metrics = metrics_registry({name: target.store for name, target in targets.items()})

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
