# Chat Server 前端设计者报告

> 版本：0.1.0 | 最后更新：2026-07-20

---

## 目录

1. [系统概述](#1-系统概述)
2. [架构与数据流](#2-架构与数据流)
3. [加密模型（前端必须实现）](#3-加密模型)
4. [认证流程](#4-认证流程)
5. [REST API 完整参考](#5-rest-api)
6. [WebSocket 协议](#6-websocket-协议)
7. [数据模型](#7-数据模型)
8. [错误处理](#8-错误处理)
9. [前端实现建议](#9-前端实现建议)
10. [附录](#10-附录)

---

## 1. 系统概述

Chat Server 是一个**零知识（Zero-Knowledge）**实时聊天后端。服务器仅存储和转发加密数据，**永远无法**解密消息内容或访问用户的私钥。

### 核心特性

| 特性 | 描述 |
|------|------|
| 端到端加密 | 消息在客户端加密，服务器只看到密文 |
| 私聊 | 一对一加密消息，实时 WebSocket 投递 |
| 群聊 | 基于共享群密钥的多人群聊 |
| 投递确认 | 每条私聊消息返回 ack（含投递状态） |
| 在线状态 | 实时跟踪用户在线/离线 |
| 消息持久化 | 所有消息存储在服务端，支持历史查询 |
| 游标分页 | 消息历史支持 `before_id`/`after_id` 游标翻页 |

### 不做什么

服务器**不**提供以下功能（由客户端实现）：
- 消息加密/解密
- 密钥管理（生成、存储、交换）
- 已读回执
- 输入状态提示
- 文件/附件传输

---

## 2. 架构与数据流

```
┌──────────┐  WebSocket   ┌──────────────┐  WebSocket   ┌──────────┐
│  Alice   │◄────────────►│              │◄────────────►│   Bob    │
│ (客户端)  │              │  Chat Server │              │ (客户端)  │
└──────────┘              │              │              └──────────┘
      │                   │   ┌─────┐    │                   │
      │  REST API         │   │redb │    │         REST API  │
      └──────────────────►│   │ KV  │◄───┘                   │
                          │   │ DB  │    │
                          │   └─────┘    │
                          └──────────────┘
```

### 消息流程（以私聊为例）

```
Alice                    Server                    Bob
  │                         │                        │
  │──[加密消息]──►           │                        │
  │   WebSocket              │                        │
  │                         │──[存储密文到 DB]──►      │
  │                         │                        │
  │                         │──[实时转发密文]─────────►│
  │                         │   (如果 Bob 在线)        │
  │                         │                        │
  │◄──[ack: delivered]──    │                        │
  │   WebSocket              │                        │
```

### 消息流程（离线投递）

```
Alice                    Server                    Bob (离线)
  │                         │                        │
  │──[加密消息]──►           │                        │
  │                         │──[存储密文到 DB]──►      │
  │                         │──[检测 Bob 离线]──►      │
  │◄──[ack: delivered]──    │                        │
  │   (delivered: false)    │                        │
  │                         │                        │
  │                         │         Bob 上线        │
  │                         │◄──[GET /api/v1/messages]│
  │                         │──[返回历史密文]─────────►│
  │                         │   (Bob 本地解密)         │
```

---

## 3. 加密模型

> **重要：前端必须实现以下加密逻辑。服务器不参与任何加密操作。**

### 3.1 密钥体系

```
用户密钥对：
  公钥 (32 bytes) ──► 注册时提交给服务器，其他用户可获取
  私钥 (32 bytes) ──► 仅存储在客户端，用于解密

私聊加密：
  发送者用 [接收者公钥] 加密消息 ──► 密文 ──► 服务器 ──► 密文 ──► 接收者用 [自己私钥] 解密

群聊加密：
  创建者生成 [对称群密钥] ──► 用每个成员的 [公钥] 分别加密群密钥
       ──► 每个成员得到 [加密的群密钥副本]
       ──► 服务器存储 (group_id, user_id) → encrypted_group_key
  成员获取自己那份 ──► 用 [自己私钥] 解密 ──► 得到 [对称群密钥]
  群消息用 [对称群密钥] 加密 ──► 所有成员用同一个群密钥解密
```

### 3.2 推荐算法

| 用途 | 推荐算法 | 库 |
|------|---------|-----|
| 密钥交换 | X25519 (Curve25519) | `tweetnacl`, `libsodium` |
| 对称加密 | XSalsa20-Poly1305 | `tweetnacl`, `libsodium` |
| 公钥签名 | Ed25519 | `tweetnacl`, `libsodium` |

### 3.3 Base64 编码规范

所有二进制数据在 JSON 中传输时使用 **标准 Base64**（无 URL 安全编码，无填充省略）。

```
原始 32 字节公钥 → Base64 编码 → JSON 字符串
加密后的消息     → Base64 编码 → JSON 字符串
加密的群密钥     → Base64 编码 → JSON 字符串
```

---

## 4. 认证流程

### 4.1 注册 → 获取 Token

```
1. 客户端生成 Curve25519 密钥对
2. POST /api/v1/register { username, public_key（32字节的Base64） }
3. 服务器返回 { id, username, token }
4. 客户端保存 token（64 个十六进制字符）
```

**安全注意事项：**
- Token 相当于密码——安全存储（localStorage、SecureStore）
- Token 在所有后续请求中使用
- 如果 Token 泄露，用户可重新注册

### 4.2 认证方式

所有需要认证的请求支持**两种方式**（按优先级）：

```http
# 方式一（推荐）：Bearer Header
GET /api/v1/users/me
Authorization: Bearer a1b2c3d4e5f6...

# 方式二（兼容）：Query 参数
GET /api/v1/users/me?token=a1b2c3d4e5f6...
```

### 4.3 认证失败

```json
// 401 Unauthorized
{
  "error": "认证 token 无效或已过期"
}
```

客户端应跳转到登录/注册页面。

---

## 5. REST API 完整参考

### 基础 URL

```
https://<host>:<port>/api/v1
```

所有带认证的请求需携带 `Authorization: Bearer <token>` header。

---

### 5.1 用户

#### `POST /api/v1/register` — 注册

**无需认证**

**请求体：**
```json
{
  "username": "alice",
  "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
}
```

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `username` | string | 1-32 字符，不可为空 | 用户名（唯一） |
| `public_key` | string (Base64) | 解码后必须为 32 字节 | Curve25519 公钥 |

**响应 200：**
```json
{
  "id": 1,
  "username": "alice",
  "token": "a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef1234567890"
}
```

**响应 400：**
```json
{ "error": "用户名已存在" }
// 或
{ "error": "公钥格式无效，需要 32 字节" }
// 或
{ "error": "用户名需为 1-32 字符" }
```

---

#### `GET /api/v1/users` — 用户列表

**需要认证**

**请求：**
```http
GET /api/v1/users
Authorization: Bearer <token>
```

**响应 200：**
```json
[
  {
    "id": 1,
    "username": "alice",
    "public_key": "AAAAAAAA...",
    "created_at": "2026-07-20T12:00:00.000Z"
  },
  {
    "id": 2,
    "username": "bob",
    "public_key": "BBBBBBBB...",
    "created_at": "2026-07-20T12:01:00.000Z"
  }
]
```

---

#### `GET /api/v1/users/me` — 当前用户

**需要认证**

**响应 200：**
```json
{
  "id": 1,
  "username": "alice",
  "public_key": "AAAAAAAA...",
  "created_at": "2026-07-20T12:00:00.000Z"
}
```

---

#### `GET /api/v1/users/{id}` — 用户详情

**需要认证**

**路径参数：** `id` = 用户 ID (int64)

**响应 200：** 同上 User 对象

**响应 404：**
```json
{ "error": "用户 999 未找到" }
```

---

#### `GET /api/v1/users/{id}/public_key` — 获取公钥

**需要认证**

> 发起私聊前，必须获取对方的公钥。用对方公钥加密第一条消息。

**回应 200：**
```json
{
  "user_id": 2,
  "public_key": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
}
```

---

### 5.2 群组

#### `POST /api/v1/groups` — 创建群组

**需要认证**

**请求体：**
```json
{
  "name": "Engineering Team",
  "member_ids": [2, 3, 4],
  "encrypted_group_keys": [
    "<群密钥用 Bob 公钥加密后的 Base64>",
    "<群密钥用 Charlie 公钥加密后的 Base64>",
    "<群密钥用 Dave 公钥加密后的 Base64>"
  ]
}
```

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `name` | string | 1-64 字符 | 群组名称 |
| `member_ids` | int64[] | 必须与 keys 一一对应 | 成员用户 ID 列表 |
| `encrypted_group_keys` | string[] (Base64) | 长度 = member_ids | 每个成员的加密群密钥 |

> **前端流程：**
> 1. 生成随机对称群密钥（32 字节）
> 2. 获取每个成员的公钥
> 3. 用每个成员公钥加密群密钥
> 4. 将加密结果 Base64 编码后放入 `encrypted_group_keys`

**响应 200：**
```json
{
  "group_id": 1,
  "name": "Engineering Team"
}
```

---

#### `GET /api/v1/groups/list` — 我的群组列表

**需要认证**

**响应 200：**
```json
[
  {
    "id": 1,
    "name": "Engineering Team",
    "creator_id": 1,
    "created_at": "2026-07-20T12:30:00.000Z"
  }
]
```

---

#### `GET /api/v1/groups/{id}/members` — 群成员列表

**需要认证**（调用者必须是群成员）

**响应 200：**
```json
[
  {
    "user_id": 1,
    "username": "alice",
    "encrypted_key": "<群密钥用 Alice 公钥加密后的 Base64>"
  },
  {
    "user_id": 2,
    "username": "bob",
    "encrypted_key": "<群密钥用 Bob 公钥加密后的 Base64>"
  }
]
```

> `encrypted_key` 是**加密后的群密钥**——用成员自己的私钥解密后得到群对称密钥。

**响应 400：**
```json
{ "error": "你不是该群成员" }
```

---

#### `POST /api/v1/groups/{id}/join` — 加入群组

**需要认证**

**请求体：**
```json
{
  "encrypted_key": "<群密钥用 User 公钥加密后的 Base64>"
}
```

> 通常由已在群内的成员为你提供加密的群密钥，然后调用此接口将你的加密密钥副本提交到服务器。

**响应 200：**
```json
{ "status": "ok", "group_id": 1 }
```

---

### 5.3 消息历史

#### `GET /api/v1/messages/{user_id}` — 私聊历史

**需要认证**

**路径参数：** `user_id` = 对方的用户 ID

**查询参数：**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 50（上限200） | 返回条数 |
| `before_id` | int | — | 返回此 ID 之前的消息（加载更早消息） |
| `after_id` | int | — | 返回此 ID 之后的消息（加载更新消息） |

**示例请求：**
```http
GET /api/v1/messages/2?limit=20
Authorization: Bearer <token>
```

```http
# 加载更早的消息（翻页）
GET /api/v1/messages/2?limit=20&before_id=100
Authorization: Bearer <token>
```

**响应 200：**
```json
{
  "messages": [
    {
      "id": 1,
      "sender_id": 1,
      "recipient_id": 2,
      "encrypted_content": "SGVsbG8gQm9iIQ==",
      "created_at": "2026-07-20T12:00:00.000Z"
    },
    {
      "id": 2,
      "sender_id": 2,
      "recipient_id": 1,
      "encrypted_content": "SGkgQWxpY2Uh",
      "created_at": "2026-07-20T12:00:05.000Z"
    }
  ],
  "limit": 20
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int64 | 消息序号（用于分页游标） |
| `sender_id` | int64 | 发送者 ID |
| `recipient_id` | int64 | 接收者 ID |
| `encrypted_content` | string (Base64) | 加密消息内容 |
| `created_at` | string (ISO8601) | 发送时间 |

> **分页实现：** 首次请求不带游标→获取最新 N 条；滚动到顶部时，用第一条消息的 `id` 作为 `before_id` 请求更早的消息。

---

#### `GET /api/v1/groups/{id}/messages` — 群聊历史

**需要认证**（调用者必须是群成员）

**路径参数：** `id` = 群组 ID

**查询参数：** 同私聊历史（`limit`、`before_id`、`after_id`）

**响应 200：**
```json
{
  "messages": [
    {
      "id": 1,
      "group_id": 1,
      "sender_id": 1,
      "encrypted_content": "SGVsbG8gZXZlcnlvbmUh",
      "created_at": "2026-07-20T12:30:00.000Z"
    }
  ],
  "limit": 50
}
```

---

## 6. WebSocket 协议

### 6.1 连接

```
ws://<host>:<port>/ws?token=<token>
```

或使用 Bearer header（推荐）：

```javascript
const ws = new WebSocket('ws://localhost:9010/ws');
// 在连接时，如果支持自定义 header，使用 Authorization: Bearer <token>
// 否则回退到 query 参数：ws://localhost:9010/ws?token=<token>
```

### 6.2 客户端 → 服务器（发送）

所有消息为单行 JSON。

#### 私聊消息
```json
{
  "type": "private",
  "to_user_id": 2,
  "encrypted_content": "SGVsbG8gQm9iIQ==",
  "created_at": "2026-07-20T12:00:00.000Z"
}
```

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `type` | string | ✅ | 固定为 `"private"` |
| `to_user_id` | int64 | ✅ | 接收者用户 ID |
| `encrypted_content` | string (Base64) | ✅ | 加密后的消息内容 |
| `created_at` | string (ISO8601) | 否 | 消息时间戳（服端自动生成） |

#### 群聊消息
```json
{
  "type": "group",
  "group_id": 1,
  "encrypted_content": "SGVsbG8gZ3JvdXA=",
  "created_at": "2026-07-20T12:30:00.000Z"
}
```

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `type` | string | ✅ | 固定为 `"group"` |
| `group_id` | int64 | ✅ | 目标群组 ID |
| `encrypted_content` | string (Base64) | ✅ | 加密后的消息内容 |
| `created_at` | string (ISO8601) | 否 | 消息时间戳 |

#### 心跳 Ping
```json
{ "type": "ping" }
```

> 建议每 30 秒发送一次 ping 来保持连接活跃。

### 6.3 服务器 → 客户端（接收）

#### 连接确认
```json
{
  "type": "connected",
  "user_id": 1,
  "username": "alice"
}
```

连接建立后立即发送。客户端可以用它确认认证成功。

#### 收到的私聊/群聊消息
```json
{
  "type": "private",
  "message_id": 42,
  "from_user_id": 2,
  "from_username": "bob",
  "encrypted_content": "SGVsbG8gQWxpY2Uh",
  "created_at": "2026-07-20T12:00:05.000Z"
}
```

```json
{
  "type": "group",
  "message_id": 15,
  "from_user_id": 2,
  "from_username": "bob",
  "group_id": 1,
  "encrypted_content": "SGVsbG8gZ3JvdXA=",
  "created_at": "2026-07-20T12:30:05.000Z"
}
```

| 字段 | 类型 | 总是存在 | 说明 |
|------|------|---------|------|
| `type` | string | ✅ | `"private"` 或 `"group"` |
| `message_id` | int64 | ✅ | 服务端消息 ID |
| `from_user_id` | int64 | ✅ | 发送者 ID |
| `from_username` | string | ✅ | 发送者用户名 |
| `group_id` | int64 | 仅群聊 | 群组 ID |
| `encrypted_content` | string (Base64) | ✅ | 加密内容 |
| `created_at` | string (ISO8601) | ✅ | 时间戳 |

#### 投递确认（Ack）
```json
{
  "type": "ack",
  "message_id": 42,
  "to_user_id": 2,
  "delivered": true,
  "created_at": "2026-07-20T12:00:05.000Z"
}
```

> 仅私聊消息返回 ack。`delivered: true` 表示对方在线且消息已实时转发；`delivered: false` 表示对方离线，消息已存储，对方上线后可通过历史 API 获取。

#### 心跳响应
```json
{ "type": "pong" }
```

#### 错误
```json
{ "type": "error", "message": "缺少 encrypted_content" }
```

### 6.4 WebSocket 消息类型汇总

| type | 方向 | 触发时机 |
|------|------|---------|
| `connected` | 出 | 连接建立时 |
| `private` | 入 | 发送私聊消息 |
| `private` | 出 | 收到私聊消息 |
| `group` | 入 | 发送群聊消息 |
| `group` | 出 | 收到群聊消息 |
| `ack` | 出 | 私聊消息投递确认 |
| `ping` | 入 | 心跳请求 |
| `pong` | 出 | 心跳响应 |
| `error` | 出 | 消息格式错误等 |

---

## 7. 数据模型

### User
```typescript
interface User {
  id: number;           // int64, 自增
  username: string;     // 1-32 字符
  public_key: string;   // Base64, 32字节 Curve25519 公钥
  created_at: string;   // ISO8601 时间戳
}
```

### PrivateMessage
```typescript
interface PrivateMessage {
  id: number;              // int64, 消息序号
  sender_id: number;       // int64
  recipient_id: number;    // int64
  encrypted_content: string; // Base64 密文
  created_at: string;      // ISO8601
}
```

### Group
```typescript
interface Group {
  id: number;           // int64, 自增
  name: string;         // 1-64 字符
  creator_id: number;   // int64
  created_at: string;   // ISO8601
}
```

### GroupMember
```typescript
interface GroupMember {
  user_id: number;      // int64
  username: string;
  encrypted_key: string; // Base64, 用该成员公钥加密的群密钥
}
```

### GroupMessage
```typescript
interface GroupMessage {
  id: number;              // int64, 消息序号
  group_id: number;        // int64
  sender_id: number;       // int64
  encrypted_content: string; // Base64 密文
  created_at: string;      // ISO8601
}
```

---

## 8. 错误处理

### HTTP 状态码

| 状态码 | 含义 | 示例 |
|--------|------|------|
| 200 | 成功 | 正常响应 |
| 400 | 请求错误 | 参数缺失、格式无效、用户已存在 |
| 401 | 未认证 | Token 缺失或无效 |
| 404 | 未找到 | 用户/群组不存在 |
| 429 | 速率限制 | 请求频率超限（>100/min） |
| 500 | 服务器错误 | 数据库错误、内部异常 |

### 错误响应格式

```json
{
  "error": "人类可读的错误描述"
}
```

### WebSocket 错误

```json
{
  "type": "error",
  "message": "具体错误原因"
}
```

### 常见错误一览

| 错误消息 | 触发条件 |
|---------|---------|
| `用户名已存在` | 注册时用户名重复 |
| `公钥格式无效，需要 32 字节` | 注册时公钥长度错误 |
| `用户名需为 1-32 字符` | 用户名过长或为空 |
| `群组名需为 1-64 字符` | 群组名过长或为空 |
| `成员与密钥数量不匹配` | member_ids 和 encrypted_group_keys 长度不一致 |
| `群密钥 base64 解码失败` | encrypted_group_keys 中某条 Base64 格式无效 |
| `用户已是群组成员` | 重复加入同一个群 |
| `你不是该群成员` | 非成员访问群消息/成员列表 |
| `缺少认证 token` | 未提供 Authorization header 或 token 参数 |
| `认证 token 无效或已过期` | Token 不存在 |
| `请求频率过高，请稍后重试` | 超过速率限制（100/min） |
| `缺少 encrypted_content` | WebSocket 消息缺少加密内容 |
| `私聊需要 to_user_id` | private 消息缺少接收者 |
| `群聊需要 group_id` | group 消息缺少群 ID |
| `未知消息类型` | WebSocket 消息 type 字段值无效 |

---

## 9. 前端实现建议

### 9.1 连接管理

```typescript
class ChatClient {
  private ws: WebSocket;
  private token: string;
  private reconnectDelay = 1000;
  private maxReconnectDelay = 30000;

  connect(token: string) {
    this.token = token;
    this.ws = new WebSocket(`ws://localhost:9010/ws?token=${token}`);

    this.ws.onopen = () => {
      this.reconnectDelay = 1000; // 重置重连延迟
    };

    this.ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      this.handleMessage(msg);
    };

    this.ws.onclose = () => {
      // 指数退避重连
      setTimeout(() => this.connect(this.token), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
    };
  }

  // 心跳
  startHeartbeat(intervalMs = 30000) {
    setInterval(() => {
      if (this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: "ping" }));
      }
    }, intervalMs);
  }
}
```

### 9.2 消息发送

```typescript
// 私聊
function sendPrivateMessage(toUserId: number, plaintext: string, recipientPublicKey: Uint8Array) {
  const encrypted = nacl.box(plaintext, nonce, recipientPublicKey, mySecretKey);
  const content = btoa(String.fromCharCode(...encrypted));

  ws.send(JSON.stringify({
    type: "private",
    to_user_id: toUserId,
    encrypted_content: content,
    created_at: new Date().toISOString()
  }));
}

// 群聊
function sendGroupMessage(groupId: number, plaintext: string, groupKey: Uint8Array) {
  const encrypted = nacl.secretbox(plaintext, nonce, groupKey);
  const content = btoa(String.fromCharCode(...encrypted));

  ws.send(JSON.stringify({
    type: "group",
    group_id: groupId,
    encrypted_content: content,
    created_at: new Date().toISOString()
  }));
}
```

### 9.3 消息历史加载策略

```
┌──────────────────────────────────────┐
│  首次进入聊天                          │
│  GET /messages/{uid}?limit=50        │
│  → 获取最近 50 条消息                  │
│  → 记住第一条消息的 id (oldest_msg_id)  │
│  → 记住最后一条消息的 id (newest_msg_id) │
└──────────────────────────────────────┘
              │
    ┌─────────┴─────────┐
    ▼                   ▼
┌──────────────┐  ┌──────────────┐
│ 用户上滑翻页   │  │ 实时新消息     │
│ (加载历史)    │  │ (WebSocket)  │
│              │  │              │
│ GET /messages│  │ 直接插入      │
│ /{uid}       │  │ 列表底部      │
│ ?before_id=  │  │              │
│ {oldest_id}  │  │ 更新          │
│ &limit=20    │  │ newest_msg_id │
│              │  │              │
│ 更新          │  └──────────────┘
│ oldest_msg_id│
└──────────────┘
```

### 9.4 群组创建完整流程

```typescript
async function createGroup(name: string, memberIds: number[]) {
  // 1. 生成随机对称群密钥
  const groupKey = nacl.randomBytes(32);

  // 2. 获取每个成员的公钥
  const members = await Promise.all(
    memberIds.map(async (id) => {
      const resp = await fetch(`/api/v1/users/${id}/public_key`, {
        headers: { Authorization: `Bearer ${token}` }
      });
      const data = await resp.json();
      return { user_id: id, public_key: base64Decode(data.public_key) };
    })
  );

  // 3. 用每个成员的公钥加密群密钥
  const encryptedKeys = members.map((m) => {
    const encrypted = nacl.box(groupKey, nonce, m.public_key, mySecretKey);
    return base64Encode(encrypted);
  });

  // 4. 提交到服务器
  const resp = await fetch('/api/v1/groups', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`
    },
    body: JSON.stringify({
      name,
      member_ids: memberIds,
      encrypted_group_keys: encryptedKeys
    })
  });

  const group = await resp.json();
  // group = { group_id, name }

  // 5. 本地存储群密钥（用自己私钥解密后使用）
  // 注意：可以从 encryptedKeys 中取出自己的那份，也可以直接用原始 groupKey
  return { groupId: group.group_id, groupKey };
}
```

### 9.5 加入群组流程

```typescript
async function joinGroup(groupId: number, encryptedGroupKey: string) {
  // encryptedGroupKey 是邀请者用你的公钥加密后发给你的群密钥（Base64）

  // 1. 解密得到群密钥
  const encrypted = base64Decode(encryptedGroupKey);
  const groupKey = nacl.boxOpen(encrypted, nonce, senderPublicKey, mySecretKey);

  // 2. 提交你的加密群密钥到服务器
  await fetch(`/api/v1/groups/${groupId}/join`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`
    },
    body: JSON.stringify({ encrypted_key: encryptedGroupKey })
  });

  // 3. 保存群密钥用于后续加密群消息
  return groupKey;
}
```

### 9.6 移动端注意事项

| 注意点 | 建议 |
|--------|------|
| WebSocket 保活 | 使用 ping/pong，间隔 ≤30s |
| 后台重连 | App 切回前台时重新建立 WebSocket |
| Token 存储 | iOS: Keychain，Android: EncryptedSharedPreferences |
| 网络切换 | 监听网络状态变化，WiFi↔蜂窝自动重连 |
| 消息可靠性 | 利用 `ack.delivered` 判断；未投递则暂存本地 |

---

## 10. 附录

### A. 完整 API 路由表

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| POST | `/api/v1/register` | 否 | 注册用户 |
| GET | `/api/v1/users` | 是 | 用户列表 |
| GET | `/api/v1/users/me` | 是 | 当前用户 |
| GET | `/api/v1/users/{id}` | 是 | 用户详情 |
| GET | `/api/v1/users/{id}/public_key` | 是 | 用户公钥 |
| POST | `/api/v1/groups` | 是 | 创建群组 |
| GET | `/api/v1/groups/list` | 是 | 我的群组 |
| GET | `/api/v1/groups/{id}/members` | 是 | 群成员 |
| POST | `/api/v1/groups/{id}/join` | 是 | 加入群组 |
| GET | `/api/v1/messages/{user_id}` | 是 | 私聊历史 |
| GET | `/api/v1/groups/{id}/messages` | 是 | 群聊历史 |
| GET | `/ws` | 是 | WebSocket 连接 |

> 旧路径（`/api/register` 等）仍可使用，但新代码建议使用 `/api/v1/`。

### B. 速率限制

| 接口类型 | 限制 | 超出后 |
|---------|------|--------|
| REST API | 100 次/分钟/token | 429 + 错误消息 |
| WebSocket | 无硬限制 | — |

> 速率限制按 token 独立计数，每分钟重置窗口。

### C. 技术栈参考（前端）

| 平台 | 推荐加密库 | 推荐 WebSocket 库 |
|------|-----------|-------------------|
| Web/JS | `tweetnacl`, `libsodium-wrappers` | 原生 `WebSocket` API |
| React Native | `react-native-sodium` | 原生 `WebSocket` API |
| iOS (Swift) | `SwiftSodium`, `libsodium` | `URLSessionWebSocketTask` |
| Android (Kotlin) | `kalium` (libsodium bindings) | `OkHttp` WebSocket |
| Flutter | `cryptography`, `sodium_libs` | `web_socket_channel` |
| Electron | `tweetnacl` | 原生 `WebSocket` API |

### D. 示例——最小可用网页客户端（概念）

```html
<script>
  // 初始化加密库 (tweetnacl)
  const keyPair = nacl.box.keyPair();

  // 1. 注册
  const resp = await fetch('/api/v1/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: 'alice',
      public_key: btoa(String.fromCharCode(...keyPair.publicKey))
    })
  });
  const { token } = await resp.json();

  // 2. 连接 WebSocket
  const ws = new WebSocket(`ws://localhost:9010/ws?token=${token}`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'private') {
      // 用自己私钥解密
      const ciphertext = Uint8Array.from(atob(msg.encrypted_content), c => c.charCodeAt(0));
      const plaintext = nacl.box.open(ciphertext, nonce, senderPublicKey, keyPair.secretKey);
      console.log(`${msg.from_username}: ${new TextDecoder().decode(plaintext)}`);
    }
  };

  // 3. 获取 Bob 的公钥并发消息
  const bobResp = await fetch('/api/v1/users/2/public_key', {
    headers: { Authorization: `Bearer ${token}` }
  });
  const { public_key } = await bobResp.json();
  const bobKey = Uint8Array.from(atob(public_key), c => c.charCodeAt(0));

  // 4. 加密并发送
  const plaintext = new TextEncoder().encode('Hello Bob!');
  const nonce = nacl.randomBytes(24);
  const encrypted = nacl.box(plaintext, nonce, bobKey, keyPair.secretKey);

  ws.send(JSON.stringify({
    type: 'private',
    to_user_id: 2,
    encrypted_content: btoa(String.fromCharCode(...encrypted)),
    created_at: new Date().toISOString()
  }));
</script>
```

---

> 如有疑问或需要补充信息，请联系后端团队。
