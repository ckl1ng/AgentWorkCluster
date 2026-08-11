<script>
  import { createEventDispatcher } from 'svelte';
  export let confirmation;
  export let busy = false;
  const dispatch = createEventDispatcher();
</script>

<section class="confirmation" role="alert" aria-live="assertive">
  <div><p>需要确认</p><h3>{confirmation.tool}</h3><span class="effect">{confirmation.side_effect}</span></div>
  <p>该操作将调用外部系统。参数已按安全策略脱敏。</p>
  <pre>{JSON.stringify(confirmation.arguments, null, 2)}</pre>
  <div class="actions"><button type="button" class="reject" disabled={busy} on:click={() => dispatch('decide', { approve: false })}>拒绝</button><button type="button" disabled={busy} on:click={() => dispatch('decide', { approve: true })}>{busy ? '正在提交...' : '批准执行'}</button></div>
</section>

<style>
  .confirmation { margin:0 16px 10px; padding:13px; border:1px solid var(--color-error); border-left-width:3px; border-radius:5px; background:var(--color-input); }.confirmation > div:first-child { display:flex; align-items:center; gap:8px; }.confirmation p { margin:0; color:var(--color-text-muted); font-size:12px; }.confirmation h3 { margin:0; color:var(--color-text); font-size:14px; }.effect { padding:2px 5px; border:1px solid var(--color-error); border-radius:3px; color:var(--color-error); font-size:10px; font-weight:700; }.confirmation > p { margin-top:8px; }.confirmation pre { overflow:auto; max-height:150px; margin:8px 0; padding:8px; border:1px solid var(--color-border); border-radius:4px; color:var(--color-text-muted); font-size:11px; }.actions { display:flex; justify-content:flex-end; gap:8px; }.actions button { min-height:32px; padding:0 10px; border:1px solid var(--color-primary); border-radius:4px; background:var(--color-primary); color:#fff; cursor:pointer; font-size:12px; font-weight:700; }.actions .reject { border-color:var(--color-error); background:transparent; color:var(--color-error); }.actions button:disabled { opacity:.55; cursor:default; }
</style>
