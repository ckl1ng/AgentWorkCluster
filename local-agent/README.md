# AWC CLI / Local Agent

`@awc/cli`（命令 `awc`）是运行在任务执行电脑上的独立 Node.js daemon 和 CLI。它可以完全脱机配置模型、工具和工作区并执行本地任务；连接 AWC 后端后，再通过出站 WebSocket 接收远程任务和回传事件。`local-agent` 命令仍作为兼容别名保留。

它不是 Agent API 的替代品，也不应部署进 `agent-service` 容器。生产环境应让 daemon 访问对外网关，由网关转发 `/api/v1/local-agent*` 和 `/local-agent/ws*`；不要向执行机公开内部 `9011` 端口。

## 前置条件

- Node.js 18+
- 已运行的 daemon（同一用户只能有一个实例）

```bash
npm install -g @awc/cli
awc init
awc daemon
```

不连接后端也可以使用：

```bash
awc workspace add /path/to/workspace --name work
export LOCAL_AGENT_MODEL_API_KEY='set-in-your-shell-only'
awc profile set default --base-url https://model.example/v1 --model-id model-name
awc run '总结当前变更' --workspace ws_xxx --profile default
```

默认状态目录为 `~/.local-agent`，其中的设备凭据、模型配置、工作区注册表和运行日志均要求私有权限。不要移动、共享或提交这些文件。

## 配对与配置

在另一个终端启动配对，生产环境传入网关地址：

```bash
awc connect pair --api https://chat.example.com
```

CLI 会打印配对会话和六码。登录网页后批准该设备；daemon 建立 WebSocket 后，Web 创建 AWC Agent 时只能选择在线 CLI 及其已同步工作区。Web 与 QQ Bot 的消息都由后端通过该连接派发：

```bash
awc workspace add /path/to/workspace --name work
awc workspace list
```

这里的本地工作区 ID 是 `ws_...`，CLI 与 Web 绑定均使用它的同步映射；后端不会保存绝对路径。

为独立 profile 设置本机模型。密钥仅从环境变量读取，daemon 以本机 state key 加密保存；AWC Agent 的 profile 不会把模型地址或密钥同步到服务端。

```bash
export LOCAL_AGENT_MODEL_API_KEY='set-in-your-shell-only'
awc profile set AGENT_ID \
  --base-url https://model.example/v1 \
  --model-id model-name
awc profile list
```

在网页完成 Agent 与已同步设备/工作区的 `local_direct` 绑定后，daemon 会接收远程运行。`server_proxy` 是控制面可选项，但当前版本不会由此 daemon 执行。

## CLI

```text
awc daemon
awc status
awc connect pair [--api URL]
local-agent workspace add PATH [--name NAME]
local-agent workspace list
awc profile set NAME --base-url URL --model-id ID [--api-key-env NAME]
awc profile list
awc profile remove NAME
awc run PROMPT --workspace ws_... [--profile NAME]
local-agent run list
local-agent run events RUN_ID
local-agent run attach RUN_ID
```

一次终端运行示例：

```bash
awc run '总结当前变更' --workspace ws_xxx --profile AGENT_ID
awc run attach run_xxx
```

## 当前限制

- 只支持文本模型运行。
- 不支持本机文件或进程工具。
- 不支持工具确认和断线恢复。
- 运行事件、输出和日志会进行密钥与路径脱敏，但仍应避免在提示词中输入不必要的敏感信息。

## 开发与测试

```bash
cd local-agent
npm test
```

协议、存储或 CLI 参数的变动需要同步更新 `../agent-service` 的 Local Agent API、`../chat-client/src/views/HelpCenter.svelte`、网关配置及各组件文档。
