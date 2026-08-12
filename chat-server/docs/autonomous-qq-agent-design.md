# 内部自主 QQ Agent 设计

## 目标与边界

本设计为内部使用的 Agent 提供事件驱动的自主行动能力：Agent 可由定时器、外部事件或运行时任务唤醒，联网获取信息，并主动向已登记的 QQ 群发送文本或 @ 已知群成员。

产品层不设置人工审批、订阅开关或人为消息额度；QQ 官方 API 的认证、权限、内容长度及 429 限制仍是不可绕过的平台边界。所有 QQ 凭据只由 QQ Gateway 保存，Agent 模型永远不能获得 access token 或直接构造 QQ HTTP 请求。

## 当前事实

- QQ Gateway 已从 `GROUP_AT_MESSAGE_CREATE` 提取 `group_openid`，但仅在该群有消息后进入会话映射；本方案新增独立群登记，兼容 `GROUP_ADD_ROBOT` 和群消息发现。
- 现有 `send_message` 是被动回复，必须携带 `msg_id`。主动投递使用独立方法，绝不传 `msg_id` 或 `event_id`。
- Agent Service 已提供不可变 Run 快照、outbox、Redis Worker、工具声明和会话隔离；自主执行复用此链路。

## 会话私有状态栏

`agent_status` 是每个 Conversation 一行的结构化状态，不是普通 `messages` 记录。

```text
conversation_id (PK)
source = user
status_encrypted = {"time":"ISO-8601 UTC","group_members":["名字", ...]}
updated_at
```

规则：

1. 每个 Conversation 独立保存，绝不按 Agent 聚合或同步到其他 Conversation。
2. `time` 是最近一次状态更新的 UTC 时间；成员仅保留名称，不保存成员最后操作时间。
3. QQ 群事件携带的 `author.member_openid` 先作为临时可显示身份；如果事件提供昵称则优先使用昵称。成员按名称去重并保持首次出现顺序。
4. 模型执行前把该状态渲染为最后一条 `user` 上下文，位于会话历史之后；它不写回聊天消息历史。
5. 前端在所有消息的最底部显示状态栏。Web 对话状态也独立，默认只有时间和空成员列表。

## QQ 目录和主动投递

QQ Gateway 是 QQ provider 状态的唯一所有者，使用 SQLite 加密数据库：

```text
qq_groups(agent_id, bot_id, group_openid, source, created_at, updated_at)
qq_group_members(agent_id, group_openid, member_openid, display_name, created_at, updated_at)
proactive_outbound(delivery_id, agent_id, group_openid, idempotency_key, status,
                  provider_message_id, attempts, last_error, created_at, updated_at)
```

- `GROUP_ADD_ROBOT` 只登记群，不创建 Agent Run；`GROUP_AT_MESSAGE_CREATE` 同样 upsert 群和作者。
- 主动接口仅接受 Agent Service 的 `Service` 凭据、当前 Agent ID、已登记 `group_openid`、内容和投递幂等键。
- `QQApiClient.send_proactive_group_message` 的 QQ 请求体仅为 `{"msg_type":0,"content":"..."}`。
- 被动回复保持原方法，仍带 `msg_id`。两个方法不可复用，避免协议字段混淆。
- Gateway 按 `delivery_id` 幂等；网络错误可重试，成功后永久记为 sent。429/5xx 采用退避，平台日限或持续 429 返回明确的延迟/失败状态。

## 自主事件与调度

Agent Service 新增：

```text
agent_schedules(id, agent_id, owner_user_id, run_at, prompt_encrypted, enabled,
                idempotency_key, last_triggered_at, created_at, updated_at)
```

- 初版支持指定 UTC 时刻的一次性唤醒；重复定时和 Cron 在同一张表扩展，不改变工具或投递协议。
- API 后台 loop 轮询到期记录，以 `schedule_id + run_at` 作为幂等来源并创建普通 Run。Run 走既有 outbox/Worker。
- 定时 Run 在目标 Agent 的独立 Conversation 中执行，避免污染 QQ 会话；提示词可要求 Agent 选择群并调用主动发送工具。
- 同一 Agent 的既有并发、日/月 token 预算仍生效。用户交互 Run 与自主 Run 使用相同的持久化恢复语义。

## 默认工具

| 工具 | 类型 | 作用 |
| --- | --- | --- |
| `web_search` | MCP stdio | 现有互联网搜索 |
| `read_url` | MCP stdio | 现有网页文本读取 |
| `web_fetch` | MCP stdio | `uvx mcp-server-fetch` 的服务端受控适配 |
| `qq_list_groups` | local | 列出当前 Agent 已登记群 |
| `qq_list_group_members` | local | 列出已在该群观察到的成员 |
| `qq_send_group_message` | local write | 主动发送群文本 |
| `qq_remind_group_member` | local write | 向已知成员发送 @ 提醒 |
| `timer_create` | local write | 创建一次性 Agent 唤醒 |

本部署信任内部 Agent，QQ 与 timer 工具可设置为 `confirmation_mode=none`；内置工具出现在每个所有者的工具目录中，仍由该 Agent 显式授权后才会进入 Run 快照。调用仍经过 JSON schema、Agent 所有权、群目录、服务间认证、幂等和审计。`web_fetch` 保持现有 MCP stdio 进程超时、响应长度和 URL 安全规则，不允许模型运行任意 shell。

## @ 成员语义

“所有成员”并不等于逐一 @。本方案只支持对目录中已观察到的 `member_openid` 定向 @；Gateway 将其渲染为 QQ 文本 mention token `<@member_openid>`，模型不能自行拼该标记。未知成员会被拒绝。

## API 契约

Agent Service 对 Gateway：

```text
POST /internal/v1/qq/proactive-messages
{ agent_id, group_openid, content, idempotency_key, member_openid? }

GET /internal/v1/qq/groups/{agent_id}
GET /internal/v1/qq/groups/{agent_id}/{group_openid}/members
```

Gateway 只对 Agent Service 暴露这些内网端点。Agent Service 的工具执行器通过该接口工作，前端不直连 Gateway。

## 故障、审计与验收

- 主动消息从不带 `msg_id`/`event_id`；被动回复必须带事件消息 ID。
- 同一 `idempotency_key` 不重复发送；重启、Gateway 重试和 HTTP 超时均不产生多条已确认投递。
- 不同 Conversation 的 `agent_status` 和成员列表互不可见；状态始终是模型上下文中的最后一条 `user` 消息。
- `GROUP_ADD_ROBOT` 登记群，群消息登记作者；未知群或未知成员的主动工具调用必须拒绝。
- scheduler 重启后未执行的任务仍可执行，已触发任务不重复创建 Run。
- 日志、trace 与 UI 不输出 Token、AppSecret、网页原文或 QQ 消息正文；消息和状态内容静态加密。

## 实施状态

本仓库实现：会话私有状态栏、群/成员发现、主动投递内网 API、受控 QQ 工具、一次性定时唤醒和 `mcp-server-fetch` 工具定义。Cron、Webhook、完整成员 API 同步和 QQ 平台级提及格式差异留作后续扩展，不改变现有表与接口边界。
