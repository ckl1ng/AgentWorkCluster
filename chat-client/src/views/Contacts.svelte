<script>
  import { onMount } from 'svelte';
  import { auth, users, groups, saveGroupKey } from '../lib/store.js';
  import { api } from '../lib/api.js';
  import { generateGroupKey, encryptGroupKeyForMember } from '../lib/crypto.js';
  import { base64ToBytes } from '../lib/utils.js';

  export let onChat = (type, id, name) => {};

  let searchQuery = '';
  let showCreateGroup = false;
  let groupName = '';
  let selectedMembers = new Set();
  let creating = false;
  let error = '';

  let searchResults = [];
  let searching = false;
  let searchError = '';
  let friendRequests = [];
  let requestStatus = '';
  let loadingContacts = true;
  let contactsError = '';
  let pendingRequestIds = new Set();
  let acceptingRequestIds = new Set();

  $: friendList = [...$users.values()];
  $: groupList = [...$groups.values()];

  onMount(loadContacts);

  async function loadContacts() {
    loadingContacts = true;
    contactsError = '';
    const [friendsResult, requestsResult] = await Promise.allSettled([api.getFriends(), api.getFriendRequests()]);
    if (friendsResult.status === 'fulfilled') {
      users.set(new Map(friendsResult.value.map(friend => [friend.id, friend])));
    }
    if (requestsResult.status === 'fulfilled') {
      friendRequests = requestsResult.value;
    }
    if (friendsResult.status === 'rejected' || requestsResult.status === 'rejected') {
      contactsError = '联系人同步失败，请检查网络后重试。';
    }
    loadingContacts = false;
  }

  /** 发起私聊 */
  function startPrivateChat(user) {
    onChat('private', user.id, user.username);
  }

  /** 切换选中群成员 */
  function toggleMember(userId) {
    if (selectedMembers.has(userId)) {
      selectedMembers.delete(userId);
    } else {
      selectedMembers.add(userId);
    }
    selectedMembers = new Set(selectedMembers);
  }

  async function searchUsers() {
    searchError = '';
    requestStatus = '';
    const query = searchQuery.trim();
    if (query.length < 2) {
      searchError = '请输入至少 2 个字符的用户名';
      return;
    }
    searching = true;
    try {
      searchResults = await api.searchUsers(query);
    } catch (e) {
      searchError = e.message || '搜索失败';
    } finally {
      searching = false;
    }
  }

  async function sendFriendRequest(userId) {
    requestStatus = '';
    pendingRequestIds = new Set([...pendingRequestIds, userId]);
    try {
      await api.sendFriendRequest(userId);
      requestStatus = '好友请求已发送';
    } catch (e) {
      requestStatus = e.message || '发送失败';
      pendingRequestIds = new Set([...pendingRequestIds].filter(id => id !== userId));
    }
  }

  async function acceptFriendRequest(userId) {
    requestStatus = '';
    acceptingRequestIds = new Set([...acceptingRequestIds, userId]);
    try {
      await api.acceptFriendRequest(userId);
      await loadContacts();
      requestStatus = '已添加为好友';
    } catch (e) {
      requestStatus = e.message || '接受失败';
    } finally {
      acceptingRequestIds = new Set([...acceptingRequestIds].filter(id => id !== userId));
    }
  }

  /** 创建群组 */
  async function createGroup() {
    error = '';
    if (!$auth.secretKey) {
      error = '此设备没有聊天密钥，无法创建群组';
      return;
    }
    if (!groupName.trim()) {
      error = '请输入群组名称';
      return;
    }
    if (groupName.length > 64) {
      error = '群组名需为 1-64 字符';
      return;
    }
    if (selectedMembers.size === 0) {
      error = '请选择至少一个成员';
      return;
    }

    creating = true;
    try {
      const memberIds = [$auth.id, ...selectedMembers];
      const gKey = generateGroupKey();

      // 获取每个成员的公钥并用它加密群密钥
      const encryptedKeys = [];
      for (const uid of memberIds) {
        const user = uid === $auth.id ? $auth : $users.get(uid);
        const memberPK = uid === $auth.id ? $auth.publicKey : base64ToBytes(user?.public_key || '');
        if (!memberPK || memberPK.length !== 32) {
          error = `无法获取用户 ${uid} 的公钥`;
          creating = false;
          return;
        }
        const encrypted = encryptGroupKeyForMember(gKey, memberPK, $auth.secretKey);
        encryptedKeys.push(encrypted);
      }

      const result = await api.createGroup(groupName.trim(), memberIds, encryptedKeys);

      // 本地保存群密钥
      saveGroupKey(result.group_id, gKey);

      // 刷新群组列表
      const updatedGroups = await api.listGroups();
      groups.set(new Map(updatedGroups.map(g => [g.id, g])));

      // 重置表单
      groupName = '';
      selectedMembers = new Set();
      showCreateGroup = false;
      onChat('group', result.group_id, result.name);
    } catch (e) {
      error = e.message || '创建失败';
    } finally {
      creating = false;
    }
  }

</script>

<div class="contacts-page">
  <div class="search-bar">
    <input
      type="text"
      placeholder="搜索用户..."
      bind:value={searchQuery}
      on:keydown={(event) => event.key === 'Enter' && searchUsers()}
    />
    <button class="join-btn" disabled={searching} on:click={searchUsers}>
      {searching ? '搜索中...' : '搜索'}
    </button>
    <button
      class="create-btn"
      on:click={() => { showCreateGroup = !showCreateGroup; error = ''; }}
    >
      {showCreateGroup ? '取消' : '+ 创建群组'}
    </button>
    <button class="refresh-btn" type="button" title="刷新联系人和请求" disabled={loadingContacts} on:click={loadContacts}>↻</button>
  </div>

  {#if contactsError}
    <div class="contacts-error" role="status"><span>{contactsError}</span><button type="button" on:click={loadContacts}>重试</button></div>
  {/if}

  {#if searchError || requestStatus}
    <p class:error={searchError} class:hint={!searchError}>{searchError || requestStatus}</p>
  {/if}

  {#if searchResults.length > 0}
    <div class="section">
      <h4>搜索结果</h4>
      <div class="user-list">
        {#each searchResults as user (user.id)}
          <div class="user-item">
            <span class="avatar">💬</span>
            <span class="name">{user.username}</span>
            {#if $users.has(user.id)}
              <span class="hint">已是好友</span>
            {:else if pendingRequestIds.has(user.id)}
              <span class="hint">请求已发送</span>
            {:else}
              <button class="primary" on:click={() => sendFriendRequest(user.id)}>添加好友</button>
            {/if}
          </div>
        {/each}
      </div>
    </div>
  {/if}

  {#if friendRequests.length > 0}
    <div class="section">
      <h4>好友请求</h4>
      <div class="user-list">
        {#each friendRequests as user (user.id)}
          <div class="user-item">
            <span class="avatar">💬</span>
            <span class="name">{user.username}</span>
            <button class="primary" disabled={acceptingRequestIds.has(user.id)} on:click={() => acceptFriendRequest(user.id)}>{acceptingRequestIds.has(user.id) ? '处理中...' : '接受'}</button>
          </div>
        {/each}
      </div>
    </div>
  {/if}

  {#if showCreateGroup}
    <div class="create-group-form">
      <input
        type="text"
        placeholder="群组名称"
        bind:value={groupName}
        maxlength="64"
      />
      <p class="hint">选择群成员（已选 {selectedMembers.size} 人）：</p>
      <div class="member-select">
        {#each friendList as user (user.id)}
          <label class="member-item">
            <input
              type="checkbox"
              checked={selectedMembers.has(user.id)}
              on:change={() => toggleMember(user.id)}
            />
            <span>{user.username}</span>
          </label>
        {/each}
        {#if friendList.length === 0}<p class="empty">先添加至少一位好友，才能创建群聊。</p>{/if}
      </div>
      <button
        class="primary"
        disabled={creating}
        on:click={createGroup}
      >
        {creating ? '创建中...' : '创建群组'}
      </button>
      {#if error}
        <p class="error">{error}</p>
      {/if}
    </div>
  {/if}

  <div class="section">
    <h4>好友</h4>
    <div class="user-list">
      {#each friendList as user (user.id)}
        <button class="user-item" on:click={() => startPrivateChat(user)}>
          <span class="avatar">💬</span>
          <span class="name">{user.username}</span>
          <span class="arrow">→</span>
        </button>
      {/each}
      {#if friendList.length === 0}
        <p class="empty">{loadingContacts ? '正在加载联系人...' : '暂无好友，请通过搜索添加'}</p>
      {/if}
    </div>
  </div>

  <div class="section">
    <h4>群组</h4>
    <div class="user-list">
      {#each groupList as group (group.id)}
        <button class="user-item" on:click={() => onChat('group', group.id, group.name)}>
          <span class="avatar">👥</span>
          <span class="name">{group.name}</span>
          <span class="arrow">→</span>
        </button>
      {/each}
      {#if groupList.length === 0}
        <p class="empty">{loadingContacts ? '正在加载群组...' : '暂无群组，可通过上方按钮创建'}</p>
      {/if}
    </div>
  </div>
</div>

<style>
  .contacts-page {
    flex: 1;
    overflow-y: auto;
    padding: var(--space-lg, 16px) var(--space-xl, 24px);
  }

  .search-bar {
    display: flex;
    gap: var(--space-sm, 8px);
    margin-bottom: var(--space-lg, 16px);
  }

  .search-bar input {
    flex: 1;
    padding: 8px 12px;
    border: 1px solid var(--color-border, #2a2a2a);
    border-radius: var(--radius-md, 8px);
    background: var(--color-input, #0f0f0f);
    color: var(--color-text, #e0e0e0);
    font-size: var(--font-md, 14px);
    outline: none;
  }

  .search-bar input:focus {
    border-color: var(--color-primary, #4a9eff);
  }

  .create-btn {
    padding: 8px 16px;
    border: 1px solid var(--color-primary, #4a9eff);
    border-radius: var(--radius-md, 8px);
    background: transparent;
    color: var(--color-primary, #4a9eff);
    cursor: pointer;
    font-size: var(--font-sm, 12px);
    white-space: nowrap;
  }

  .create-btn:hover {
    background: var(--color-active);
  }

  .join-btn {
    padding: 8px 16px;
    border: 1px solid var(--color-border, #2a2a2a);
    border-radius: var(--radius-md, 8px);
    background: transparent;
    color: var(--color-text-muted, #888);
    cursor: pointer;
    font-size: var(--font-sm, 12px);
    white-space: nowrap;
  }

  .join-btn:hover {
    color: var(--color-text, #e0e0e0);
  }

  .refresh-btn { width: 36px; padding: 0; border: 1px solid var(--color-border, #2a2a2a); border-radius: var(--radius-md, 8px); background: transparent; color: var(--color-text-muted, #888); cursor: pointer; font-size: 18px; line-height: 1; }
  .refresh-btn:hover:not(:disabled) { border-color: var(--color-primary, #4a9eff); color: var(--color-primary, #4a9eff); }
  .refresh-btn:disabled { opacity: .5; cursor: default; }
  .contacts-error { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: -4px 0 14px; padding: 9px 10px; border: 1px solid color-mix(in srgb, var(--color-error) 45%, var(--color-border)); border-radius: var(--radius-sm, 4px); background: color-mix(in srgb, var(--color-error) 8%, var(--color-surface)); color: var(--color-text-muted, #888); font-size: 12px; }
  .contacts-error button { min-height: 28px; padding: 0 8px; border: 1px solid var(--color-border); border-radius: 4px; background: transparent; color: var(--color-text); cursor: pointer; font-size: 12px; }

  .create-group-form {
    padding: var(--space-lg, 16px);
    margin-bottom: var(--space-lg, 16px);
    border: 1px solid var(--color-border, #2a2a2a);
    border-radius: var(--radius-md, 8px);
    background: var(--color-surface, #1a1a1a);
    box-shadow: var(--shadow-elevated);
  }

  .create-group-form input {
    width: 100%;
    padding: 8px 12px;
    margin-bottom: var(--space-sm, 8px);
    border: 1px solid var(--color-border, #2a2a2a);
    border-radius: var(--radius-sm, 4px);
    background: var(--color-input, #0f0f0f);
    color: var(--color-text, #e0e0e0);
    font-size: var(--font-md, 14px);
    outline: none;
  }

  .hint {
    font-size: var(--font-sm, 12px);
    color: var(--color-text-muted, #888);
    margin-bottom: var(--space-sm, 8px);
  }

  .member-select {
    max-height: 200px;
    overflow-y: auto;
    margin-bottom: var(--space-md, 12px);
  }

  .member-item {
    display: flex;
    align-items: center;
    gap: var(--space-sm, 8px);
    padding: 4px 0;
    cursor: pointer;
    font-size: var(--font-md, 14px);
  }

  .primary {
    width: 100%;
    padding: 8px;
    border: none;
    border-radius: var(--radius-md, 8px);
    background: var(--color-primary, #4a9eff);
    color: #fff;
    cursor: pointer;
    font-size: var(--font-md, 14px);
  }

  .primary:disabled {
    opacity: 0.4;
  }

  .user-item .primary { width: auto; min-width: 72px; padding: 7px 10px; }

  .error {
    margin-top: var(--space-sm, 8px);
    font-size: var(--font-sm, 12px);
    color: var(--color-error, #ff5252);
  }

  .section {
    margin-bottom: var(--space-xl, 24px);
  }

  h4 {
    font-size: var(--font-sm, 12px);
    color: var(--color-text-muted, #888);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: var(--space-sm, 8px);
  }

  .user-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .user-item {
    display: flex;
    align-items: center;
    gap: var(--space-md, 12px);
    width: 100%;
    padding: var(--space-sm, 8px) var(--space-md, 12px);
    border: none;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
    border-radius: var(--radius-md, 8px);
    font-size: var(--font-md, 14px);
    transition: background 0.1s;
  }

  .user-item:hover {
    background: var(--color-hover);
  }

  .avatar {
    font-size: 20px;
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .name {
    flex: 1;
  }

  .arrow {
    color: var(--color-text-muted, #888);
    font-size: var(--font-sm, 12px);
  }

  .empty {
    padding: var(--space-lg, 16px);
    text-align: center;
    color: var(--color-text-muted, #888);
    font-size: var(--font-sm, 12px);
  }

  @media (max-width: 768px) {
    .contacts-page { padding: 16px 12px; }
    .search-bar { display: grid; grid-template-columns: 1fr auto; margin-bottom: 14px; }
    .search-bar input { grid-column: 1 / -1; min-width: 0; }
    .join-btn, .create-btn { padding: 8px 12px; }
    .refresh-btn { height: 36px; }
    .create-group-form { padding: 12px; }
    .section { margin-bottom: 20px; }
    .user-item { padding: 9px 8px; }
  }
</style>
