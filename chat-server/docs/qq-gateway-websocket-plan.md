# QQ Gateway WebSocket 接入计划

## 1. 文档目的

本计划用于把 QQ Bot 接入稳定收敛到 QQ 官方 WebSocket Gateway 协议，覆盖连接建立、鉴权、心跳、断线恢复、事件分发、异步 Agent 运行、消息回复、状态持久化、可观测性和部署验收。

本计划的边界是 QQ Gateway 与 Agent 平台之间的集成。Webhook 不再作为正式接入路径；`/qq/webhook*` 仅保留明确返回 `410 Gone` 的迁移兼容端点，不应被 Caddy、QQ 开放平台或运维文档引用。

## 2. 当前基线

### 2.1 已完成（代码与真实环境）

- Gateway 通过 `GET /gateway` 获取 QQ 返回的 `ws://` 或 `wss://` 地址，再建立 WebSocket。
- Identify 使用 `GROUP_AND_C2C_EVENT`，默认 Intents 为 `33554432`，覆盖群聊 @ 和私聊消息。
- WebSocket 接收循环已处理 Hello、Dispatch、Heartbeat、Heartbeat ACK、Invalid Session、RESUMED 和重连入口。
- 事件处理在后台协程执行，不阻塞 WebSocket 接收循环；Agent 运行完成后通过 QQ HTTP API 发送最终回复。
- Inbox、出站消息和 QQ 群/私聊到 `conversation_id` 的映射使用 Gateway 数据库持久化，并按 `bot_id + event_id` 去重。
- Access Token 在进程内缓存并提前刷新；连接异常使用指数退避重连。
- 连接配置由 Agent 设置页提交，AppSecret 加密保存；服务间调用使用 `AGENT_SERVICE_SECRET`。
- Webhook 反向代理已从两份 Caddy 配置删除，公开 Webhook 路由返回 410。
- 前端、根 README 和 Gateway README 已改为 WebSocket-only 说明。
- 已在真实 QQ 环境完成 Token 获取、WebSocket 建连、READY、心跳和 Bot ID 自动识别。
- 已在真实 QQ 环境验证私聊和群聊 @ 消息均可进入 Agent 调度；健康检查中的 `events_scheduled` 已增长，`reconnects` 为 0。

### 2.2 当前风险和缺口

1. **Resume 仍需真实故障联调**：正常连接已验证；仍需通过短暂断网、服务端 opcode 7 和 Invalid Session 确认 opcode 6、恢复窗口和漏事件补发行为。
2. **心跳需要长时间观测**：本地 fake Gateway 已验证 Heartbeat ACK 超时会主动重连；真实 QQ 环境仍需至少 30 分钟持续运行和网络抖动演练，确认无误判或重连风暴。
3. **重连幂等需要真实故障演练**：代码已在本地 fake Gateway 覆盖 Resume 和不可恢复 Invalid Session；需验证断网、Gateway 重启和 QQ 重复 Dispatch 后一个 event_id 只产生一个 run 和一条回复。
4. **链路审计仍需外部汇聚**：Gateway 已提供 `event_id` 的结构化阶段日志和健康计数，但日志尚未接入集中采集或保留策略，也没有独立的按 event_id 查询 API。
5. **过载策略不足**：后台任务已有并发上限，但等待槽位的事件仅在内存任务中等待，尚未实现有界持久化排队、队列长度指标和过载拒绝策略。
6. **会话状态只在内存保存**：Gateway 重启后会重新 Identify；跨进程持久化 session_id/seq 仍是可选增强项。
7. **工具动作尚未定义**：当前 Agent 输出是文本回复。踢人、发图片等 QQ 管理动作不能直接根据模型自由 JSON 执行，必须先设计授权、参数校验、幂等键、确认和审计协议。

## 3. 协议基线

QQ Gateway payload 统一采用：

```json
{
  "op": 0,
  "d": {},
  "s": 42,
  "t": "GATEWAY_EVENT_NAME"
}
```

实现必须遵循以下规则：

- `GET /gateway` 或 `/gateway/bot` 取得 WebSocket 地址；连接成功后先接收 opcode 10 Hello。
- 收到 Hello 后发送 opcode 2 Identify，token 格式为 `Bot {appid}.{app_token}`，当前实现对应 `QQBot {access_token}` 的项目约定，需以实际 QQ API 返回格式联调确认。
- Identify 的 `intents` 默认使用 `33554432`；`shard` 当前为 `[0, 1]`，不支持多分片时不得伪造其他分片。
- 按 Hello 的 `heartbeat_interval` 发送 opcode 1，`d` 为最近收到的 `s`，首次为 `null`。
- 收到 opcode 11 仅表示 Heartbeat ACK，不应再额外发送 ACK。
- opcode 1 的方向是 Send/Receive：服务端若发送 opcode 1，客户端也回复 opcode 1；opcode 11 是服务端对客户端心跳的 ACK。
- 保存 READY 的 `session_id` 和最近 Dispatch 的 `s`；短暂断线发送 opcode 6 Resume。
- 收到 opcode 7 Reconnect 时关闭当前连接并优先 Resume；收到 opcode 9 Invalid Session 时依据 `d` 是否可恢复决定 Resume 或重新 Identify。
- opcode 0 Dispatch 必须先更新序列号，再按 `t` 分发事件；未知事件不得进入 Agent 运行。
- opcode 12（HTTP Callback ACK）和 opcode 13（回调地址验证）只属于 HTTP 回调模式；WebSocket-only Gateway 不处理，也不应把它们转成 Agent 事件。

> WebSocket 没有 Webhook 的“立即返回 HTTP 200”步骤。收到事件后应立即从接收循环转入后台任务，最终回复使用 QQ 消息 HTTP API 和事件中的 `msg_id`/`id`，受 QQ 被动回复窗口约束。

## 4. 分阶段执行计划

### 阶段 A：协议正确性和连接生命周期（P0，代码完成，待故障演练）

目标：确保连接不会因心跳、错误的重连方式或状态丢失而静默失效。

- Gateway 连接循环已按 `connect -> hello -> identify/resume -> receive -> close` 执行；后续补充真实 Gateway 状态矩阵测试。
- `session_id`、`last_sequence`、Resume 次数和 heartbeat deadline 已集中在 runtime 状态中。
- 已使用 deadline 调度心跳，并在 ACK 超时后触发重连。
- opcode 7、9、11 已处理；fake Gateway 已覆盖 ACK 超时重连、Reconnect 后 Resume 和不可恢复 Invalid Session 后重新 Identify，异常关闭码和真实补发行为仍需环境演练。
- 只在明确不可恢复时清空会话，避免每次网络抖动都重新 Identify。
- 对 `/gateway` 返回 URL 做 scheme、host 和路径校验，禁止降级到普通 HTTP 请求作为 WebSocket 连接。

已验收：真实 QQ 环境的 Hello、Identify、READY、Dispatch 和 Heartbeat ACK。

剩余验收：模拟或真实演练 Reconnect、可恢复/不可恢复 Invalid Session 和短暂网络断开，连接均进入预期状态。

### 阶段 B：事件接收和异步处理（P0，基础链路已验收）

目标：WebSocket 接收循环始终可读，LLM 慢任务不阻塞网关。

- 收到 Dispatch 后只做解析、序列号记录和后台任务提交；后台任务使用 `QQ_MAX_EVENT_TASKS` 并发上限。
- 保持当前 `process_event` 协程模型；Agent 请求、轮询 run 和 QQ 回复都不能在 `recv` 循环中同步等待。
- 已为后台任务设置并发上限；取消策略和过载时的持久化排队仍需补充。
- 被动回复窗口使用事件接收时间计算；超过窗口后标记过期，不再使用失效 `msg_id` 重试。
- 所有发送失败进入持久化重试；401 刷新 Token，429/5xx 退避，其他 4xx 进入人工可诊断错误。

已验收：私聊与群聊 @ Dispatch 均进入 Agent 调度。

剩余验收：Agent 人为延迟 30~60 秒时 WebSocket 仍持续心跳；同一事件重复投递只生成一个 run 和一条回复；超时事件不会继续发送。

### 阶段 C：会话、幂等和数据恢复（P0，待演练）

目标：重启、断线、重复消息和多次投递不会破坏上下文或产生重复副作用。

- 保持 `bot_id + event_id` Inbox 幂等键，并为 processing、failed、completed、expired 状态定义恢复规则。
- 保持 `(bot_id, scope_type, scope_id) -> conversation_id` 映射；为长期不用的映射增加可配置回收策略，不能误删仍在运行的会话。
- 评估是否需要持久化 QQ session_id/seq。第一版可只在进程内 Resume，重启后重新 Identify；若要跨重启补事件，再扩展加密 session 存储。
- 进程启动先恢复连接配置，再启动 pending event retry loop；恢复顺序必须避免重复运行。

验收：Gateway 重启、数据库 WAL 恢复、连接重复发送事件、Agent API 短暂不可用和 QQ API 短暂 5xx 后，最终至多产生一条有效回复。

### 阶段 D：可观测性和安全（P1，基础实现完成）

目标：能够定位“没收到、没运行、没回复”的具体环节，同时不泄露凭证和消息内容。

- `/healthz` 已提供连接状态、最近 seq、重连/Resume、Heartbeat ACK、Dispatch 和事件阶段计数。
- 已增加 `event_id` 的结构化阶段日志：received、duplicate、agent_submitted、run_completed、reply_sent、expired 和 failed。
- 日志仅记录 agent_id、bot_id、event_id、event_type、scope_type、run_id 和错误类别；单测确保不记录 AppSecret 或消息原文。
- Dispatch 先持久化 Inbox claim，再调度 Agent；重复 event_id 只记录 duplicate，不会重复创建 run。
- Inbox 在 Agent 运行轮询期间持续刷新活动时间；同一事件在本进程活动期间不会被 retry loop 或重复 Dispatch 二次调度，出站发送也有 inflight 互斥。
- `/healthz` 区分进程健康和 QQ 连接状态；部署探针不能把短暂重连误判为整个服务崩溃。
- 保持 Gateway 9013 仅内网可达；Caddy 不暴露 `/qq/webhook*`，QQ 连接由 Gateway 主动出站建立。

剩余验收：在真实消息上确认 `received -> agent_submitted -> run_completed -> reply_sent` 阶段日志完整，并将日志接入部署环境的保留/检索机制。

### 阶段 E：容量控制与会话回收（P1）

目标：在突发消息和长期运行下保持资源边界明确。

- 为待处理事件建立有界持久化队列，记录队列长度、排队时长和因过载过期的事件数。
- 为 `channel_conversations` 增加可配置空闲回收，回收前排除仍有 processing 事件的 scope。
- 清理不再使用的 `scope_locks`，避免高基数 QQ scope 长期占用内存。
- 评估 session_id/seq 的加密持久化；仅在真实 Resume 补发验证需要时实施。

验收：突发消息不会无限创建内存任务；长期闲置会话和锁可回收；过载事件可诊断且不重复发送。

### 阶段 F：工具动作和扩展事件（P2）

目标：在不扩大模型权限的前提下支持 QQ 非文本动作。

- 先定义受限动作协议：动作类型白名单、严格 schema、目标 scope 校验、用户/管理员授权和二次确认。
- 每个动作必须有幂等键、审计记录、超时、失败回滚或明确不可回滚说明。
- 将踢人、发图、加好友等动作拆成独立 QQ API client 能力，不允许模型直接拼接 URL 或 HTTP 参数。
- 为 `GROUP_ADD_ROBOT` 等非消息事件建立显式 handler；默认忽略，不自动触发 Agent。

验收：未经授权的动作被拒绝并审计；重复动作不会重复执行；文本消息路径不受扩展事件影响。

## 5. 测试计划

### 单元测试

- Gateway URL 解析：`/gateway` 返回裸 URL、`data.url`、`websocket_url`、非法 scheme。
- Identify、Heartbeat、Resume、Reconnect、Invalid Session payload 序列化。
- 序列号更新：缺少 `s`、`s=0`、乱序和重复 Dispatch。
- 事件规范化：群聊 @、私聊、空内容、未知事件和异常字段。
- Inbox/outbound 幂等、过期和重试状态转换。

### 集成测试（下一步优先实施）

- 使用本地 fake WebSocket Gateway 模拟完整 Hello -> Identify -> READY -> event -> heartbeat ACK 流程。
- 模拟断线后 Resume 成功，并验证遗漏 Dispatch 被补发。
- 模拟 Resume 失败后重新 Identify。
- 模拟 Agent 延迟、Agent 失败、QQ 401、429、5xx 和被动窗口过期。
- 验证数据库重启恢复、重复事件和多连接 Agent 隔离。

### 发布前演练

- 连接运行至少 30 分钟，确认心跳 ACK 持续、无异常重连风暴。
- 主动断网、恢复网络、重启 Gateway、重启 Agent API，检查状态和最终回复。
- 群聊 @ 与私聊各发送正常消息、重复消息、空消息和超长消息。
- 检查日志、健康检查、Caddy 配置和公网端口扫描结果。

## 6. 下一步执行顺序

1. **验收 P1 可观测性**：发送一条测试消息，确认 `/healthz` 阶段计数和 `qq-gateway.log` 中的 event_id 阶段日志完整。
2. **P0 fake Gateway 测试已完成**：本地 WebSocket 测试已覆盖 Heartbeat ACK 超时重连、READY 后 Reconnect -> Resume，以及不可恢复 Invalid Session -> Identify。
3. **P0 故障演练**：先完成一条真实消息的阶段日志验收；随后在测试 Bot 上执行 Gateway 重启、短暂断网、Agent API 重启和重复消息，记录 Resume、重连和幂等结果。
4. **P1 容量边界**：实现有界持久化排队、会话/锁回收和队列指标，再做突发消息演练。
5. **P2 动作能力**：仅在文本回复稳定后，设计并审核 QQ 管理动作的授权、确认、审计和幂等协议。

## 7. 部署和运维步骤

1. 配置 `QQ_GATEWAY_ENABLED=true`、`AGENT_SERVICE_SECRET` 和 `QQ_GATEWAY_MASTER_KEY`。
   本机统一写入 `chat-server/.agent.env`；不要单独运行 Gateway 后再使用根目录脚本管理其他服务。
2. 部署并重启服务：

   ```bash
   cd /home/zhouzw/AgentWorkCluster
   ./stop.sh
   ./start.sh
   ```

3. 在网页目标 Agent 设置中点击“断开连接”，再点击“一键连接 QQ Bot”，使旧的 `513 intents` 配置替换为 `33554432`。
4. 查看 `http://127.0.0.1:9013/healthz`，确认进程健康且连接状态为 connected。
5. 分别验证私聊和群聊 @；检查 Agent run、Gateway event_id 和 QQ 最终回复。
6. 若连接失败，先检查 Gateway 日志中的连接阶段、HTTP 状态、opcode 和错误类别，不要重新配置 Webhook 回调地址。

### 7.1 当前轮验收记录

- 2026-08-12：本地 fake Gateway 测试已覆盖 Heartbeat ACK 缺失。首次连接在发送 Heartbeat 后未收到 opcode 11 时，Gateway 会判定超时、重连并重新 Identify。
- 当前部署实例：`/healthz` 显示一个 QQ 连接处于 `connected`，`session_active=true`，最近 opcode 为 11，未见重连。重启后尚未收到新的业务 Dispatch，因此业务阶段计数从零开始属于预期。

### 7.2 下一轮真实验证

1. 记录验证前的健康检查：

   ```bash
   curl -sS http://127.0.0.1:9013/healthz
   ```

2. 从 QQ 向测试 Bot 发送一条私聊消息，或在允许测试的群中 @ Bot；不要把消息原文、Token 或 AppSecret 粘贴到日志或工单。
3. 等待最终回复后检查：

   ```bash
   curl -sS http://127.0.0.1:9013/healthz
   tail -n 100 qq-gateway/.runtime/qq-gateway.log
   ```

4. 验收 `events_claimed`、`events_scheduled`、`events_agent_submitted`、`events_run_completed` 和 `events_reply_sent` 均增长；日志同一 `event_id` 应依次出现 `received`、`agent_submitted`、`run_completed`、`reply_sent`，且无消息正文或凭证。
5. 正常消息验收后，用根目录脚本重启 Gateway 所在服务组，再发送一条新消息，确认连接恢复且只产生一条 Agent run 和一条 QQ 回复：

   ```bash
   cd /home/zhouzw/AgentWorkCluster
   ./stop.sh
   ./start.sh
   ```

6. 只有上述稳定性验收完成后，才进入阶段 E 的有界持久化队列、指标与会话/锁回收实现。

## 8. 完成定义

- WebSocket 连接按官方 Hello、Identify、Heartbeat、Dispatch、Resume 流程运行。
- 心跳和重连在 Agent 慢任务、消息高峰和短暂网络故障下仍稳定。
- 私聊和群聊 @ 均能触发唯一 Agent run，并在有效窗口内收到最终回复。
- Gateway 重启、重复投递和 QQ API 重试不会产生重复 run 或重复回复。
- `/qq/webhook*` 不再被 Caddy 暴露，相关文档只说明 WebSocket-only。
- 运维可以通过日志、健康检查和 event_id 追踪每条消息的完整链路。
- 任何 QQ 管理动作都必须经过独立授权和审计设计后才能上线。
