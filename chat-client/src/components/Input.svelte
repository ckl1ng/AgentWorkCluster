<script>
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { auth } from '../lib/store.js';
  import { formatFileSize, validateFile } from '../lib/attachments.js';
  import { loadStickers, removeSticker, stickers } from '../lib/stickers.js';

  export let pendingFiles = [];
  const dispatch = createEventDispatcher();
  let text = '';
  let fileInput;
  let showStickers = false;
  let stickerUrls = new Map();

  onMount(() => { loadStickers($auth?.id); });
  onDestroy(() => stickerUrls.forEach(url => URL.revokeObjectURL(url)));

  function stageFiles(files) {
    const validFiles = files.filter(Boolean).filter(file => {
      const error = validateFile(file);
      if (error) dispatch('error', { message: error });
      return !error;
    });
    if (validFiles.length) dispatch('stage', { files: validFiles });
  }

  function send() {
    const caption = text.trim();
    if (!caption && !pendingFiles.length) return;
    dispatch('send', { text: caption, files: pendingFiles });
    text = '';
    pendingFiles = [];
  }

  function getStickerUrl(sticker) {
    if (!stickerUrls.has(sticker.id)) stickerUrls.set(sticker.id, URL.createObjectURL(sticker.blob));
    return stickerUrls.get(sticker.id);
  }

  function selectFile(event) {
    stageFiles([...event.currentTarget.files]);
    event.currentTarget.value = '';
  }

  function sendSticker(sticker) {
    const file = new File([sticker.blob], sticker.name, { type: sticker.type });
    dispatch('send', { text: '', files: [file] });
    showStickers = false;
  }

  async function deleteSticker(event, sticker) {
    event.stopPropagation();
    const url = stickerUrls.get(sticker.id);
    if (url) URL.revokeObjectURL(url);
    stickerUrls.delete(sticker.id);
    await removeSticker(sticker.id);
  }

  function handleKeydown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }
</script>

<div class="composer">
  {#if pendingFiles.length}
    <div class="pending-files" aria-label="待发送附件">
      {#each pendingFiles as file, index (file.name + file.size + index)}
        <div class="pending-file" title={file.name}>
          <span>{file.name}</span><small>{formatFileSize(file.size)}</small>
          <button type="button" title="移除附件" on:click={() => dispatch('remove', { index })}>x</button>
        </div>
      {/each}
    </div>
  {/if}
  <div class="input-bar">
    <input class="text-input" type="text" placeholder="输入消息..." bind:value={text} on:keydown={handleKeydown} />
    <input bind:this={fileInput} class="file-input" type="file" multiple on:change={selectFile} />
    <button class="tool-button plus" type="button" title="添加本地图片或文件" on:click={() => fileInput?.click()}>+</button>
    <div class="sticker-wrap">
      <button class:active={showStickers} class="tool-button" type="button" title="发送表情" on:click={() => showStickers = !showStickers}>表情</button>
      {#if showStickers}
        <div class="sticker-panel">
          {#if $stickers.length}
            {#each $stickers as sticker (sticker.id)}
              <div class="sticker-item">
                <button class="sticker" type="button" title={sticker.name} on:click={() => sendSticker(sticker)}><img src={getStickerUrl(sticker)} alt={sticker.name} /></button>
                <button class="remove-sticker" type="button" title="移除表情" on:click={(event) => deleteSticker(event, sticker)}>x</button>
              </div>
            {/each}
          {:else}
            <div class="empty-stickers">收藏图片后会显示在这里</div>
          {/if}
        </div>
      {/if}
    </div>
    <button class="send-button" on:click={send} disabled={!text.trim() && !pendingFiles.length}>发送</button>
  </div>
</div>

<style>
  .composer { flex-shrink: 0; border-top: 1px solid var(--color-border, #2a2a2a); background: var(--color-surface, #1a1a1a); padding-bottom: env(safe-area-inset-bottom, 0px); }
  .pending-files { display: flex; gap: 8px; overflow-x: auto; padding: 8px var(--space-lg, 16px) 0; }
  .pending-file { display: grid; grid-template-columns: minmax(0, 1fr) auto; min-width: 150px; max-width: 220px; padding: 6px 8px; border: 1px solid var(--color-border, #2a2a2a); border-radius: 5px; background: var(--color-input, #0f0f0f); color: var(--color-text, #e0e0e0); font-size: 12px; }
  .pending-file span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pending-file small { color: var(--color-text-muted, #888); }
  .pending-file button { grid-row: 1 / span 2; grid-column: 2; width: 18px; padding: 0; border: 0; background: transparent; color: var(--color-text-muted, #888); cursor: pointer; }
  .input-bar { display: flex; align-items: center; gap: var(--space-sm, 8px); padding: var(--space-md, 12px) var(--space-lg, 16px); }
  .text-input { flex: 1; min-width: 0; padding: 10px 12px; border: 1px solid var(--color-border, #2a2a2a); border-radius: 6px; background: var(--color-input, #0f0f0f); color: var(--color-text, #e0e0e0); font-size: var(--font-md, 14px); outline: none; }
  .text-input:focus { border-color: var(--color-primary, #4a9eff); }
  .file-input { display: none; }
  button { border: none; border-radius: var(--radius-md, 8px); color: #fff; cursor: pointer; font-size: var(--font-md, 14px); font-weight: 500; flex-shrink: 0; }
  .tool-button { padding: 10px 12px; background: transparent; border: 1px solid var(--color-border, #2a2a2a); color: var(--color-text, #e0e0e0); }
  .plus { width: 38px; padding: 8px 0; font-size: 21px; line-height: 20px; }
  .tool-button.active { border-color: var(--color-primary, #4a9eff); color: var(--color-primary, #4a9eff); }
  .send-button { padding: 10px 18px; background: var(--color-primary, #4a9eff); box-shadow: 0 2px 8px color-mix(in srgb, var(--color-primary) 25%, transparent); }
  .send-button:hover:not(:disabled) { background: var(--color-primary-dim, #2a6ecc); }
  button:disabled { opacity: 0.4; cursor: default; }
  .sticker-wrap { position: relative; }
  .sticker-panel { position: absolute; z-index: 5; bottom: calc(100% + 10px); right: 0; width: 288px; max-height: 260px; overflow-y: auto; display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; padding: 10px; border: 1px solid var(--color-border, #2a2a2a); border-radius: var(--radius-md, 8px); background: var(--color-surface, #1a1a1a); box-shadow: 0 8px 24px rgba(0, 0, 0, .35); }
  .sticker-item { position: relative; min-width: 0; }
  .sticker { width: 58px; height: 58px; padding: 2px; background: transparent; }
  .sticker img { width: 100%; height: 100%; object-fit: contain; }
  .remove-sticker { position: absolute; right: -3px; top: -3px; width: 17px; height: 17px; padding: 0; border-radius: 50%; background: #8d3030; font-size: 12px; line-height: 17px; }
  .empty-stickers { grid-column: 1 / -1; padding: 16px 4px; color: var(--color-text-muted, #888); font-size: 12px; text-align: center; }
  @media (max-width: 768px) {
    .pending-files { padding: 8px 12px 0; }
    .input-bar { gap: 6px; padding: 10px 12px; }
    .tool-button { padding: 9px 8px; font-size: 12px; }
    .plus { width: 34px; padding: 7px 0; font-size: 20px; }
    .send-button { padding: 9px 11px; font-size: 13px; }
    .sticker-panel { right: -52px; width: min(288px, calc(100vw - 24px)); }
  }
</style>
