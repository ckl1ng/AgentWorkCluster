<script>
  import { onDestroy, tick } from 'svelte';
  import { agents, auth, activeChat, avatarRevision, getLocalAvatar, messages, users, groups, groupKeys, saveGroupKey, unreadCounts } from '../lib/store.js';
  import { api } from '../lib/api.js';
  import { decryptGroup, decryptGroupBytes, decryptGroupKey, decryptPrivate, decryptPrivateBytes, encryptGroup, encryptGroupBytes, encryptPrivate, encryptPrivateBytes } from '../lib/crypto.js';
  import { base64ToBytes } from '../lib/utils.js';
  import { createFilePayload, FILE_CONTENT_TYPE, isImageType, parseFilePayload, validateFile } from '../lib/attachments.js';
  import { mergeMessageLists, messageRenderKey } from '../lib/message-list.js';
  import { addSticker } from '../lib/stickers.js';
  import Message from '../components/Message.svelte';
  import Input from '../components/Input.svelte';

  export let ws = null;

  let msgListEl;
  let initialLoading = false;
  let earlierLoading = false;
  let oldestId = null;
  let historyError = '';
  let historyVersion = 0;
  let activeHistoryController = null;
  let activeEarlierController = null;
  let previousConvKey = null;
  let groupMembers = new Map();
  let loadedMemberGroupId = null;
  let loadingMemberGroupId = null;
  let decryptedCache = new Map();  // msgId → plaintext
  let decryptedBytesCache = new Map();
  let imageUrlCache = new Map();
  let fileInfoCache = new Map();
  let loadingGroupKeyId = null;
  let attemptedGroupKeyId = null;
  let groupKeyError = '';
  let draggingFile = false;
  let pendingFiles = [];
  let delegation = null;
  let creatingTask = false;
  let scrollFrame = null;
  let stickToBottom = true;
  const MAX_RENDERED_MESSAGES = 200;

  /** 计算消息列表的 store key */
  $: chatType = $activeChat?.type;
  $: chatId = $activeChat?.id;
  $: convKey = chatType === 'private'
    ? `private:${chatId}`
    : (chatType === 'group' ? `group:${chatId}` : null);

  /** 当前对话的消息列表 */
  $: msgList = convKey ? ($messages.get(convKey) || []) : [];

  /** 对方的公钥（私聊需要） */
  $: peerUser = chatType === 'private' ? $users.get(chatId) : null;
  $: peerPublicKey = peerUser?.public_key ? base64ToBytes(peerUser.public_key) : null;

  /** 群密钥 */
  $: gKey = chatType === 'group' ? $groupKeys.get(chatId) : null;
  $: avatarTick = $avatarRevision;

  // 历史群消息不包含发送者昵称，进入群聊时补齐成员目录，用于头像和昵称显示。
  $: if (chatType === 'group' && chatId && loadedMemberGroupId !== chatId && loadingMemberGroupId !== chatId) {
    loadGroupMemberNames(chatId);
  }

  $: if (chatType === 'group' && chatId && !gKey && loadingGroupKeyId !== chatId && attemptedGroupKeyId !== chatId) {
    loadGroupKey(chatId);
  }

  // 切换对话时取消旧请求。所有异步结果都用会话快照校验，避免旧会话覆盖当前视图。
  $: if (convKey !== previousConvKey) {
    if (previousConvKey) releaseConversationCache(previousConvKey);
    previousConvKey = convKey;
    historyVersion += 1;
    oldestId = null;
    historyError = '';
    activeHistoryController?.abort();
    activeEarlierController?.abort();
    activeHistoryController = null;
    activeEarlierController = null;
    initialLoading = false;
    earlierLoading = false;
    stickToBottom = true;
    if (convKey) {
      clearUnread(convKey);
      loadHistory(convKey, chatType, chatId, historyVersion);
    }
  }

  // 新消息到达时自动滚到底部
  $: if (msgList.length) {
    pruneCurrentConversationCache();
    if (stickToBottom) scrollToBottom();
  }

  onDestroy(() => {
    activeHistoryController?.abort();
    activeEarlierController?.abort();
    if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
    for (const url of imageUrlCache.values()) URL.revokeObjectURL(url);
    for (const info of fileInfoCache.values()) URL.revokeObjectURL(info.url);
  });

  /** 清除当前对话的未读计数 */
  function clearUnread(key) {
    if (!key) return;
    unreadCounts.update(m => {
      const n = new Map(m);
      n.delete(key);
      return n;
    });
  }

  /** 加载消息历史 */
  async function loadHistory(key, type, id, version) {
    const controller = new AbortController();
    activeHistoryController = controller;
    initialLoading = true;
    try {
      let data;
      if (type === 'private') {
        data = await api.getPrivateMessages(id, { limit: 50, signal: controller.signal });
      } else if (type === 'group') {
        data = await api.getGroupMessages(id, { limit: 50, signal: controller.signal });
      }
      if (controller.signal.aborted || version !== historyVersion || key !== convKey) return;
      if (data && data.messages) {
        messages.update(m => {
          const n = new Map(m);
          // Preserve messages that arrived through WebSocket while this request was pending.
          n.set(key, mergeMessageLists(data.messages, n.get(key) || [], MAX_RENDERED_MESSAGES));
          return n;
        });
        if (data.messages.length > 0) {
          oldestId = data.messages[0].id;
        }
        await tick();
        if (version === historyVersion && key === convKey) scrollToBottom();
      }
    } catch (error) {
      if (!controller.signal.aborted && version === historyVersion && key === convKey) {
        historyError = error.message || '消息加载失败，请重试';
      }
    } finally {
      if (version === historyVersion && activeHistoryController === controller) {
        activeHistoryController = null;
        initialLoading = false;
      }
    }
  }

  /** 加载更早的消息 */
  async function loadEarlier() {
    if (initialLoading || earlierLoading || !oldestId || !convKey) return;
    const key = convKey;
    const type = chatType;
    const id = chatId;
    const beforeId = oldestId;
    const version = historyVersion;
    const controller = new AbortController();
    activeEarlierController = controller;
    const previousScrollTop = msgListEl?.scrollTop || 0;
    const previousScrollHeight = msgListEl?.scrollHeight || 0;
    earlierLoading = true;
    stickToBottom = false;
    try {
      let data;
      if (type === 'private') {
        data = await api.getPrivateMessages(id, {
          limit: 20,
          beforeId,
          signal: controller.signal,
        });
      } else if (type === 'group') {
        data = await api.getGroupMessages(id, {
          limit: 20,
          beforeId,
          signal: controller.signal,
        });
      }
      if (controller.signal.aborted || version !== historyVersion || key !== convKey) return;
      if (data && data.messages && data.messages.length > 0) {
        messages.update(m => {
          const n = new Map(m);
          const current = n.get(key) || [];
          n.set(key, mergeMessageLists(data.messages, current, MAX_RENDERED_MESSAGES));
          return n;
        });
        oldestId = data.messages[0].id;

        await tick();
        if (version === historyVersion && key === convKey && msgListEl) {
          msgListEl.scrollTop = previousScrollTop + msgListEl.scrollHeight - previousScrollHeight;
        }
      }
    } catch (error) {
      if (!controller.signal.aborted && version === historyVersion && key === convKey) {
        historyError = error.message || '更早消息加载失败，请重试';
      }
    } finally {
      if (activeEarlierController === controller) activeEarlierController = null;
      if (version === historyVersion && key === convKey) earlierLoading = false;
    }
  }

  async function loadGroupKey(groupId) {
    loadingGroupKeyId = groupId;
    attemptedGroupKeyId = groupId;
    groupKeyError = '';
    try {
      const group = $groups.get(groupId);
      if (!group) throw new Error('群组信息不存在');
      const members = await api.getGroupMembers(groupId);
      rememberGroupMembers(groupId, members);
      const ownKey = members.find(member => member.user_id === $auth.id)?.encrypted_key;
      if (!ownKey) throw new Error('未找到你的群密钥');

      let creator = group.creator_id === $auth.id ? $auth : $users.get(group.creator_id);
      if (!creator?.public_key && group.creator_id !== $auth.id) {
        creator = await api.getUser(group.creator_id);
      }
      const creatorPublicKey = group.creator_id === $auth.id
        ? $auth.publicKey
        : base64ToBytes(creator.public_key);
      const groupKey = decryptGroupKey(ownKey, creatorPublicKey, $auth.secretKey);
      if (!groupKey) throw new Error('无法解密群密钥');
      saveGroupKey(groupId, groupKey);
    } catch (e) {
      groupKeyError = e.message || '无法加载群密钥';
    } finally {
      if (loadingGroupKeyId === groupId) loadingGroupKeyId = null;
    }
  }

  async function loadGroupMemberNames(groupId) {
    loadingMemberGroupId = groupId;
    try {
      const members = await api.getGroupMembers(groupId);
      if (chatType === 'group' && chatId === groupId) rememberGroupMembers(groupId, members);
    } catch {
      // 消息仍可用用户 ID 的首字母显示头像，成员目录失败不阻断聊天。
    } finally {
      if (loadingMemberGroupId === groupId) loadingMemberGroupId = null;
    }
  }

  function rememberGroupMembers(groupId, members) {
    if (chatType !== 'group' || chatId !== groupId) return;
    groupMembers = new Map(members.map(member => [member.user_id, member]));
    loadedMemberGroupId = groupId;
  }

  function senderName(msg) {
    if (msg.sender_id === $auth.id) return $auth.username;
    return msg.from_username || $users.get(msg.sender_id)?.username || groupMembers.get(msg.sender_id)?.username || `用户 ${msg.sender_id}`;
  }

  function avatarUrlFor(msg) {
    // Reference the revision so top-bar avatar updates refresh existing messages.
    avatarTick;
    if (msg.sender_id === $auth.id) return getLocalAvatar(msg.sender_id);
    return msg.from_avatar || $users.get(msg.sender_id)?.avatar || groupMembers.get(msg.sender_id)?.avatar || getLocalAvatar(msg.sender_id);
  }

  /** 解密消息内容 */
  function getDecrypted(msg) {
    const cacheKey = messageCacheKey(msg);
    if (decryptedCache.has(cacheKey)) {
      return decryptedCache.get(cacheKey);
    }

    if (isImage(msg) || isFile(msg)) return null;
    const bytes = getDecryptedBytes(msg);
    const plaintext = bytes ? new TextDecoder().decode(bytes) : null;
    decryptedCache.set(cacheKey, plaintext);
    return plaintext;
  }

  function isImage(msg) {
    return isImageType(msg.content_type || '');
  }

  function isFile(msg) {
    return msg.content_type === FILE_CONTENT_TYPE;
  }

  function getDecryptedBytes(msg) {
    const cacheKey = messageCacheKey(msg);
    if (decryptedBytesCache.has(cacheKey)) return decryptedBytesCache.get(cacheKey);

    let bytes = null;

    if (chatType === 'private') {
      // 确定发送者和接收者的密钥
      const isSelf = msg.sender_id === $auth.id;
      if (isSelf) {
        // 我发的，用我的私钥 + 对方公钥解密
        if (peerPublicKey) {
          bytes = decryptPrivateBytes(
            msg.encrypted_content,
            peerPublicKey,
            $auth.secretKey
          );
        }
      } else {
        // 收到的，用发送者公钥 + 我的私钥解密
        const senderUser = $users.get(msg.sender_id);
        if (senderUser?.public_key) {
          const senderPK = base64ToBytes(senderUser.public_key);
          bytes = decryptPrivateBytes(
            msg.encrypted_content,
            senderPK,
            $auth.secretKey
          );
        }
      }
    } else if (chatType === 'group' && gKey) {
      bytes = decryptGroupBytes(msg.encrypted_content, gKey);
    }

    decryptedBytesCache.set(cacheKey, bytes);
    return bytes;
  }

  function getImageUrl(msg) {
    if (!isImage(msg)) return null;
    const cacheKey = messageCacheKey(msg);
    if (imageUrlCache.has(cacheKey)) return imageUrlCache.get(cacheKey);
    const bytes = getDecryptedBytes(msg);
    if (!bytes) return null;
    const url = URL.createObjectURL(new Blob([bytes], { type: msg.content_type }));
    imageUrlCache.set(cacheKey, url);
    return url;
  }

  function getAttachmentInfo(msg) {
    if (!isFile(msg)) return null;
    const cacheKey = messageCacheKey(msg);
    if (fileInfoCache.has(cacheKey)) return fileInfoCache.get(cacheKey);
    const attachment = parseFilePayload(getDecryptedBytes(msg) || new Uint8Array());
    if (!attachment) return null;
    const info = {
      ...attachment,
      url: URL.createObjectURL(new Blob([attachment.data], { type: attachment.type })),
    };
    fileInfoCache.set(cacheKey, info);
    return info;
  }

  function getFileInfo(msg) {
    const attachment = getAttachmentInfo(msg);
    return attachment?.isImage ? null : attachment;
  }

  function getAttachmentImageUrl(msg) {
    const attachment = getAttachmentInfo(msg);
    return attachment?.isImage ? attachment.url : getImageUrl(msg);
  }

  function messageCacheKey(msg) {
    return `${convKey}:${msg.client_message_id || msg.id}`;
  }

  /** 发送消息（含本地回声 — 立即显示已发送的消息） */
  async function handleSend(e) {
    sendError = '';
    const files = e.detail.files || (e.detail.file ? [e.detail.file] : []);
    const plaintext = e.detail.text || '';
    const mention = !files.length && plaintext.match(/^@([A-Za-z0-9_.-]+)\s+([\s\S]+)$/);
    if (mention) {
      const target = $agents.find(agent => agent.id === mention[1] || agent.name.toLowerCase() === mention[1].toLowerCase());
      if (target) {
        delegation = { agent: target, title: `委派给 ${target.name}`, goal: mention[2].trim() };
        return;
      }
    }
    if (files.length) {
      for (const file of files) {
        await sendPayload(
          await createFilePayload(file, file.name, plaintext),
          FILE_CONTENT_TYPE
        );
      }
      return;
    }
    await sendPayload(plaintext, 'text/plain');
  }

  async function createMentionTask() {
    if (!delegation || !delegation.title.trim() || !delegation.goal.trim() || creatingTask) return;
    creatingTask = true;
    sendError = '';
    try {
      await api.createTask({
        title: delegation.title.trim(), goal: delegation.goal.trim(), assigned_agent_id: delegation.agent.id,
        budget_snapshot: { max_total_tokens: 12000, max_tool_calls: 24, max_concurrent_runs: 2, max_depth: 3, max_subtasks: 4 },
      });
      delegation = null;
    } catch (error) {
      sendError = error.message || '无法创建委派任务';
    } finally {
      creatingTask = false;
    }
  }

  async function sendPayload(payload, contentType) {
    const now = new Date().toISOString();
    let encrypted, msgType, extraFields;

    if (chatType === 'private') {
      if (!peerPublicKey) return;
      encrypted = payload instanceof Uint8Array
        ? encryptPrivateBytes(payload, peerPublicKey, $auth.secretKey)
        : encryptPrivate(payload, peerPublicKey, $auth.secretKey);
      msgType = 'private';
      extraFields = {
        to_user_id: chatId,
        sender_id: $auth.id,
        recipient_id: chatId,
        from_username: $auth.username,
        delivered: false,
      };
    } else if (chatType === 'group') {
      if (!gKey) return;
      encrypted = payload instanceof Uint8Array
        ? encryptGroupBytes(payload, gKey)
        : encryptGroup(payload, gKey);
      msgType = 'group';
      extraFields = {
        group_id: chatId,
        sender_id: $auth.id,
        from_username: $auth.username,
      };
    } else {
      return;
    }

    const clientMessageId = globalThis.crypto?.randomUUID?.()
      || `msg_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    if (!ws?.send({
      type: msgType,
      encrypted_content: encrypted,
      content_type: contentType,
      created_at: now,
      client_message_id: clientMessageId,
      ...(msgType === 'private' ? { to_user_id: chatId } : { group_id: chatId }),
    })) {
      sendError = '连接未建立，消息尚未发送。请稍后重试。';
      return;
    }

    // 本地回声：发送成功进入 WebSocket 缓冲后立即显示。
    const tempId = `temp_${clientMessageId}`;
    const localMsg = {
      id: tempId,
      encrypted_content: encrypted,
      content_type: contentType,
      created_at: now,
      client_message_id: clientMessageId,
      ...extraFields,
    };

    messages.update(m => {
      const n = new Map(m);
      const arr = n.get(convKey) || [];
      n.set(convKey, [...arr, localMsg].slice(-MAX_RENDERED_MESSAGES));
      return n;
    });

    // 缓存明文（本地消息不需要解密）
    if (typeof payload === 'string') decryptedCache.set(messageCacheKey(localMsg), payload);

  }

  let sendError = '';

  function handleInputError(event) {
    sendError = event.detail.message;
  }

  function stageFiles(files) {
    const accepted = files.filter(Boolean).filter(file => {
      const error = validateFile(file);
      if (error) sendError = error;
      return !error;
    });
    if (accepted.length) pendingFiles = [...pendingFiles, ...accepted];
  }

  function handleDragOver(event) {
    event.preventDefault();
    draggingFile = true;
  }

  function handleDrop(event) {
    event.preventDefault();
    draggingFile = false;
    stageFiles([...event.dataTransfer.files]);
  }

  function handlePaste(event) {
    const item = [...event.clipboardData.items].find(item => item.kind === 'file');
    if (!item) return;
    event.preventDefault();
    stageFiles([item.getAsFile()]);
  }

  async function collectImage(event) {
    const msg = event.detail.msg;
    const attachment = getAttachmentInfo(msg);
    const bytes = attachment?.data || getDecryptedBytes(msg);
    if (!bytes) return;
    try {
      const type = attachment?.type || msg.content_type;
      await addSticker(new File([bytes], `sticker-${Date.now()}.${type.split('/')[1] || 'png'}`, { type }));
      sendError = '已收藏为表情';
    } catch {
      sendError = '收藏表情失败';
    }
  }

  /** 滚动到底部 */
  function scrollToBottom() {
    if (!msgListEl || scrollFrame !== null) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = null;
      if (msgListEl) msgListEl.scrollTop = msgListEl.scrollHeight;
    });
  }

  function releaseConversationCache(key) {
    const prefix = `${key}:`;
    for (const [cacheKey, url] of imageUrlCache) {
      if (cacheKey.startsWith(prefix)) {
        URL.revokeObjectURL(url);
        imageUrlCache.delete(cacheKey);
      }
    }
    for (const [cacheKey, info] of fileInfoCache) {
      if (cacheKey.startsWith(prefix)) {
        URL.revokeObjectURL(info.url);
        fileInfoCache.delete(cacheKey);
      }
    }
    for (const cacheKey of decryptedCache.keys()) {
      if (cacheKey.startsWith(prefix)) decryptedCache.delete(cacheKey);
    }
    for (const cacheKey of decryptedBytesCache.keys()) {
      if (cacheKey.startsWith(prefix)) decryptedBytesCache.delete(cacheKey);
    }
  }

  function pruneCurrentConversationCache() {
    const currentKeys = new Set(msgList.map(messageCacheKey));
    const prefix = `${convKey}:`;
    for (const [cacheKey, url] of imageUrlCache) {
      if (cacheKey.startsWith(prefix) && !currentKeys.has(cacheKey)) {
        URL.revokeObjectURL(url);
        imageUrlCache.delete(cacheKey);
      }
    }
    for (const [cacheKey, info] of fileInfoCache) {
      if (cacheKey.startsWith(prefix) && !currentKeys.has(cacheKey)) {
        URL.revokeObjectURL(info.url);
        fileInfoCache.delete(cacheKey);
      }
    }
    for (const cacheKey of decryptedCache.keys()) {
      if (cacheKey.startsWith(prefix) && !currentKeys.has(cacheKey)) decryptedCache.delete(cacheKey);
    }
    for (const cacheKey of decryptedBytesCache.keys()) {
      if (cacheKey.startsWith(prefix) && !currentKeys.has(cacheKey)) decryptedBytesCache.delete(cacheKey);
    }
  }

  /** 处理滚动到顶部加载更多 */
  function handleScroll() {
    if (!msgListEl || initialLoading || earlierLoading) return;
    const distanceFromBottom = msgListEl.scrollHeight - msgListEl.scrollTop - msgListEl.clientHeight;
    stickToBottom = distanceFromBottom < 48;
    if (msgListEl.scrollTop < 50) {
      loadEarlier();
    }
  }
</script>

<div
  class="chat-room"
  role="region"
  aria-label="聊天区域"
  class:dragging={draggingFile}
  on:dragover={handleDragOver}
  on:dragleave={() => draggingFile = false}
  on:drop={handleDrop}
  on:paste={handlePaste}
>
  <div
    class="message-list"
    bind:this={msgListEl}
    on:scroll={handleScroll}
  >
    {#if initialLoading}
      <div class="loading">加载中...</div>
    {/if}

    {#if !initialLoading && msgList.length === 0}
      <div class="empty-chat">暂无消息，发送第一条消息吧</div>
    {/if}

    {#if earlierLoading}
      <div class="loading">正在加载更早消息...</div>
    {/if}

    {#if historyError}
      <div class="loading error-state">{historyError}</div>
    {/if}

    {#if groupKeyError}
      <div class="loading">{groupKeyError}</div>
    {/if}

    {#each msgList as msg (messageRenderKey(msg))}
      {@const decrypted = getDecrypted(msg)}
      {@const imageUrl = getAttachmentImageUrl(msg)}
      {@const attachmentInfo = getAttachmentInfo(msg)}
      <Message
        {msg}
        isSelf={msg.sender_id === $auth.id}
        senderName={senderName(msg)}
        avatarUrl={avatarUrlFor(msg)}
        {decrypted}
        {imageUrl}
        fileInfo={getFileInfo(msg)}
        {attachmentInfo}
        on:collect={collectImage}
      />
    {/each}
  </div>

  {#if sendError}<div class="loading">{sendError}</div>{/if}
  {#if delegation}<section class="delegation-confirm" aria-label="确认任务委派"><div><strong>委派给 {delegation.agent.name}</strong><button type="button" title="取消委派" on:click={() => delegation = null}>×</button></div><label>任务标题<input bind:value={delegation.title} maxlength="160" /></label><label>将上传的任务目标<textarea bind:value={delegation.goal} maxlength="50000"></textarea></label><footer><button type="button" class="secondary" on:click={() => delegation = null}>取消</button><button type="button" on:click={createMentionTask} disabled={creatingTask}>{creatingTask ? '正在创建...' : '确认委派'}</button></footer></section>{/if}
  <Input
    bind:pendingFiles
    on:send={handleSend}
    on:stage={(event) => stageFiles(event.detail.files)}
    on:remove={(event) => pendingFiles = pendingFiles.filter((_, index) => index !== event.detail.index)}
    on:error={handleInputError}
  />
</div>

<style>
  .chat-room {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }

  .chat-room.dragging {
    outline: 2px solid var(--color-primary, #4a9eff);
    outline-offset: -2px;
  }

  .message-list {
    flex: 1;
    overflow-y: auto;
    padding: var(--space-lg, 16px) 0;
    display: flex;
    flex-direction: column;
  }

  .loading {
    text-align: center;
    padding: var(--space-md, 12px);
    color: var(--color-text-muted, #888);
    font-size: var(--font-sm, 12px);
    flex-shrink: 0;
  }
  .error-state { color: var(--color-error, #ff5252); }

  .delegation-confirm { margin: 0 var(--space-lg, 16px) var(--space-sm, 8px); padding: 10px; border: 1px solid var(--color-primary); border-radius: 5px; background: var(--color-surface, #181818); display: grid; gap: 8px; }.delegation-confirm > div, .delegation-confirm footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; }.delegation-confirm label { display: grid; gap: 4px; color: var(--color-text-muted); font-size: 11px; }.delegation-confirm input, .delegation-confirm textarea { width: 100%; border: 1px solid var(--color-border); border-radius: 4px; background: var(--color-input); color: var(--color-text); padding: 6px; font: inherit; }.delegation-confirm textarea { min-height: 72px; resize: vertical; }.delegation-confirm button { border: 1px solid var(--color-primary); border-radius: 4px; background: var(--color-primary); color: #fff; padding: 6px 9px; cursor: pointer; }.delegation-confirm button.secondary, .delegation-confirm div button { background: transparent; border-color: var(--color-border); color: var(--color-text-muted); }

  .empty-chat {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--color-text-muted, #888);
    font-size: var(--font-md, 14px);
  }
</style>
