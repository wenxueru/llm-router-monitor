# Project conventions

## Structure

- Keep a flat `router_monitor/` package, thin `.sh` entries in `scripts/`, and one HTML dashboard.
- Put Router adapters in `sources.py`, engine collection in `worker.py`, SGLang policies in `maintenance.py`, and aggregation in `visualization.py`.
- Use classes for owned state and resource lifecycles; use functions for stateless transformation and rendering. Prefer composition over inheritance.
- Exclude credentials, personal/SSH/deployment settings, caches, and build artifacts from Git.

## Behavior

- Preserve missing values, counter-reset handling, count/token weighting, complete DP coverage, and worst-rank health. Proxy health does not establish rank health.
- Scope Router credentials to configured origins; use separate worker credentials.
- Deregister managed SGLang hosts only after all ranks stay unhealthy and idle for the grace period and a connection probe fails. HTTP errors and read timeouts retain the worker. Recovery, re-registration, and registry loss reset the timer; accepted removal requests do not prove deletion.
- Keepalive and model-mismatch cleanup are opt-in. Keepalive requires observed idle load and backs off on failure.
- Store in-memory history as Unix timestamps and per-model metrics; compute summaries once.

## Dashboard

- Retain ECharts LICENSE/NOTICE, EN/ZH, themes, synchronized zoom, and legend selection.
- Show only model series with valid observations for the chart's metric and time window; retain zero values and observed unknown models.
- Keep thin trend lines visible at every zoom level.
- Bridge metric gaps up to 30 seconds; TTFT/ITL may connect more distant observations. Never bridge collection outages or unavailable models.
- Keep axes nonnegative, percentages within 0–100%, and count ticks integral.
- Each page load uses the server-configured window (12 hours by default), ignoring saved browser window values. Window changes apply only to the current page; its range starts no earlier than available data.

## Verification

Run unittest discovery and Ruff. Prefer behavior and boundary tests over implementation assertions. Preserve HTML in wheels and scripts/tests in source archives.
