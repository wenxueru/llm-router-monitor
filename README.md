<p align="center"><img src="https://raw.githubusercontent.com/wenxueru/llm-router-monitor/main/docs/images/logo.svg" alt="Router Monitor" width="72"></p>

# LLM Router Monitor

English | [简体中文](https://github.com/wenxueru/llm-router-monitor/blob/main/README.zh-CN.md)

Monitor LLM Routers and inference workers from one dashboard.

- Automatic Router detection and worker grouping by model.
- 15 per-model trend charts, rank details, and health reasons.
- English/Chinese, light/dark themes, and a self-contained HTML interface.

![Dashboard — anonymized data](https://raw.githubusercontent.com/wenxueru/llm-router-monitor/main/docs/images/dashboard.jpg)

## Install and run

Python 3.10+:

```bash
python -m pip install llm-router-monitor
rmon http://127.0.0.1:4000
```

Open [localhost:8899](http://localhost:8899/). `llm-router-monitor` is equivalent to `rmon`; all options are listed in `rmon --help`.

| Option | Purpose |
| --- | --- |
| `--metrics-url URL` | Prometheus endpoint |
| `--admin-url URL` | Separate admin endpoint |
| `--worker URL` | Add a worker; repeat for multiple endpoints |
| `--router-type TYPE` | Override automatic detection |
| `--web-port PORT` | Dashboard port; default `8899`, `0` for terminal only |
| `--interval SECONDS` | Collection interval; default `2` |
| `--history-window MINUTES` | Initial chart window on each page load; default `720` (12 hours) |

The dashboard binds to `0.0.0.0`. Set `ROUTER_API_KEY` for Router authentication and `WORKER_API_KEY` for worker authentication. The positional URL takes precedence over `--router-url` and `ROUTER_URL`.

## Supported Routers

Enable the Router's metrics endpoint or exporter before connecting.

| Router | Discovery and metrics |
| --- | --- |
| SGLang | `/workers`; rank health, load, and inference metrics |
| vLLM Production Stack | Worker discovery from Router metric labels |
| Standalone vLLM | Direct engine metrics |
| [LiteLLM](https://docs.litellm.ai/docs/proxy/prometheus) | `/model/info` and deployment metrics; enable the Prometheus callback |
| NVIDIA Dynamo | Model deployments and frontend metrics |
| Envoy | Admin `/clusters?format=json` and upstream metrics |
| NGINX Plus | Upstreams exposed by the Prometheus exporter |
| Traefik | Upstreams exposed by enabled health checks |
| NGINX OSS, HAProxy, Kong, Ray Serve | Proxy metrics; configure engines with `--worker` |
| Other proxies | Configure SGLang/vLLM engines with `--worker` |

Envoy and bare NGINX Plus addresses are discovered as HTTP endpoints. Use `--worker` for HTTPS or custom paths.

Envoy with a separate admin listener:

```bash
rmon http://127.0.0.1:8080 \
  --admin-url http://127.0.0.1:9901 \
  --metrics-url http://127.0.0.1:9901/stats/prometheus
```

Exporter with explicit workers:

```bash
rmon http://127.0.0.1:8080 \
  --metrics-url http://127.0.0.1:9113/metrics \
  --worker http://worker-a:8000 --worker http://worker-b:8000
```

## Reading the dashboard

- Each chart shows models with observations for that metric in the selected time window. Zero is a valid observation; a missing value is not. “Unknown model” means model discovery failed, and remains visible when metrics are available.
- Trends use thin lines; zoom in to inspect dense observations. Zoom stays synchronized across charts. Use the legend to hide or show a model.
- Missing values remain missing. TTFT/ITL connect valid observations; other charts bridge gaps up to 30 seconds with dashed lines. Collection outages and unavailable models stay disconnected.
- Axes adapt to the data: nonnegative values, percentages within 0–100%, and integer count ticks.

### Metrics and history

- TTFT/ITL use count-weighted counter deltas; cache hit uses a 60-second token-weighted window.
- Each page load uses the server's configured window: 12 hours by default, starting no earlier than available data. Window adjustments apply only to the current page.
- History accumulates in memory and clears when the monitor restarts.
- Refresh interval, language, and theme are remembered. Window and refresh inputs accept decimals, in minutes and seconds respectively.

![Per-model trends — anonymized data](https://raw.githubusercontent.com/wenxueru/llm-router-monitor/main/docs/images/trends.jpg)

| Endpoint | Response |
| --- | --- |
| `/data` | Router metadata, summaries, worker rows, and per-model metrics |
| `/history?n=100` | Samples with `at` (Unix seconds) and `by_model` |

## SGLang maintenance

These policies apply to SGLang-managed registrations only.

| Option | Default | Action |
| --- | --- | --- |
| `--unregister-unavailable-after SECONDS` | `1800` | Unregister a host after all ranks stay unhealthy and idle, followed by a failed connection probe; `0` disables cleanup |
| `--keepalive` | Off | Send short inference requests to observed idle workers |
| `--auto-unregister` | Off | Remove registrations whose model differs from the serving model |

Recovery, re-registration, or lost registry visibility resets the cleanup timer. Keepalive backs off after failures. Worker processes continue running after deregistration.

To launch SGLang Router, install a compatible `sglang_router` runtime exposing `RouterArgs`, then run `bash scripts/router.sh`. Monitor it with `rmon --host 127.0.0.1` (API `2730`, metrics `29000`).

## Development

Keep runtime modules flat in `router_monitor/`:

| Responsibility | Modules |
| --- | --- |
| Entry and lifecycle | `cli.py`, `config.py`, `router.py` |
| Collection and metrics | `collectors.py`, `sources.py`, `worker.py`, `metrics.py`, `http.py` |
| State and maintenance | `state.py`, `maintenance.py` |
| Presentation | `visualization.py`, `dashboard.py`, `templates/dashboard.html` |

Shell entries live in `scripts/`; behavioral tests live in `tests/`. Frontend tests require Node.js.

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests
ruff check .
ruff format --check router_monitor tests
python -m build
```

Build outputs are written to `dist/`. Install a wheel with `python -m pip install dist/llm_router_monitor-0.0.1-py3-none-any.whl`.
