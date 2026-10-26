<p align="center"><img src="docs/images/logo.svg" alt="Router Monitor logo" width="72"></p>

# Router Monitor

[English](README.md) | 简体中文

LLM Router 与推理 worker 的统一监控面板，支持自动识别 Router，并附带 SGLang Router 启动入口。

![面板预览（匿名化数据）](docs/images/dashboard.jpg)

## 启动

需要 Python 3.10+，并能访问 Router 的监控接口。

```bash
python -m pip install -e .
bash scripts/monitor.sh --router-url http://127.0.0.1:4000
```

访问 [localhost:8899](http://localhost:8899/)。Router 类型默认 `auto`，页头显示识别结果和观测到的请求速率。

SGLang 默认 API/指标端口为 `2730`/`29000`，可直接使用：

```bash
bash scripts/monitor.sh --host 127.0.0.1
```

如需启动 SGLang Router，先安装兼容 `RouterArgs` API 的 `sglang_router` 运行时，再执行 `bash scripts/router.sh`。其他 Router 使用各自的启动方式。

## Router 支持范围

通过注册接口和 Prometheus 指标自动识别。需要在 Router 或 exporter 中开启指标。

| Router | 发现方式 | 面板数据 |
| --- | --- | --- |
| [SGLang](https://github.com/sgl-project/sglang) | `/workers` | Worker/rank 健康、负载、推理指标；可选维护策略 |
| [vLLM Production Stack](https://github.com/vllm-project/production-stack) | Router 指标中的 `server` 标签 | 自动发现 worker，直接采集 vLLM 引擎指标 |
| [独立 vLLM](https://docs.vllm.ai/en/stable/usage/metrics/) | 引擎指标 | 单端点及各引擎指标 |
| [LiteLLM](https://docs.litellm.ai/docs/proxy/prometheus) | `/model/info` 和部署指标标签 | 各部署输出速率、TTFT、token 缓存命中、请求成功率 |
| [NVIDIA Dynamo](https://docs.nvidia.com/dynamo/dev/reference/observability/metrics-catalog) | 前端指标中的 `model` 标签 | 各模型输出速率、TTFT、ITL |
| [Envoy](https://www.envoyproxy.io/docs/envoy/latest/operations/admin.html) | 管理接口 `/clusters?format=json` | 上游发现、代理流量、直接采集 worker 指标 |
| [NGINX Plus](https://github.com/nginx/nginx-prometheus-exporter) | Exporter 的上游 `server` 标签 | 上游发现、代理流量、直接采集 worker 指标 |
| [Traefik](https://doc.traefik.io/traefik/reference/install-configuration/observability/metrics/) | 开启健康检查后的 `traefik_service_server_up` | 上游发现、服务流量、直接采集 worker 指标 |
| [NGINX OSS](https://github.com/nginx/nginx-prometheus-exporter)、[HAProxy](https://www.haproxy.com/documentation/haproxy-unified-gateway/administration/prometheus-metrics/haproxy-stats-metrics/)、[Kong](https://developer.konghq.com/plugins/prometheus/)、[Ray Serve](https://docs.ray.io/en/latest/serve/monitoring.html) | 显式配置 `--worker` | 代理流量和直接采集的 worker 指标 |
| 未识别代理 | 显式配置 `--worker` | 直接采集 SGLang/vLLM worker 指标 |

- 推理引擎单独识别：SGLang、vLLM worker 可以部署在任意受支持的代理后面。
- llm-d 等 Kubernetes 路由栈可接入 Envoy 管理接口及引擎端点；不包含 Kubernetes 资源发现。
- LiteLLM、Dynamo 展示**模型部署**，不会虚构 worker/rank 数量。配置 `--worker` 后，集群视图切换为指定的 worker。
- 未提供的指标保持缺失。代理健康检查不能代表各引擎 rank 的健康状态。
- Envoy socket 地址和 NGINX Plus 裸地址按 HTTP 上游处理；其他协议或路径需显式配置端点。
- 自动识别需要可辨认的接口。接口受限或指标改名时，可用 `--router-type` 指定类型。

### 接入示例

LiteLLM 开启 [Prometheus callback](https://docs.litellm.ai/docs/proxy/prometheus) 后即可使用启动示例。需要认证时，在环境中设置 `ROUTER_API_KEY`。

Envoy 使用独立管理端口：

```bash
bash scripts/monitor.sh --router-url http://127.0.0.1:8080 \
  --admin-url http://127.0.0.1:9901 \
  --metrics-url http://127.0.0.1:9901/stats/prometheus
```

代理通过 exporter 提供指标，worker 显式配置：

```bash
bash scripts/monitor.sh --router-url http://127.0.0.1:8080 \
  --metrics-url http://127.0.0.1:9113/metrics \
  --worker http://worker-a:8000 --worker http://worker-b:8000
```

## 配置

```bash
bash scripts/monitor.sh --help
bash scripts/router.sh --help
```

命令行参数优先于环境变量和默认值。

| 监控参数 | 默认值 / 用途 | 环境变量 |
| --- | --- | --- |
| `--router-url` | 完整 Router 基地址，覆盖 host/port | `ROUTER_URL` |
| `--router-type` | `auto` | — |
| `--metrics-url` | 指定 Prometheus URL，关闭地址回退 | `METRICS_URL` |
| `--admin-url` | 默认 Router URL；可指定 Envoy 管理端口 | `ROUTER_ADMIN_URL` |
| `--worker` | 可重复传入 worker 基地址 | — |
| `--api-key` | Router/管理接口/指标接口 Bearer token | `ROUTER_API_KEY` |
| `--worker-api-key` | 独立的 worker Bearer token | `WORKER_API_KEY` |
| `--host` / `--port` | `127.0.0.1` / `2730` | `MASTER_HOST` / — |
| `--prom-port` | `29000`，SGLang 独立指标端口 | `PROM_PORT` |
| `--interval` | `2` 秒 | `INTERVAL` |
| `--history-window` | `5` 分钟 | — |
| `--web-port` | `8899`；`0` 为仅终端 | — |

- 面板监听 `0.0.0.0`。
- 未指定指标 URL 时，尝试默认指标地址，包括 Envoy 管理路径和 SGLang 独立端口。
- Router 密钥仅用于配置的 Router/管理/指标来源；发现的 worker 只使用独立的 worker 密钥。
- LiteLLM 采集只读被动接口，不触发推理健康探测，也不访问云供应商地址。
- 监控也可通过 `sglang-router-monitor` 或 `python -m router_monitor` 启动。Shell 默认使用 `python3`，可用 `PYTHON_BIN` 指定解释器。

## 面板

- 按模型分组，支持搜索、筛选、排序和引擎详情展开。
- 健康原因区分 rank 不健康、上游不健康、计算慢、请求错误率高和疑似卡住。
- 15 张单指标趋势图，每个模型一条线，各图颜色一致。
- 时间区间联动缩放、图例筛选和真实采样点的悬停明细。
- 页面自动更新；时间窗口、刷新间隔、中英文和跟随系统/浅色/深色主题统一收进设置。
- 自动更新及切换语言、主题时保留缩放与图例选择。

单个 HTML 内嵌 Apache ECharts 6.1.0、CSS、JavaScript、LICENSE 和 NOTICE，运行时无需 CDN。只更新 HTML 时刷新浏览器即可；更新后端需要重启 monitor。

![按模型区分的推理趋势（匿名化数据）](docs/images/trends.jpg)

### 指标与接口

- 页面读取缓存采样，不会触发采集。历史数据在内存中保留一小时。
- TTFT/ITL 按计数器增量和次数加权；缓存命中率使用 60 秒 token 加权窗口。
- vLLM 输出速率使用生成 token 增量；网关输出速率反映代理报告的 token 数。
- SGLang prefill 要求完整 DP 覆盖；rank 健康和成功率取最差观测。
- LiteLLM 成功率要求同时提供请求和失败计数器；没有引擎健康等级时，低于 90% 标记为请求错误率高。
- 缺失指标、计数器重置和序列变更保持缺值。30 秒内的短指标缺口用虚线连接，采集中断和模型不可用仍断开。

| 接口 | 内容 |
| --- | --- |
| `/` | 面板 |
| `/data` | 最新采样：Router 信息、`summary` 汇总、`worker_rows` 和分模型数据 |
| `/history?n=100` | 历史采样：`at`（Unix 秒）和 `by_model` 分模型指标 |

## SGLang worker 维护

维护策略仅用于 SGLang Router 注册记录，其他 Router 和手动添加的 worker 只读。清理只移除注册，不停止 worker 进程。

| 参数 | 默认值 | 行为 |
| --- | --- | --- |
| `--unregister-unavailable-after 秒数` | `1800` | 持续不健康、空闲且最终连接探测失败后注销 |
| `--keepalive` | 关闭 | 向已观测为空闲的 worker 发送短请求 |
| `--auto-unregister` | 关闭 | 实际模型与注册模型不一致时注销 |

- 清理要求所有 rank 明确不健康、注册负载为零。最终 `/v1/models` 探测无法建立连接才注销；HTTP 响应和读取超时均保留。
- 恢复、重新注册或丢失注册表观测后重新计时；注销失败等待下一轮完整宽限期。`--unregister-unavailable-after 0` 可关闭。
- 保活要求观测到 Router/worker 运行与排队负载均为零。默认每轮 5 秒，每个空闲 worker 最多 8 个请求，输出上限 64 token，提示词最多 512 词。
- 任一保活失败都会退避，最长 60 秒。完整调优参数见 `--help`。

## 开发

```text
router_monitor/                 运行时包
  cli.py                        参数解析与应用生命周期
  config.py / http.py           配置与 HTTP 通信
  sources.py                    Router 发现与指标适配
  collectors.py / worker.py     Router 与引擎采集
  metrics.py / state.py         指标计算与共享历史
  visualization.py / dashboard.py  展示数据、Web 与终端输出
  templates/dashboard.html      单文件界面
  router.py                     SGLang Router 启动入口
scripts/                        轻量 .sh 入口
docs/images/                    Logo 与匿名化预览图
tests/                          入口、前端、指标与集成测试
```

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
ruff check .
ruff format --check router_monitor tests
```

测试使用 HTTP 模拟接口及 Node.js 执行内嵌 ECharts SVG 渲染。实际兼容性取决于已安装的 Router 版本和开启的指标标签。
