<p align="center"><img src="docs/images/logo.svg" alt="Router Monitor" width="72"></p>

# LLM Router Monitor

[English](README.md) | 简体中文

集中监控 LLM Router 与推理 worker。

- 自动识别 Router，支持命名 Router 切换，按模型展示 worker 集群。
- 15 张分模型趋势图、rank 明细及健康原因。
- 中英文、明暗主题，界面为单个独立 HTML 文件。

![面板预览 · 匿名化数据](docs/images/dashboard.jpg)

## 安装与运行

需要 Python 3.10+：

```bash
python -m pip install llm-router-monitor
rmon http://127.0.0.1:4000
```

访问 [localhost:8899](http://localhost:8899/)。`llm-router-monitor` 与 `rmon` 等价，完整参数见 `rmon --help`。

| 参数 | 用途 |
| --- | --- |
| `--router-config 文件` | 命名 Router 的 JSON 配置 |
| `--metrics-url URL` | Prometheus 或 NGINX `stub_status` 地址 |
| `--admin-url URL` | 独立管理接口地址 |
| `--worker URL` | 添加 worker，可重复传入 |
| `--router-type TYPE` | 手动指定 Router 类型 |
| `--web-port PORT` | 面板端口，默认 `8899`；`0` 为仅终端 |
| `--interval 秒数` | 采集间隔，默认 `2` 秒 |
| `--history-window 分钟数` | 每次打开页面的初始窗口，默认 `120` 分钟（2 小时） |

面板监听 `0.0.0.0`。Router 认证使用 `ROUTER_API_KEY`，worker 认证使用 `WORKER_API_KEY`。位置参数 URL 优先于 `--router-url` 和 `ROUTER_URL`。

## 多 Router

将配置保存为以显示名称为键的 JSON 对象，例如 `routers.local.json`：

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

- 每个目标必填 HTTP(S) `endpoint`，URL 中不能嵌入凭据。可选字段：`router_type`、`worker_urls`、`metrics_endpoint`、`admin_url`、`api_key`、`worker_api_key` 和 `interval`。
- 每个目标的启动配置来自 JSON。`--web-port`、`--history-window` 和 `--history-prometheus-*` 由所有目标共享，Web 端口必须非零。
- 在面板中切换目标，各目标独立采集并保存历史。`/`、`/data` 和 `/history` 支持 `?router=名称`，默认选择第一个目标。
- JSON 模式默认关闭维护操作；按目标设置 `keepalive`、`keepalive_unconditional`、`auto_unregister` 或正数 `unregister_unavailable_after` 启用对应策略。
- 本地配置不要纳入 Git。Router 凭据使用 `api_key`，worker 凭据使用 `worker_api_key`。

## 支持的 Router

接入前需开启 Router 指标接口或 exporter。

| Router | 发现方式与指标 |
| --- | --- |
| SGLang | `/workers`；rank 健康、负载及推理指标 |
| vLLM Production Stack | 从 Router 指标标签发现 worker |
| 独立 vLLM | 直接采集引擎指标 |
| [LiteLLM](https://docs.litellm.ai/docs/proxy/prometheus) | `/model/info` 与部署指标；需开启 Prometheus callback |
| NVIDIA Dynamo | 模型部署与前端指标 |
| Envoy | 管理接口 `/clusters?format=json` 及上游指标 |
| NGINX Plus | Prometheus exporter 提供的上游信息 |
| Traefik | 健康检查提供的上游信息 |
| NGINX OSS | 原生 `stub_status` 请求计数或 exporter 指标；通过 `--worker` 指定引擎 |
| HAProxy、Kong、Ray Serve | 代理指标；通过 `--worker` 指定引擎 |
| 其他代理 | 通过 `--worker` 指定 SGLang/vLLM 引擎 |

使用 NGINX 原生 `stub_status` 时，在 JSON 中设置 `metrics_endpoint`，或在单 Router 模式传入 `--metrics-url`。它提供计算请求速率所需的计数，不提供推理指标。

Envoy 和 NGINX Plus 裸地址按 HTTP 连接；HTTPS 或自定义路径请用 `--worker` 指定。

Envoy 独立管理端口：

```bash
rmon http://127.0.0.1:8080 \
  --admin-url http://127.0.0.1:9901 \
  --metrics-url http://127.0.0.1:9901/stats/prometheus
```

Exporter 与显式 worker：

```bash
rmon http://127.0.0.1:8080 \
  --metrics-url http://127.0.0.1:9113/metrics \
  --worker http://worker-a:8000 --worker http://worker-b:8000
```

## 查看面板

- 每张图只显示所选时间窗口内有该指标观测值的模型。零值正常显示，缺值不算观测；“未知模型”表示模型识别失败，有指标时仍会显示。
- 趋势使用细折线，数据密集时可放大查看。各图同步缩放，点击图例可隐藏或显示模型。
- 缺值不填造：TTFT/ITL 连接有效观测点，其他图表用虚线连接 30 秒内的缺口；采集中断或模型不可用时保留断线。
- 坐标轴自适应数据范围：指标非负，百分比限定 0–100%，计数使用整数刻度。
- 点击提示条的 × 可收起当前页面中的本次提示。提示变化、恢复后再次异常或刷新页面时会重新显示，顶部状态指示不受影响。

### 指标与历史

- TTFT/ITL 使用计数器增量按次数加权；缓存命中率使用 60 秒 token 加权窗口。
- worker 模型查询失败时，面板使用该主机受管注册记录中唯一明确的模型名。缺失或冲突时归入「未知模型」。模型不匹配清理只依据 worker 查询成功返回的模型名。
- 每次打开页面均使用服务端配置的窗口：默认 2 小时，最早从首条数据开始。窗口调整仅对当前页面生效。
- 默认使用内存历史。配置 [Prometheus 持久历史](#prometheus-持久历史) 后，重启仍可查看已抓取的样本。
- **设置 → 显示**可调整历史窗口、刷新频率、语言和主题。浏览器记住刷新频率、语言和主题。
- 在本机或通过端口转发打开 **设置 → 保活**。点击**应用**即可更新所选 Router 的运行配置。Monitor 重启后恢复启动配置。

![分模型趋势 · 匿名化数据](docs/images/trends.jpg)

| 接口 | 内容 |
| --- | --- |
| `/data` | Router 信息、汇总、worker 明细与分模型指标 |
| `/history?n=100` | 包含 `at`（Unix 秒）和 `by_model` 的历史采样 |
| `/metrics` | 所有 Router 目标的带时间戳模型指标 |

## Prometheus 持久历史

Monitor 在 `/metrics` 暴露带采集时间戳的模型指标。给这个 monitor 配置一个 Prometheus 抓取任务：

```yaml
scrape_configs:
  - job_name: router-monitor
    scrape_interval: 2s
    honor_timestamps: true
    static_configs:
      - targets: ["monitor.example:8899"]
```

抓取间隔应不大于各目标中最短的采集间隔，并保持 `honor_timestamps` 开启。每台 monitor 使用独立的 job 名称。

```bash
rmon http://router.example:2730 \
  --history-prometheus-url http://prometheus.example:9090
```

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--history-prometheus-url` | 未设置 | Prometheus 基础地址，启用持久历史查询 |
| `--history-prometheus-job` | `router-monitor` | 该 monitor 对应的抓取任务 |
| `--history-prometheus-lookback-hours` | `24` | 历史查询的最大时长，单位为小时 |

面板初始窗口仍为 120 分钟。这些参数同样适用于 `--router-config`，且需要非零 Web 端口。

- 已抓取样本的保存时长由 Prometheus 保留策略决定。面板合并近期本地观测，补上最近一次抓取之后的数据。
- Grafana 或 PromQL 可以查询同一组指标，例如 `router_monitor_model_tps{job="router-monitor"}`。用 `router` 和 `model` 标签筛选。
- 未配置 Prometheus 地址时，历史保留在内存中，重启后清空。启用后若历史查询失败，接口返回错误；`/data` 继续提供实时数据。

## SGLang worker 维护

### 保活

保活默认关闭。可在面板中选择模式，也可通过启动参数配置。

| 模式 | 启动参数 | 调度行为 |
| --- | --- | --- |
| 自适应 | `--keepalive` | 按轮向空闲 worker 发保活。遇推理流量时取消，安静满 5 秒且 worker 空闲后恢复。 |
| 无条件 | `--keepalive-unconditional` | 向已识别模型的 worker 持续发保活。请求成功后立即补位，有推理流量时照常运行。 |

```bash
rmon http://127.0.0.1:2730 --keepalive-unconditional
```

- 命名 Router 可按目标设置 `"keepalive": true` 或 `"keepalive_unconditional": true`。
- 两种模式的并发上限都取总并发配置和「符合条件的 worker 数 × 单 worker 上限」中的较小值。
- 自适应模式从整轮结束后开始计时。Router 指标缺失或 worker 观测过期时暂停，失败的轮次使用指数退避。
- 在 `/data` 的 `keepalive` 字段可查看状态、在途数、发出数和取消数。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--keepalive-concurrency` | `8` | 单 worker 并发上限 |
| `--keepalive-total-concurrency` | `32` | 每个 Router 的保活总并发上限 |
| `--keepalive-interval` | `5` | 自适应模式的轮次间隔，或无条件模式的单请求失败重试间隔（秒） |
| `--keepalive-traffic-poll-interval` | `1` | 自适应模式检测推理流量的间隔，单位为秒 |
| `--keepalive-max-interval` | `60` | 自适应模式失败后的最大退避秒数 |
| `--keepalive-prompt-words` | `512` | 合成上下文字数上限，随机长度为上限的八分之一至上限 |
| `--keepalive-min-tokens` | `0` | 生成 token 下限，`0` 使用 worker 默认值 |
| `--keepalive-max-tokens` | `64` | 生成预算上限，随机预算的下界为上限四分之一与生成下限的较大值 |

随机长度和预算下界向下取整，且至少为 1。`--keepalive-model` 可把保活限定在单个模型。

### 注册清理

- `--auto-unregister` 注销实际模型与注册模型不一致的记录，默认关闭。
- `--unregister-unavailable-after` 设定不可用宽限期。所有 rank 持续不健康且空闲，复查失败后注销注册记录。单 Router 模式默认 1,800 秒，设为 `0` 关闭。
- 复查使用连接失败、连接或读取超时、远端协议异常判定不可用。恢复、重新注册或注册表中断会重新计时。注销只作用于 Router 的注册记录。

本地启动 SGLang Router 需要提供 `RouterArgs` 的兼容 `sglang_router`。运行 `bash scripts/router.sh`，API 默认端口为 `2730`，指标端口为 `29000`。

## 开发

模块职责、采集数据流和多 Router 隔离见[架构说明](docs/architecture.zh-CN.md)。

`scripts/` 存放 shell 入口，`tests/` 存放行为测试。前端测试需要 Node.js。

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests
ruff check .
ruff format --check router_monitor tests
python -m build
```

构建产物位于 `dist/`，使用 `python -m pip install dist/llm_router_monitor-0.0.2-py3-none-any.whl` 安装 wheel。
