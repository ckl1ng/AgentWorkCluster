/**
 * Svelte writable stores — 全局应用状态
 *
 * 不使用任何状态管理库。Svelte 的 writable/store 是语言级响应式，足够用。
 */

import { writable, derived } from 'svelte/store';
import { base64ToBytes, bytesToBase64 } from './utils.js';

// ── 认证 ──

/** @type {import('svelte/store').Writable<{id:number,username:string,token:string,publicKey:Uint8Array,secretKey:Uint8Array}|null>} */
export const auth = writable(null);

// ── 在线状态 ──

/** @type {import('svelte/store').Writable<Map<number, boolean>>} */
export const onlineStatus = writable(new Map());

// 头像文件目前保存在浏览器本地。该 revision 让同一标签页中的消息列表能立刻
// 响应顶部栏的头像修改，而不必等待页面刷新。
export const avatarRevision = writable(0);

export function avatarStorageKey(userId) {
  return `chat_avatar_${userId}`;
}

export function getLocalAvatar(userId) {
  if (userId == null) return '';
  try {
    return localStorage.getItem(avatarStorageKey(userId)) || '';
  } catch {
    return '';
  }
}

export function saveLocalAvatar(userId, value) {
  localStorage.setItem(avatarStorageKey(userId), value);
  avatarRevision.update((revision) => revision + 1);
}

// ── 当前活跃对话 ──

/**
 * activeChat: { type: 'private'|'group', id: number, name: string } | null
 * id: 私聊时为对方 user_id，群聊时为 group_id
 */
export const activeChat = writable(null);

/** 当前用户可见的私有 Agent。密钥和完整模型配置永不写入此 store。 */
export const agents = writable([]);

// ── 消息缓存 ──

/**
 * messages: Map<convKey, Message[]>
 * convKey: 私聊 "private:{convId}" 或群聊 "group:{groupId}"
 */
export const messages = writable(new Map());

// ── 用户缓存（对方用户信息 + 公钥） ──

/**
 * users: Map<number, { id, username, public_key, created_at }>
 */
export const users = writable(new Map());

// ── 群组缓存 ──

/**
 * groups: Map<number, { id, name, creator_id, created_at }>
 */
export const groups = writable(new Map());

// ── 群密钥缓存（本地解密后的群对称密钥） ──

/**
 * groupKeys: Map<number, Uint8Array>  group_id → 对称群密钥
 */
function restoreGroupKeys() {
  try {
    const raw = JSON.parse(localStorage.getItem('chat_group_keys') || '{}');
    return new Map(Object.entries(raw).map(([id, key]) => [Number(id), base64ToBytes(key)]));
  } catch {
    localStorage.removeItem('chat_group_keys');
    return new Map();
  }
}

export const groupKeys = writable(restoreGroupKeys());

groupKeys.subscribe((keys) => {
  const serializable = Object.fromEntries([...keys].map(([id, key]) => [id, bytesToBase64(key)]));
  localStorage.setItem('chat_group_keys', JSON.stringify(serializable));
});

export function saveGroupKey(groupId, key) {
  groupKeys.update((keys) => {
    const next = new Map(keys);
    next.set(groupId, key);
    return next;
  });
}

/** 清除仅属于当前登录会话的内存和本地密钥缓存。 */
export function clearSessionState() {
  onlineStatus.set(new Map());
  activeChat.set(null);
  messages.set(new Map());
  users.set(new Map());
  groups.set(new Map());
  agents.set([]);
  unreadCounts.set(new Map());
  groupKeys.set(new Map());
}

// ── 未读计数 ──

/**
 * unreadCounts: Map<convKey, number>
 */
export const unreadCounts = writable(new Map());

// ── 对话列表（派生） ──

/**
 * 从好友、群组和消息派生对话列表。
 *
 * 不以消息作为会话存在的前提，确保刚进入应用时也能直接看到已有联系人和群聊。
 */
export const conversations = derived(
  [messages, users, groups, auth, unreadCounts],
  ([$messages, $users, $groups, $auth, $unreadCounts]) => {
    const convs = [];

    for (const [peerId, user] of $users.entries()) {
      const key = `private:${peerId}`;
      const msgs = $messages.get(key) || [];
      const lastMsg = msgs[msgs.length - 1];
      convs.push({
        key,
        type: 'private',
        peerId,
        name: user?.username || `用户 ${peerId}`,
        lastMsg,
        lastTime: lastMsg?.created_at || '',
        unread: $unreadCounts.get(key) || 0,
      });
    }

    for (const [groupId, group] of $groups.entries()) {
      const key = `group:${groupId}`;
      const msgs = $messages.get(key) || [];
      const lastMsg = msgs[msgs.length - 1];
      convs.push({
        key,
        type: 'group',
        groupId,
        name: group?.name || `群 ${groupId}`,
        lastMsg,
        lastTime: lastMsg?.created_at || '',
        unread: $unreadCounts.get(key) || 0,
      });
    }

    convs.sort((a, b) => {
      return (b.lastTime || '').localeCompare(a.lastTime || '');
    });

    return convs;
  }
);
