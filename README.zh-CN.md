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
- 目标设置全部来自 JSON，不继承单 Router 的命令行或环境变量设置。`--web-port`、`--history-window` 和 `--history-prometheus-*` 设置由所有目标共享；端口不能为 `0`。
- 在面板中切换目标，各目标独立采集并保存历史。`/`、`/data` 和 `/history` 支持 `?router=名称`，默认选择第一个目标。
- JSON 模式默认关闭维护操作；按目标设置 `keepalive`、`auto_unregister` 或正数 `unregister_unavailable_after` 启用对应策略。
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

### 指标与历史

- TTFT/ITL 使用计数器增量按次数加权；缓存命中率使用 60 秒 token 加权窗口。
- worker 模型查询失败时，面板使用该主机受管注册记录中唯一明确的模型名。缺失或冲突时归入「未知模型」。模型不匹配清理只依据 worker 查询成功返回的模型名。
- 每次打开页面均使用服务端配置的窗口：默认 2 小时，最早从首条数据开始。窗口调整仅对当前页面生效。
- 默认使用内存历史。配置 [Prometheus 持久历史](#prometheus-持久历史) 后，重启仍可查看已抓取的样本。
- 刷新间隔、语言和主题会自动记忆。窗口按分钟、刷新按秒输入，均支持小数。

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

## SGLang 维护

以下策略仅作用于 SGLang 管理的注册记录。

| 参数 | 默认值 | 行为 |
| --- | --- | --- |
| `--unregister-unavailable-after 秒数` | `1800` | 所有 rank 持续不健康且空闲，最终可用性探测失败后注销；`0` 关闭清理 |
| `--keepalive` | 关闭 | 向已观测为空闲的 worker 发送随机合成任务；失败后退避重试 |
| `--auto-unregister` | 关闭 | 注销实际模型与注册模型不一致的记录 |

- 不可用清理仅接受探测中的连接失败、连接或读取超时、远端协议异常；HTTP 状态错误及其他探测失败仍保留 worker。
- 恢复、重新注册或丢失注册表观测后，宽限期重新计时。
- 注销只移除 Router 注册记录，不停止 worker 进程。删除请求被接受不代表记录已删除。

### 保活负载

保活使用合成观测，随机选择摘要、故障排查、状态汇报或原因比较任务。每次请求的任务内容和采样温度都会变化。

| 参数 | 默认值 | 每次请求或每轮的行为 |
| --- | --- | --- |
| `--keepalive-prompt-words` | `512` | 合成上下文长度在配置字数的八分之一至上限之间随机变化，另加少量任务说明 |
| `--keepalive-max-tokens` | `64` | 生成预算在配置上限的四分之一至上限之间随机变化，实际输出可能更短 |
| `--keepalive-concurrency` | `8` | 每轮向每个空闲 worker 随机发送 1 至配置上限个请求；每个 Router 最多同时执行 32 个请求 |

长度与 token 下限向下取整，最少为 1。仅向已观测为空闲且模型已识别的 worker 发送保活请求。

启动 SGLang Router：安装提供 `RouterArgs` 的兼容 `sglang_router` 运行时，执行 `bash scripts/router.sh`。使用 `rmon --host 127.0.0.1` 监控，默认 API 端口为 `2730`，指标端口为 `29000`。

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
