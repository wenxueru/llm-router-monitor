# Project conventions

## Scope and structure

- LLM Router monitoring with automatic detection and a SGLang Router launcher. Keep SSH, deployment, credentials, and personal configuration outside the repo.
- Use a flat `router_monitor/` package: CLI/lifecycle in `cli.py`, Web/terminal views in `dashboard.py`, and shared data/history in `state.py`. `scripts/` contains thin executable `.sh` entries using `PYTHON_BIN` (default `python3`).
- Keep Router discovery/dialects in `sources.py`, engine collection in `worker.py`, orchestration in `collectors.py`, and SGLang policies in `maintenance.py`. Use Typer, owned pooled HTTPX clients, and the official Prometheus parser.

## Measurements and policies

- Derive summaries once in `visualization.py`; history stores only Unix timestamps and per-model metrics.
- Unknown health, unsupported metrics, counter resets, and changed series remain missing. Proxy health never establishes engine-rank health. Preserve count/token weighting, full DP prefill coverage, and worst health/minimum success across TP ranks.
- Router credentials stay on configured Router/admin/metrics origins; worker credentials are separate.
- Unavailable cleanup defaults to 1800 seconds (`0` disables it): require all ranks unhealthy/idle and a failed connection probe. Reset timing on recovery, re-registration, or lost registry visibility. Keepalive and model-mismatch cleanup are independent opt-ins; maintenance never applies to manually added workers or other Routers.
- Keepalive uses 64-token outputs, requires observed idle Router/worker load, and backs off on any failed request.

## Interface

- One HTML embeds ECharts and its LICENSE/NOTICE. Preserve EN/ZH, system/light/dark themes, Settings, automatic updates, linked zoom, and legend selection. HTML changes need only a browser refresh.
- Group workers by model and show health reasons. Collapse only workers with neither metrics nor healthy ranks. Use one metric per trend chart and consistent model colors.
- Dashed links may bridge metric gaps up to 30 seconds, never collection outages or unavailable models.

## Verification

- Run unittest discovery and Ruff. Keep behavioral tests; consolidate fixtures and avoid assertions tied only to implementation structure.
- Ignore environments, generated files, caches, logs, and scratch output. Keep only source, documentation, and intentional assets in Git; `commit.tmp` is a local commit-message draft. Include launcher scripts, tests, and documentation in the source distribution, but ship only the runtime package and bundled HTML in wheels.
