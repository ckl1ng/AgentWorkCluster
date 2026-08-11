# Local Agent

`local-agent` 是运行在任务执行电脑上的 Node.js daemon 和 CLI。它把已经配对的设备连接到 Agent 平台，登记可执行工作区和本机模型，并执行服务端派发的 `local_direct` 文本模型运行。

它不是 Agent API 的替代品，也不应部署进 `agent-service` 容器。生产环境应让 daemon 访问对外网关，由网关转发 `/api/v1/local-agent*` 和 `/local-agent/ws*`；不要向执行机公开内部 `9011` 端口。

## 前置条件

- Node.js 18+
- 一台可访问平台网关的执行机
- 已运行的 daemon（同一用户只能有一个实例）
- 网页中已有可绑定的 Agent

```bash
cd local-agent
npm ci
node bin/local-agent.js daemon
```

默认状态目录为 `~/.local-agent`，其中的设备凭据、模型配置、工作区注册表和运行日志均要求私有权限。不要移动、共享或提交这些文件。

## 配对与配置

在另一个终端启动配对，生产环境传入网关地址：

```bash
node bin/local-agent.js auth login --api https://chat.example.com
```

CLI 会打印配对会话和六码。登录网页后，到 Agent 的“配置 -> 本地执行”批准该设备。批准完成后，添加一个本地工作区，CLI 会将其同步到控制面：

```bash
node bin/local-agent.js workspace add /path/to/workspace --name work
node bin/local-agent.js workspace list
```

这里的本地工作区 ID 是 `ws_...`，只能给 CLI 使用；网页显示的远端工作区 ID 不能替换它。

为 Agent 设置本机模型。密钥仅从环境变量读取，daemon 以设备凭据派生的密钥加密保存，服务端只登记模型地址和模型 ID。

```bash
export LOCAL_AGENT_MODEL_API_KEY='set-in-your-shell-only'
node bin/local-agent.js model set AGENT_ID \
  --base-url https://model.example/v1 \
  --model-id model-name
node bin/local-agent.js model list
```

在网页完成 Agent 与已同步设备/工作区的 `local_direct` 绑定后，daemon 会接收远程运行。`server_proxy` 是控制面可选项，但当前版本不会由此 daemon 执行。

## CLI

```text
local-agent daemon
local-agent status
local-agent auth login [--api URL]
local-agent workspace add PATH [--name NAME]
local-agent workspace list
local-agent model set AGENT_ID --base-url URL --model-id ID [--api-key-env NAME]
local-agent model list
local-agent model remove AGENT_ID
local-agent run PROMPT --workspace ws_... [--agent AGENT_ID]
local-agent run list
local-agent run events RUN_ID
local-agent run attach RUN_ID
```

一次终端运行示例：

```bash
node bin/local-agent.js run '总结当前变更' --workspace ws_xxx --agent AGENT_ID
node bin/local-agent.js run attach run_xxx
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
