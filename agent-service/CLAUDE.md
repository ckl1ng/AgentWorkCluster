# CLAUDE.md

本文件为 AI 助手（Claude 等）在 `agent-service/`（Chat Agent 平台后端）中工作时提供指导。它是 `chat-server/` 根目录 CLAUDE.md 的补充：根文档描述 Rust 聊天服务与整体部署，本文档聚焦 Agent 平台的服务端实现。

## 项目概述

`agent-service/` 是聊天服务器的 Agent 平台后端：用户创建配置了模型与工具的 Agent，向 Agent 发起"运行（Run）"，服务端编排模型调用与工具执行，把结果、轨迹、确认请求实时推送给前端。生产环境由 **FastAPI API + PostgreSQL + Redis（Streams / Pub/Sub）+ 独立 Worker** 构成，容器内监听内部端口 **9011**，经网关 Caddy 代理对外。

本目录的运行、配置和验证入口见 `README.md`；跨组件端口、网关和启动顺序见仓库根目录 `../README.md` 与 `../CLAUDE.md`。实现约束、数据安全和迁移规则以本文件为准。

Agent 平台是分阶段交付的（Phase A → Phase B → 本地 Agent → Task 集群编排），每个阶段都对应独立的数据库迁移与测试门禁。设计基线记录在父仓库的 `docs/agent-platform-design.md`；对能力做结构性修改时，需同步检查 `app/main.py`、数据库迁移与存储层、网关 `Caddyfile`、前端 API 代理，不能只改一个服务。

### 技术栈

- **Python 3.12** + **FastAPI 0.115**（uvicorn，端口 9011）
- **PostgreSQL**（生产，schema 归 Alembic 管理）+ **SQLite**（开发/测试，schema 由 `AgentStore._migrate()` 自建）
- **Redis**：Streams（`agent-runs` 运行派发、`task-events` 任务派发）+ Pub/Sub（`agent-events` / `task-events` 实时转发）
- **Alembic** 迁移；**psycopg** / `sqlite3` 直连（无 ORM，`app/db.py` 提供统一 DB-API 表面）
- **cryptography.Fernet**：敏感内容静态加密
- **httpx / httpcore**：模型流式调用、HTTP/MCP 工具调用（含自定义 SSRF 防护网络后端）
- **jsonschema**（Draft 202012）：工具参数校验

## 目录结构

```
agent-service/
├── Dockerfile                 # alembic upgrade head && uvicorn app.main:app --port 9011
├── alembic.ini / migrations/  # 5 个迁移（Phase A / Phase B / Local Agent / Task）
├── app/
│   ├── main.py                # 核心：FastAPI 路由 + WebSocket + AgentStore + 运行编排（~4400 行）
│   ├── harness.py             # 模型 harness：上下文预算、工具声明、流式解析、工具执行、SSRF 网络后端
│   ├── safety.py              # 共享边界检查：脱敏、审计载荷、公网 URL/DNS 校验、响应摘要
│   ├── web_search_mcp.py      # 零依赖的 STDIO MCP server（内置 search_web / read_url）
│   ├── worker.py              # Redis Streams 消费 Worker（agent-runs → orchestrate_run）
│   ├── db.py                  # SQLite/PostgreSQL 兼容层（Connection / Row / Cursor）
│   ├── state_machine.py       # Agent Run 状态机
│   ├── task_state_machine.py  # Task 生命周期状态机
│   └── evaluation.py          # 确定性评估契约（断言、发布门禁，无 LLM 裁判）
├── tests/                     # unittest 回归门禁（见"测试"）
│   └── fixtures/              # test_model_service.py（确定性模型）、tool_simulator.py
├── evaluation/baseline_cases.json  # 20 个基线评估用例
└── scripts/migrate_sqlite_to_postgres.py  # 一次性非破坏迁移脚本
```

## 核心数据流

### 运行（Run）生命周期

1. **创建**：`POST /api/v1/agent-conversations/{id}/runs` → `AgentStore.create_run`：
   - 校验 Agent 状态、并发上限（`run_policy.max_concurrent_runs`）、日/月 token 预算；
   - 写入 `runs`（`queued`）+ 用户消息（加密）；
   - **`_freeze_run`**：把 Agent 配置、解密后的系统提示词、工具（含解密 config）打包成不可变快照存 `run_snapshots`（Task 运行还会附加任务上下文与任务系统提示词）；
   - `execution_target=local` → 写 `local_run_dispatches`（不写 outbox）；否则写 outbox 事件 `agent.run.queued`。
2. **派发（outbox 模式）**：`outbox_publisher_loop` 定期把未发布事件推入 Redis Stream `agent-runs`（Redis 不可用时运行保持 `queued`，由 outbox 重试，绝不误报终态失败）；无 Redis 的开发/测试环境则直接 `asyncio.create_task(orchestrate_run(...))`。
3. **执行**：Worker（`worker.py`）消费 `agent-runs` → `main.orchestrate_run`：
   - `try_start_run`（attempt+1，崩溃恢复用 `recover=True`）→ `sync_task_run_state("running")`；
   - `prepare_context` 按预算组装上下文 → 流式调用模型 `/chat/completions`；
   - 模型返回工具调用时逐个执行：写/破坏性工具先走确认门禁（见下），只读工具直接执行并限频；
   - 结果落 `trace_events`（加密 payload + 脱敏 redacted_payload）、`final_content_encrypted`、`usage`，最后 `completed`。
4. **确认恢复**：用户批准工具确认后，outbox 写入 `agent.run.confirmed`（`resume_confirmation=True`）→ Worker 以 `resume_confirmation=True` 编排，从确认记录里的 checkpoint（消息、assistant 工具调用、工具）断点续跑。

### 实时事件

- 运行事件写入 `trace_events`（每 run 递增 `sequence`，加密 payload 审计用 `redacted_payload`）。
- `/agent/ws`：`agent.subscribe {run_id, after_sequence}` → 先回放已落库事件，再订阅内存 `EventHub`；多进程部署时 `redis_event_relay` 把 Pub/Sub 消息转回本地 hub。**回放来自 Postgres，Pub/Sub 只是低延迟通道**，丢消息不丢数据。
- `/task/ws`：`task.subscribe` / `task.subscribe_all`，任务派发事件（短调度摘要，无工作内容）经 `TaskEventHub` + Redis 转发；`subscribe_all` 只向本 owner 投递。
- `/local-agent/ws`：本地 daemon 通道，设备 access token 认证，协议 v1 信封（`hello` / `run.claim` / `lease.renew` / `run.event` / `run.finish`），租约 90 秒。

### 认证

- **用户**：`authenticate()` 调用 Rust 聊天服务的内部接口 `CHAT_AUTH_INTROSPECTION_URL`（默认 `http://127.0.0.1:9010/internal/v1/auth/introspect`），携带 `Authorization: Service <AGENT_SERVICE_SECRET>`，验证用户的 `Bearer <user_token>`。
- **本地设备**：配对后设备持有一次性 refresh 凭据 → `issue_device_access_token` 签发 HMAC-SHA256 签名的短时 access token（600s），`authenticate_device_access_token` 验签。

## 关键设计决策

### 1. 存储抽象（无 ORM）

`app/db.py` 的 `Connection` 用一套保守的查询表面同时覆盖 SQLite（开发/测试）与 PostgreSQL（生产）：命名参数 `:name` 在 PG 下转成 `%(name)s`，`?` 转成 `%s`；返回的 `Row` 同时支持整数下标与列名取值。

- **生产**：schema 归 `migrations/` 的 Alembic 管理，`AgentStore._migrate()` 只做 `SELECT 1 FROM agents` 启动自检。
- **开发/测试**：`_migrate()` 用 `executescript` 建全表 + `_ensure_column` 增量补列 + `_encrypt_legacy_plaintext` 一次性把旧明文原地加密。
- **注意**：加列/加表必须**同步**迁移文件、`_migrate` 脚本、`_ensure_column` 列表三处。

### 2. 静态加密与脱敏

- 所有敏感内容（系统提示词、消息、run 终稿、trace payload、工具 config、模型密钥、任务目标/结果、记忆）用 `AGENT_MASTER_KEY` 的 Fernet 加密落库；明文列始终为空（迁移里有 `CHECK (xxx = '')` 约束）。
- 写库一律走 `_encrypt_text` / `_encrypt_json`，读库走 `_decrypt_text` / `_decrypt_json`；不要在存储层出现明文。
- trace 的审计列 `redacted_payload` 由 `safety.audit_payload` 生成：只保留长度/用量/摘要，绝不复制模型/用户明文。
- `redact()` 递归脱敏 `authorization/cookie/api_key/token/password/secret` 等键与 URL 查询参数；`response_summary` 只保留响应前 4 KiB 并脱敏。**任何日志、事件、摘要输出前都必须经过这两个函数。**

### 3. 工具治理（Phase B 门禁）

- 工具五类 `kind`：`http` / `openapi` / `mcp`（Streamable-HTTP）/ `mcp_stdio` / `local`；外加内建的 `task` 工具（见 Task 域）。
- `side_effect`（read/write/destructive）+ `confirmation_mode`（none/per_run/per_call）+ `rate_limit_per_run`。`validate_tool_payload` 强制：非 GET/HEAD 的 HTTP 操作不能标 read；`write` 必须要求确认；`destructive` 必须 `per_call`。
- `harness.tool_declarations` 会把"缺 Phase B 确认门禁的写工具"从模型声明中剔除（防御遗留/畸形配置）。
- **确认门禁（per_run 按 per_call 从严）**：写/破坏性工具（或 `confirmation_mode != none`）在 `orchestrate_run` 中创建 `tool_confirmations`（`arguments_hash` = sha256(sort(arguments))，参数与 checkpoint 加密），run 转入 `waiting_confirmation`；批准时校验 hash 未被篡改，拒绝则 run 取消。
- **SSRF 纵深防御**（`safety.py` + `harness.py`）：
  1. URL scheme/主机名校验（默认只允许 HTTPS，禁 localhost/.local）；
  2. 连接前 DNS 解析，任一结果非公网即拒绝（`assert_safe_public_url`）；
  3. `SafeNetworkBackend` 只连接已校验的 IP（禁 Unix socket、禁重定向）；
  4. 连接后 `assert_public_peer` 复核实际 peer，阻断 DNS rebinding。

### 4. 上下文预算（phase-a-sliding-window-v1）

`prepare_context` 把系统提示词、运行时状态（权威 JSON：剩余工具调用/输出 token、约束）、授权记忆、历史消息装入一个 input 预算内：按 `context_window - max_output_tokens - tool_tokens` 计算预算，从最新消息往前保留、丢弃旧的，必要时截断系统提示词。token 估算用 tokenizer 无关的 UTF-8 长度启发式（`estimate_tokens`）。每次运行把 context manifest 加密存 `runs.context_manifest_encrypted` 供审计。

### 5. 长期记忆（可解释、不静默覆盖）

记忆按 `kind`（preference/profile/constraint/fact/experience）区分；同 kind 出现不同值时不覆盖旧值，而是把新值标 `conflicted` 供用户手动清理，同 kind 完全重复则拒绝。检索是无 embedding 的关键词重叠 + `importance` 打分（`embedding` 列保留但当前为 `[]`）。只有 `memory_enabled` 的 Agent 可用。

### 6. Task 域（多 Agent 集群编排，严格隔离）

Task 是归属用户、有明确提出者（`proposer_kind`: user/agent）的工作单元，生命周期见 `task_state_machine.py`（draft → queued → assigned → in_progress → waiting_confirmation → awaiting_proposer_close → attention_required → closed/cancelled）。关键规则：

- **隔离**：每个 Task 有独立 `context_scope_id`，只有本 Task 授权上下文可见；快照系统提示词明示"Different tasks are strictly isolated"。子任务上下文只含显式 handoff 输入包，不含父任务内容。
- **执行者只通过服务端强制工具行动**：`TASK_TOOL_DEFINITIONS`（post_progress / submit_result / request_proposer_decision / accept_assignment / decline_assignment / delegate_task / collect_child_result / close_delegated_task）由 `execute_task_tool` 校验当前 Assignment 有效性后路由，模型不能绕过。
- **预算**：每 Task 快照 `budget_snapshot`（max_total_tokens / max_tool_calls / max_concurrent_runs / max_depth / max_subtasks，见 `TASK_BUDGET_LIMITS`）。`_task_budget_status` 聚合 runs usage 与 tool_invocations 计数；用尽时 Task 转 `attention_required` 并通知提出者。
- **委派**：`delegate_task` 检查深度/子任务数/并行上限，并要求子任务预算 ≤ 父任务可分配额度；创建隔离子 Task + `task_handoffs` + 新 run。
- **所有权**：只有提出者可关闭/取消（`_require_task_proposer`）；run 失败/取消只把 Task 推到 `attention_required`，**永不自动关闭**。
- 派发事件是"短调度摘要"（`redact(...)[:280]`），绝不含工作对话/代码/密钥/绝对路径；`task_*` 命令支持 `idempotency_key` 去重（`task_command_deduplications`）。

### 7. 本地 Agent（Local Agent 控制面）

- `execution_target=cloud|local`；本地绑定时 `model_mode=server_proxy`（服务端持有密钥代跑）或 `local_direct`（模型密钥只留本机 daemon，服务端快照**不含** API key，绑定前必须已登记本地模型且该 Agent 从未提交服务端密钥）。
- 配对流程：`start_pairing` 生成一次性 pairing secret + 6 位码（`LA-xxxxxx`）→ 用户 `approve_pairing` → daemon `claim_pairing` 换取设备凭据。
- 派发：run 创建即写 `local_run_dispatches`（pending）→ daemon 经 WebSocket `offer_local_run` 拿 90s 租约 → `claim_local_run`（原子地把 run 置 running）→ 周期 `lease.renew` → `run.event` 增量上报 → `run.finish`。
- **当前限制**（不可描述为生产远程执行）：Local Agent 只执行文本模型运行；不支持本机文件/进程工具、终端确认、断线恢复。

### 8. 评估与发布门禁

`app/evaluation.py` 无模型依赖、无 LLM 裁判：`evaluate_assertions` 只做确定性断言（output_contains / output_not_contains / state / tool_name / error_category / confirmation_required / context_last_message）。`release_gate` 要求每个基线用例通过、任何 safety 用例失败即阻断、p95 延迟 ≤ 5000ms。基线用例在 `evaluation/baseline_cases.json`（20 个），测试用 `fixtures/test_model_service.py`（SSE 确定性模型）+ `fixtures/tool_simulator.py`（无网络/文件 IO）。

## 环境变量

| 变量 | 用途 |
|---|---|
| `AGENT_MASTER_KEY` | **必填**。Fernet 加密密钥（存密钥/内容静态加密） |
| `AGENT_SERVICE_SECRET` | **必填**。服务间认证（调用聊天认证接口的 `Service` 头） |
| `AGENT_DATABASE_URL` | 生产 Postgres DSN；未设时回落到 `PGHOST/PGUSER/...` |
| `AGENT_DATABASE_PATH` | 开发/测试 SQLite 路径（默认 `./data/agents.db`） |
| `CHAT_AUTH_INTROSPECTION_URL` | 认证 introspection 内部接口（默认 `http://127.0.0.1:9010/internal/v1/auth/introspect`） |
| `REDIS_URL` | 派发 Streams + 实时 Pub/Sub；为空时退化为进程内编排 |
| `AGENT_ALLOW_HTTP` | 工具/模型 URL 是否允许 http（默认 false） |
| `AGENT_TOOL_RESPONSE_LIMIT` | 工具响应字节上限（默认 1 MiB） |
| `AMAP_WEATHER_API_KEY` | 内置高德天气查询的服务端密钥；仅在出站请求时读取，绝不入库、记录或回传 |

## 常用任务

### 添加新的 REST 端点

1. 在 `app/main.py` 定义 Pydantic payload（`*Payload`），加 `@app.get/post/...` 处理器；
2. 处理器开头 `user = await authenticated_user(authorization)`（或 `authenticated_device`）；
3. 逻辑放在 `AgentStore` 方法里（存储抽象之上），HTTP 层只做 payload 校验与 4xx 映射；
4. 若涉及派发/实时，复用 `enqueue_run` / `emit` / outbox。

### 添加新的数据库表 / 列

1. 生产：新增 `migrations/versions/<date>_<seq>_<name>.py` 迁移；
2. 本地 SQLite：在 `AgentStore._migrate()` 的 `executescript` 中同步建表，新增列加入 `_ensure_column` 列表；若含新明文遗留数据，扩展 `_encrypt_legacy_plaintext`；
3. 更新 `scripts/migrate_sqlite_to_postgres.py` 的 `TABLES`（如需迁移）；
4. 为存储方法补测试。

### 修改工具治理

改任何一条治理规则都需四件套同步：`validate_tool_payload`（创建校验）、`harness.tool_declarations`（模型声明过滤）、`orchestrate_run` 的确认/限频/执行分支、`test_phase_b_governance.py` 与 `test_safety.py` 对应断言。保持最小授权：非 GET/HEAD 不能标 read；write 必须确认；destructive 必须 per_call。

### 测试（Agent 平台回归门禁）

```bash
cd /home/zhouzw/agentWorkCluster/agent-service
python -m unittest discover -s tests -p 'test_*.py' -v
```

测试全部走临时 SQLite + `Fernet.generate_key()`，不依赖 Redis/Postgres/模型。

### 运行

```bash
# API（开发）
cd /home/zhouzw/agentWorkCluster/agent-service
AGENT_SERVICE_SECRET=... AGENT_MASTER_KEY=... uvicorn app.main:app --host 0.0.0.0 --port 9011
# 或容器：alembic upgrade head && uvicorn app.main:app

# Worker（独立进程，消费 agent-runs）
REDIS_URL=redis://... python -m app.worker
```

## 注意事项与坑

- **明文禁区**：任何敏感列的明文版本必须保持为空；只在 `redacted_payload`/审计摘要里出现脱敏后的信息。
- **生产 schema 归属 Alembic**：不要在 Postgres 上直接执行 `_migrate()` 的 DDL。
- **run 状态机 vs Task 状态机是两套**：`app/state_machine.py`（run）与 `app/task_state_machine.py`（task）分别 `require_transition`，同一状态重复请求是安全的。
- **outbox 是派发唯一入口**：run 创建后不经 outbox 就不会被 Worker 拾取；Redis 故障时运行必须保持 `queued`，不能误判终态。
- **本地 Agent 文档一致性**：修改 CLI/设备/工作区 API 或本地派发时，同步更新 `chat-client/src/views/HelpCenter.svelte`、两个仓库的 README 与本文件；不得把未上线的 `server_proxy` 描述成可本机运行。
- **模型密钥与绝对路径**：绝不记录、回显或提交模型 API Key、Authorization 头、Cookie、提示词原文或未脱敏工具响应。
