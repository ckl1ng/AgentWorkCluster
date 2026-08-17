# Agent Service

`agent-service` 是 agentWorkCluster 的 Agent 平台后端。它提供 Agent、会话、运行、受控工具、长期记忆、评估、任务编排和 Local Agent 控制面；它不负责普通聊天的消息中继或用户登录。

## 运行位置

| 模式 | 存储与执行 | 适用场景 |
| --- | --- | --- |
| 本地开发 | SQLite；未配置 Redis 时进程内执行 | 快速联调与测试 |
| 生产 | PostgreSQL + Redis Streams/PubSub + 独立 Worker | 多进程、可靠派发与实时事件 |

API 默认监听 `9011`，仅应由开发代理或生产 Caddy 访问。用户认证通过 Rust 聊天服务的内部 introspection 接口完成，客户端必须经同源网关访问，而不是直接暴露 `9011`。

## 快速启动

从仓库根目录启动完整本机联调环境最省事：

```bash
cp chat-server/.agent.env.example chat-server/.agent.env
# 在 .agent.env 中设置 AGENT_SERVICE_SECRET 和 AGENT_MASTER_KEY
python3 -m pip install -r agent-service/requirements.txt
./start.sh
```

仅启动 API 时，先确保聊天认证服务可访问，再执行：

```bash
cd agent-service
export AGENT_SERVICE_SECRET='development-service-secret'
export AGENT_MASTER_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export AGENT_DATABASE_PATH='./data/agents.db'
export CHAT_AUTH_INTROSPECTION_URL='http://127.0.0.1:9012/internal/v1/auth/introspect'
uvicorn app.main:app --host 127.0.0.1 --port 9011
```

`AGENT_MASTER_KEY` 必须是 Fernet key；可用 `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成。不要将任一密钥、数据库口令或 Bearer token 写入文件、日志或提交。

启用 Redis 时，配置 PostgreSQL DSN 后另启 Worker：

```bash
cd agent-service
export AGENT_DATABASE_URL='postgresql://agent:password@127.0.0.1:5432/agent'
export REDIS_URL='redis://127.0.0.1:6379/0'
alembic upgrade head
python -m app.worker
```

## 配置

| 变量 | 说明 |
| --- | --- |
| `AGENT_SERVICE_SECRET` | 必填；调用聊天认证内部接口的服务间密钥 |
| `AGENT_MASTER_KEY` | 必填；Fernet 静态加密密钥 |
| `AGENT_DATABASE_URL` | 生产 PostgreSQL DSN |
| `AGENT_DATABASE_PATH` | SQLite 路径，默认 `./data/agents.db` |
| `CHAT_AUTH_INTROSPECTION_URL` | 聊天服务认证接口，默认 `http://127.0.0.1:9010/internal/v1/auth/introspect` |
| `REDIS_URL` | 运行与任务派发、跨进程事件；为空时使用开发退化路径 |
| `AGENT_ALLOW_HTTP` | 是否允许模型和工具使用 HTTP；生产环境保持 `false` |
| `AGENT_TOOL_RESPONSE_LIMIT` | 单次工具响应上限，默认 1 MiB |
| `AMAP_WEATHER_API_KEY` | 内置高德天气工具的服务端密钥；未设置时该工具会明确报配置缺失，密钥不会写入数据库或返回给模型 |

生产环境的 schema 由 Alembic 管理。SQLite 仅用于开发和测试；不要把本地 SQLite 文件直接当作生产迁移方案。

## 能力与边界

- `POST /api/v1/agent-conversations/{id}/runs` 创建不可变运行快照；Worker 调用模型、执行已授权工具，并把脱敏事件推送至 `/agent/ws`。
- 工具按 `read`、`write`、`destructive` 分级。非 `GET`/`HEAD` 不能是 `read`；写操作需确认，破坏性操作逐次确认。
- 内置工具目录包含高德天气查询（`amap_weather`）；为 Agent 授权该工具即可按城市 adcode 查询，服务端从 `AMAP_WEATHER_API_KEY` 读取密钥，无需导入或配置密钥。
- Task API 与 `/task/ws` 提供预算受限、上下文隔离的多 Agent 委派；Task run 不能绕过服务端定义的任务工具。
- `/api/v1/local-agent*` 与 `/local-agent/ws` 用于设备配对、工作区同步和 `local_direct` 派发。Web 可分别创建云端 Agent 与 AWC Agent：AWC Agent 必须绑定已通过 WebSocket 在线的 CLI，模型/API Key/profile 仅保存在 CLI；Web 与 QQ Gateway 都经后端把消息转发至该连接。CLI 离线时后端拒绝创建新的 AWC run。当前本地 daemon 默认执行文本模型运行，另支持 `codex` 执行器（把运行委托给本机 Codex 外部 CLI agent，黑盒、工作区内、内部工具不受平台治理）；不支持本机文件/进程工具、工具确认或断线恢复。

敏感模型配置、消息、运行结果和审计原文均以 `AGENT_MASTER_KEY` 加密存储；对外 trace、日志与错误必须使用脱敏载荷，不能暴露提示词、密钥或原始工具响应。

## 结构

```text
app/main.py               FastAPI 路由、存储、编排、WebSocket
app/harness.py            模型流、工具执行、SSRF 防护与上下文准备
app/safety.py             脱敏、审计和公网地址校验
app/worker.py             Redis Streams Worker
app/state_machine.py      Run 状态机
app/task_state_machine.py Task 状态机
migrations/               PostgreSQL Alembic 迁移
tests/                    确定性回归测试
evaluation/               基线评估用例
```

## 验证

```bash
cd agent-service
python -m unittest discover -s tests -p 'test_*.py' -v
curl --fail http://127.0.0.1:9011/healthz
```

修改 API 路由、WebSocket、Local Agent 或 Task 契约时，同步检查 `../chat-client/vite.config.js`、`../chat-server/Caddyfile`、前端调用与根目录文档。部署说明见 `../chat-server/deploy/systemd/README.md`。
