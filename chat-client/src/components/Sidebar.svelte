<script>
  import { fade, slide } from 'svelte/transition';
  import { Bot, ChevronDown, ChevronRight, MoonStar, Plus, RefreshCw, Settings2, SunMedium, Users } from 'lucide-svelte';
  import { activeChat, agents, conversations, onlineStatus } from '../lib/store.js';
  import { formatBriefTime } from '../lib/utils.js';

  export let onSelectChat = (type, id, name) => {};
  export let open = true;
  export let theme = 'dark';
  export let loading = false;
  export let onThemeChange = (nextTheme) => {};
  export let onOpenContacts = () => {};
  export let onRefresh = () => {};
  export let onCreateAgent = () => {};
  export let activeView = 'chat';

  let settingsOpen = false;
  let collapsed = { private: false, group: false, agent: false };
  let recentKeys = [];

  if (typeof localStorage !== 'undefined') {
    try { recentKeys = JSON.parse(localStorage.getItem('chat_recent_conversations') || '[]'); } catch { recentKeys = []; }
  }

  $: groupChats = ordered($conversations.filter(conv => conv.type === 'group'));
  $: privateChats = ordered($conversations.filter(conv => conv.type === 'private'));
  $: agentItems = ordered($agents, 'agent');

  function itemKey(item, type = item.type) {
    if (type === 'agent') return `agent:${item.id}`;
    return item.key || `${item.type}:${item.type === 'private' ? item.peerId : item.groupId}`;
  }

  function ordered(items, type) {
    return [...items].sort((left, right) => {
      const leftIndex = recentKeys.indexOf(itemKey(left, type));
      const rightIndex = recentKeys.indexOf(itemKey(right, type));
      if (leftIndex < 0 && rightIndex < 0) return 0;
      if (leftIndex < 0) return 1;
      if (rightIndex < 0) return -1;
      return leftIndex - rightIndex;
    });
  }

  function promote(item, type = item.type) {
    const key = itemKey(item, type);
    recentKeys = [key, ...recentKeys.filter(existing => existing !== key)].slice(0, 100);
    try { localStorage.setItem('chat_recent_conversations', JSON.stringify(recentKeys)); } catch {}
  }

  function toggleSection(section) {
    collapsed = { ...collapsed, [section]: !collapsed[section] };
  }

  function handleClick(conv) {
    promote(conv);
    onSelectChat(conv.type, conv.type === 'private' ? conv.peerId : conv.groupId, conv.name);
  }

  $: activeKey = activeView === 'chat' && $activeChat ? `${$activeChat.type}:${String($activeChat.id)}` : '';

  function isActive(conv) {
    const id = conv.type === 'private' ? conv.peerId : conv.groupId;
    return activeKey === `${conv.type}:${String(id)}`;
  }
</script>

<aside class="app-sidebar" class:open aria-label="会话导航">
  <div class="sidebar-header">
    <div class="brand">
      <span class="brand-mark">CC</span>
      <div class="brand-copy">
        <p class="eyebrow">CHAT CLIENT</p>
        <h1>工作区</h1>
      </div>
    </div>
    <div class="header-actions">
      <button class="icon-button" type="button" title="刷新群聊和联系人" aria-label="刷新群聊和联系人" disabled={loading} on:click={onRefresh}>
        <span class:spinning={loading}><RefreshCw size={16} strokeWidth={2} /></span>
      </button>
      <button class="action-button" type="button" title="管理联系人和群组" aria-label="管理联系人和群组" on:click={onOpenContacts}>
        <Users size={15} strokeWidth={2} /><span>管理</span>
      </button>
    </div>
  </div>

  <div class="sidebar-intro">
    <p>最近的会话、协作频道和 Agent 都在这里。</p>
  </div>

  <nav class="conv-list" aria-label="当前会话">
    <section class="conversation-section">
      <div class="section-heading" role="button" tabindex="0" aria-expanded={!collapsed.group} on:click={() => toggleSection('group')} on:keydown={(event) => event.key === 'Enter' && toggleSection('group')}>
        <span class="section-title">{#if collapsed.group}<ChevronRight size={14} />{:else}<ChevronDown size={14} />{/if}<span>群聊</span></span>
        <span class="count">{groupChats.length}</span>
      </div>
      {#if !collapsed.group && loading && !groupChats.length}
        <p class="loading-state" transition:fade>正在同步...</p>
      {:else if !collapsed.group && groupChats.length}
        <div transition:slide={{ duration: 180 }}>
          {#each groupChats as conv (conv.key)}
            <button class="conv-item" class:active={isActive(conv)} on:click={() => handleClick(conv)}>
              <span class="avatar group-avatar" aria-hidden="true">群</span>
              <span class="info">
                <span class="top-row"><span class="name">{conv.name}</span>{#if conv.lastTime}<span class="time">{formatBriefTime(conv.lastTime)}</span>{/if}</span>
                <span class="preview">{conv.lastMsg ? '[加密消息]' : '开始群聊'}</span>
              </span>
              {#if conv.unread > 0}<span class="unread-badge">{conv.unread > 99 ? '99+' : conv.unread}</span>{/if}
            </button>
          {/each}
        </div>
      {:else if !collapsed.group}
        <button class="empty-action" type="button" on:click={onOpenContacts} transition:fade>创建或加入群聊</button>
      {/if}
    </section>

    <section class="conversation-section">
      <div class="section-heading" role="button" tabindex="0" aria-expanded={!collapsed.private} on:click={() => toggleSection('private')} on:keydown={(event) => event.key === 'Enter' && toggleSection('private')}>
        <span class="section-title">{#if collapsed.private}<ChevronRight size={14} />{:else}<ChevronDown size={14} />{/if}<span>私聊</span></span>
        <span class="count">{privateChats.length}</span>
      </div>
      {#if !collapsed.private && loading && !privateChats.length}
        <p class="loading-state" transition:fade>正在同步...</p>
      {:else if !collapsed.private && privateChats.length}
        <div transition:slide={{ duration: 180 }}>
          {#each privateChats as conv (conv.key)}
            {@const online = $onlineStatus.get(conv.peerId)}
            <button class="conv-item" class:active={isActive(conv)} on:click={() => handleClick(conv)}>
              <span class="avatar" aria-hidden="true">{conv.name.slice(0, 1).toUpperCase()}<i class:online class:offline={online === false}></i></span>
              <span class="info">
                <span class="top-row"><span class="name">{conv.name}</span>{#if conv.lastTime}<span class="time">{formatBriefTime(conv.lastTime)}</span>{/if}</span>
                <span class="preview">{conv.lastMsg ? '[加密消息]' : (online ? '在线' : '开始聊天')}</span>
              </span>
              {#if conv.unread > 0}<span class="unread-badge">{conv.unread > 99 ? '99+' : conv.unread}</span>{/if}
            </button>
          {/each}
        </div>
      {:else if !collapsed.private}
        <button class="empty-action" type="button" on:click={onOpenContacts} transition:fade>添加联系人</button>
      {/if}
    </section>

    <section class="conversation-section">
      <div class="section-heading" role="button" tabindex="0" aria-expanded={!collapsed.agent} on:click={() => toggleSection('agent')} on:keydown={(event) => event.key === 'Enter' && toggleSection('agent')}>
        <span class="section-title">{#if collapsed.agent}<ChevronRight size={14} />{:else}<ChevronDown size={14} />{/if}<span>Agent</span></span>
        <div class="section-tools">
          <span class="count">{$agents.length}</span>
          <button class="add-agent" type="button" title="创建 Agent" aria-label="创建 Agent" on:click|stopPropagation={onCreateAgent}><Plus size={14} /></button>
        </div>
      </div>
      {#if !collapsed.agent && $agents.length}
        <div transition:slide={{ duration: 180 }}>
          {#each agentItems as agent (agent.id)}
            <button class="conv-item" class:active={activeKey === `agent:${String(agent.id)}`} on:click={() => { promote(agent, 'agent'); onSelectChat('agent', agent.id, agent.name); }}>
              <span class="avatar agent-avatar" aria-hidden="true">{agent.name.slice(0, 1).toUpperCase()}</span>
              <span class="info"><span class="top-row"><span class="name">{agent.name}</span></span><span class="preview">{agent.state === 'active' ? (agent.description || '可运行') : '已暂停'}</span></span>
              <span class="agent-indicator"><Bot size={14} /></span>
            </button>
          {/each}
        </div>
      {:else if !collapsed.agent}
        <button class="empty-action" type="button" on:click={onCreateAgent} transition:fade>创建个人 Agent</button>
      {/if}
    </section>
  </nav>

  <div class="sidebar-footer">
    {#if settingsOpen}
      <div class="settings-panel" transition:slide={{ duration: 180 }}>
        <span class="settings-label">界面外观 <em>{theme === 'light' ? '浅色' : '深色'}</em></span>
        <div class="theme-options" role="radiogroup" aria-label="界面主题">
          <button class:active={theme === 'dark'} type="button" role="radio" aria-checked={theme === 'dark'} on:click={() => onThemeChange('dark')}>
            <MoonStar size={14} /><span>深色</span>
          </button>
          <button class:active={theme === 'light'} type="button" role="radio" aria-checked={theme === 'light'} on:click={() => onThemeChange('light')}>
            <SunMedium size={14} /><span>浅色</span>
          </button>
        </div>
      </div>
    {/if}
    <button class="settings-button" class:expanded={settingsOpen} type="button" aria-expanded={settingsOpen} on:click={() => settingsOpen = !settingsOpen}>
      <Settings2 size={16} /><span>设置</span>{#if settingsOpen}<ChevronDown size={16} />{:else}<ChevronRight size={16} />{/if}
    </button>
  </div>
</aside>

<style>
  .app-sidebar {
    width: 320px;
    min-width: 320px;
    margin: 18px 0 18px 18px;
    border: 1px solid var(--color-border);
    border-radius: 24px;
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.04), transparent 16%), var(--color-panel);
    backdrop-filter: blur(18px);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    box-shadow: var(--shadow-soft);
    transition: transform var(--duration-normal) var(--ease-soft), opacity var(--duration-normal) var(--ease-soft), width var(--duration-normal) var(--ease-soft);
    overflow: hidden;
  }

  .sidebar-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 18px 14px;
    border-bottom: 1px solid var(--color-border);
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .brand-mark {
    display: grid;
    width: 38px;
    height: 38px;
    place-items: center;
    border-radius: 14px;
    background: linear-gradient(135deg, var(--color-primary), color-mix(in srgb, var(--color-primary) 55%, var(--color-accent)));
    color: white;
    font-size: 12px;
    font-weight: 800;
  }

  .eyebrow {
    margin-bottom: 2px;
    color: var(--color-primary);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
  }

  h1 {
    font-size: 20px;
    line-height: 1.1;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .icon-button,
  .action-button,
  .add-agent,
  .settings-button,
  .theme-options button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    border: 1px solid var(--color-border);
    border-radius: 12px;
    background: var(--color-input);
    color: var(--color-text-muted);
    cursor: pointer;
  }

  .icon-button {
    width: 36px;
    height: 36px;
    padding: 0;
  }

  .action-button {
    min-height: 36px;
    padding: 0 12px;
    color: var(--color-primary);
  }

  .icon-button:hover:not(:disabled),
  .action-button:hover,
  .theme-options button:hover,
  .settings-button:hover,
  .settings-button.expanded,
  .add-agent:hover {
    color: var(--color-text);
    border-color: var(--color-border-strong);
    background: var(--color-hover);
  }

  .action-button:hover {
    color: var(--color-primary);
  }

  .icon-button:disabled {
    opacity: 0.55;
    cursor: default;
  }

  .spinning {
    animation: spin 0.9s linear infinite;
  }

  .sidebar-intro {
    padding: 0 18px 16px;
    color: var(--color-text-muted);
    font-size: 13px;
    line-height: 1.6;
  }

  .conv-list {
    flex: 1;
    overflow-y: auto;
    padding: 6px 12px 18px;
  }

  .conversation-section + .conversation-section {
    margin-top: 18px;
  }

  .section-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 34px;
    padding: 0 8px 8px;
    color: var(--color-text-muted);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.7px;
    cursor: pointer;
    user-select: none;
  }

  .section-title,
  .section-tools {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .count {
    display: inline-grid;
    min-width: 22px;
    height: 22px;
    place-items: center;
    padding: 0 6px;
    border-radius: 999px;
    background: var(--color-input);
    font-size: 10px;
    letter-spacing: 0;
  }

  .add-agent {
    width: 22px;
    height: 22px;
    padding: 0;
  }

  .conv-item {
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
    min-height: 70px;
    margin: 4px 0;
    padding: 10px 12px;
    border: 1px solid transparent;
    border-radius: 18px;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .conv-item:hover {
    transform: translateY(-1px);
    background: var(--color-hover);
    border-color: var(--color-border);
  }

  .conv-item.active {
    background: linear-gradient(180deg, rgba(73, 164, 255, 0.16), rgba(73, 164, 255, 0.08));
    border-color: color-mix(in srgb, var(--color-primary) 38%, var(--color-border));
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
  }

  .avatar {
    position: relative;
    display: grid;
    width: 42px;
    height: 42px;
    place-items: center;
    flex-shrink: 0;
    overflow: visible;
    border-radius: 16px;
    background: var(--color-avatar);
    color: var(--color-avatar-text);
    font-size: 14px;
    font-weight: 700;
  }

  .group-avatar {
    background: var(--color-group-avatar);
    color: var(--color-group-avatar-text);
    font-size: 12px;
  }

  .agent-avatar {
    background: var(--color-agent-avatar);
    color: var(--color-agent-avatar-text);
  }

  i {
    position: absolute;
    right: -2px;
    bottom: -2px;
    display: block;
    width: 12px;
    height: 12px;
    border: 2px solid var(--color-panel-strong);
    border-radius: 50%;
    background: var(--color-offline);
  }

  i.online {
    background: var(--color-online);
  }

  .info {
    min-width: 0;
    flex: 1;
  }

  .top-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
  }

  .name {
    overflow: hidden;
    font-size: 14px;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .time,
  .preview {
    color: var(--color-text-muted);
  }

  .time {
    flex-shrink: 0;
    font-size: 10px;
  }

  .preview {
    display: block;
    overflow: hidden;
    margin-top: 3px;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .unread-badge,
  .agent-indicator {
    display: grid;
    place-items: center;
    flex-shrink: 0;
  }

  .unread-badge {
    min-width: 22px;
    height: 22px;
    padding: 0 6px;
    border-radius: 999px;
    background: var(--color-primary);
    color: white;
    font-size: 10px;
    font-weight: 700;
  }

  .agent-indicator {
    width: 24px;
    height: 24px;
    border-radius: 10px;
    background: var(--color-active);
    color: var(--color-primary);
  }

  .empty-action,
  .loading-state {
    width: 100%;
    padding: 12px;
    border-radius: 16px;
    font-size: 12px;
  }

  .empty-action {
    border: 1px dashed var(--color-border-strong);
    background: transparent;
    color: var(--color-text-muted);
    cursor: pointer;
    text-align: left;
  }

  .empty-action:hover {
    color: var(--color-primary);
    border-color: var(--color-primary);
    background: var(--color-active);
  }

  .loading-state {
    color: var(--color-text-muted);
  }

  .sidebar-footer {
    flex-shrink: 0;
    border-top: 1px solid var(--color-border);
    padding: 12px;
  }

  .settings-panel {
    margin-bottom: 10px;
    padding: 12px;
    border: 1px solid var(--color-border);
    border-radius: 16px;
    background: var(--color-input);
  }

  .settings-label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
    color: var(--color-text-muted);
    font-size: 11px;
    font-weight: 600;
  }

  .settings-label em {
    color: var(--color-primary);
    font-style: normal;
    font-weight: 700;
  }

  .theme-options {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }

  .theme-options button {
    min-height: 36px;
    padding: 0 10px;
  }

  .theme-options button.active {
    color: var(--color-primary);
    border-color: var(--color-primary);
    background: var(--color-active);
  }

  .settings-button {
    width: 100%;
    min-height: 44px;
    justify-content: flex-start;
    padding: 0 12px;
  }

  .settings-button :global(svg:last-child) {
    margin-left: auto;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (min-width: 769px) {
    .app-sidebar:not(.open) {
      width: 0;
      min-width: 0;
      margin-left: 0;
      opacity: 0;
      overflow: hidden;
      transform: translateX(-16px);
      pointer-events: none;
    }
  }

  @media (max-width: 768px) {
    .app-sidebar {
      position: fixed;
      inset: 12px auto 12px 12px;
      z-index: 60;
      width: min(320px, calc(100vw - 24px));
      min-width: 0;
      margin: 0;
      transform: translateX(calc(-100% - 18px));
    }

    .app-sidebar.open {
      transform: translateX(0);
    }
  }
</style>
