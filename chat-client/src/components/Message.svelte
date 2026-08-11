<script>
  import { createEventDispatcher } from 'svelte';
  import { formatTime } from '../lib/utils.js';
  import { formatFileSize } from '../lib/attachments.js';

  export let msg;
  export let isSelf = false;
  export let decrypted = null;
  export let imageUrl = null;
  export let fileInfo = null;
  export let attachmentInfo = null;
  export let senderName = '';
  export let avatarUrl = '';
  const dispatch = createEventDispatcher();
  $: time = formatTime(msg.created_at || '');
  $: status = msg.delivered !== undefined ? (msg.delivered ? '✓✓' : '✓') : '';
</script>

<div class="message-wrapper" class:self={isSelf}>
  <div class="message-avatar" class:self={isSelf} aria-label={`${senderName || '用户'} 的头像`}>
    {#if avatarUrl}
      <img src={avatarUrl} alt="" />
    {:else}
      <span>{(senderName || '?').trim().slice(0, 1).toUpperCase()}</span>
    {/if}
  </div>
  <div class="message-bubble" class:self={isSelf}>
    {#if !isSelf && senderName}<span class="sender">{senderName}</span>{/if}
    <div class="body">
      {#if imageUrl}
        <img class="image" src={imageUrl} alt="图片消息" />
        <button class="collect" type="button" title="收藏为表情" on:click={() => dispatch('collect', { msg })}>收藏</button>
        {#if attachmentInfo?.caption}<div class="caption">{attachmentInfo.caption}</div>{/if}
      {:else if fileInfo}
        <a class="file" href={fileInfo.url} download={fileInfo.name}>
          <strong>{fileInfo.name}</strong><span>{formatFileSize(fileInfo.size)}</span>
        </a>
        {#if attachmentInfo?.caption}<div class="caption">{attachmentInfo.caption}</div>{/if}
      {:else if decrypted !== null}
        {decrypted}
      {:else}
        <span class="encrypted">[加密消息]</span>
      {/if}
    </div>
    <div class="meta"><span class="time">{time}</span>{#if isSelf && status}<span class="status">{status}</span>{/if}</div>
  </div>
</div>

<style>
  .message-wrapper { display: flex; align-items: flex-end; gap: 8px; margin-bottom: 4px; padding: 0 var(--space-lg, 16px); }
  /* row-reverse makes the main-axis start the right edge, so flex-start keeps
     the sender avatar on the far right and the whole message group right-aligned. */
  .message-wrapper.self { justify-content: flex-start; flex-direction: row-reverse; }
  .message-avatar { display: grid; width: 32px; height: 32px; place-items: center; flex: 0 0 32px; overflow: hidden; border: 1px solid var(--color-border, #2a2a2a); border-radius: 50%; background: var(--color-avatar, #2c665d); color: var(--color-avatar-text, #e9fffa); font-size: 12px; font-weight: 700; }
  .message-avatar.self { background: var(--color-group-avatar, #365167); color: var(--color-group-avatar-text, #e8f5ff); }
  .message-avatar img { width: 100%; height: 100%; object-fit: cover; }
  .message-bubble { max-width: 70%; padding: 9px 12px; border: 1px solid color-mix(in srgb, var(--color-border) 70%, transparent); border-radius: 7px; background: var(--color-other-msg, #2a2a2a); border-top-left-radius: 2px; }
  .message-bubble.self { background: var(--color-self-msg, #1a3a5c); border-color: color-mix(in srgb, var(--color-primary) 35%, transparent); border-top-left-radius: 7px; border-top-right-radius: 2px; }
  .sender { font-size: 11px; color: var(--color-primary, #4a9eff); font-weight: 500; display: block; margin-bottom: 2px; }
  .body { font-size: var(--font-md, 14px); line-height: 1.5; word-break: break-word; }
  .encrypted { font-style: italic; color: var(--color-text-muted, #888); font-size: var(--font-sm, 12px); }
  .meta { display: flex; align-items: center; gap: 4px; margin-top: 4px; justify-content: flex-end; }
  .image { display: block; max-width: min(360px, 100%); max-height: 360px; border-radius: 4px; object-fit: contain; }
  .collect { margin-top: 6px; padding: 3px 7px; border: 1px solid var(--color-border, #2a2a2a); border-radius: 4px; background: transparent; color: var(--color-text-muted, #888); font-size: 11px; cursor: pointer; }
  .collect:hover { color: var(--color-text, #e0e0e0); }
  .caption { margin-top: 6px; white-space: pre-wrap; }
  .file { display: flex; flex-direction: column; min-width: 210px; padding: 8px 10px; border: 1px solid var(--color-border, #2a2a2a); border-radius: 5px; background: var(--color-input, #0f0f0f); color: inherit; text-decoration: none; }
  .file:hover { border-color: var(--color-primary, #4a9eff); }
  .file span { color: var(--color-text-muted, #888); font-size: 12px; }
  .time, .status { font-size: 10px; color: var(--color-text-muted, #888); }
</style>
