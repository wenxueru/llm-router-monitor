"""Exercise the complete bundled dashboard through its controls and rendered views."""

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from importlib.resources import files


class DashboardParser(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.scripts = []
        self.script = None
        self.script_id = None
        self.select = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element = {"attrs": attrs, "options": []}
        self.elements.append(element)
        if tag == "select":
            self.select = element
        elif tag == "option" and self.select is not None:
            self.select["options"].append(attrs.get("value", ""))
        elif tag == "script":
            self.script = ""
            self.script_id = attrs.get("id")

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None
        elif tag == "script":
            self.scripts.append({"id": self.script_id, "code": self.script})
            self.script = None

    def handle_data(self, data):
        if self.script is not None:
            self.script += data


HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map();
class Element {
  constructor(attrs = {}, options = []) {
    this.attrs = attrs;
    this.dataset = Object.fromEntries(Object.entries(attrs)
      .filter(([key]) => key.startsWith('data-'))
      .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()), value]));
    this.options = options.map(value => ({value}));
    this.value = options[0] ?? '';
    this.hidden = 'hidden' in attrs;
    this.listeners = {};
    this.children = [];
    this.textContent = '';
    this.html = '';
    if (attrs.id) elements.set(attrs.id, this);
  }
  set innerHTML(html) {
    this.html = html;
    this.children = [];
    for (const tag of html.matchAll(/<[^/!][^>]*>/g)) {
      const attrs = Object.fromEntries([...tag[0].matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
      if (attrs.id || attrs['data-worker']) this.children.push(new Element(attrs));
    }
  }
  get innerHTML() { return this.html; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  trigger(event) { return (this.listeners[event] ?? this['on' + event])?.({preventDefault(){}}); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  removeAttribute(key) { delete this.attrs[key]; }
  checkValidity() { const n = Number(this.value); return this.value !== '' && Number.isFinite(n) && n >= Number(this.attrs.min ?? '-Infinity') && n <= Number(this.attrs.max ?? 'Infinity'); }
  replaceChildren(...options) { this.options = options; this.value = options[0]?.value ?? ''; }
}
const staticElements = input.elements.map(item => new Element(item.attrs, item.options));
const allElements = () => [...staticElements, ...staticElements.flatMap(element => element.children)];
const root = {dataset: {}};
const document = {
  documentElement: root,
  addEventListener() {},
  getElementById: id => elements.get(id) ?? null,
  querySelectorAll: selector => allElements().filter(element => selector.slice(1, -1) in element.attrs),
};
const stored = input.stored;
const media = {matches: input.dark, addEventListener(event, handler) { this.handler = handler; }};
const fixture = {
  snapshot: {
    ts: '2026-10-25 12:00:00', router_url: 'http://router:2730', age_seconds: 0,
    prom_ok: true, registry_ok: true,
    summary: {workers: 3, healthy_workers: 1, unhealthy_workers: 2, healthy_ranks: 16, ranks: 24,
      metrics_workers: 2, tps: 1500, running: 3, waiting: 0, hit_pct: 30, ttft_mean_ms: 5,
      kv_pct: 25, swa_pct: 10, itl_ms: 1, prefill_tokens: 0, retract_rate: 0, abort_rate: 0,
      health_missing_workers: 1, batch_hit_min_pct: 10, coverage: {running: 2, gen_tps: 2},
      balance: {minimum: 1, maximum: 2, max_mean: 1.3}},
    by_model_stats: {
      a: {workers: 1, tps: 500, running: 1, waiting: 0, ttft_mean_ms: 4, itl_ms: 0.8,
        prefill_tokens: 100, retract_rate: 0, abort_rate: 0, kv_pct: 25, swa_pct: 10,
        unhealthy_workers: 0, health_missing_workers: 0, success_pct: 100, hit_pct: 20, batch_hit_min_pct: 10},
      b: {workers: 2, tps: 1000, running: 2, waiting: 3, ttft_mean_ms: 8, itl_ms: 2,
        prefill_tokens: 300, retract_rate: 0.5, abort_rate: 1, kv_pct: 70, swa_pct: 50,
        unhealthy_workers: 1, health_missing_workers: 1, success_pct: 80, hit_pct: 80, batch_hit_min_pct: 20},
    },
    worker_rows: [
      {url: 'http://healthy:4563', model: 'a', status: 'healthy', metrics_ok: true, healthy_ranks: 8, ranks: 8},
      {url: 'http://warning:4563', model: 'b', status: 'unhealthy', metrics_ok: true, healthy_ranks: 8, ranks: 8,
        success_pct: 70, dp_metrics: [{rank: '0', gen_tps: 1000}], counter_window: {seconds: 2}},
      {url: 'http://unavailable:4563', model: 'b', status: 'unhealthy', metrics_ok: false, healthy_ranks: 0, ranks: 8},
    ],
  },
  failed: null,
};
fixture.history = [0, 2].map(offset => ({at: 1792900800 + offset,
  by_model: fixture.snapshot.by_model_stats}));
let nextTimer = 0;
const timers = new Map();
const context = {
  document, matchMedia: () => media,
  ResizeObserver: class { observe() {} },
  getComputedStyle: () => ({getPropertyValue: key => {
    const light = root.dataset.theme === 'light';
    return ({
      '--text': light ? '#23364b' : '#e2eaf3', '--muted': light ? '#66788c' : '#93a4ba',
      '--panel': light ? '#fff' : '#111a27', '--inset': light ? '#f7f9fc' : '#0d1521',
      '--line': light ? '#dbe3ed' : '#243247', '--blue': light ? '#286ac0' : '#79b8ff',
      '--green': '#6ad4b1', '--purple': '#bd9bfa', '--amber': '#edbd74', '--red': '#f48b98', '--cyan': '#76cdd6',
    })[key];
  }}),
  localStorage: {
    getItem: key => {if (input.blocked) throw Error('blocked'); return stored[key] ?? null;},
    setItem: (key, value) => {if (input.blocked) throw Error('blocked'); stored[key] = value;},
  },
  Option: function(text, value) {this.text = text; this.value = value;},
  setTimeout: (callback, delay) => {timers.set(++nextTimer, {callback, delay}); return nextTimer;},
  clearTimeout: id => timers.delete(id), setInterval() {}, AbortController,
  fetch: async path => {
    if (fixture.failed === 'all' || fixture.failed === 'history' && path.startsWith('/history')) throw Error('offline');
    return {ok: true, json: async () => path === '/data' ? fixture.snapshot : fixture.history};
  },
};
vm.createContext(context);
// Use ECharts' actual SVG SSR renderer; only its DOM attachment is adapted.
delete context.document;
vm.runInContext(input.scripts.find(script => script.id === 'echarts-vendor').code, context);
context.document = document;
context.echarts.setPlatformAPI({measureText: text => ({width: String(text).length * 6})});
const renderedCharts = new Map();
const nativeInit = context.echarts.init;
context.echarts.init = (element, theme, options) => {
  const chart = nativeInit(null, theme, {...options, ssr: true, width: 360, height: 240});
  chart.getDom = () => element;
  const setOption = chart.setOption.bind(chart);
  chart.setOption = (...args) => {
    setOption(...args);
    element.innerHTML = chart.renderToSVGString();
  };
  renderedCharts.set(element.attrs.id, chart);
  return chart;
};
context.echarts.getInstanceByDom = element => renderedCharts.get(element.attrs.id);
const initialGlobals = Object.keys(context).sort();
for (const script of input.scripts)
  if (script.id !== 'echarts-vendor') vm.runInContext(script.code, context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await flush();
  const $ = id => elements.get(id);
  const workerButton = url => allElements().find(element => element.dataset.worker === encodeURIComponent(url));
  const tick = async () => {
    const [id, timer] = [...timers].find(([, timer]) => timer.delay === Number($('interval').value) * 1000);
    timers.delete(id);
    await timer.callback();
  };
  ASSERTIONS
  for (const chart of renderedCharts.values()) chart.dispose();
  console.log('passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to exercise the dashboard")
class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (files("router_monitor") / "templates/dashboard.html").read_text(encoding="utf-8")
        source = source.replace("INTERVAL_MS", "2000").replace("COLLECTION_S", "2").replace("WIN_DEFAULT_MIN", "5")
        cls.dashboard = DashboardParser(source)

    def evaluate(self, assertions, *, stored=None, blocked=False, dark=False):
        result = subprocess.run(
            ["node", "-e", HARNESS.replace("ASSERTIONS", assertions)],
            input=json.dumps(
                {
                    "elements": self.dashboard.elements,
                    "scripts": self.dashboard.scripts,
                    "stored": stored or {},
                    "blocked": blocked,
                    "dark": dark,
                }
            ),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-8000:])

    def test_preferences_initialize_and_follow_system_theme(self):
        for theme in ["light", "dark", "system"]:
            with self.subTest(theme=theme):
                self.evaluate(
                    f"""
assert.equal(root.lang, 'zh-CN');
assert.equal(root.dataset.theme, {"light" if theme == "light" else "dark"!r});
media.matches = false;
media.handler();
assert.equal(root.dataset.theme, {"dark" if theme == "dark" else "light"!r});
assert.equal($('status').textContent, '实时');
assert.match($('interval').title, /采集间隔 2 秒/);
assert.deepEqual(Object.keys(context).sort(), initialGlobals);
""",
                    stored={"router-monitor-theme": theme, "router-monitor-language": "zh-CN"},
                    dark=True,
                )
        for blocked in [False, True]:
            self.evaluate(
                "assert.equal(root.lang, 'en'); assert.equal(root.dataset.theme, 'light');",
                stored={"router-monitor-theme": "invalid", "router-monitor-language": "invalid"},
                blocked=blocked,
            )

    def test_filters_expansion_and_language_changes_preserve_user_state(self):
        self.evaluate(r"""
assert.match($('worker-rows').innerHTML, /healthy:4563/);
assert.match($('worker-rows').innerHTML, /warning:4563/);
assert.doesNotMatch($('worker-rows').innerHTML, /unavailable:4563/);
assert.equal($('unhealthy-rows').hidden, true);
const groups = $('worker-rows').innerHTML.split('<tr class="worker-model">').slice(1);
assert.equal(groups.length, 2);
assert.match(groups[0], />a<span class="muted">1 workers/);
assert.match(groups[0], /healthy:4563/);
assert.doesNotMatch(groups[0], /warning:4563/);
assert.match(groups[1], />b<span class="muted">1 workers/);
assert.match(groups[1], /warning:4563/);
fixture.snapshot.worker_rows.push({...fixture.snapshot.worker_rows[0], url: 'http://another:4563'});
await tick();
const modelA = () => $('worker-rows').innerHTML.split('<tr class="worker-model">')[1];
assert.match(modelA(), />a<span class="muted">2 workers/);
assert.ok(modelA().indexOf('another:4563') < modelA().indexOf('healthy:4563'));
allElements().find(element => element.dataset.key === 'url').trigger('click');
assert.ok(modelA().indexOf('healthy:4563') < modelA().indexOf('another:4563'));
assert.doesNotMatch(modelA(), /warning:4563/);
$('unhealthy-toggle').trigger('click');
assert.match($('unhealthy-rows').innerHTML, />b<span class="muted">1 workers/);
assert.match($('unhealthy-rows').innerHTML, /unavailable:4563/);
workerButton('http://warning:4563').trigger('click');
assert.match($('worker-rows').innerHTML, /Request success 70%/);
$('model').value = 'b'; $('model').trigger('change');
$('language').value = 'zh-CN'; $('language').trigger('change');
assert.equal($('model').value, 'b');
assert.equal($('worker-rows').innerHTML.split('<tr class="worker-model">').length, 2);
assert.match($('worker-rows').innerHTML, />b<span class="muted">1 个 worker/);
assert.equal($('status').textContent, '实时');
assert.equal($('unhealthy-rows').hidden, false);
assert.match($('worker-rows').innerHTML, /请求成功率 70%/);
assert.doesNotMatch($('worker-rows').innerHTML, /healthy:4563/);
assert.equal(stored['router-monitor-language'], 'zh-CN');
$('theme').value = 'light'; $('theme').trigger('change');
assert.equal(root.dataset.theme, 'light');
assert.equal(stored['router-monitor-theme'], 'light');
$('search').value = 'missing'; $('search').trigger('input');
assert.match($('worker-rows').innerHTML, /无匹配项/);
assert.equal($('unhealthy-group').hidden, true);
""")

    def test_worker_badges_explain_health_in_both_languages(self):
        self.evaluate(r"""
const worker = fixture.snapshot.worker_rows[1];
for (const [ranks, level, english, chinese] of [
  [7, 0, 'Rank unhealthy', 'rank 不健康'],
  [8, 1, 'Slow compute', '计算慢'],
  [8, 89, 'Slow compute', '计算慢'],
  [8, 90, 'High request error rate', '请求错误率高'],
  [8, 99.9, 'High request error rate', '请求错误率高'],
  [8, 100, 'Suspected stall', '疑似卡住'],
  [7, 91, 'Rank unhealthy · High request error rate', 'rank 不健康 · 请求错误率高'],
  [8, null, 'High request error rate', '请求错误率高'],
]) {
  worker.healthy_ranks = ranks;
  worker.health_level = level;
  for (const [language, label] of [['en', english], ['zh-CN', chinese]]) {
    $('language').value = language; $('language').trigger('change');
    assert.ok($('worker-rows').innerHTML.includes('>' + label + '</span>'), label);
  }
}
assert.match($('worker-rows').innerHTML, /健康/);
$('unhealthy-toggle').trigger('click');
assert.match($('unhealthy-rows').innerHTML, /rank 不健康/);
""")

    def test_gateway_identity_deployments_and_unknown_rank_health(self):
        self.evaluate(r"""
fixture.snapshot.router_type = 'litellm';
fixture.snapshot.router_label = 'LiteLLM';
fixture.snapshot.registry_scope = 'deployments';
fixture.snapshot.rps = 12.5;
const worker = fixture.snapshot.worker_rows[1];
worker.ranks = null;
worker.healthy_ranks = null;
worker.registry_failed = true;
worker.health_level = null;
worker.success_pct = 100;
await tick();
assert.match($('router-kind').textContent, /LiteLLM/);
assert.match($('endpoint').textContent, /12.5 req\/s/);
assert.equal($('workers-title').textContent, 'Deployment fleet');
assert.match($('worker-rows').innerHTML, /Upstream unhealthy/);
assert.match($('worker-rows').innerHTML, /Rank health unreported/);
$('language').value = 'zh-CN'; $('language').trigger('change');
assert.equal($('workers-title').textContent, '模型部署');
assert.match($('worker-rows').innerHTML, /上游不健康/);
assert.match($('worker-rows').innerHTML, /未提供 rank 健康状态/);
""")

    def test_polling_renders_trends_and_retains_data_during_connection_failure(self):
        self.evaluate(r"""
assert.equal($('status').textContent, 'Live');
assert.ok($('settings'));
for (const removed of ['pause', 'refresh', 'download', 'models', 'age']) assert.equal($(removed), undefined);
$('interval').value = '3.5'; await $('interval').trigger('change');
assert.ok([...timers.values()].some(timer => timer.delay === 3500));
assert.equal(stored['router-monitor-interval'], '3.5');
$('interval').value = ''; await $('interval').trigger('input');
assert.ok([...timers.values()].some(timer => timer.delay === 3500));
$('interval').value = '3.5';

$('window').value = '120.5'; await $('window').trigger('input');
for (const chart of renderedCharts.values()) {
  const yAxis = chart.getOption().yAxis?.[0];
  assert.equal(yAxis.scale, true);
  assert.equal(yAxis.min({min: 0, max: 1}), 0);
  assert.ok(yAxis.max({min: 0, max: 0}) >= 1);
  const axis = chart.getOption().xAxis?.[0];
  if (axis?.type === 'time') {
    assert.ok(axis.max - axis.min <= 120.5 * 60000);
    assert.equal(axis.min, fixture.history[0].at * 1000);
  }
}
assert.equal($('history-note').textContent, 'Last 120.5 min');
const percentageAxis = renderedCharts.get('plot-success').getOption().yAxis[0];
assert.equal(percentageAxis.max({min: 99, max: 100}), 100);
const countAxis = renderedCharts.get('plot-running').getOption().yAxis[0];
assert.equal(countAxis.minInterval, 1);
assert.ok(Number.isInteger(countAxis.max({min: 0, max: 3})));

assert.equal($('history-count').textContent, '2 samples');
const plots = [...elements].filter(([id]) => id.startsWith('plot-'));
for (const [, plot] of plots) {
  assert.match(plot.innerHTML, /<svg/);
  assert.doesNotMatch(plot.innerHTML, /NaN/);
}
fixture.failed = 'history';
await tick();
assert.equal($('status').textContent, 'Disconnected');
assert.equal($('notice').textContent, 'History unavailable');
assert.equal($('history-count').textContent, '2 samples');
fixture.failed = 'all';
await tick();
assert.equal($('notice').textContent, 'Service unavailable');
assert.match($('worker-rows').innerHTML, /warning:4563/);
fixture.failed = null;
await tick();
assert.equal($('status').textContent, 'Live');
assert.equal($('notice').hidden, true);
$('health').value = 'unhealthy'; $('health').trigger('change');
assert.equal($('unhealthy-rows').hidden, false);
assert.match($('worker-rows').innerHTML, /warning:4563/);
""")

    def test_charts_preserve_zoom_selection_theme_and_gaps(self):
        self.evaluate(r"""
const load = renderedCharts.get('plot-running');
assert.equal(renderedCharts.size, 15);
for (const chart of renderedCharts.values()) {
  const series = chart.getOption().series;
  assert.deepEqual(Array.from(series, item => item.id), ['a', 'b']);
  assert.equal(series[0].itemStyle.color, load.getOption().series[0].itemStyle.color);
}
assert.equal(load.getOption().series[0].data[0][1], 1);
assert.equal(load.getOption().series[1].data[0][1], 2);
assert.equal(renderedCharts.get('plot-queue').getOption().series[1].data[0][1], 3);
assert.equal(renderedCharts.get('plot-kv').getOption().series[1].data[0][1], 70);
assert.equal(renderedCharts.get('plot-swa').getOption().series[1].data[0][1], 50);
assert.equal(renderedCharts.get('plot-success').getOption().series[1].data[0][1], 80);
load.dispatchAction({type: 'legendUnSelect', name: 'a'});
load.dispatchAction({type: 'dataZoom', start: 20, end: 80});
assert.equal(renderedCharts.get('plot-kv').getOption().dataZoom[0].start, 20);
await tick();
assert.equal(renderedCharts.get('plot-running'), load);
assert.equal(load.getOption().legend[0].selected.a, false);
$('language').value = 'zh-CN'; $('language').trigger('change');
assert.equal($('title-running').textContent, '运行请求');
assert.equal(load.getOption().legend[0].selected.a, false);
assert.equal(load.getOption().dataZoom[0].start, 20);
$('theme').value = 'light'; $('theme').trigger('change');
assert.equal(load.getOption().textStyle.color, '#23364b');
assert.equal(load.getOption().legend[0].selected.a, false);
fixture.history.push({at: fixture.history.at(-1).at + 60,
  by_model: {a: {...fixture.snapshot.by_model_stats.a, running: null}}});
await tick();
const data = load.getOption().series.find(series => series.id === 'a').data;
assert.equal(data.filter(point => point[1] === null).length, 2);
assert.equal(load.getOption().series.find(series => series.id === 'b').data.at(-1)[1], null);
assert.equal(load.getOption().series[0].connectNulls, false);
$('reset-zoom').trigger('click');
assert.equal(load.getOption().dataZoom[0].start, 0);
assert.equal(load.getOption().dataZoom[0].end, 100);
assert.equal(load.getOption().legend[0].selected.a, false);
""")

    def test_chart_units_follow_data_magnitude(self):
        self.evaluate(r"""
for (const [value, label, formatted] of [
  [500, 'ms', '500'], [15000, 's', '15'],
  [120000, 'min', '2'], [7200000, 'h', '2'],
]) {
  for (const sample of fixture.history)
    for (const model of Object.values(sample.by_model)) model.ttft_mean_ms = value;
  await tick();
  const option = renderedCharts.get('plot-ttft').getOption();
  assert.equal($('note-ttft').textContent, label);
  assert.equal(option.yAxis[0].axisLabel.formatter(value), formatted);
  assert.match(option.tooltip[0].formatter([
    {axisValue: 1792900800000, value: [1792900800000, value], seriesName: 'a', color: 'blue'}
  ]), new RegExp('<b>' + formatted + '</b>'));
}
for (const sample of fixture.history)
  for (const model of Object.values(sample.by_model)) model.tps = 2500000;
await tick();
assert.equal($('note-throughput').textContent, 'M token/s');
assert.equal(renderedCharts.get('plot-throughput').getOption().yAxis[0].axisLabel.formatter(2500000), '2.5');
assert.equal($('note-success').textContent, '%');
""")

    def test_short_metric_gaps_have_dashed_links_but_outages_do_not(self):
        self.evaluate(r"""
const cases = [
  {times: [0, 2, 4], values: [1, null, 3], links: 1},
  {times: [0, 6, 12, 18, 24, 30], values: [1, null, null, null, null, 3], links: 1},
  {times: [0, 8, 16, 24, 32], values: [1, null, null, null, 3], links: 0, latencyLinks: 1},
  {times: [0, 20, 22], values: [1, null, 3], links: 0},
  {times: [0, 2, 4], values: [1, null, 3], outage: true, links: 0},
  {times: [0, 2, 4], values: [1, null, 3], absent: true, links: 0},
];
for (const test of cases) {
  fixture.history = test.times.map((offset, index) => ({
    at: 1792900800 + offset,
    by_model: test.absent && index === 1 ? {} : {a: {
      ...fixture.snapshot.by_model_stats.a,
      running: test.values[index],
      ttft_mean_ms: test.values[index],
      metrics_workers: test.outage && index === 1 ? 0 : 1,
    }},
  }));
  await tick();
  const series = renderedCharts.get('plot-running').getOption().series[0];
  assert.equal(series.markLine.data.length, test.links);
  const latency = renderedCharts.get('plot-ttft').getOption().series[0];
  assert.equal(latency.markLine.data.length, test.latencyLinks ?? test.links);
  assert.equal(latency.markLine.lineStyle.type, 'solid');
  assert.equal(latency.data[1][1], null);

  assert.equal(series.data[1][1], null);
  assert.equal(series.connectNulls, false);
  if (test.links) {
    assert.equal(series.markLine.lineStyle.type, 'dashed');
    assert.equal(series.markLine.silent, true);
    assert.equal(series.markLine.data[0][0].coord[1], 1);
    assert.equal(series.markLine.data[0][1].coord[1], 3);
  }
}
""")
