<script>
  import { createEventDispatcher } from 'svelte';
  import { onDestroy, onMount, tick } from 'svelte';
  import { agents, auth, getLocalAvatar } from '../lib/store.js';
  import { api } from '../lib/api.js';
  import { AgentWebSocket } from '../lib/agent-ws.js';
  import AgentSettings from './AgentSettings.svelte';
  import AgentRuns from './AgentRuns.svelte';
  import ToolConfirmation from '../components/ToolConfirmation.svelte';

  export let agentId;
  const dispatch = createEventDispatcher();
  let agent = null;
  let conversation = null;
  let conversations = [];
  let messageList = [];
  let traces = {};
  let input = '';
  let loading = true;
  let error = '';
  let runningRunId = null;
  let socket = null;
  let messageListEl;
  let settingsOpen = false;
  let runsOpen = false;
  let confirmation = null;
  let confirmationBusy = false;
  let localDevice = null;
  let agentStatus = { source: 'user', time: '', group_members: [] };

  onMount(load);
  onDestroy(() => socket?.disconnect());

  async function load() {
    loading = true; error = '';
    try {
      agent = await api.getAgent(agentId);
      await loadLocalDevice();
      conversations = (await api.listAgentConversations(agentId)).filter(item => !item.channel_provider);
      conversation = conversations[0] || await api.createAgentConversation(agentId);
      if (!conversations.some(item => item.id === conversation.id)) conversations = [conversation, ...conversations];
      await loadConversation();
    } catch (e) { error = e.message || '无法加载 Agent'; }
    finally { loading = false; }
  }

  async function loadLocalDevice() {
    if (agent?.execution_target !== 'local' || !agent.default_device_id) { localDevice = null; return; }
    const devices = await api.listLocalDevices();
    localDevice = devices.find((device) => device.id === agent.default_device_id) || null;
  }

  function localDispatchLabel(run) {
    const state = run?.local_dispatch?.executor_state;
    if (state === 'pending' || state === 'offered') return '等待本机领取';
    if (state === 'claimed') return '本机执行中';
    if (state === 'completed') return '本机已完成';
    if (state === 'failed') return '本机失败';
    if (state === 'cancelled') return '本机已取消';
    return '';
  }

  async function loadConversation() {
    if (!conversation) return;
    const data = await api.getAgentConversation(conversation.id);
    conversation = data.conversation;
    agentStatus = data.agent_status || { source: 'user', time: '', group_members: [] };
    // Tool responses are context records, not chat bubbles. Keep this guard for
    // compatibility with older Agent API instances that still return role=tool.
    messageList = data.messages.filter(message => message.role === 'user' || message.role === 'assistant');
    const runIds = [...new Set(messageList.filter(message => message.run_id).map(message => message.run_id))];
    const loaded = await Promise.all(runIds.map(loadTrace));
    messageList = messageList.map(message => {
      if (message.role !== 'assistant' || !message.run_id || !traces[message.run_id]) return message;
      const trace = traces[message.run_id];
      const reasoning = trace.events.filter(event => event.type === 'agent.message.reasoning.delta')
        .map(event => event.payload?.content || '').join('')
        || trace.events.find(event => event.type === 'agent.run.completed')?.payload?.reasoning || '';
      return reasoning ? { ...message, reasoning } : message;
    });
    const active = loaded.filter(item => item?.run && ['queued', 'running', 'waiting_confirmation'].includes(item.run.state)).at(-1);
    if (active) { runningRunId = active.runId; connect(active.runId); if (active.run.state === 'waiting_confirmation') await loadPendingConfirmation(active.runId); }
    await tick(); scrollBottom();
  }

  async function loadTrace(runId) {
    try {
      const [events, run] = await Promise.all([api.getAgentTrace(runId), api.getAgentRun(runId)]);
      traces = { ...traces, [runId]: { events, run, open: false } };
      return { runId, run };
    } catch { /* The run may be inaccessible after deletion. */ }
  }

  function connect(runId) {
    socket?.disconnect();
    const after = traces[runId]?.events?.at(-1)?.sequence || 0;
    socket = new AgentWebSocket($auth.token, applyEvent);
    socket.connect(runId, after);
  }

  function applyEvent(event) {
    if (!event?.run_id) return;
    const previous = traces[event.run_id] || { events: [], run: null, open: true };
    const events = event.sequence && previous.events.some(item => item.sequence === event.sequence)
      ? previous.events : [...previous.events, event];
    let run = previous.run;
    if (event.type === 'agent.run.started') run = { ...run, state: 'running' };
    if (event.type === 'agent.tool.confirmation_required') run = { ...run, state: 'waiting_confirmation' };
    if (event.type === 'agent.run.completed') run = { ...run, state: 'completed', usage: event.payload.usage || run?.usage };
    if (event.type === 'agent.run.failed') run = { ...run, state: 'failed' };
    if (event.type === 'agent.run.cancelled') run = { ...run, state: 'cancelled' };
    traces = { ...traces, [event.run_id]: { ...previous, events, run } };

    if (event.type === 'agent.tool.confirmation_required') {
      confirmation = { ...event.payload, runId: event.run_id };
    }

    if (event.type === 'agent.message.delta') {
      const id = `stream:${event.run_id}`;
      const index = messageList.findIndex(message => message.id === id);
      if (index === -1) messageList = [...messageList, { id, run_id: event.run_id, role: 'assistant', content: event.payload.content, streaming: true }];
      else messageList = messageList.map((message, i) => i === index ? { ...message, content: message.content + event.payload.content } : message);
    }
    if (event.type === 'agent.message.reasoning.delta') {
      const id = `stream:${event.run_id}`;
      const index = messageList.findIndex(message => message.id === id);
      if (index === -1) messageList = [...messageList, { id, run_id: event.run_id, role: 'assistant', content: '', reasoning: event.payload.content, streaming: true }];
      else messageList = messageList.map((message, i) => i === index ? { ...message, reasoning: (message.reasoning || '') + event.payload.content } : message);
    }
    if (event.type === 'agent.run.completed') {
      const id = `stream:${event.run_id}`;
      const streamed = messageList.find(message => message.id === id);
      const streamedContent = streamed?.content || '';
      const reportedContent = event.payload.content || '';
      const content = streamedContent.length >= reportedContent.length ? streamedContent : reportedContent;
      const streamedReasoning = streamed?.reasoning || '';
      const reportedReasoning = event.payload.reasoning || '';
      const reasoning = streamedReasoning.length >= reportedReasoning.length ? streamedReasoning : reportedReasoning;
      const completed = { id: `assistant:${event.run_id}`, run_id: event.run_id, role: 'assistant', content, reasoning, streaming: false };
      messageList = messageList.some(message => message.id === id)
        ? messageList.map(message => message.id === id ? completed : message)
        : [...messageList, completed];
      runningRunId = null;
      confirmation = null;
    }
    if (event.type === 'agent.run.failed' || event.type === 'agent.run.cancelled') {
      runningRunId = null;
      confirmation = null;
      error = event.payload.error || event.payload.summary || 'Agent 运行失败';
    }
    tick().then(scrollBottom);
  }

  async function loadPendingConfirmation(runId) {
    try {
      const pending = (await api.listRunConfirmations(runId)).find(item => item.state === 'pending');
      if (pending) confirmation = {
        runId, confirmation_id: pending.id, tool: pending.tool_name, arguments: pending.arguments,
        arguments_hash: pending.arguments_hash, side_effect: 'write',
      };
    } catch { /* The trace remains available even if a confirmation expired. */ }
  }

  async function decideConfirmation(event) {
    if (!confirmation || confirmationBusy) return;
    confirmationBusy = true; error = '';
    try {
      await api.decideToolConfirmation(confirmation.runId, confirmation.confirmation_id, {
        approve: event.detail.approve, arguments_hash: confirmation.arguments_hash,
      });
      confirmation = null;
      if (!event.detail.approve) runningRunId = null;
    } catch (e) { error = e.message || '无法提交工具确认'; }
    finally { confirmationBusy = false; }
  }

  async function send() {
    const content = input.trim();
    if (!content || runningRunId || !conversation) return;
    error = '';
    input = '';
    const localId = `user:${Date.now()}`;
    messageList = [...messageList, { id: localId, role: 'user', content, created_at: new Date().toISOString() }];
    scrollBottom();
    try {
      const run = await api.createAgentRun(conversation.id, content);
      runningRunId = run.id;
      traces = { ...traces, [run.id]: { events: [], run, open: true } };
      connect(run.id);
    } catch (e) {
      messageList = messageList.filter(message => message.id !== localId);
      input = content;
      error = e.message || '无法提交任务';
    }
  }

  async function clearContext() {
    if (!conversation || !confirm('清空上下文后，后续请求不再携带此前消息。旧记录会保留。')) return;
    try { conversation = await api.clearAgentContext(conversation.id); }
    catch (e) { error = e.message || '无法清空上下文'; }
  }

  async function newConversation() {
    if (runningRunId) return;
    try {
      conversation = await api.createAgentConversation(agentId);
      conversations = [conversation, ...conversations];
      messageList = [];
      traces = {};
      agentStatus = { source: 'user', time: '', group_members: [] };
      error = '';
      await tick();
      scrollBottom();
    }
    catch (e) { error = e.message || '无法创建会话'; }
  }

  async function selectConversation(next) {
    if (next.id === conversation?.id || runningRunId) return;
    socket?.disconnect();
    conversation = next;
    messageList = [];
    traces = {};
    agentStatus = { source: 'user', time: '', group_members: [] };
    confirmation = null;
    error = '';
    await loadConversation();
  }

  async function cancelRun() {
    if (!runningRunId) return;
    try { await api.cancelAgentRun(runningRunId); }
    catch (e) { error = e.message || '无法取消运行'; }
  }

  async function deleteAgent() {
    if (runningRunId || !confirm(`删除「${agent?.name || '此 Agent'}」后将无法恢复其配置和会话。`)) return;
    try {
      await api.deleteAgent(agentId);
      socket?.disconnect();
      dispatch('deleted', { agentId });
    } catch (e) { error = e.message || '无法删除 Agent'; }
  }

  function toggleTrace(runId) {
    const trace = traces[runId];
    if (trace) traces = { ...traces, [runId]: { ...trace, open: !trace.open } };
  }

  function traceSteps(events, finalContent = '') {
    const steps = [];
    const appendText = (kind, content) => {
      if (!content) return;
      const previous = steps[steps.length - 1];
      if (previous?.kind === kind) previous.content += content;
      else steps.push({ kind, content });
    };
    const findTool = (name) => [...steps].reverse().find(step => step.kind === 'tool' && step.name === name && !step.completed);
    for (const event of [...events].sort((a, b) => (a.sequence || 0) - (b.sequence || 0))) {
      const payload = event.payload || {};
      if (event.type === 'agent.message.reasoning.delta') appendText('thought', payload.content || '');
      else if (event.type === 'agent.message.delta') appendText('dialogue', payload.content || '');
      else if (event.type === 'agent.tool.started' || event.type === 'agent.tool.confirmation_required') {
        steps.push({
          kind: 'tool', name: payload.tool || '工具', type: payload.tool_type || 'tool',
          arguments: payload.arguments || {}, status: event.type === 'agent.tool.confirmation_required' ? 'waiting' : 'running',
          summary: payload.summary || '', completed: false,
        });
      } else if (event.type === 'agent.tool.completed') {
        const tool = findTool(payload.tool);
        if (tool) Object.assign(tool, { result: payload.result, status: 'completed', completed: true, summary: payload.summary || tool.summary });
        else steps.push({ kind: 'tool', name: payload.tool || '工具', type: payload.tool_type || 'tool', arguments: payload.arguments || {}, result: payload.result, status: 'completed', completed: true, summary: payload.summary || '' });
      } else if (event.type === 'agent.tool.rejected') {
        const tool = findTool(payload.tool);
        if (tool) Object.assign(tool, { status: 'rejected', completed: true, summary: payload.summary || '工具操作被拒绝' });
      }
    }
    // The last text delta is the final answer and is rendered in its own block.
    // Remove it from the process timeline once the run has completed.
    const last = steps[steps.length - 1];
    if (finalContent && last?.kind === 'dialogue' && last.content === finalContent) steps.pop();
    return steps;
  }

  function formatToolArguments(argumentsValue) {
    try { return JSON.stringify(argumentsValue || {}, null, 2); }
    catch { return String(argumentsValue || '{}'); }
  }

  function toolResultLabel(result) {
    if (!result) return '';
    if (result.error) return `失败：${result.error}`;
    if (result.status) return result.status === 'ok' ? '执行完成' : `状态：${result.status}`;
    return '执行完成';
  }
  function syncAgent(event) {
    agent = event.detail.agent;
    agents.update(items => items.map(item => item.id === agent.id ? { ...item, ...agent } : item));
    void loadLocalDevice();
  }

  function settingsSaved(event) {
    syncAgent(event);
    settingsOpen = false;
  }
  function scrollBottom() { if (messageListEl) messageListEl.scrollTop = messageListEl.scrollHeight; }
  function keydown(event) { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); } }
</script>

{#if runsOpen}
  <AgentRuns {agentId} on:close={() => runsOpen = false} />
{:else if settingsOpen}
  <AgentSettings {agent} conversationId={conversation?.id || ''} on:saved={settingsSaved} on:updated={syncAgent} on:cancel={() => settingsOpen = false} />
{:else}
<section class="agent-chat">
  <aside class="conversation-sidebar" aria-label="Agent 会话">
    <div class="conversation-sidebar-heading"><span>会话</span><button type="button" title="新建会话" aria-label="新建会话" on:click={newConversation} disabled={!!runningRunId}>＋</button></div>
    <div class="conversation-items">
      {#each conversations as item (item.id)}
        <button class:active={item.id === conversation?.id} type="button" on:click={() => selectConversation(item)} disabled={!!runningRunId} title={item.title}>{item.title || '新会话'}</button>
      {/each}
    </div>
  </aside>
  <div class="conversation-main">
  <div class="agent-toolbar">
    <div class="agent-avatar" aria-hidden="true">{#if agent?.avatar_url}<img src={agent.avatar_url} alt="" />{:else}<span>{(agent?.name || 'A').slice(0, 1).toUpperCase()}</span>{/if}</div>
    <span class="state" class:paused={agent?.state !== 'active'}>{agent?.state === 'active' ? '可运行' : '已暂停'}</span>
    {#if agent?.execution_target === 'local'}<span class="local-state" class:offline={localDevice?.status !== 'online'} title={localDevice ? `${localDevice.display_name} · ${localDevice.status}` : '绑定设备不可用'}>{localDevice?.status === 'online' ? '本机在线' : '等待本机'}</span>{/if}
    <span class="agent-purpose">{agent?.description || '个人 Agent'}</span>
    <div class="toolbar-actions"><button type="button" title="清空当前上下文" on:click={clearContext} disabled={!!runningRunId}>清空上下文</button><button type="button" on:click={() => runsOpen = true}>运行记录</button>{#if agent?.is_owner}<button type="button" on:click={() => settingsOpen = true} disabled={!!runningRunId}>配置</button><button class="delete" type="button" title="删除 Agent" on:click={deleteAgent} disabled={!!runningRunId}>删除</button>{/if}</div>
  </div>
  <div class="messages" bind:this={messageListEl}>
    {#if loading}<p class="status">正在加载 Agent...</p>
    {:else if error && !messageList.length}<p class="status error">{error}</p>
    {:else if !messageList.length}<p class="status">发送第一条消息以开始运行</p>{/if}
    {#each messageList as message (message.id)}
      <article class="message-row" class:user={message.role === 'user'}>
        <div class="message-avatar" class:user={message.role === 'user'} aria-label={message.role === 'user' ? '你的头像' : `${agent?.name || 'Agent'} 的头像`}>
          {#if message.role === 'user' && getLocalAvatar($auth?.id)}<img src={getLocalAvatar($auth?.id)} alt="" />
          {:else if message.role !== 'user' && agent?.avatar_url}<img src={agent.avatar_url} alt="" />
          {:else}<span>{message.role === 'user' ? ($auth?.username || '你').slice(0, 1).toUpperCase() : (agent?.name || 'A').slice(0, 1).toUpperCase()}</span>{/if}
        </div>
        <div class="message" class:user={message.role === 'user'}>
          <div class="message-meta">{message.role === 'user' ? '你' : (agent?.name || 'Agent')}{#if message.streaming}<span> 正在生成</span>{/if}</div>
          {#if message.role === 'assistant' && message.run_id && traces[message.run_id]}
            {@const trace = traces[message.run_id]}
            {@const run = trace.run}
            {@const events = trace.events || []}
            {@const steps = traceSteps(events, message.streaming ? '' : (message.content || ''))}
            <details class="run-process" open={message.streaming || trace.open}>
              <summary><span class="process-title">思考与工具</span><span class="process-count">{steps.length ? `${steps.length} 个步骤` : '执行记录'}</span></summary>
              {#if run}<div class="run-meta"><span>{run.state}</span>{#if localDispatchLabel(run)}<span>{localDispatchLabel(run)}</span>{/if}<span>尝试 {run.attempt || 0}</span>{#if run.usage?.total_tokens}<span>{run.usage.total_tokens} tokens</span>{/if}</div>{/if}
              {#if steps.length}
                <div class="trace-steps">
                  {#each steps as step, index (index)}
                    {#if step.kind === 'thought'}
                      <details class="trace-step thought" open={message.streaming && index === steps.length - 1}><summary><span class="step-icon">思</span>思考</summary><div>{step.content}</div></details>
                    {:else if step.kind === 'dialogue'}
                      <div class="trace-step dialogue"><span class="step-icon">话</span><div>{step.content}</div></div>
                    {:else}
                      <details class="trace-step tool" open={step.status === 'waiting'}>
                        <summary><span class="step-icon">工</span><span class="tool-name">{step.name}</span><span class="tool-type">{step.type}</span><span class:tool-waiting={step.status === 'waiting'} class="tool-status">{step.status === 'running' ? '执行中' : step.status === 'waiting' ? '等待确认' : step.status === 'rejected' ? '已拒绝' : '已完成'}</span></summary>
                        <div class="tool-detail"><div class="tool-label">参数</div><pre>{formatToolArguments(step.arguments)}</pre>{#if step.summary}<div class="tool-summary">{step.summary}</div>{/if}{#if step.result}<div class="tool-label">结果</div><div class="tool-result">{toolResultLabel(step.result)}</div>{/if}</div>
                      </details>
                    {/if}
                  {/each}
                </div>
              {:else}<p class="trace-empty">暂无思考或工具记录</p>{/if}
            </details>
            <section class="final-result"><div class="result-label">最终结果</div><div class="content">{message.content || (message.streaming ? '正在生成...' : '')}</div></section>
          {:else}
            <div class="content">{message.content}</div>
          {/if}
        </div>
      </article>
    {/each}
    <aside class="agent-status" aria-label="会话状态栏">
      <div><span>状态来源</span><strong>{agentStatus.source || 'user'}</strong></div>
      <div><span>时间</span><time>{agentStatus.time ? new Date(agentStatus.time).toLocaleString() : '未更新'}</time></div>
      <div><span>群成员</span><p>{agentStatus.group_members?.length ? agentStatus.group_members.join('、') : '暂无'}</p></div>
    </aside>
  </div>
  {#if confirmation}<ToolConfirmation {confirmation} busy={confirmationBusy} on:decide={decideConfirmation} />{/if}
  {#if error && messageList.length}<p class="run-error" role="status">{error}</p>{/if}
  <div class="composer"><textarea bind:value={input} rows="2" placeholder={runningRunId ? 'Agent 正在运行...' : '输入任务...'} disabled={!!runningRunId || agent?.state !== 'active'} on:keydown={keydown}></textarea>{#if runningRunId}<button class="cancel" type="button" on:click={cancelRun}>停止</button>{:else}<button type="button" on:click={send} disabled={!input.trim() || agent?.state !== 'active'}>发送</button>{/if}</div>
  </div>
</section>
{/if}

<style>
  .agent-chat { flex: 1; display: flex; min-width: 0; min-height: 0; }
  .conversation-sidebar { display:flex; flex:0 0 178px; min-width:0; flex-direction:column; border-right:1px solid var(--color-border); background:var(--color-surface); }.conversation-sidebar-heading { display:flex; min-height:44px; align-items:center; justify-content:space-between; padding:0 10px 0 14px; border-bottom:1px solid var(--color-border); color:var(--color-text-muted); font-size:12px; font-weight:700; }.conversation-sidebar-heading button { display:grid; width:26px; height:26px; place-items:center; min-height:0; padding:0; font-size:17px; }.conversation-items { display:grid; gap:2px; overflow:auto; padding:7px; }.conversation-items button { width:100%; overflow:hidden; margin:0; padding:7px 8px; border-color:transparent; color:var(--color-text-muted); text-align:left; text-overflow:ellipsis; white-space:nowrap; }.conversation-items button.active { border-color:var(--color-primary); background:var(--color-active); color:var(--color-primary); }.conversation-main { display:flex; flex:1; min-width:0; flex-direction:column; }
  .agent-toolbar { display: flex; align-items: center; min-height: 44px; gap: 10px; padding: 0 16px; border-bottom: 1px solid var(--color-border); background: var(--color-surface); }.agent-avatar, .message-avatar { display: grid; width: 32px; height: 32px; place-items: center; flex: 0 0 32px; overflow: hidden; border: 1px solid var(--color-border); border-radius: 50%; background: var(--color-avatar); color: var(--color-avatar-text); font-size: 12px; font-weight: 700; }.agent-avatar img, .message-avatar img { width: 100%; height: 100%; object-fit: cover; }.message-avatar.user { background: var(--color-group-avatar); color: var(--color-group-avatar-text); }
  .state,.local-state { padding: 2px 6px; border: 1px solid color-mix(in srgb, var(--color-online) 55%, var(--color-border)); border-radius: 4px; color: var(--color-online); font-size: 11px; font-weight: 700; }.state.paused,.local-state.offline { color: var(--color-error); border-color: var(--color-error); }
  .agent-purpose { overflow: hidden; flex: 1; color: var(--color-text-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }.toolbar-actions { display: flex; gap: 6px; }.toolbar-actions .delete { color: var(--color-error); }
  button { min-height: 30px; padding: 0 9px; border: 1px solid var(--color-border); border-radius: 4px; background: transparent; color: var(--color-text-muted); cursor: pointer; font-size: 12px; } button:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); } button:disabled { opacity: .5; cursor: default; }
  .messages { flex: 1; overflow-y: auto; padding: 18px 0; }.status { display: grid; min-height: 180px; place-items: center; color: var(--color-text-muted); font-size: 13px; }.status.error, .run-error { color: var(--color-error); }
  .agent-status { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:12px; margin:24px max(16px, 7vw) 4px; padding:10px 12px; border-top:1px solid var(--color-border); color:var(--color-text-muted); font-size:11px; }.agent-status div { min-width:0; }.agent-status span { display:block; margin-bottom:3px; font-size:10px; }.agent-status strong,.agent-status time,.agent-status p { color:var(--color-text); font:inherit; overflow-wrap:anywhere; }.agent-status p { margin:0; }
  .message-row { display: flex; align-items: flex-end; gap: 8px; margin-bottom: 12px; padding: 0 max(16px, 7vw); }.message-row.user { flex-direction: row-reverse; }.message { max-width: min(780px, 78%); padding: 11px 13px; border: 1px solid color-mix(in srgb, var(--color-border) 70%, transparent); border-radius: 8px; border-top-left-radius: 2px; background: var(--color-other-msg); }.message.user { border-color: color-mix(in srgb, var(--color-primary) 35%, transparent); border-top-left-radius: 8px; border-top-right-radius: 2px; background: var(--color-self-msg); }.message-meta { margin-bottom: 5px; color: var(--color-primary); font-size: 11px; font-weight: 600; }.message.user .message-meta { color: var(--color-text-muted); }.message-meta span { color: var(--color-primary); font-weight: 400; }.content { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.55; font-size: 14px; }
  .run-process { margin: 8px 0 10px; border: 1px solid color-mix(in srgb, var(--color-primary) 22%, var(--color-border)); border-radius: 6px; background: color-mix(in srgb, var(--color-input) 78%, transparent); overflow: hidden; }.run-process > summary { display: flex; align-items: center; gap: 8px; padding: 8px 10px; color: var(--color-primary); cursor: pointer; list-style-position: inside; user-select: none; }.process-title { font-size: 12px; font-weight: 700; }.process-count { margin-left: auto; color: var(--color-text-muted); font-size: 10px; }.run-meta { display:flex; flex-wrap:wrap; gap:5px; padding: 0 10px 7px; }.run-meta span { padding:2px 5px; border:1px solid var(--color-border); border-radius:3px; color:var(--color-text-muted); font-size:10px; }.trace-steps { display: grid; gap: 5px; padding: 0 7px 8px; }.trace-step { border-left: 2px solid var(--color-border); color: var(--color-text-muted); font-size: 12px; line-height: 1.55; }.trace-step > summary { display: flex; align-items: center; gap: 6px; padding: 6px 8px; cursor: pointer; list-style: none; user-select: none; }.trace-step > summary::-webkit-details-marker, .run-process > summary::-webkit-details-marker { display: none; }.trace-step > summary::before, .run-process > summary::before { content: '›'; color: var(--color-text-muted); font-size: 16px; transition: transform .15s ease; }.trace-step[open] > summary::before, .run-process[open] > summary::before { transform: rotate(90deg); }.step-icon { display: inline-grid; width: 19px; height: 19px; place-items: center; border-radius: 4px; background: color-mix(in srgb, var(--color-primary) 17%, var(--color-input)); color: var(--color-primary); font-size: 10px; font-weight: 700; }.trace-step > div { max-height: 260px; overflow: auto; padding: 0 9px 8px 33px; white-space: pre-wrap; overflow-wrap: anywhere; }.trace-step.thought { border-left-color: var(--color-primary); }.trace-step.dialogue { display: flex; gap: 7px; padding: 6px 8px; }.trace-step.dialogue > div { max-height: none; padding: 0; }.trace-step.tool { border-left-color: color-mix(in srgb, var(--color-online) 65%, var(--color-border)); }.tool-name { color: var(--color-text); font-weight: 600; }.tool-type { padding: 2px 5px; border: 1px solid var(--color-border); border-radius: 3px; color: var(--color-text-muted); font-size: 10px; }.tool-status { margin-left: auto; color: var(--color-online); font-size: 10px; }.tool-waiting { color: var(--color-warning, #d99b36); }.tool-detail { padding-bottom: 8px !important; }.tool-label { margin: 4px 0 3px; color: var(--color-text-muted); font-size: 10px; font-weight: 700; }.tool-detail pre { max-height: 180px; margin: 0; padding: 7px 8px; overflow: auto; border: 1px solid var(--color-border); border-radius: 4px; background: var(--color-bg); color: var(--color-text); font: 11px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }.tool-summary, .tool-result { margin-top: 5px; color: var(--color-text-muted); font-size: 11px; }.trace-empty { padding: 0 10px 9px; color: var(--color-text-muted); font-size: 11px; }.final-result { padding-top: 9px; border-top: 1px solid color-mix(in srgb, var(--color-border) 75%, transparent); }.result-label { margin-bottom: 4px; color: var(--color-text-muted); font-size: 10px; font-weight: 700; letter-spacing: .5px; }
  .run-error { padding: 0 16px 8px; font-size: 12px; }.composer { display: flex; flex-shrink: 0; gap: 8px; padding: 12px 16px calc(12px + env(safe-area-inset-bottom, 0px)); border-top: 1px solid var(--color-border); background: var(--color-surface); }.composer textarea { flex: 1; min-width: 0; border: 1px solid var(--color-border); border-radius: 5px; background: var(--color-input); color: var(--color-text); font: inherit; padding: 9px 10px; resize: none; outline: none; }.composer textarea:focus { border-color: var(--color-primary); }.composer > button { align-self: end; min-width: 54px; border-color: var(--color-primary); background: var(--color-primary); color: #fff; font-weight: 700; }.composer > button.cancel { border-color: var(--color-error); background: transparent; color: var(--color-error); }
  @media (max-width: 600px) { .conversation-sidebar { flex-basis:112px; }.conversation-sidebar-heading { padding:0 7px; }.conversation-items { padding:5px; }.conversation-items button { padding:7px 5px; font-size:11px; }.agent-toolbar { padding: 0 10px; }.agent-purpose, .state { display: none; }.toolbar-actions { margin-left: auto; }.toolbar-actions .delete { padding: 0 6px; }.messages { padding: 14px 0; }.message-row { padding: 0 12px; }.message { max-width: 82%; }.agent-status { grid-template-columns:1fr; gap:7px; margin-inline:12px; }.composer { padding: 10px calc(10px + env(safe-area-inset-right, 0px)) calc(10px + env(safe-area-inset-bottom, 0px)) calc(10px + env(safe-area-inset-left, 0px)); } }
</style>
