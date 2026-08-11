# Chat Client

端到端加密（E2EE）聊天前端 — 基于 **Svelte 4 + Vite 5 + tweetnacl**，配套 [chat-server](https://github.com/ckl1ng/chat-server) 使用。

![version](https://img.shields.io/badge/version-0.1.0-blue)
![svelte](https://img.shields.io/badge/svelte-4.x-orange)
![license](https://img.shields.io/badge/license-private-red)

---

## 功能

- **零知识注册/登录** — 客户端生成 Curve25519 密钥对，私钥不出设备
- **端到端加密私聊** — nacl.box 非对称加密，WebSocket 实时投递
- **端到端加密群聊** — 创建群组、对称密钥分发、群消息 secretbox 加解密
- **在线状态感知** — 实时显示用户在线/离线
- **消息历史** — REST API 游标分页加载
- **投递确认** — 已发送 / 已投递 状态回执
- **Agent 工作区** — 创建和配置 Agent、发起可追踪运行、管理受控工具与长期记忆、查看运行记录和评估对比
- **Task 工作台** — 创建、指派和验收预算受限的任务；查看子任务、执行记录、确认请求和通知
- **应用内帮助** — 从帮助入口查看聊天、云端 Agent、工具治理、运行工作台，以及 Local Agent CLI 的完整操作说明
- **暗色主题** — 全应用 CSS Variables 暗色主题，移动端响应式

## 快速启动

### 环境要求

- Node.js 18+
- npm 9+（或 pnpm）

### 启动

```bash
# 1. 安装依赖
npm install

# 2. 启动开发服务器
npm run dev
```

浏览器访问 **http://localhost:3000**。

> 开发服务器内置代理默认指向聊天 API `/api` → `http://127.0.0.1:9010`，`/ws` → `ws://127.0.0.1:9010`；Agent API 的 `/api/v1/agents*`、`/agent-conversations*`、`/agent-runs*`、`/tools*`、`/evaluations*`、`/local-agent*` 及 `/agent/ws` → `http://127.0.0.1:9011`。
> 可通过 `CHAT_SERVER_URL=http://host:port npm run dev` 覆盖服务端地址。
>
> 请确保 [chat-server](https://github.com/ckl1ng/chat-server) 已在对应端口运行。

### 后台启动

默认将聊天 API 和 Agent API 分别代理到本机的 `9010` 与 `9011`：

```bash
cp .frontend.env.example .frontend.env  # 可选：修改代理地址或端口
./start.sh

# 停止由脚本启动的 Vite 服务
./stop.sh
```

启动日志和 PID 位于 `.runtime/`。脚本会在 `3000` 已被占用时退出，避免误杀其他进程。

### 命令

| 命令 | 说明 |
|------|------|
| `npm run dev` | 启动开发服务器 (localhost:3000) |
| `npm run build` | 生产构建 → `dist/` |
| `npm run preview` | 预览生产构建 |
| `npm test` | 运行前端单元测试 |

## Agent 工作区

在侧边栏创建或选择 Agent 后，可从配置页完成模型连接、运行策略、工具和记忆管理；运行记录页面可按状态查看历史运行、脱敏轨迹、工具确认和评估对比。执行过程不展示模型隐藏推理。

工具必须先创建，再分配给指定 Agent 才会进入其运行快照。HTTP `GET`/`HEAD` 可标记为 `read`；其他 HTTP 方法应标记为 `write` 并选择确认策略；破坏性工具必须逐次确认。MCP 工具需填写服务 URL 和远程工具名。所有 JSON 表单字段必须填写 JSON 对象，创建失败时界面会显示服务端返回的字段路径和校验原因。

前后端部署必须同步：Agent 工作区依赖服务端的 `/tools`、`/memories`、`/agent-runs` 和 `/evaluations` 路由及网关转发；Task 工作台依赖 `/tasks`、`/task-dispatch-events`、`/notifications` 与 `/task/ws`。某个接口返回 `404` 通常表示远端 Agent API 或网关仍是旧版本；运行创建返回 `500` 时应由服务端检查 Agent API、Worker、模型连接和依赖服务日志。

Task 由用户创建或从聊天中委派给 Cloud Agent。Task 拥有独立上下文、预算、提出者和执行者；模型只能经服务端定义的 Task 工具提交进度、结果或创建子任务。网页仅显示脱敏调度摘要，Agent 的运行内容仍应在运行记录中按权限查看。

### 本地执行与帮助中心

“配置 → 本地执行”可批准 `local-agent auth login` 生成的配对会话、选择已同步设备和工作区，并将 Agent 绑定为 `server_proxy` 或 `local_direct`。当前仅 `local_direct` 会被服务端派发给用户电脑上的 daemon；运行工作台会显示本机派发状态，且不会转交给云端 Worker。`server_proxy` 是已提供的控制面选项，但当前版本不支持由 daemon 执行它的运行。

完整的安装、配对、工作区注册、本机模型配置、终端运行和排错步骤在登录后的“帮助中心 → 本地执行与 CLI”。CLI 位于配套仓库的 `local-agent/`，最小启动流程为：

```bash
cd /home/zhouzw/agentWorkCluster/local-agent
npm install
node bin/local-agent.js daemon
node bin/local-agent.js auth login --api https://chat.example.com
```

生产部署时，CLI 运行在需要执行任务的电脑上，并通过对外网关连接；网关必须转发 `/api/v1/local-agent*` 和 `/local-agent/ws*`，不要暴露内部 Agent API 的 `9011`。可将 `node bin/local-agent.js daemon` 配置为该用户的 `systemd --user` 服务。当前 Local Agent 仅支持文本模型运行；不支持本机文件/进程工具、终端工具确认或断线恢复，不能作为生产远程执行器部署。私聊和群聊的端到端加密边界不覆盖 Agent run：提交给 Agent 的任务、授权工具结果和必要上下文会按 Agent 平台策略发送给模型服务并保存审计记录。

## 技术栈

| 层 | 技术 | 说明 |
|------|------|------|
| UI 框架 | **Svelte 4** | 编译为原生 JS，零虚拟 DOM 运行时 |
| 构建 | **Vite 5** | 原生 ESM，冷启动 <2s，HMR 瞬时 |
| 加密 | **tweetnacl** | ~10KB 纯 JS，Curve25519 + Salsa20 |
| HTTP | `fetch` | 浏览器原生 |
| WebSocket | 原生 API | 无额外依赖 |
| 状态管理 | Svelte stores | 语言级响应式 |
| 样式 | CSS Variables | 暗色主题，零依赖 |
| 语言 | JavaScript (ES2020+) | 无 TypeScript 编译开销 |

**总计：4 个 npm 包，运行时零额外依赖。**

## 项目结构

```
src/
├── main.js                    # 入口：挂载 App
├── App.svelte                 # 根组件：认证路由 + WS 管理 + 消息分发
├── app.css                    # 全局 CSS 变量 + 暗色主题 + 响应式
├── lib/
│   ├── api.js                 # REST API 封装（Bearer auth + 统一错误处理）
│   ├── crypto.js              # tweetnacl 封装（box/secretbox/群密钥分发）
│   ├── store.js               # Svelte stores（auth/messages/unread/online...)
│   ├── ws.js                  # WebSocket 管理器（心跳 + 指数退避重连）
│   ├── agent-ws.js            # Agent 运行事件订阅与重连
│   └── utils.js               # Base64 编解码、时间格式化、convId 计算
├── views/
│   ├── Login.svelte           # 注册（自动生成密钥对）+ Token 登录
│   ├── ChatRoom.svelte        # 聊天窗口（私聊/群聊复用 + 本地回声 + 历史分页）
│   ├── Contacts.svelte        # 用户搜索 + 发起私聊 + 创建/加入群组
│   ├── AgentChat.svelte       # Agent 会话和运行交互
│   ├── AgentSettings.svelte   # Agent 基础配置与工具/记忆页签
│   ├── AgentGovernance.svelte # 工具治理、候选导入和长期记忆
│   ├── AgentRuns.svelte       # 运行记录、确认请求和评估对比
│   ├── TaskPanel.svelte       # 任务树、通知、验收和运行明细
│   └── HelpCenter.svelte      # 应用内帮助中心
└── components/
    ├── Header.svelte          # 顶部栏：当前对话标题 + 在线状态 + 登出
    ├── Sidebar.svelte         # 侧边栏：对话列表 + 在线指示器 + 未读角标
    ├── Message.svelte         # 消息气泡：文本 + 时间 + 投递状态
    └── Input.svelte           # 消息输入框 + Enter 发送
```

## 加密模型

```
私聊：  nacl.box(明文, nonce, 对方公钥, 我的私钥) → Base64 → 服务器 → 对方解密
群聊：  nacl.secretbox(明文, nonce, 群对称密钥) → Base64 → 服务器 → 成员解密

传输格式：nonce(24B) || ciphertext → Base64 编码
```

- 私钥和群密钥存储在 `localStorage` 中（原型阶段可接受）
- 群密钥创建时用每个成员的 Curve25519 公钥分别加密分发
- 服务器仅转发和存储密文，无法解密任何内容

## 设计文档

详细设计见 [DESIGN.md](DESIGN.md)，包含：
- 技术选型对比分析（为何不用 React/Vue/TS/Tailwind）
- 组件树与数据流
- WebSocket 重连与心跳策略
- 完整的 crypto.js API 设计
- 实现路线图

## License

私有项目
