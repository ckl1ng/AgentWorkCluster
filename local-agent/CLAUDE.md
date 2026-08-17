# Local Agent Development Guide

本文件约束在 `local-agent/` 中工作的 AI 助手。该目录是 Node.js ESM daemon/CLI，部署位置是用户的执行电脑，不是云端 Agent API 容器。

## 入口与状态

- `bin/local-agent.js`：可执行入口。
- `src/cli.js`：命令解析；CLI usage 必须与此处保持一致。
- `src/daemon.js`：单实例 daemon、IPC、运行调度和远程派发。
- `src/transport.js`：与 Agent API 的 WebSocket 协议和租约。
- `src/workspace.js`：工作区注册、路径边界和远端 ID 映射。
- `src/model-store.js`：本机模型密钥的 AES-256-GCM 加密存储。
- `src/sanitizer.js`、`src/permissions.js`：输出脱敏与 0700/0600 权限门禁。

默认状态目录为 `~/.local-agent`。不要在测试、日志、错误或文档中输出其中的凭据、模型 API Key、绝对工作区路径或 refresh token。

## 不可破坏的安全契约

- 所有工作区内路径都要经 `resolveWorkspacePath` 校验，拒绝 traversal、符号链接逃逸及 special file。
- 凭据文件和状态目录权限不足时必须失败，不能“兼容”宽松权限。
- 模型 API key 只经环境变量进入 daemon，持久化时必须加密；`status`、`model list` 和远程事件不得回显密钥。
- 输出与事件经过 sanitizer；新增流式/错误路径也必须覆盖。
- WebSocket 使用短时 device access token，远程执行仅接受已配对、已注册工作区的有效租约。

## 产品边界

`local_direct` 才会被服务器派发给 daemon。`server_proxy` 不是本机运行模式。执行器（`executor_kind`）在 Agent 绑定处选择：`model` 运行内置本地文本模型，`codex` 把运行委托给本机 Codex 外部 CLI agent（黑盒、工作区内、内部工具不受平台治理）。除 `codex` 外，不得实现或宣传本机文件/进程工具、工具确认或断线恢复，除非服务端契约和安全模型一并扩展。

本地 `ws_...` ID 与网页的远端工作区 ID 是两套标识：CLI 发起 `run` 使用本地 ID；远程派发由 daemon 通过 `remote_id` 映射回来，不能混用。

## 常用命令

```bash
npm test
node bin/local-agent.js daemon
node bin/local-agent.js auth login --api https://gateway.example
node bin/local-agent.js status
```

涉及 CLI usage、配对、工作区注册、模型登记或传输协议的变更，还必须同步更新：

- `README.md`
- `../agent-service/app/main.py` 和相关测试
- `../chat-client/src/views/HelpCenter.svelte`
- `../chat-server/Caddyfile`、`../chat-server/README.md` 与 `../chat-server/CLAUDE.md`

每次修改后运行 `npm test`。协议变更还要覆盖断线、无效凭据、租约过期和远端工作区未映射的测试。
