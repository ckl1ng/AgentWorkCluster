# CLAUDE.md

本文件为在 `chat-client` 中工作的 AI 助手提供约束和常用命令。

## 项目概述

这是一个 Svelte 4 + Vite 5 单页聊天客户端。普通聊天使用端到端加密；Agent 工作区通过同源 `/api/v1` 调用独立 Agent 服务。保持界面为实际可操作的聊天和管理工作区，不要改造成营销页。

## 关键目录

```text
src/lib/api.js                   REST 封装、Bearer token、错误格式化
src/lib/ws.js                    聊天 WebSocket
src/lib/agent-ws.js              Agent 运行事件 WebSocket
src/views/TaskPanel.svelte       Task 树、通知、指派和验收工作台
src/views/AgentChat.svelte       Agent 会话与运行
src/views/AgentSettings.svelte   Agent 基础配置
src/views/AgentGovernance.svelte 工具、候选导入和长期记忆
src/views/AgentRuns.svelte       运行记录与评估对比
src/views/HelpCenter.svelte      应用内帮助中心（聊天、Agent、Local Agent CLI）
```

## API 与代理

浏览器始终请求相对路径 `/api/v1`。`vite.config.js` 必须让更具体的 Agent 前缀优先代理到 `AGENT_SERVER_URL`（默认 `http://127.0.0.1:9011`）：`agents`、`agent-conversations`、`agent-runs`、`tools`、`evaluations`、`local-agent`、`tasks`、`task-dispatch-events`、`notifications`，以及 `/agent/ws`、`/task/ws`。其余 `/api` 与 `/ws` 仍走 `CHAT_SERVER_URL`（默认 `http://127.0.0.1:9010`）。

新增 Agent API 时，同时更新服务端 `Caddyfile`、客户端 Vite 代理、`src/lib/api.js` 和相关 README。远端返回阶段 B 接口 `404` 表示部署版本或网关不一致，不能用前端假数据掩盖；运行创建 `500` 应保留服务端错误正文以便排查。

Local Agent 控制面使用 `/api/v1/local-agent*` 和 `/local-agent/ws*`。网页的 `AgentSettings.svelte` 已可批准设备配对、选择已同步工作区并绑定 `server_proxy` 或 `local_direct`；`AgentRuns.svelte` 已显示本机派发状态。当前只有 `local_direct` 会被服务端派发到 daemon，不能把 `server_proxy` 描述为已可本机运行。生产文档应将 CLI 部署在执行机、连接对外网关，并记录两个控制面前缀；不得建议暴露内部 `9011`。不要把已上线的配对、选择和绑定界面描述为“未实现”。实际 CLI 参数以 `../local-agent/src/cli.js` 为准，帮助中心必须同时说明本地 `ws_...` 工作区 ID 与网页远端工作区 ID 不可互换。

TaskPanel 通过 `/task/ws` 接收调度变化，并以 REST 刷新完整详情。不要将工作内容、模型隐藏推理、密钥或绝对路径放进调度摘要、通知或桌面通知；Task 的隔离、预算和所有权由服务端强制，前端不能用假数据或本地状态绕过这些限制。

`api.js` 必须保留 `error.status` 与 `error.data`。FastAPI 422 的 `detail` 数组要显示字段路径和原因，不要显示 `[object Object]`。

## Agent 安全边界

- Agent 工具必须先创建并显式分配给 Agent。
- 非 `GET`/`HEAD` HTTP 工具不可标记为 `read`；`write` 必须要求确认；`destructive` 必须逐次确认。
- MCP 工具需要 URL 和远程工具名。输入 Schema、Headers、参数位置均为 JSON 对象，应在提交前校验。
- 不显示模型隐藏推理；工具响应、trace 和错误信息按服务端脱敏内容呈现。
- 不在客户端代码、日志、帮助文本或提交中写入 API Key、Bearer token、Cookie 或私钥。
- Local Agent 当前只执行文本模型运行，不支持本机文件/进程工具、工具确认或断线恢复。帮助内容必须明确此限制，且不得把本机 API Key 或绝对路径写入示例。

## 验证与提交

```bash
npm run build
npm test
```

修改 Agent 交互、API 契约或帮助内容时，也检查 `README.md` 是否仍与服务端部署方式一致。涉及 Local Agent 的文档更新还要同步检查 `../chat-server/README.md`、`../chat-server/CLAUDE.md` 与 CLI usage。提交前运行构建和测试；使用 SSH 配置的 Git 远端推送。
