# Chat Client 前端设计文档

> 版本：0.1.0 | 最后更新：2026-07-20 | 配套后端：[chat-server](../chat-server/)

---

## 目录

1. [设计目标与约束](#1-设计目标与约束)
2. [技术栈决策](#2-技术栈决策)
3. [项目结构](#3-项目结构)
4. [架构设计](#4-架构设计)
5. [组件树](#5-组件树)
6. [数据流](#6-数据流)
7. [路由设计](#7-路由设计)
8. [加密集成](#8-加密集成)
9. [WebSocket 管理](#9-websocket-管理)
10. [API 封装](#10-api-封装)
11. [视图详细设计](#11-视图详细设计)
12. [样式策略](#12-样式策略)
13. [实现路线图](#13-实现路线图)

---

## 1. 设计目标与约束

### 1.1 目标

构建一个与 [chat-server](../chat-server/) 配套的**零知识（Zero-Knowledge）**聊天前端，实现：

- 用户注册/登录（Curve25519 密钥对 + Token 认证）
- 端到端加密私聊（WebSocket 实时 + 历史 REST API）
- 端到端加密群聊（创建、加入、收发消息）
- 在线状态感知
- 消息历史游标分页

### 1.2 硬约束（低配置开发机）

| 约束 | 影响 | 应对 |
|------|------|------|
| CPU 弱 | 编译慢、加密慢 | Svelte（编译快）、tweetnacl（纯 JS，不用 WASM） |
| 内存小 | 开发服务器/浏览器内存敏感 | Vite（内存低）、无虚拟 DOM（运行时开销 0） |
| 磁盘紧张 | node_modules 要大 | pnpm（硬链接省 50%+ 空间） |
| GPU 弱/无 | 动画吃力 | 极简 CSS 动画，无 heavy transition |

### 1.3 软约束

| 约束 | 说明 |
|------|------|
| 无 TypeScript | 省掉 tsc 编译开销和类型检查内存 |
| 无组件库 | 聊天 UI 核心只有消息气泡 + 输入框，手写更快 |
| 无测试框架 | 起步阶段浏览器手动测试 |
| 纯 JavaScript | ES2020+，充分利用现代浏览器原生能力 |

---

## 2. 技术栈决策

### 2.1 最终选择

```
UI 框架：     Svelte 4（编译为原生 JS，无虚拟 DOM 运行时）
构建工具：    Vite 5（原生 ESM，冷启动 <2s，HMR 瞬时）
包管理：      pnpm（硬链接，省 50%+ 磁盘）
加密库：      tweetnacl（~10KB，纯 JS，零依赖）
HTTP：        fetch（浏览器原生）
WebSocket：  原生 WebSocket API
Base64：      内置 btoa/atob + Uint8Array
路由：        手动条件渲染（应用简单，不需要路由器）
状态管理：    Svelte writable stores（语言级响应式）
样式：        CSS Variables + 手写 CSS（零依赖）
```

### 2.2 对比分析

| 维度 | 选择 | 被淘汰方案 | 淘汰原因 |
|------|------|-----------|---------|
| UI 框架 | Svelte 4 | React 19 | React 虚拟 DOM + 运行时 ~40KB，低配机负担 |
| | | Vue 3 | 运行时 ~30KB，Svelte 编译后更轻 |
| | | Alpine.js | 简单场景轻，聊天应用复杂逻辑散落难维护 |
| | | 纯 Vanilla | 代码量上去后自写 DOM 胶水比框架更重 |
| 加密库 | tweetnacl | libsodium-wrappers | ~200KB+ WASM，初始化慢，Sodium 功能过头 |
| | | Web Crypto API | Curve25519 支持不足（部分浏览器无） |
| 包管理 | pnpm | npm | npm 扁平 node_modules 空间大 |
| | | yarn | pnpm 更省空间 |
| 样式方案 | 手写 CSS | Tailwind CSS | 编译扫描 class 开销 + 学习负担 |
| | | CSS Modules | Svelte 自带 scoped style，不需要 |
| 类型 | 纯 JS | TypeScript | tsc 编译 + 类型检查吃内存 |
| 路由 | 条件渲染 | svelte-spa-router | 聊天应用 4 个视图，不需要 |

### 2.3 tweetnacl 接口兼容性

文档中伪代码使用 `nacl.box` / `nacl.secretbox`，tweetnacl 接口完全一致：

```javascript
import nacl from 'tweetnacl';

// 密钥对生成
const keyPair = nacl.box.keyPair();           // → { publicKey, secretKey }

// 非对称加密（私聊）
const nonce = nacl.randomBytes(24);
const encrypted = nacl.box(message, nonce, recipientPK, mySK);

// 非对称解密
const decrypted = nacl.box.open(encrypted, nonce, senderPK, mySK);

// 对称加密（群聊）
const nonce2 = nacl.randomBytes(24);
const encrypted2 = nacl.secretbox(message, nonce2, groupKey);

// 对称解密
const decrypted2 = nacl.secretbox.open(encrypted2, nonce2, groupKey);
```

---

## 3. 项目结构

```
chat-client/
├── DESIGN.md                # 本文件
├── README.md                # 项目说明 + 快速启动
├── package.json
├── pnpm-lock.yaml
├── vite.config.js
├── index.html
├── public/
│   └── favicon.svg
└── src/
    ├── main.js              # 入口：挂载 App 组件
    ├── App.svelte           # 根组件：认证状态 → 视图路由
    ├── lib/
    │   ├── api.js           # REST API 封装（fetch 包装，~60 行）
    │   ├── ws.js            # WebSocket 连接管理（重连/心跳，~80 行）
    │   ├── crypto.js        # tweetnacl 封装（密钥/加密/解密，~50 行）
    │   ├── store.js         # Svelte writable stores（~60 行）
    │   └── utils.js         # Base64 编解码、时间格式化等工具（~30 行）
    ├── views/
    │   ├── Login.svelte     # 登录/注册视图
    │   ├── ChatList.svelte  # 对话列表（私聊 + 群聊混合）
    │   ├── ChatRoom.svelte  # 聊天窗口（私聊/群聊复用）
    │   └── Contacts.svelte  # 用户列表 + 创建群组
    └── components/
        ├── Message.svelte   # 单条消息气泡（文本 + 时间 + 状态）
        ├── Input.svelte     # 消息输入框 + 发送按钮
        ├── Sidebar.svelte   # 侧边栏（对话列表导航）
        └── Header.svelte    # 顶部栏（当前对话标题 + 用户信息）
```

---

## 4. 架构设计

### 4.1 整体架构图

```
┌─────────────────────────────────────────────────────┐
│                     App.svelte                       │
│  ┌─────────────────────────────────────────────────┐│
│  │              auth state (store)                   ││
│  │  null → Login.svelte                              ││
│  │  User → Main Layout                               ││
│  │    ├── Sidebar.svelte  (ChatList)                ││
│  │    └── Content Area                              ││
│  │        ├── ChatRoom.svelte                       ││
│  │        └── Contacts.svelte                       ││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
             │                │
        ┌────▼────┐     ┌────▼────┐
        │  api.js  │     │  ws.js  │
        │ (fetch)  │     │(WebSocket)│
        └────┬─────┘     └────┬─────┘
             │                │
        ┌────▼────────────────▼────┐
        │     chat-server          │
        │  REST API  │  WebSocket  │
        └─────────────────────────┘
```

### 4.2 模块职责

| 模块 | 职责 | 大小估算 |
|------|------|---------|
| `api.js` | REST API 请求封装，自动附加 Authorization header，统一错误处理 | ~60 行 |
| `ws.js` | WebSocket 连接生命周期：连接、心跳、指数退避重连、消息分发 | ~80 行 |
| `crypto.js` | tweetnacl 封装：密钥对生成、box/seal/open、base64 编码 | ~50 行 |
| `store.js` | Svelte stores：auth、conversations、messages、onlineStatus | ~60 行 |
| `utils.js` | Base64↔Uint8Array、ISO8601 格式化、会话 ID 计算 | ~30 行 |

---

## 5. 组件树

```
App.svelte
├── [未认证] Login.svelte
│     ├── 用户名输入框
│     ├── 注册按钮（自动生成密钥对）
│     └── 登录按钮（输入已有 token）
│
└── [已认证] 主布局
    ├── Header.svelte
    │     ├── 当前用户信息
    │     └── 登出按钮
    │
    ├── Sidebar.svelte
    │     └── ChatList.svelte
    │           ├── 私聊列表项（头像 + 用户名 + 最后消息预览）
    │           └── 群聊列表项（群名 + 最后消息预览）
    │
    └── 内容区
          ├── ChatRoom.svelte
          │     ├── 消息列表（Scroll + 上滑加载历史）
          │     │     └── Message.svelte × N
          │     │           ├── 发送者头像/名称
          │     │           ├── 消息气泡（解密后的文本）
          │     │           ├── 时间戳
          │     │           └── 投递状态（已发送/已投递/未投递）
          │     └── Input.svelte
          │           ├── 文本输入框
          │           └── 发送按钮
          │
          └── Contacts.svelte
                ├── 用户搜索
                ├── 用户列表（可点击发起私聊）
                └── 创建群组按钮 → 多选用户 → 生成群密钥 → 提交
```

---

## 6. 数据流

### 6.1 认证流程

```
用户输入 username
       │
       ▼
crypto.js 生成 Curve25519 密钥对
       │
       ├── publicKey → Base64 编码
       └── secretKey → 本地存储（localStorage）
       │
       ▼
POST /api/v1/register { username, public_key }
       │
       ▼
服务器返回 { id, username, token }
       │
       ▼
token 存入 localStorage
       │
       ▼
store.setAuth({ id, username, token, publicKey, secretKey })
       │
       ▼
ws.js 使用 token 建立 WebSocket
       │
       ▼
进入主界面
```

### 6.2 发送私聊消息

```
用户输入文本 → 点击发送
       │
       ▼
crypto.js: 获取对方 publicKey
crypto.js: nacl.box(plaintext, nonce, recipientPK, mySK)
       │
       ▼
Base64 编码密文
       │
       ▼
ws.js.send({ type: "private", to_user_id, encrypted_content })
       │
       ▼
──── 两种路径 ────
       │
       ├── 对方在线 → 服务器实时转发 → ws.onmessage → Message.svelte
       │                                    │
       │                              crypto.js: nacl.box.open() → 明文显示
       │
       └── 服务器 → ack { delivered: true/false }
                    │
              Message 气泡状态更新
```

### 6.3 加载消息历史

```
进入聊天窗口
       │
       ▼
GET /api/v1/messages/{uid}?limit=50
       │
       ▼
收到 { messages: [...] }
       │
       ├── 记录 oldest_msg_id（第一条的 id）
       ├── 记录 newest_msg_id（最后一条的 id）
       └── 渲染消息列表
       │
       ▼
用户上滑到顶 → 触发加载更早消息
       │
       ▼
GET /api/v1/messages/{uid}?limit=20&before_id={oldest_msg_id}
       │
       ▼
更新 oldest_msg_id，消息插入列表顶部
       │
       ▼
WebSocket 收到新消息 → 更新 newest_msg_id，消息追加到列表底部
```

---

## 7. 路由设计

不引入路由库，在 `App.svelte` 中用 Svelte 条件渲染实现：

```svelte
{#if !$auth}
  <Login />
{:else}
  <div class="layout">
    <Sidebar onSelect={setActiveChat} />
    {#if activeView === 'chat'}
      <ChatRoom chatId={activeChatId} chatType={activeChatType} />
    {:else if activeView === 'contacts'}
      <Contacts onChat={startChat} />
    {/if}
  </div>
{/if}
```

### 视图状态枚举

| 状态 | 组件 | 说明 |
|------|------|------|
| 未认证 | `Login.svelte` | 注册/登录 |
| 聊天 | `ChatRoom.svelte` | 私聊或群聊 |
| 联系人 | `Contacts.svelte` | 用户列表、创建群组 |

---

## 8. 加密集成

### 8.1 crypto.js 接口设计

```javascript
// src/lib/crypto.js

import nacl from 'tweetnacl';

/**
 * 生成 Curve25519 密钥对
 * @returns {{ publicKey: Uint8Array, secretKey: Uint8Array }}
 */
export function generateKeyPair() {
  return nacl.box.keyPair();
}

/**
 * 加密私聊消息
 * @param {string} plaintext - 明文
 * @param {Uint8Array} recipientPublicKey - 接收者公钥（32 bytes）
 * @param {Uint8Array} senderSecretKey - 发送者私钥（32 bytes）
 * @returns {{ encrypted: Uint8Array, nonce: Uint8Array }}
 */
export function encryptPrivate(plaintext, recipientPublicKey, senderSecretKey) {
  const message = new TextEncoder().encode(plaintext);
  const nonce = nacl.randomBytes(24);
  const encrypted = nacl.box(message, nonce, recipientPublicKey, senderSecretKey);
  return { encrypted, nonce };
}

/**
 * 解密私聊消息
 * @param {Uint8Array} encrypted - 密文
 * @param {Uint8Array} nonce - 随机数（24 bytes）
 * @param {Uint8Array} senderPublicKey - 发送者公钥
 * @param {Uint8Array} recipientSecretKey - 接收者私钥
 * @returns {string|null} 明文，解密失败返回 null
 */
export function decryptPrivate(encrypted, nonce, senderPublicKey, recipientSecretKey) {
  const decrypted = nacl.box.open(encrypted, nonce, senderPublicKey, recipientSecretKey);
  if (!decrypted) return null;
  return new TextDecoder().decode(decrypted);
}

/**
 * 生成对称群密钥
 * @returns {Uint8Array} 32 bytes 随机密钥
 */
export function generateGroupKey() {
  return nacl.randomBytes(32);
}

/**
 * 用公钥加密群密钥（分发给群成员）
 * @param {Uint8Array} groupKey - 群对称密钥
 * @param {Uint8Array} memberPublicKey - 成员公钥
 * @param {Uint8Array} creatorSecretKey - 创建者私钥
 * @returns {{ encrypted: Uint8Array, nonce: Uint8Array }}
 */
export function encryptGroupKey(groupKey, memberPublicKey, creatorSecretKey) {
  const nonce = nacl.randomBytes(24);
  const encrypted = nacl.box(groupKey, nonce, memberPublicKey, creatorSecretKey);
  return { encrypted, nonce };
}

/**
 * 解密群密钥（成员获取群密钥）
 * @param {Uint8Array} encryptedKey - 加密的群密钥
 * @param {Uint8Array} nonce - 随机数
 * @param {Uint8Array} creatorPublicKey - 创建者公钥
 * @param {Uint8Array} memberSecretKey - 成员私钥
 * @returns {Uint8Array|null}
 */
export function decryptGroupKey(encryptedKey, nonce, creatorPublicKey, memberSecretKey) {
  return nacl.box.open(encryptedKey, nonce, creatorPublicKey, memberSecretKey);
}

/**
 * 对称加密群消息
 * @param {string} plaintext - 明文
 * @param {Uint8Array} groupKey - 群对称密钥
 * @returns {{ encrypted: Uint8Array, nonce: Uint8Array }}
 */
export function encryptGroup(plaintext, groupKey) {
  const message = new TextEncoder().encode(plaintext);
  const nonce = nacl.randomBytes(24);
  const encrypted = nacl.secretbox(message, nonce, groupKey);
  return { encrypted, nonce };
}

/**
 * 对称解密群消息
 * @param {Uint8Array} encrypted - 密文
 * @param {Uint8Array} nonce - 随机数
 * @param {Uint8Array} groupKey - 群对称密钥
 * @returns {string|null}
 */
export function decryptGroup(encrypted, nonce, groupKey) {
  const decrypted = nacl.secretbox.open(encrypted, nonce, groupKey);
  if (!decrypted) return null;
  return new TextDecoder().decode(decrypted);
}
```

### 8.2 密钥存储策略

```
localStorage
├── chat_token          # 认证 token（64 个十六进制字符）
├── chat_user           # { id, username, publicKey(Base64), secretKey(Base64) }
└── chat_group_keys     # { "1": "<Base64 群密钥>", "2": "<Base64 群密钥>" }
```

> **安全提醒：** 生产环境应使用更安全的存储（Web Crypto subtle storage、或至少用用户口令派生加密密钥来加密这些值）。当前阶段为原型开发，localStorage 可接受。

### 8.3 Nonce 处理

Nonce 必须与密文一起传输。消息格式需要扩展为 **nonce + 密文** 的复合结构：

```
加密消息格式（发送前）：
  nonce (24 bytes) || ciphertext (variable)

JSON 传输：
  {
    "encrypted_content": "<Base64(nonce || ciphertext)>"
  }
```

> **需与后端协商：** 当前后端 API 的 `encrypted_content` 字段是透明的 `Vec<u8>`，可以直接用复合格式。前端加密时将 nonce 拼接在密文前面，解密时取前 24 字节作为 nonce，后面是密文。

---

## 9. WebSocket 管理

### 9.1 ws.js 接口设计

```javascript
// src/lib/ws.js

/**
 * WebSocket 连接管理器
 *
 * 功能：
 * - 使用 token 建立 WebSocket 连接
 * - 指数退避自动重连
 * - 30s 心跳 ping/pong
 * - 消息回调注册
 */
class ChatWebSocket {
  constructor(url, token) { ... }

  /** 建立连接 */
  connect() { ... }

  /** 断开连接 */
  disconnect() { ... }

  /** 发送 JSON 消息 */
  send(msg) { ... }

  /** 注册消息处理器 */
  onMessage(handler) { ... }

  /** 移除消息处理器 */
  offMessage(handler) { ... }
}
```

### 9.2 重连策略

```
首次断开 → 等待 1s 重连
第2次     → 等待 2s
第3次     → 等待 4s
第4次     → 等待 8s
...
上限       → 30s（之后固定每 30s 重试）
连接成功   → 重置为 1s
```

### 9.3 心跳机制

```
连接建立 → 每 30s 发送 { type: "ping" }
         → 期待 { type: "pong" }
         → 60s 内无 pong → 视为断开，触发重连
```

---

## 10. API 封装

### 10.1 api.js 接口设计

```javascript
// src/lib/api.js

const BASE = 'http://localhost:9010/api/v1';

// 内部：自动附加 Authorization header，统一错误处理
async function request(method, path, body) { ... }

// 对外接口
export const api = {
  // 用户
  register(username, publicKey)                  // POST /register
  getUsers()                                      // GET /users
  getMe()                                         // GET /users/me
  getUser(id)                                     // GET /users/{id}
  getPublicKey(userId)                            // GET /users/{id}/public_key

  // 群组
  createGroup(name, memberIds, encryptedKeys)     // POST /groups
  listGroups()                                    // GET /groups/list
  getGroupMembers(groupId)                        // GET /groups/{id}/members
  joinGroup(groupId, encryptedKey)                // POST /groups/{id}/join

  // 消息历史
  getPrivateMessages(userId, { limit, beforeId, afterId })  // GET /messages/{user_id}
  getGroupMessages(groupId, { limit, beforeId, afterId })   // GET /groups/{id}/messages
};
```

### 10.2 错误处理策略

```
HTTP 401 → token 失效 → 清除 localStorage → 跳转登录
HTTP 400 → 显示错误提示（用户名重复、格式无效等）
HTTP 429 → 显示"请稍后重试"
HTTP 500 → 显示"服务器错误"
网络错误 → 显示"网络连接异常"
```

---

## 11. 视图详细设计

### 11.1 Login.svelte — 登录/注册

```
┌──────────────────────────────────┐
│        Chat Server               │
│                                  │
│  ┌────────────────────────┐     │
│  │  用户名                 │     │
│  └────────────────────────┘     │
│                                  │
│  ┌────────────────────────┐     │
│  │  Token（已有账户时输入）  │     │
│  └────────────────────────┘     │
│                                  │
│  ┌──────────┐ ┌─────────────┐  │
│  │  注册     │ │ Token 登录   │  │
│  └──────────┘ └─────────────┘  │
│                                  │
│  注册时自动生成密钥对，公钥提交    │
│  到服务器，私钥存 localStorage     │
└──────────────────────────────────┘
```

**状态：**
- 输入用户名
- 可选输入已有 Token
- "注册" → 生成密钥对 → POST /api/v1/register → 保存 token + 密钥 → 进入主界面
- "Token 登录" → 直接使用已有 token → GET /api/v1/users/me 验证 → 进入主界面

### 11.2 ChatList.svelte — 对话列表

```
┌────────────────────┐
│  💬 聊天            │
│                    │
│  ┌────────────────┐│
│  │ 🔵 Alice       ││  ← 私聊，在线状态灯
│  │ 最后消息预览...  ││
│  └────────────────┘│
│  ┌────────────────┐│
│  │ 🟢 Bob         ││
│  │ (离线)          ││
│  └────────────────┘│
│  ┌────────────────┐│
│  │ 👥 工程组       ││  ← 群聊
│  │ Charlie: 最新...││
│  └────────────────┘│
│                    │
│  [+ 新建对话]       │
└────────────────────┘
```

**数据来源：**
- 私聊列表：汇总所有私聊消息的对方用户 ID
- 群聊列表：GET /api/v1/groups/list
- 在线状态：WebSocket 连接事件

### 11.3 ChatRoom.svelte — 聊天窗口（核心视图）

```
┌──────────────────────────────────────────┐
│  Header: Alice 🔵在线                     │
├──────────────────────────────────────────┤
│                                          │
│  ┌─────────────────────────────────┐    │
│  │        [加载更早消息...]           │    │  ← 上滑触发 before_id 分页
│  │                                  │    │
│  │  ┌──────────────────────┐       │    │
│  │  │ Alice                 │       │    │  ← 对方消息（左对齐）
│  │  │ Hello Bob!            │       │    │
│  │  │ 12:01 PM  ✓已投递     │       │    │
│  │  └──────────────────────┘       │    │
│  │                                  │    │
│  │        ┌──────────────────────┐ │    │
│  │        │ 我                    │ │    │  ← 自己消息（右对齐）
│  │        │ Hi Alice!            │ │    │
│  │        │ 12:02 PM             │ │    │
│  │        └──────────────────────┘ │    │
│  └─────────────────────────────────┘    │
│                                          │
├──────────────────────────────────────────┤
│  ┌──────────────────────────┐ ┌──────┐  │
│  │  输入消息...               │ │ 发送  │  │
│  └──────────────────────────┘ └──────┘  │
└──────────────────────────────────────────┘
```

**滚动行为：**
- 新消息自动滚到底部
- 用户上滑到顶 → 触发 `loadEarlier()` → `before_id` 游标分页
- 加载历史后保持当前滚动位置（记录第一条消息高度差）

**消息状态标识：**
| 状态 | 图标 | 含义 |
|------|------|------|
| 发送中 | ⏳ | WebSocket 发送中 |
| 已发送 | ✓ | 消息已发到服务器 |
| 已投递 | ✓✓ | 服务器确认对方在线接收 |
| 未投递 | ✓✗ | 对方离线，已存入数据库 |

### 11.4 Contacts.svelte — 联系人 + 群组管理

```
┌──────────────────────────────────────────┐
│  联系人                                    │
├──────────────────────────────────────────┤
│  ┌────────────────────────────────────┐  │
│  │ 🔍 搜索用户...                      │  │
│  └────────────────────────────────────┘  │
│                                          │
│  ┌─────────────────────────────────┐    │
│  │ 🔵 Alice                        │    │
│  │    点击发起私聊                   │    │
│  ├─────────────────────────────────┤    │
│  │ 🟢 Bob                          │    │
│  ├─────────────────────────────────┤    │
│  │ 🔵 Charlie                      │    │
│  └─────────────────────────────────┘    │
│                                          │
│  ─── 群组 ───                            │
│  ┌─────────────────────────────────┐    │
│  │ 👥 工程组 (3人)                  │    │
│  │ 👥 设计组 (5人)                  │    │
│  └─────────────────────────────────┘    │
│                                          │
│  [+ 创建新群组]                           │
│    → 多选用户 → 输入群名 → 生成群密钥      │
│    → 用每个成员公钥加密 → POST /groups     │
└──────────────────────────────────────────┘
```

---

## 12. 样式策略

### 12.1 CSS Variables 体系

```css
:root {
  /* 颜色 */
  --color-bg:           #0f0f0f;
  --color-surface:      #1a1a1a;
  --color-border:       #2a2a2a;
  --color-text:         #e0e0e0;
  --color-text-muted:   #888;
  --color-primary:      #4a9eff;
  --color-primary-dim:  #2a6ecc;
  --color-self-msg:     #1a3a5c;
  --color-other-msg:    #2a2a2a;
  --color-online:       #4caf50;
  --color-offline:      #666;
  --color-error:        #ff5252;

  /* 间距 */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 12px;
  --space-lg: 16px;
  --space-xl: 24px;

  /* 圆角 */
  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 16px;

  /* 字体 */
  --font-sm: 12px;
  --font-md: 14px;
  --font-lg: 16px;
}
```

### 12.2 设计原则

- **暗色主题**（默认）—— 对眼睛友好，聊天场景标准选择
- **消息气泡** —— 自己的消息右对齐蓝色，对方消息左对齐深灰
- **最小动画** —— 仅消息出现时的 `fadeIn` + `slideUp`，低配机无压力
- **移动端响应式** —— `max-width: 768px` 时侧边栏折叠为顶部 tab

---

## 13. 实现路线图

### 阶段 0：项目脚手架（预估 10 分钟）

- [x] 创建目录结构
- [x] 编写 DESIGN.md（本文件）
- [ ] `pnpm create vite chat-client --template vanilla`
- [ ] `pnpm add -D @sveltejs/vite-plugin-svelte svelte`
- [ ] `pnpm add tweetnacl`
- [ ] 配置 `vite.config.js`（svelte 插件）
- [ ] 创建 `src/main.js` + `src/App.svelte` 最小骨架
- [ ] 验证 `pnpm dev` 能在浏览器打开

### 阶段 1：核心基础设施（预估 30 分钟）

- [ ] `src/lib/utils.js` — Base64 编解码工具
- [ ] `src/lib/crypto.js` — tweetnacl 封装
- [ ] `src/lib/api.js` — REST API 封装
- [ ] `src/lib/store.js` — Svelte stores（auth、conversations、messages）
- [ ] `src/lib/ws.js` — WebSocket 管理器

### 阶段 2：认证视图（预估 20 分钟）

- [ ] `src/views/Login.svelte` — 注册 + Token 登录
- [ ] 在 `App.svelte` 实现 auth 状态路由

### 阶段 3：聊天核心（预估 60 分钟）

- [ ] `src/components/Header.svelte` — 顶部栏
- [ ] `src/components/Message.svelte` — 消息气泡
- [ ] `src/components/Input.svelte` — 输入框
- [ ] `src/views/ChatRoom.svelte` — 聊天窗口
  - 消息列表渲染
  - 加密发送 + 解密接收
  - 历史消息加载 + 游标分页
  - 实时消息接收（WebSocket）
  - 投递状态更新（ack）

### 阶段 4：对话列表 + 联系人（预估 30 分钟）

- [ ] `src/components/Sidebar.svelte` — 侧边栏
- [ ] `src/views/ChatList.svelte` — 对话列表
- [ ] `src/views/Contacts.svelte` — 用户列表 + 发起私聊

### 阶段 5：群聊功能（预估 40 分钟）

- [ ] 创建群组流程（多选用户 → 生成群密钥 → 加密分发 → POST）
- [ ] 加入群组流程
- [ ] 群聊消息加密/解密（对称密钥）
- [ ] 群聊窗口（复用 ChatRoom）

### 阶段 6：打磨（预估 20 分钟）

- [ ] 在线状态实时更新
- [ ] 未读消息计数
- [ ] 输入框 Enter 发送
- [ ] 消息时间格式化（今天/昨天/日期）
- [ ] 重连时消息列表刷新

---

## 附录 A：与后端的协议差异点

| 对比项 | 后端文档 | 前端方案 | 说明 |
|--------|---------|---------|------|
| `encrypted_content` 格式 | 纯密文 Base64 | nonce(24B) + 密文 → Base64 | nonce 需随密文传输 |
| 认证方式 | Query `?token=` | `Authorization: Bearer` header | 后端支持 Bearer（见 CLAUDE.md），前端优先用 header |
| API 前缀 | `/api/v1/` | `/api/v1/` | 一致 |
| WebSocket 认证 | `ws://host/ws?token=` | Header 优先，Query fallback | 浏览器 WebSocket 不支持自定义 header，使用 query |

> 关于 nonce 拼接：`encrypted_content` 在后端是 `Vec<u8>`（不透明），前端可以在数据层面自由组合。**发送时拼接 nonce + ciphertext → Base64；收到后 Base64 解码 → 取前 24 字节为 nonce → 剩余为密文。**

---

## 附录 B：依赖清单

```json
{
  "devDependencies": {
    "@sveltejs/vite-plugin-svelte": "^3.0.0",
    "svelte": "^4.2.0",
    "vite": "^5.0.0"
  },
  "dependencies": {
    "tweetnacl": "^1.0.3"
  }
}
```

**总计：4 个包，node_modules ~15MB（pnpm），zero 额外运行时依赖。**
