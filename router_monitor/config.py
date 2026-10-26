"""Immutable monitor settings."""

from dataclasses import dataclass, field
from enum import Enum

import httpx


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
    history_window: float = 5.0
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
