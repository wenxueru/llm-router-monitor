"""Immutable monitor settings."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import AfterValidator, ConfigDict, Field, TypeAdapter
from typing_extensions import Required, TypedDict


class RouterType(str, Enum):
    auto = "auto"
    sglang = "sglang"
    vllm = "vllm"
    litellm = "litellm"
    envoy = "envoy"
    nginx = "nginx"
    haproxy = "haproxy"
    traefik = "traefik"
    kong = "kong"
    ray = "ray"
    dynamo = "dynamo"
    generic = "generic"


@dataclass(frozen=True)
class MonitorConfig:
    host: str
    port: int = 2730
    prom_port: int = 29000
    endpoint: str = ""
    metrics_endpoint: str = ""
    admin_url: str = ""
    router_type: RouterType = RouterType.auto
    worker_urls: tuple[str, ...] = ()
    api_key: str = field(default="", repr=False)
    worker_api_key: str = field(default="", repr=False)
    interval: float = 2.0
    history_window: float = 120.0
    web_port: int = 8899
    keepalive: bool = False
    auto_unregister: bool = False
    unregister_unavailable_after: float = 1800
    keepalive_model: str = ""
    keepalive_interval: float = 5.0
    keepalive_max_interval: float = 60.0
    keepalive_max_tokens: int = 64
    keepalive_concurrency: int = 8
    keepalive_prompt_words: int = 512

    @property
    def router_url(self) -> str:
        return self.endpoint.rstrip("/") if self.endpoint else f"http://{self.host}:{self.port}"

    @property
    def prom_url(self) -> str:
        if self.metrics_endpoint:
            return self.metrics_endpoint
        return f"{self.router_url}/metrics" if self.endpoint else f"http://{self.host}:{self.prom_port}/metrics"

    @property
    def separate_prom_url(self) -> str:
        return str(httpx.URL(self.router_url).copy_with(port=self.prom_port, path="/metrics", query=None))

    @property
    def admin_endpoint(self) -> str:
        return self.admin_url.rstrip("/") if self.admin_url else self.router_url


@dataclass(frozen=True)
class PrometheusHistoryConfig:
    url: str
    job: str = "router-monitor"
    lookback_hours: int = 24


def validate_http_endpoint(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("Expected an HTTP(S) URL without credentials.") from None
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ValueError("Expected an HTTP(S) URL without credentials.")
    return value


def _router_name(value: str) -> str:
    if not value.strip():
        raise ValueError("Router name must not be blank.")
    return value


HttpEndpoint = Annotated[str, AfterValidator(validate_http_endpoint)]
RouterName = Annotated[str, AfterValidator(_router_name)]
Port = Annotated[int, Field(ge=1, le=65535)]
Interval = Annotated[float, Field(ge=0.1)]
PositiveInt = Annotated[int, Field(ge=1)]


class _RouterOptions(TypedDict, total=False):
    endpoint: Required[HttpEndpoint]
    port: Port
    prom_port: Port
    metrics_endpoint: HttpEndpoint | Literal[""]
    admin_url: HttpEndpoint | Literal[""]
    router_type: RouterType
    worker_urls: list[HttpEndpoint]
    api_key: str
    worker_api_key: str
    interval: Interval
    keepalive: bool
    auto_unregister: bool
    unregister_unavailable_after: Annotated[float, Field(ge=0)]
    keepalive_model: str
    keepalive_interval: Interval
    keepalive_max_interval: Interval
    keepalive_max_tokens: PositiveInt
    keepalive_concurrency: PositiveInt
    keepalive_prompt_words: PositiveInt


def load_router_configs(path: Path, *, history_window: float, web_port: int) -> dict[str, MonitorConfig]:
    schema = TypeAdapter(
        Annotated[dict[RouterName, _RouterOptions], Field(min_length=1)],
        config=ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True),
    )
    entries = schema.validate_json(path.read_bytes())
    configs = {}
    for name, settings in entries.items():
        worker_urls = tuple(url.rstrip("/") for url in settings.pop("worker_urls", ()))
        settings.setdefault("unregister_unavailable_after", 0)
        configs[name] = MonitorConfig(
            host=httpx.URL(settings["endpoint"]).host,
            history_window=history_window,
            web_port=web_port,
            worker_urls=worker_urls,
            **settings,
        )
    return configs
