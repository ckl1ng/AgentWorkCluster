<script>
  import { fade } from 'svelte/transition';
  import { CircleHelp, LogOut, Menu, MessagesSquare, SquarePen } from 'lucide-svelte';
  import { auth, activeChat, getLocalAvatar, onlineStatus, saveLocalAvatar } from '../lib/store.js';
  import { api } from '../lib/api.js';

  export let onLogout = () => {};
  export let onToggleSidebar = () => {};
  export let onOpenContacts = () => {};
  export let onOpenHelp = () => {};
  export let view = 'chat';

  let avatarInput;
  let avatarUrl = '';
  let avatarOwnerId = null;
  let avatarError = '';

  $: title = '';
  $: subtitle = '';
  $: avatarInitial = ($auth?.username || '?').trim().slice(0, 1).toUpperCase() || '?';
  $: if ($auth?.id !== avatarOwnerId) {
    avatarOwnerId = $auth?.id ?? null;
    avatarUrl = getLocalAvatar(avatarOwnerId);
    avatarError = '';
  }

  $: {
    if (view === 'help') {
      title = '帮助中心';
      subtitle = '功能说明与操作指引';
    } else if (view === 'contacts') {
      title = '联系人';
      subtitle = '搜索、添加好友和创建群组';
    } else if ($activeChat) {
      if ($activeChat.type === 'private') {
        title = $activeChat.name || `用户 ${$activeChat.id}`;
        const online = $onlineStatus.get($activeChat.id);
        subtitle = online ? '在线' : '离线';
      } else if ($activeChat.type === 'group') {
        title = $activeChat.name || `群 ${$activeChat.id}`;
        subtitle = '群组协作';
      } else {
        title = $activeChat.name || 'Agent';
        subtitle = '自动化工作流';
      }
    } else {
      title = '总览';
      subtitle = '消息、Agent 与任务';
    }
  }

  async function handleAvatarChange(event) {
    avatarError = '';
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = '';
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      avatarError = '请选择图片文件';
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      avatarError = '头像图片不能超过 5 MiB';
      return;
    }

    try {
      const compressedAvatar = await compressAvatar(file);
      await api.updateAvatar(compressedAvatar);
      saveLocalAvatar($auth.id, compressedAvatar);
      avatarUrl = compressedAvatar;
    } catch {
      avatarError = '头像同步失败，请检查连接后重试';
    }
  }

  function compressAvatar(file) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      const url = URL.createObjectURL(file);
      image.onload = () => {
        URL.revokeObjectURL(url);
        const maxSize = 320;
        const scale = Math.min(1, maxSize / Math.max(image.width, image.height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(image.width * scale));
        canvas.height = Math.max(1, Math.round(image.height * scale));
        const context = canvas.getContext('2d');
        if (!context) {
          reject(new Error('Canvas unavailable'));
          return;
        }
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL('image/jpeg', .86));
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error('Invalid image'));
      };
      image.src = url;
    });
  }
</script>

<header class="app-header">
  <div class="title-area">
    <button class="icon-button" type="button" title="显示或隐藏侧栏" aria-label="显示或隐藏侧栏" on:click={onToggleSidebar}><Menu size={18} /></button>
    <div class="title-copy">
      <div class="title-row"><h2>{title}</h2><span class="subtitle" class:online={subtitle === '在线'}>{subtitle}</span></div>
      <p>保持会话、自动化与任务在同一个上下文里。</p>
    </div>
  </div>

  <div class="user-area">
    <div class="header-actions">
      <button class="toolbar-button" type="button" title="帮助中心" aria-label="帮助中心" on:click={onOpenHelp}><CircleHelp size={16} /><span>帮助</span></button>
      <button class="toolbar-button accent" type="button" title="管理联系人和群组" aria-label="管理联系人和群组" on:click={onOpenContacts}><MessagesSquare size={16} /><span>管理</span></button>
    </div>
    <input bind:this={avatarInput} class="avatar-input" type="file" accept="image/*" on:change={handleAvatarChange} />
    <button class="avatar-button" type="button" title="设置头像" aria-label="设置头像" on:click={() => avatarInput?.click()}>
      {#if avatarUrl}
        <img src={avatarUrl} alt={`${$auth?.username || ''} 的头像`} />
      {:else}
        <span>{avatarInitial}</span>
      {/if}
      <span class="avatar-edit" aria-hidden="true"><SquarePen size={11} /></span>
    </button>
    <div class="identity"><strong>{$auth?.username || ''}</strong><span>当前账号</span></div>
    <button class="toolbar-button" type="button" title="登出" aria-label="登出" on:click={onLogout}><LogOut size={16} /><span>退出</span></button>
  </div>

  {#if avatarError}<span class="avatar-error" role="status" transition:fade>{avatarError}</span>{/if}
</header>

<style>
  .app-header { position: relative; display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 18px; padding: 14px 18px; border: 1px solid var(--color-border); border-radius: 22px; background: linear-gradient(180deg, rgba(255, 255, 255, .04), transparent 18%), var(--color-panel); backdrop-filter: blur(18px); box-shadow: var(--shadow-soft); flex-shrink: 0; }
  .title-area, .title-row, .user-area, .header-actions { display: flex; align-items: center; }
  .title-area { gap: 14px; min-width: 0; flex: 1; }
  .title-copy { min-width: 0; }
  .title-row { gap: 10px; min-width: 0; }
  h2 { overflow: hidden; font-size: 20px; text-overflow: ellipsis; white-space: nowrap; }
  .subtitle, .title-copy p, .identity span { color: var(--color-text-muted); }
  .subtitle { font-size: 12px; }
  .subtitle.online { color: var(--color-online); }
  .title-copy p { margin-top: 4px; font-size: 12px; }
  .user-area { gap: 10px; flex-shrink: 0; }
  .header-actions { gap: 8px; }
  .icon-button, .toolbar-button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; border: 1px solid var(--color-border); border-radius: 12px; background: var(--color-input); color: var(--color-text-muted); cursor: pointer; }
  .icon-button { width: 40px; height: 40px; padding: 0; }
  .toolbar-button { min-height: 40px; padding: 0 12px; }
  .toolbar-button.accent { color: var(--color-primary); }
  .icon-button:hover, .toolbar-button:hover { border-color: var(--color-border-strong); background: var(--color-hover); color: var(--color-text); }
  .toolbar-button.accent:hover { color: var(--color-primary); }
  .avatar-input { display: none; }
  .avatar-button { position: relative; display: grid; width: 40px; min-width: 40px; height: 40px; min-height: 40px; place-items: center; overflow: visible; padding: 0; border: 1px solid var(--color-border-strong); border-radius: 14px; background: var(--color-avatar); color: var(--color-avatar-text); font-size: 14px; font-weight: 700; }
  .avatar-button:hover { border-color: var(--color-primary); }
  .avatar-button img { width: 100%; height: 100%; border-radius: 14px; object-fit: cover; }
  .avatar-edit { position: absolute; right: -4px; bottom: -4px; display: grid; width: 18px; height: 18px; place-items: center; border: 1px solid var(--color-panel-strong); border-radius: 999px; background: var(--color-primary); color: white; }
  .identity { display: grid; gap: 1px; min-width: 0; }
  .identity strong { max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
  .identity span { font-size: 11px; }
  .avatar-error { position: absolute; right: 18px; top: calc(100% + 8px); z-index: 3; padding: 8px 10px; border: 1px solid color-mix(in srgb, var(--color-danger) 44%, var(--color-border)); border-radius: 12px; background: color-mix(in srgb, var(--color-danger-bg) 65%, var(--color-panel-strong)); color: var(--color-danger); font-size: 11px; box-shadow: var(--shadow-soft); }
  @media (max-width: 980px) { .app-header { align-items: flex-start; flex-direction: column; } .user-area { width: 100%; justify-content: space-between; } }
  @media (max-width: 768px) { .app-header { gap: 14px; margin-bottom: 12px; padding: 14px; } .title-copy p, .identity span, .toolbar-button span { display: none; } .user-area, .header-actions { gap: 8px; } .identity strong { max-width: 84px; font-size: 12px; } .toolbar-button { min-width: 40px; padding: 0 10px; } }
</style>
