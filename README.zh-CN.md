<p align="center"><img src="docs/images/logo.svg" alt="Router Monitor" width="72"></p>

# LLM Router Monitor

[English](README.md) | 简体中文

集中监控 LLM Router 与推理 worker。

- 自动识别 Router，按模型展示 worker 集群。
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
| `--metrics-url URL` | Prometheus 指标地址 |
| `--admin-url URL` | 独立管理接口地址 |
| `--worker URL` | 添加 worker，可重复传入 |
| `--router-type TYPE` | 手动指定 Router 类型 |
| `--web-port PORT` | 面板端口，默认 `8899`；`0` 为仅终端 |
| `--interval 秒数` | 采集间隔，默认 `2` 秒 |
| `--history-window 分钟数` | 每次打开页面的初始窗口，默认 `720` 分钟（12 小时） |

面板监听 `0.0.0.0`。Router 认证使用 `ROUTER_API_KEY`，worker 认证使用 `WORKER_API_KEY`。位置参数 URL 优先于 `--router-url` 和 `ROUTER_URL`。

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
| NGINX OSS、HAProxy、Kong、Ray Serve | 代理指标；通过 `--worker` 指定引擎 |
| 其他代理 | 通过 `--worker` 指定 SGLang/vLLM 引擎 |

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
- 每次打开页面均使用服务端配置的窗口：默认 12 小时，最早从首条数据开始。窗口调整仅对当前页面生效。
- 历史在内存中持续积累，monitor 重启后清空。
- 刷新间隔、语言和主题会自动记忆。窗口按分钟、刷新按秒输入，均支持小数。

![分模型趋势 · 匿名化数据](docs/images/trends.jpg)

| 接口 | 内容 |
| --- | --- |
| `/data` | Router 信息、汇总、worker 明细与分模型指标 |
| `/history?n=100` | 包含 `at`（Unix 秒）和 `by_model` 的历史采样 |

## SGLang 维护

以下策略仅作用于 SGLang 管理的注册记录。

| 参数 | 默认值 | 行为 |
| --- | --- | --- |
| `--unregister-unavailable-after 秒数` | `1800` | 所有 rank 持续不健康且空闲，最终连接探测失败后注销；`0` 关闭清理 |
| `--keepalive` | 关闭 | 向已观测为空闲的 worker 发送短推理请求 |
| `--auto-unregister` | 关闭 | 注销实际模型与注册模型不一致的记录 |

恢复、重新注册或丢失注册表观测后，清理计时重新开始。保活失败后退避重试。注销不停止 worker 进程。

启动 SGLang Router：安装提供 `RouterArgs` 的兼容 `sglang_router` 运行时，执行 `bash scripts/router.sh`。使用 `rmon --host 127.0.0.1` 监控，默认 API 端口为 `2730`，指标端口为 `29000`。

## 开发

`router_monitor/` 保持扁平结构：

| 职责 | 模块 |
| --- | --- |
| 入口与生命周期 | `cli.py`、`config.py`、`router.py` |
| 采集与指标 | `collectors.py`、`sources.py`、`worker.py`、`metrics.py`、`http.py` |
| 状态与维护 | `state.py`、`maintenance.py` |
| 展示 | `visualization.py`、`dashboard.py`、`templates/dashboard.html` |

`scripts/` 存放 shell 入口，`tests/` 存放行为测试。前端测试需要 Node.js。

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests
ruff check .
ruff format --check router_monitor tests
python -m build
```

构建产物位于 `dist/`，使用 `python -m pip install dist/llm_router_monitor-0.0.1-py3-none-any.whl` 安装 wheel。
