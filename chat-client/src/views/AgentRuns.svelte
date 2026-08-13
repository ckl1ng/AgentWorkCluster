<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import { api } from '../lib/api.js';

  export let agentId;
  const dispatch = createEventDispatcher();
  let state = '';
  let runs = [];
  let selected = null;
  let trace = [];
  let confirmations = [];
  let evaluations = [];
  let baselineId = '';
  let candidateId = '';
  let comparison = null;
  let loading = true;
  let error = '';

  onMount(load);

  async function load() {
    loading = true; error = '';
    try {
      const [items, evaluationItems] = await Promise.all([
        api.listAgentRuns({ agentId, state: state || undefined }), api.listEvaluationRuns(),
      ]);
      runs = items;
      evaluations = evaluationItems;
      if (!selected && runs.length) await selectRun(runs[0]);
    } catch (e) { error = e.message || '无法加载运行工作台'; }
    finally { loading = false; }
  }

  async function selectRun(run) {
    selected = run; trace = []; confirmations = [];
    try {
      const [events, approvals] = await Promise.all([api.getAgentTrace(run.id), api.listRunConfirmations(run.id)]);
      trace = events;
      confirmations = approvals;
    } catch (e) { error = e.message || '无法加载运行详情'; }
  }

  async function compare() {
    if (!baselineId || !candidateId || baselineId === candidateId) return;
    try { comparison = await api.compareEvaluationRuns(baselineId, candidateId); }
    catch (e) { error = e.message || '无法比较评估结果'; }
  }

  function localDispatchLabel(run) {
    const dispatch = run?.local_dispatch;
    if (!dispatch) return '';
    const state = { pending: '等待本机', offered: '等待领取', claimed: '本机执行中', completed: '本机已完成', failed: '本机失败', cancelled: '本机已取消' }[dispatch.executor_state] || dispatch.executor_state;
    return `${state} · 工作区 ${dispatch.workspace_id}`;
  }
</script>

<section class="workbench" aria-label="运行工作台">
  <header><div><p>RUN WORKBENCH</p><h2>运行与评估</h2></div><button type="button" class="close" title="关闭运行工作台" on:click={() => dispatch('close')}>×</button></header>
  <div class="filter"><label>运行状态<select bind:value={state} on:change={load}><option value="">全部</option><option value="queued">排队</option><option value="running">运行中</option><option value="waiting_confirmation">等待确认</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select></label><button type="button" class="secondary" on:click={load}>刷新</button></div>
  <div class="run-grid">
    <aside>{#if loading}<p class="muted">正在加载...</p>{:else}{#each runs as run (run.id)}<button type="button" class:active={selected?.id === run.id} on:click={() => selectRun(run)}><strong>{run.state}</strong><small>{localDispatchLabel(run) || new Date(run.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</small><span>{run.usage?.total_tokens || 0} tokens</span></button>{:else}<p class="muted">没有运行记录</p>{/each}{/if}</aside>
    <article class="detail">{#if selected}<div class="run-summary"><strong>{selected.state}</strong>{#if localDispatchLabel(selected)}<span>{localDispatchLabel(selected)}</span>{/if}<span>尝试 {selected.attempt || 0}</span><span>{selected.usage?.total_tokens || 0} tokens</span></div>{#if selected.final_content}<p class="final">{selected.final_content}</p>{/if}<h3>工具确认</h3>{#each confirmations as item (item.id)}<div class="confirmation"><strong>{item.tool_name}</strong><span>{item.state}</span><code>{JSON.stringify(item.arguments)}</code></div>{:else}<p class="muted">本次运行没有确认请求</p>{/each}<h3>审计时间线</h3><ol>{#each trace as event (event.sequence)}<li class:failed={event.type === 'agent.run.failed'}><time>{new Date(event.timestamp).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</time><span>{event.payload.summary || event.payload.error || event.type}</span></li>{/each}</ol>{:else}<p class="muted">从左侧选择运行记录</p>{/if}</article>
  </div>
  <section class="evaluation"><h3>评估对比</h3><div class="filter"><label>基线<select bind:value={baselineId}><option value="">选择评估运行</option>{#each evaluations as item (item.id)}<option value={item.id}>{item.harness_version} · {item.started_at}</option>{/each}</select></label><label>候选<select bind:value={candidateId}><option value="">选择评估运行</option>{#each evaluations as item (item.id)}<option value={item.id}>{item.harness_version} · {item.started_at}</option>{/each}</select></label><button type="button" on:click={compare} disabled={!baselineId || !candidateId || baselineId === candidateId}>比较</button></div>{#if comparison}<p class:blocked={!comparison.passed} class:passed={comparison.passed}>{comparison.passed ? '无评估回归' : `检测到 ${comparison.regressions.length} 个回归`}</p>{#if comparison.regressions.length}<ul>{#each comparison.regressions as caseId}<li>{caseId}</li>{/each}</ul>{/if}{/if}</section>
  {#if error}<p class="error" role="status">{error}</p>{/if}
</section>

<style>
  .workbench { flex:1; min-width:0; overflow:auto; padding:26px max(18px,6vw) 48px; }.workbench > header,.filter,.run-grid,.evaluation { max-width:960px; }.workbench header { display:flex; justify-content:space-between; margin-bottom:18px; }.workbench header p { margin:0; color:var(--color-primary); font-size:10px; font-weight:700; letter-spacing:1px; } h2 { margin:4px 0 0; font-size:22px; } h3 { margin:18px 0 10px; font-size:14px; }.filter { display:flex; align-items:end; gap:10px; margin-bottom:14px; }.filter label { display:grid; flex:1; gap:6px; color:var(--color-text-muted); font-size:12px; font-weight:600; }select { min-height:34px; padding:0 9px; border:1px solid var(--color-border); border-radius:4px; background:var(--color-input); color:var(--color-text); font:inherit; }.run-grid { display:grid; grid-template-columns:220px minmax(0,1fr); min-height:330px; border:1px solid var(--color-border); border-radius:6px; overflow:hidden; }.run-grid aside { overflow:auto; border-right:1px solid var(--color-border); background:var(--color-surface); }.run-grid aside button { display:grid; width:100%; gap:4px; padding:11px; border:0; border-bottom:1px solid var(--color-border); background:transparent; color:var(--color-text); cursor:pointer; text-align:left; }.run-grid aside button.active { background:var(--color-active); }.run-grid small,.run-grid span,.muted { color:var(--color-text-muted); font-size:11px; }.detail { min-width:0; padding:16px; }.run-summary { display:flex; flex-wrap:wrap; gap:7px; }.run-summary span,.confirmation span { padding:2px 5px; border:1px solid var(--color-border); border-radius:3px; color:var(--color-text-muted); font-size:11px; }.final { white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.55; }.confirmation { display:grid; grid-template-columns:auto auto 1fr; align-items:center; gap:8px; padding:8px 0; border-bottom:1px solid var(--color-border); }.confirmation code { overflow:auto; color:var(--color-text-muted); font-size:11px; }ol { margin:0; padding:0; list-style:none; }li { display:flex; gap:10px; padding:7px 0; border-bottom:1px solid var(--color-border); color:var(--color-text-muted); font-size:12px; }li.failed,.blocked { color:var(--color-error); }time { flex:0 0 76px; color:var(--color-text-muted); }.evaluation { margin-top:20px; padding-top:2px; border-top:1px solid var(--color-border); }.passed { color:var(--color-online); }.close,.secondary,button { min-height:34px; padding:0 12px; border:1px solid var(--color-primary); border-radius:4px; background:var(--color-primary); color:#fff; cursor:pointer; font-size:12px; font-weight:700; }.close,.secondary { border-color:var(--color-border); background:transparent; color:var(--color-text-muted); }.error { color:var(--color-error); font-size:12px; }button:disabled { opacity:.55; cursor:default; }@media(max-width:700px){.run-grid{grid-template-columns:1fr}.run-grid aside{max-height:200px;border-right:0;border-bottom:1px solid var(--color-border)}.filter{align-items:stretch;flex-direction:column}.confirmation{grid-template-columns:1fr}.workbench{padding:20px 14px 36px}}
</style>
