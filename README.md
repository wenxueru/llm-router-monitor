<p align="center"><img src="https://raw.githubusercontent.com/wenxueru/llm-router-monitor/main/docs/images/logo.svg" alt="Router Monitor" width="72"></p>

# LLM Router Monitor

English | [简体中文](https://github.com/wenxueru/llm-router-monitor/blob/main/README.zh-CN.md)

Monitor LLM Routers and inference workers from one dashboard.

- Automatic Router detection, named Router switching, and worker grouping by model.
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
| `--router-config FILE` | JSON configuration of named Routers |
| `--metrics-url URL` | Prometheus or NGINX `stub_status` endpoint |
| `--admin-url URL` | Separate admin endpoint |
| `--worker URL` | Add a worker; repeat for multiple endpoints |
| `--router-type TYPE` | Override automatic detection |
| `--web-port PORT` | Dashboard port; default `8899`, `0` for terminal only |
| `--interval SECONDS` | Collection interval; default `2` |
| `--history-window MINUTES` | Initial chart window on each page load; default `120` (2 hours) |

The dashboard binds to `0.0.0.0`. Set `ROUTER_API_KEY` for Router authentication and `WORKER_API_KEY` for worker authentication. The positional URL takes precedence over `--router-url` and `ROUTER_URL`.

## Multiple Routers

Save a JSON object keyed by display name, for example `routers.local.json`:

```json
{
  "inference": {
    "endpoint": "http://router.example:2730",
    "router_type": "sglang",
    "interval": 2
  },
  "gateway": {
    "endpoint": "http://gateway.example:8080",
    "router_type": "nginx",
    "metrics_endpoint": "http://gateway.example:8080/stub_status",
    "worker_urls": ["http://engine.example:8000"]
  }
}
```

```bash
rmon --router-config routers.local.json --web-port 8899 --history-window 120
```

- Each target requires an HTTP(S) `endpoint` without embedded credentials. Optional fields: `router_type`, `worker_urls`, `metrics_endpoint`, `admin_url`, `api_key`, `worker_api_key`, and `interval`.
- JSON supplies all target settings; single-Router CLI and environment settings do not carry over. `--web-port`, `--history-window`, and `--history-prometheus-*` settings are shared; the port must be nonzero.
- Switch targets in the dashboard; each has independent collection and history. `/`, `/data`, and `/history` accept `?router=NAME` and default to the first target.
- Maintenance defaults to off in JSON mode. Enable `keepalive`, `auto_unregister`, or a positive `unregister_unavailable_after` per target.
- Keep local configuration outside Git. Use `api_key` for Router credentials and `worker_api_key` for worker credentials.

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
| NGINX OSS | Native `stub_status` request totals or exporter metrics; configure engines with `--worker` |
| HAProxy, Kong, Ray Serve | Proxy metrics; configure engines with `--worker` |
| Other proxies | Configure SGLang/vLLM engines with `--worker` |

For native NGINX `stub_status`, set `metrics_endpoint` in JSON or pass `--metrics-url` in single-Router mode. It supplies request-rate counters, not inference metrics.

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
- If a worker model lookup fails, the dashboard uses the single known model name shared by its managed Router registrations. Missing or conflicting names leave the worker under “Unknown model”. Only a successful worker lookup can establish a model mismatch.
- Each page load uses the server's configured window: 2 hours by default, starting no earlier than available data. Window adjustments apply only to the current page.
- History is in memory by default. Set up [Prometheus history](#prometheus-history) to preserve scraped samples across restarts.
- Refresh interval, language, and theme are remembered. Window and refresh inputs accept decimals, in minutes and seconds respectively.

![Per-model trends — anonymized data](https://raw.githubusercontent.com/wenxueru/llm-router-monitor/main/docs/images/trends.jpg)

| Endpoint | Response |
| --- | --- |
| `/data` | Router metadata, summaries, worker rows, and per-model metrics |
| `/history?n=100` | Samples with `at` (Unix seconds) and `by_model` |
| `/metrics` | Timestamped model metrics for all Router targets |

## Prometheus history

Monitor exposes timestamped model metrics at `/metrics`. Add one scrape job for this monitor in Prometheus:

```yaml
scrape_configs:
  - job_name: router-monitor
    scrape_interval: 2s
    honor_timestamps: true
    static_configs:
      - targets: ["monitor.example:8899"]
```

Use a scrape interval no longer than the shortest target collection interval, with `honor_timestamps` enabled. Assign each monitor a separate job name.

```bash
rmon http://router.example:2730 \
  --history-prometheus-url http://prometheus.example:9090
```

| Option | Default | Purpose |
| --- | --- | --- |
| `--history-prometheus-url` | Unset | Prometheus base URL; enables stored history |
| `--history-prometheus-job` | `router-monitor` | Scrape job for this monitor |
| `--history-prometheus-lookback-hours` | `24` | Maximum history query window in hours |

The initial page window remains 120 minutes. These options also apply with `--router-config` and require a nonzero web port.

- Prometheus retention determines how long scraped samples remain available. The dashboard merges them with recent local observations to include data collected since the latest scrape.
- Grafana or PromQL can query the same series, for example `router_monitor_model_tps{job="router-monitor"}`. Use the `router` and `model` labels for filtering.
- Without a Prometheus URL, history stays in memory until restart. When configured, a failed history query returns an error; live data continues through `/data`.

## SGLang maintenance

These policies apply to SGLang-managed registrations only.

| Option | Default | Action |
| --- | --- | --- |
| `--unregister-unavailable-after SECONDS` | `1800` | Unregister a host after all ranks stay unhealthy and idle, followed by a failed availability probe; `0` disables cleanup |
| `--keepalive` | Off | Send varied synthetic tasks to observed idle workers; back off after failures |
| `--auto-unregister` | Off | Remove registrations whose model differs from the serving model |

- Availability cleanup requires a connection error, a connection/read timeout, or a remote protocol error from the probe. HTTP status errors and other probe failures retain the worker.
- Recovery, re-registration, or lost registry visibility resets the grace period.
- Deregistration removes Router records, not worker processes. An accepted removal request does not confirm deletion.

### Keepalive workload

Keepalive randomly chooses summarization, troubleshooting, status-writing, or comparison tasks using synthetic observations. Task content and sampling temperature vary between requests.

| Option | Default | Per-request or per-round behavior |
| --- | --- | --- |
| `--keepalive-prompt-words` | `512` | Synthetic context length varies from one eighth to the configured word limit; task instructions add a small overhead |
| `--keepalive-max-tokens` | `64` | Generation budget varies from one quarter to the configured limit; actual output may be shorter |
| `--keepalive-concurrency` | `8` | Each idle worker receives a random 1 to this many requests per round; at most 32 requests run concurrently per Router |

Length and token lower bounds round down, with a minimum of 1. Only workers with observed idle load and an identified model receive keepalive requests.

To launch SGLang Router, install a compatible `sglang_router` runtime exposing `RouterArgs`, then run `bash scripts/router.sh`. Monitor it with `rmon --host 127.0.0.1` (API `2730`, metrics `29000`).

## Development

See [Architecture](docs/architecture.md) for module boundaries, data flow, and Router isolation.

Shell entries live in `scripts/`; behavioral tests live in `tests/`. Frontend tests require Node.js.

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests
ruff check .
ruff format --check router_monitor tests
python -m build
```

Build outputs are written to `dist/`. Install a wheel with `python -m pip install dist/llm_router_monitor-0.0.1-py3-none-any.whl`.
