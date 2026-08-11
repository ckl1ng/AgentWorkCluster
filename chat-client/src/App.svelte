<script>
  import { fade } from 'svelte/transition';
  import { auth, activeChat, agents, onlineStatus, messages, unreadCounts, users, groups, saveLocalAvatar, clearSessionState } from './lib/store.js';
  import { ChatWebSocket } from './lib/ws.js';
  import { api } from './lib/api.js';
  import { base64ToBytes } from './lib/utils.js';
  import { hasMessage, mergeMessageLists, upsertMessage } from './lib/message-list.js';
  import Login from './views/Login.svelte';
  import Header from './components/Header.svelte';
  import Sidebar from './components/Sidebar.svelte';
  import ChatRoom from './views/ChatRoom.svelte';
  import Contacts from './views/Contacts.svelte';
  import AgentCreate from './views/AgentCreate.svelte';
  import AgentChat from './views/AgentChat.svelte';
  import HelpCenter from './views/HelpCenter.svelte';
  import TaskPanel from './views/TaskPanel.svelte';

  let activeView = 'chat';  // 'chat' | 'contacts' | 'agent-create' | 'help'
  let sidebarOpen = typeof window === 'undefined' || !window.matchMedia('(max-width: 768px)').matches;
  let theme = 'dark';
  let directoryLoading = false;
  let directoryError = '';
  const MAX_CACHED_MESSAGES_PER_CONVERSATION = 200;

  /** @type {ChatWebSocket|null} */
  let ws = null;

  if (typeof localStorage !== 'undefined') {
    theme = localStorage.getItem('chat_theme') === 'light' ? 'light' : 'dark';
  }

  $: if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    try {
      localStorage.setItem('chat_theme', theme);
    } catch { /* A blocked storage area should not prevent theme changes. */ }
  }

  // 尝试从 localStorage 恢复登录态
  $: {
    if (!$auth) {
      const saved = localStorage.getItem('chat_auth');
      if (saved) {
        try {
          const parsed = JSON.parse(saved);
          auth.set({
            id: parsed.id,
            username: parsed.username,
            token: parsed.token,
            publicKey: base64ToBytes(parsed.publicKey),
            secretKey: base64ToBytes(parsed.secretKey),
          });
        } catch {
          localStorage.removeItem('chat_auth');
        }
      }
    }
  }

  // 登录成功后初始化
  $: if ($auth && !ws) {
    initApp();
  }

  function initApp() {
    const token = $auth.token;
    ws = new ChatWebSocket(token);
    ws.onMessage(handleWsMessage);
    ws.connect();

    loadDirectory();
  }

  async function loadUsers() {
    const data = await api.getFriends();
    const map = new Map();
    for (const u of data) {
      if (u && u.id != null) map.set(u.id, u);
    }
    users.set(map);
  }

  async function loadGroups() {
    const data = await api.listGroups();
    const map = new Map();
    for (const g of data) {
      if (g && g.id != null) map.set(g.id, g);
    }
    groups.set(map);
  }

  async function loadProfile() {
    const profile = await api.getMe();
    if (profile?.avatar) saveLocalAvatar(profile.id, profile.avatar);
  }

  async function loadAgents() {
    const data = await api.listAgents();
    agents.set(Array.isArray(data) ? data : []);
  }

  async function loadDirectory() {
    if (directoryLoading) return;
    directoryLoading = true;
    directoryError = '';
    const results = await Promise.allSettled([loadUsers(), loadGroups(), loadProfile(), loadAgents()]);
    const failed = results.find(result => result.status === 'rejected');
    if (failed) {
      if (failed.reason?.status === 401) {
        handleLogout();
      } else {
        directoryError = '无法同步群聊和联系人，请检查网络后重试。';
      }
    }
    directoryLoading = false;
  }

  function handleWsMessage(msg) {
    switch (msg.type) {
      case 'connected':
        // 服务端确认连接建立，记录自身连接状态
        onlineStatus.update(m => {
          const n = new Map(m);
          n.set($auth.id, true);
          return n;
        });
        // 重连后刷新对话列表
        refreshOnReconnect();
        break;
      case 'user_online':
        onlineStatus.update(m => {
          const n = new Map(m);
          n.set(msg.user_id, true);
          return n;
        });
        break;
      case 'user_offline':
        onlineStatus.update(m => {
          const n = new Map(m);
          n.set(msg.user_id, false);
          return n;
        });
        break;
      case 'private':
        handlePrivateMessage(msg);
        break;
      case 'group':
        handleGroupMessage(msg);
        break;
      case 'ack':
        handleAck(msg);
        break;
    }
  }

  function handlePrivateMessage(msg) {
    const convKey = `private:${msg.from_user_id}`;
    const received = {
      id: msg.message_id,
      sender_id: msg.from_user_id,
      recipient_id: $auth.id,
      encrypted_content: msg.encrypted_content,
      content_type: msg.content_type || 'text/plain',
      created_at: msg.created_at,
      from_username: msg.from_username || '',
      from_avatar: msg.from_avatar || '',
      client_message_id: msg.client_message_id,
      delivered: true,
    };
    let isNewMessage = false;

    messages.update(m => {
      const n = new Map(m);
      const arr = n.get(convKey) || [];
      isNewMessage = !hasMessage(arr, received);
      n.set(convKey, upsertMessage(arr, received, MAX_CACHED_MESSAGES_PER_CONVERSATION));
      return n;
    });

    // 如果当前不在该对话中，增加未读计数
    const isActive = $activeChat?.type === 'private' && $activeChat?.id === msg.from_user_id;
    if (isNewMessage && !isActive && msg.from_user_id !== $auth.id) {
      unreadCounts.update(m => {
        const n = new Map(m);
        n.set(convKey, (n.get(convKey) || 0) + 1);
        return n;
      });
    }
  }

  function handleGroupMessage(msg) {
    const convKey = `group:${msg.group_id}`;
    const received = {
      id: msg.message_id,
      group_id: msg.group_id,
      sender_id: msg.from_user_id,
      encrypted_content: msg.encrypted_content,
      content_type: msg.content_type || 'text/plain',
      created_at: msg.created_at,
      from_username: msg.from_username || '',
      from_avatar: msg.from_avatar || '',
      client_message_id: msg.client_message_id,
    };
    let isNewMessage = false;

    messages.update(m => {
      const n = new Map(m);
      const arr = n.get(convKey) || [];
      isNewMessage = !hasMessage(arr, received);
      n.set(convKey, upsertMessage(arr, received, MAX_CACHED_MESSAGES_PER_CONVERSATION));
      return n;
    });

    // 如果当前不在该群聊中（且不是自己发的），增加未读计数
    const isActive = $activeChat?.type === 'group' && $activeChat?.id === msg.group_id;
    if (isNewMessage && !isActive && msg.from_user_id !== $auth.id) {
      unreadCounts.update(m => {
        const n = new Map(m);
        n.set(convKey, (n.get(convKey) || 0) + 1);
        return n;
      });
    }
  }

  function handleAck(msg) {
    if (!msg.client_message_id || msg.message_id == null || msg.to_user_id == null) return;
    const convKey = `private:${msg.to_user_id}`;

    messages.update(m => {
      const n = new Map(m);
      const arr = n.get(convKey) || [];
      const pending = arr.find(item => item.client_message_id === msg.client_message_id);
      if (!pending) return m;

      const acknowledged = {
        ...pending,
        id: msg.message_id,
        delivered: msg.delivered,
      };
      n.set(convKey, mergeMessageLists([], [
        ...arr.filter(item => item !== pending),
        acknowledged,
      ], MAX_CACHED_MESSAGES_PER_CONVERSATION));
      return n;
    });
  }

  /** 重连后刷新消息 — 重新拉取当前活跃对话的历史 */
  async function refreshOnReconnect() {
    loadDirectory();
  }

  function handleLogout() {
    if (ws) {
      ws.disconnect();
      ws = null;
    }
    localStorage.removeItem('chat_auth');
    localStorage.removeItem('chat_token');
    clearSessionState();
    auth.set(null);
  }

  function handleSelectChat(type, id, name = '') {
    activeChat.set({ type, id, name });
    activeView = 'chat';
    if (window.matchMedia('(max-width: 768px)').matches) sidebarOpen = false;
  }

  function handleOpenContacts() {
    activeView = 'contacts';
    if (window.matchMedia('(max-width: 768px)').matches) sidebarOpen = false;
  }

  function handleCreateAgent() {
    activeView = 'agent-create';
    if (window.matchMedia('(max-width: 768px)').matches) sidebarOpen = false;
  }

  function handleOpenHelp() {
    activeView = 'help';
    if (window.matchMedia('(max-width: 768px)').matches) sidebarOpen = false;
  }

  function handleAgentCreated(event) {
    const agent = event.detail.agent;
    agents.update(items => [agent, ...items.filter(item => item.id !== agent.id)]);
    handleSelectChat('agent', agent.id, agent.name);
  }

  function handleAgentDeleted(event) {
    const agentId = event.detail.agentId;
    agents.update(items => items.filter(item => item.id !== agentId));
    if ($activeChat?.type === 'agent' && $activeChat.id === agentId) activeChat.set(null);
    activeView = 'chat';
  }

  function toggleSidebar() {
    sidebarOpen = !sidebarOpen;
  }

  function handleThemeChange(nextTheme) {
    theme = nextTheme === 'light' ? 'light' : 'dark';
  }

  function openHomeChat(type, item) {
    handleSelectChat(type, item.id, item.name || item.username);
  }
</script>

<div class="app">
  {#if !$auth}
    <Login />
  {:else}
    <div class="layout">
      <Sidebar
        open={sidebarOpen}
        {theme}
        loading={directoryLoading}
        activeView={activeView}
        onSelectChat={handleSelectChat}
        onOpenContacts={handleOpenContacts}
        onCreateAgent={handleCreateAgent}
        onThemeChange={handleThemeChange}
        onRefresh={loadDirectory}
      />
      <button class="sidebar-overlay" class:open={sidebarOpen} aria-label="关闭侧栏" on:click={() => sidebarOpen = false}></button>
      <div class="main">
        <Header view={activeView} onLogout={handleLogout} onToggleSidebar={toggleSidebar} onOpenContacts={handleOpenContacts} onOpenHelp={handleOpenHelp} />
        <div class="workspace">
          <div class="stage-shell">
            <div class="stage-surface">
              {#key `${activeView}:${$activeChat?.type || 'none'}:${$activeChat?.id || 'none'}`}
                <div class="view-stage" transition:fade={{ duration: 180 }}>
                  {#if activeView === 'help'}
                    <HelpCenter on:close={() => activeView = 'chat'} />
                  {:else if activeView === 'chat' && $activeChat?.type === 'agent'}
                    <AgentChat agentId={$activeChat.id} on:deleted={handleAgentDeleted} />
                  {:else if activeView === 'chat' && $activeChat}
                    <ChatRoom {ws} />
                  {:else if activeView === 'agent-create'}
                    <AgentCreate on:created={handleAgentCreated} on:cancel={() => activeView = 'chat'} />
                  {:else if activeView === 'contacts'}
                    <Contacts onChat={handleSelectChat} />
                  {:else}
                    <div class="home">
                      <section class="home-hero">
                        <div class="hero-copy">
                          <p class="home-kicker">SECURE WORKSPACE</p>
                          <h2>把聊天、Agent 和任务放进同一个操作台。</h2>
                          <p>最近会话、可执行 Agent 和需要处理的任务都在一个连续的工作流里，不需要来回切换心智。</p>
                        </div>
                        <div class="hero-metrics" aria-label="工作区概览">
                          <div class="metric-card"><span>联系人</span><strong>{$users.size}</strong><small>可直接发起私聊</small></div>
                          <div class="metric-card"><span>群聊</span><strong>{$groups.size}</strong><small>端到端加密同步</small></div>
                          <div class="metric-card"><span>Agent</span><strong>{$agents.length}</strong><small>随时接手任务</small></div>
                        </div>
                      </section>
                      {#if directoryError}
                        <div class="directory-error" role="status"><span>{directoryError}</span><button type="button" on:click={loadDirectory}>重新加载</button></div>
                      {/if}
                      <div class="home-grid">
                        <section class="home-section" aria-labelledby="home-groups-title">
                          <div class="home-section-heading"><div><p class="section-kicker">GROUP CHANNELS</p><h3 id="home-groups-title">当前群聊</h3></div><span>{$groups.size}</span></div>
                          {#if directoryLoading}
                            <p class="home-loading">正在加载群聊...</p>
                          {:else if $groups.size}
                            <div class="quick-list">
                              {#each [...$groups.values()] as group (group.id)}
                                <button class="quick-item" on:click={() => openHomeChat('group', group)}>
                                  <span class="quick-avatar group">群</span><span class="quick-text"><span class="quick-name">{group.name}</span><span class="quick-meta">已加入的协作频道</span></span><span class="quick-arrow" aria-hidden="true">›</span>
                                </button>
                              {/each}
                            </div>
                          {:else}
                            <button class="empty-home-action" on:click={handleOpenContacts}>创建或加入群聊</button>
                          {/if}
                        </section>
                        <section class="home-section" aria-labelledby="home-contacts-title">
                          <div class="home-section-heading"><div><p class="section-kicker">DIRECT CONTACTS</p><h3 id="home-contacts-title">联系人</h3></div><span>{$users.size}</span></div>
                          {#if directoryLoading}
                            <p class="home-loading">正在加载联系人...</p>
                          {:else if $users.size}
                            <div class="quick-list">
                              {#each [...$users.values()] as user (user.id)}
                                <button class="quick-item" on:click={() => openHomeChat('private', user)}>
                                  <span class="quick-avatar">{user.username.slice(0, 1).toUpperCase()}</span><span class="quick-text"><span class="quick-name">{user.username}</span><span class="quick-meta">私密会话</span></span><span class="quick-arrow" aria-hidden="true">›</span>
                                </button>
                              {/each}
                            </div>
                          {:else}
                            <button class="empty-home-action" on:click={handleOpenContacts}>添加联系人</button>
                          {/if}
                        </section>
                        <section class="home-section" aria-labelledby="home-agents-title">
                          <div class="home-section-heading"><div><p class="section-kicker">AUTOMATION</p><h3 id="home-agents-title">Agent</h3></div><span>{$agents.length}</span></div>
                          {#if directoryLoading}
                            <p class="home-loading">正在加载 Agent...</p>
                          {:else if $agents.length}
                            <div class="quick-list">
                              {#each $agents as agent (agent.id)}
                                <button class="quick-item" on:click={() => openHomeChat('agent', agent)}>
                                  <span class="quick-avatar agent">{agent.name.slice(0, 1).toUpperCase()}</span><span class="quick-text"><span class="quick-name">{agent.name}</span><span class="quick-meta">{agent.description || '可执行任务与工具流程'}</span></span><span class="quick-arrow" aria-hidden="true">›</span>
                                </button>
                              {/each}
                            </div>
                          {:else}
                            <button class="empty-home-action" on:click={handleCreateAgent}>创建个人 Agent</button>
                          {/if}
                        </section>
                      </div>
                    </div>
                  {/if}
                </div>
              {/key}
            </div>
          </div>
          <TaskPanel />
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  :global(*) {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
  }

  :global(body) {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--color-bg, #0f0f0f);
    color: var(--color-text, #e0e0e0);
    font-size: var(--font-md, 14px);
    overflow: hidden;
    min-height: 100vh;
    min-height: 100dvh;
  }

  :global(#app) {
    height: 100vh;
    height: 100dvh;
  }

  .app {
    height: 100vh;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    background:
      linear-gradient(180deg, rgba(73, 164, 255, 0.08), transparent 18%),
      linear-gradient(180deg, var(--color-bg-elevated), var(--color-bg));
  }

  .layout {
    position: relative;
    display: flex;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
  }

  .main {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    position: relative;
    padding: 18px 18px 18px 0;
  }

  .workspace {
    flex: 1;
    display: flex;
    min-height: 0;
    gap: 18px;
    overflow: hidden;
  }

  .stage-shell { flex: 1; min-width: 0; display: flex; min-height: 0; }
  .stage-surface { flex: 1; min-width: 0; min-height: 0; display: flex; overflow: hidden; border: 1px solid var(--color-border); border-radius: 26px; background: linear-gradient(180deg, rgba(255, 255, 255, 0.04), transparent 18%), var(--color-panel); backdrop-filter: blur(18px); box-shadow: var(--shadow-elevated); }
  .view-stage { flex: 1; min-width: 0; min-height: 0; display: flex; }
  .home { height: 100%; overflow-y: auto; padding: clamp(28px, 6vh, 68px) clamp(18px, 5vw, 56px); }
  .home-hero { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(280px, 0.95fr); gap: 20px; align-items: stretch; margin-bottom: 24px; }
  .hero-copy, .metric-card, .home-section { border: 1px solid var(--color-border); border-radius: 22px; background: linear-gradient(180deg, rgba(255, 255, 255, 0.03), transparent 100%), var(--color-panel-strong); box-shadow: var(--shadow-soft); }
  .hero-copy { padding: 28px 30px; }
  .home-kicker, .section-kicker { color: var(--color-primary); font-size: 11px; font-weight: 700; letter-spacing: 0.8px; }
  .hero-copy h2 { margin: 10px 0 12px; max-width: 720px; font-size: var(--font-2xl); line-height: 1.18; }
  .hero-copy p:last-child { max-width: 640px; color: var(--color-text-muted); line-height: 1.7; }
  .hero-metrics { display: grid; gap: 12px; }
  .metric-card { display: grid; gap: 6px; align-content: start; padding: 20px 22px; }
  .metric-card span { color: var(--color-text-muted); font-size: var(--font-sm); }
  .metric-card strong { font-size: 30px; line-height: 1; }
  .metric-card small { color: var(--color-text-soft); font-size: var(--font-sm); }
  .home-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
  .home-section { min-width: 0; overflow: hidden; }
  .home-section-heading { display: flex; align-items: center; justify-content: space-between; min-height: 86px; padding: 18px 20px; border-bottom: 1px solid var(--color-border); }
  .home-section-heading h3 { margin-top: 5px; font-size: 18px; }
  .home-section-heading > span { display: grid; min-width: 32px; height: 32px; place-items: center; padding: 0 10px; border-radius: 999px; background: var(--color-active); color: var(--color-primary); font-size: 12px; font-weight: 700; }
  .quick-list { padding: 10px; }
  .quick-item { display: flex; align-items: center; width: 100%; min-height: 68px; gap: 12px; padding: 10px 12px; border: 1px solid transparent; border-radius: 16px; background: transparent; color: inherit; cursor: pointer; text-align: left; }
  .quick-item:hover { transform: translateY(-1px); border-color: var(--color-border-strong); background: var(--color-hover); }
  .quick-avatar { display: grid; width: 40px; height: 40px; place-items: center; flex-shrink: 0; border-radius: 14px; background: var(--color-avatar); color: var(--color-avatar-text); font-size: 13px; font-weight: 700; }
  .quick-avatar.group { background: var(--color-group-avatar); color: var(--color-group-avatar-text); }
  .quick-avatar.agent { background: var(--color-agent-avatar); color: var(--color-agent-avatar-text); }
  .quick-text { min-width: 0; display: grid; gap: 2px; flex: 1; }
  .quick-name { overflow: hidden; font-size: 14px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
  .quick-meta { overflow: hidden; color: var(--color-text-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
  .quick-arrow { color: var(--color-text-muted); font-size: 22px; }
  .empty-home-action { width: calc(100% - 20px); min-height: 56px; margin: 10px; border: 1px dashed var(--color-border-strong); border-radius: 16px; background: transparent; color: var(--color-text-muted); cursor: pointer; font-size: 13px; text-align: left; padding: 0 16px; }
  .empty-home-action:hover { color: var(--color-primary); border-color: var(--color-primary); background: var(--color-active); }
  .home-loading { min-height: 56px; padding: 18px 20px; color: var(--color-text-muted); font-size: 13px; }
  .directory-error { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 0 20px; padding: 14px 16px; border: 1px solid color-mix(in srgb, var(--color-danger) 34%, var(--color-border)); border-radius: 16px; background: color-mix(in srgb, var(--color-danger-bg) 70%, var(--color-panel-strong)); color: var(--color-text-soft); font-size: 13px; }
  .directory-error button { flex-shrink: 0; min-height: 34px; padding: 0 12px; border: 1px solid var(--color-border); border-radius: 10px; background: transparent; color: var(--color-text); cursor: pointer; font-size: 12px; }
  @media (max-width: 1100px) {
    .home-hero, .home-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .hero-copy { grid-column: 1 / -1; }
  }
  @media (max-width: 980px) {
    .workspace { flex-direction: column; }
    .stage-shell { min-height: 52vh; }
  }
  @media (max-width: 768px) {
    .main { padding: 12px; }
    .workspace { gap: 12px; }
    .stage-surface { border-radius: 20px; }
    .home { padding: 24px 14px; }
    .hero-copy { padding: 22px 20px; }
    .hero-copy h2 { font-size: 24px; }
    .home-hero, .home-grid { grid-template-columns: 1fr; gap: 14px; }
  }

  .sidebar-overlay {
    display: none;
  }

  @media (max-width: 768px) {
    .sidebar-overlay.open {
      display: block;
      position: fixed;
      inset: 0;
      z-index: 59;
      border: 0;
      background: rgba(4, 8, 12, .48);
      backdrop-filter: blur(6px);
    }
  }
</style>
