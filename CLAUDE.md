# agentWorkCluster Development Guide

本文件适用于在仓库根目录进行跨组件改动的 AI 助手。进入某个组件后，优先遵循该组件的 `CLAUDE.md`；本文件只定义全局拓扑和同步规则。

## 所有权

| 目录 | 所有权 |
| --- | --- |
| `chat-client/` | Svelte UI、浏览器加密、同源 API/WebSocket 代理 |
| `chat-server/` | Rust 聊天 API、用户认证、Caddy 与 systemd 部署 |
| `agent-service/` | Agent/Task API、运行编排、工具治理、存储与 Worker |
| `local-agent/` | 执行机 daemon、CLI、工作区和本机模型凭据 |

普通聊天 API 与 `/ws` 归 Rust 服务；Agent、Task 和 Local Agent 控制面归 FastAPI。不要把 Agent 路由或持久化逻辑塞入 Rust 聊天服务，也不要让浏览器绕过网关直接请求内部 `9011`。

## 运行与部署

- 根目录 `start.sh`/`stop.sh`/`restart.sh` 仅用于本机联调，Rust 服务使用 `9012`，Agent API 使用 `9011`，Vite 使用 `3000`。
- 生产环境使用 `chat-server/deploy/systemd/` 与 Caddy；不要把这些 systemd 服务与根目录脚本混用。
- 没有 `REDIS_URL` 时，Agent API 使用 SQLite 与进程内执行，适合开发。生产运行需要 PostgreSQL、Redis、Alembic migration 和独立 `agent-worker`。

## 跨组件契约

新增或修改 Agent/Task/Local Agent HTTP 或 WebSocket 接口时，同步检查：

1. `agent-service/app/main.py` 的认证、存储与测试。
2. `chat-client/vite.config.js`、`src/lib/api.js` 和对应视图。
3. `chat-server/Caddyfile`、Compose/systemd 配置与部署文档。
4. 根 README 和受影响组件 README/CLAUDE。

前端和网关缺少新前缀会造成 `404`；运行创建 `500` 应查看 Agent API/Worker 日志与模型、密钥、数据库、Redis 配置，不能在前端伪造成功响应。

## 安全基线

- 不提交或输出 API Key、Fernet key、服务间密钥、Bearer token、Cookie、私钥、未脱敏提示词或工具响应。
- Agent 工具遵循最小授权：非 `GET`/`HEAD` 不能为 `read`，`write` 需要确认，`destructive` 需要逐次确认。
- Agent 存储中的敏感内容必须加密，日志和事件必须脱敏。
- Local Agent 只接受已配对设备与已注册工作区；本地路径必须限制在工作区内，状态目录和凭据文件必须保持私有权限。
- Task 必须保持上下文、预算和所有权隔离；只通过服务端定义的 Task 工具交换进度、结果与委派信息。

## 验证

按受影响组件运行测试；跨组件契约变更至少验证前端构建、Agent 单元测试和网关前缀。完整命令见根 `README.md`。修改启动脚本时，以真实健康检查、PID 和日志行为为准，不凭文档假设。
