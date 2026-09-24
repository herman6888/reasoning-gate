# kev-proxy

一个 OpenAI 兼容的 **reasoning-effort 决策中间层**。它坐在 agent（Hermes / Codex）
和推理后端（halogen / 任何接受 `reasoning_effort` 的 OpenAI 兼容服务）之间，
用一个本地小模型（Kev-4B）对每个请求实时判断「这次该用多少思考预算」，
把判定注入后端请求后转发。目标：简单任务少想、复杂任务多想，省算力而不牺牲质量。

```
client (Hermes chat_completions / Codex responses)
        │
        ▼
   kev-proxy :8910  ──► Kev-4B 决策服务 (/v1/systemone)
        │                      (dohnuts.cpp HIP, Q8_0, GPU)
        ▼
   后端 (halogen :8731)  ← 注入 reasoning_effort 后转发
```

## 它解决什么问题

主流 agent harness（包括 hermes-agent）对自定义 OpenAI 兼容端点**不会**发送
`reasoning_effort`——要么全开思考、要么全关，无法按任务难度调节。kev-proxy 补上
这一层：每个请求先问一次 Kev-4B「下一步需要的推理深度」，再把对应的 effort
塞给后端。

## 核心特性

- **双协议**：同时吃 `/v1/chat/completions`（Hermes）和 `/v1/responses`（Codex）。
  Codex 的 effort 字段是 `reasoning.effort`，读取时两种形状都兼容，回写时都设。
- **lease 缓存**：Kev 每次同时答「effort + 有效代数」，缓存 key 锚定最新一条
  用户消息的 hash，工具输出增长不会击穿缓存。夜间实测 ~69% 请求命中缓存，零决策开销。
- **fail-open**：Kev 挂了 / 超时 / 低置信 → 保持客户端默认 effort，绝不阻断请求。
- **系统包装剥离**：决策前剥掉 cron / skill / memory-context 等 harness 样板文本，
  让 Kev 判断真实任务而非 boilerplate（否则语义信号被稀释，置信度骤降）。
- **none 护栏**：`none`（零思考）误判代价最高，要求更高置信度，否则自动升到 `minimal`。
- **全量审计**：每个请求写 JSONL（decision / lease_hit / none_bumped / state_preview /
  applied），供事后分析 effort 分布与算力节省。

## 部署

### 1. Kev-4B 决策服务（后端）

需要一个暴露 `/v1/systemone` 的 Kev-4B 服务。本项目实测用
[dohnuts.cpp](https://github.com/DreamBlooms/dohnuts.cpp) 自编译 HIP 版
（AMD gfx120x，Q8_0 全量 GPU，~4.4 GB VRAM，决策延迟中位 ~2.8s）。
也兼容任何返回相同 schema 的 System-One 决策服务。

### 2. kev-proxy

```bash
cp .env.example .env   # 按你的拓扑改
# 直接跑
KEV_URL=http://<kev-host>:8905/v1/systemone \
BACKEND_CHAT=http://<backend>:8731/v1/chat/completions \
BACKEND_RESP=http://<backend>:8731/v1/responses \
python3 kev_proxy.py
```

### 3. systemd（Linux）

```bash
# 改 kev-proxy.service 里的 Environment= 路径与地址
cp kev-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kev-proxy
```

### 4. 注册成可选模型

**Hermes**（config.yaml 顶层 `model_aliases:`，direct alias 才能让 `-m kev-auto` 生效）：
```yaml
model_aliases:
  kev-auto:
    model: kev-auto
    provider: kev-proxy
    base_url: http://127.0.0.1:8910/v1
    api_key: kev-local
```

**Codex**（`~/.codex/config.toml` + `model_catalog.json`）：
```toml
[model_providers.kev]
name = "kev (auto effort via Kev-4B)"
base_url = "http://127.0.0.1:8910/v1"
wire_api = "responses"
requires_openai_auth = false
```

**cron 钉模型**：`hermes cron edit <id> --model kev-auto --provider kev-proxy`

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KEV_URL` | `http://192.168.8.80:8905/v1/systemone` | Kev-4B 决策服务地址 |
| `BACKEND_CHAT` | `http://192.168.8.89:8731/v1/chat/completions` | 后端 chat 端点 |
| `BACKEND_RESP` | `http://192.168.8.89:8731/v1/responses` | 后端 responses 端点 |
| `BACKEND_MODEL` | `halogen-qwen3.8-flash-next` | 转发给后端的真实模型名 |
| `KEV_DEFAULT_EFFORT` | `medium` | 无客户端 effort 时的兜底 |
| `KEV_LOG` | `~/.hermes/kev-proxy/decisions.jsonl` | 决策审计日志路径 |
| `KEV_LEASE_CAP` | `5` | lease 有效代数上限 |
| `KEV_NONE_MIN_CONF` | `0.55` | 判 `none` 所需的最低置信度 |

effort 映射遵循 halogen 契约：`minimal/low/medium/high/xhigh` + `none`
（chat template 折叠成三档：minimal,low→low；high,xhigh→xhigh）。

## 夜间实测（整夜 cron + 交互会话）

- 65 次请求：真决策 13、lease 命中 45（69% 零决策开销）、fail-open 7
- 决策延迟：中位 2.8s，最快 0.56s，GPU 忙时最慢 9.3s
- 简单任务判 minimal/none 后，后端 reasoning tokens 从默认 100+ 降到 13–39
- 改进后（剥包装 + lease 收紧 + none 护栏）：cron 任务置信度从 0.27 升到 0.66

## 已知局限

- Kev-4B 是**文本语义**决策模型，对数值状态失明；数值阈值判断应写普通代码。
- 决策模型本身有延迟（GPU 忙时排队），lease 缓存是主要缓解手段。
- 置信度整体偏低（0.3–0.68 常见），靠 fail-open + none 护栏兜底，不是硬保证。

## License

MIT
