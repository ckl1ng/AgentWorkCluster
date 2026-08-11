<script>
  import { onDestroy, onMount } from 'svelte';
  import { api } from '../lib/api.js';
  import { agents, auth } from '../lib/store.js';

  let tasks = [];
  let selected = null;
  let detail = { context: [], assignments: [], results: [], runs: [], confirmations: [], traces: {} };
  let filter = '';
  let loading = true;
  let error = '';
  let closing = false;
  let socket = null;
  let reconnectTimer = null;
  let refreshTimer = null;
  let reconnectAttempt = 0;
  let mounted = false;
  let notifications = [];
  let notificationsLoaded = false;
  let notificationOpen = false;
  let createOpen = false;
  let createSaving = false;
  let draft = { title: '', goal: '', assigned_agent_id: '' };

  const labels = {
    queued: '排队中', assigned: '已指派', in_progress: '进行中', waiting_confirmation: '等待确认',
    awaiting_proposer_close: '待我收尾', attention_required: '需处理', closed: '已完成', cancelled: '已取消',
  };

  async function load() {
    try {
      loading = true;
      const [taskItems, notificationItems] = await Promise.all([
        api.listTasks(filter || undefined), api.listNotifications(),
      ]);
      tasks = Array.isArray(taskItems) ? taskItems : [];
      updateNotifications(Array.isArray(notificationItems) ? notificationItems : []);
      if (selected) {
        const fresh = tasks.find((item) => item.id === selected.id);
        if (fresh) await open(fresh); else { selected = null; detail = { context: [], assignments: [], results: [], runs: [], confirmations: [], traces: {} }; }
      }
    } catch (e) { error = e.message || '无法加载任务'; }
    finally { loading = false; }
  }

  async function open(task) {
    selected = task;
    error = '';
    try {
      const [current, context, assignments, results, runs, confirmations] = await Promise.all([
        api.getTask(task.id), api.getTaskContext(task.id), api.getTaskAssignments(task.id), api.getTaskResults(task.id), api.getTaskRuns(task.id), api.getTaskConfirmations(task.id),
      ]);
      selected = current;
      detail = { context, assignments, results, runs, confirmations, traces: {} };
    } catch (e) { error = e.message || '无法加载任务详情'; }
  }

  function updateNotifications(items) {
    const previous = new Set(notifications.map((item) => item.id));
    notifications = items;
    if (notificationsLoaded && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      for (const item of items) {
        if (!item.read_at && !previous.has(item.id)) {
          new Notification('任务需要处理', { body: item.payload?.title || '有新的任务通知' });
        }
      }
    }
    notificationsLoaded = true;
  }

  function scheduleLoad() {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => load(), 120);
  }

  function connect() {
    if (!mounted) return;
    const token = localStorage.getItem('chat_token');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${proto}//${location.host}/task/ws?token=${encodeURIComponent(token || '')}`);
    socket.onopen = () => {
      reconnectAttempt = 0;
      socket?.send(JSON.stringify({ type: 'task.subscribe_all' }));
      scheduleLoad();
    };
    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        if (event.type === 'task.resync_required' || event.task_id) scheduleLoad();
      } catch { /* The REST refresh remains available after malformed events. */ }
    };
    socket.onclose = () => {
      socket = null;
      if (!mounted) return;
      const delay = Math.min(10000, 500 * (2 ** reconnectAttempt));
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(connect, delay);
    };
  }

  async function requestDesktopNotifications() {
    if (typeof Notification === 'undefined') return;
    await Notification.requestPermission();
  }

  async function openNotification(notification) {
    notificationOpen = false;
    if (!notification.read_at) await api.markNotificationRead(notification.id);
    const task = tasks.find((item) => item.id === notification.task_id);
    if (task) await open(task);
    await load();
  }

  async function action(kind) {
    if (!selected || closing) return;
    closing = true;
    try {
      if (kind === 'close') await api.closeTask(selected.id, selected.result_summary || '已验收结果');
      if (kind === 'reopen') await api.reopenTask(selected.id);
      if (kind === 'cancel') await api.cancelTask(selected.id);
      await load();
    } catch (e) { error = e.message || '任务操作失败'; }
    finally { closing = false; }
  }

  async function createTask() {
    if (!draft.title.trim() || !draft.goal.trim() || createSaving) return;
    createSaving = true;
    error = '';
    try {
      const payload = { title: draft.title.trim(), goal: draft.goal.trim() };
      if (draft.assigned_agent_id) payload.assigned_agent_id = draft.assigned_agent_id;
      const task = await api.createTask(payload);
      draft = { title: '', goal: '', assigned_agent_id: '' };
      createOpen = false;
      await load();
      await open(task);
    } catch (e) { error = e.message || '无法创建任务'; }
    finally { createSaving = false; }
  }

  async function loadTrace(runId) {
    if (detail.traces[runId]) return;
    try {
      const trace = await api.getAgentTrace(runId);
      detail = { ...detail, traces: { ...detail.traces, [runId]: trace } };
    } catch (e) { error = e.message || '无法加载运行轨迹'; }
  }

  function flattenTaskTree(items) {
    const ids = new Set(items.map((item) => item.id));
    const children = new Map();
    const roots = [];
    for (const item of items) {
      if (item.parent_task_id && ids.has(item.parent_task_id)) {
        const group = children.get(item.parent_task_id) || [];
        group.push(item);
        children.set(item.parent_task_id, group);
      } else roots.push(item);
    }
    const flattened = [];
    const visit = (item, depth) => {
      flattened.push({ ...item, depth });
      for (const child of children.get(item.id) || []) visit(child, depth + 1);
    };
    for (const root of roots) visit(root, 0);
    return flattened;
  }

  $: visible = flattenTaskTree(tasks);
  $: unreadNotifications = notifications.filter((item) => !item.read_at);
  $: canManage = selected?.proposer_kind === 'user' && selected?.proposer_id === String($auth?.id);
  onMount(() => { mounted = true; load(); connect(); });
  onDestroy(() => {
    mounted = false;
    window.clearTimeout(reconnectTimer);
    window.clearTimeout(refreshTimer);
    socket?.close();
  });
</script>

<aside class="tasks" aria-label="任务工作台">
  <header><div><p>TASKS</p><h2>任务</h2></div><div class="header-actions"><button type="button" title="创建任务" aria-label="创建任务" on:click={() => createOpen = !createOpen}>+</button><button type="button" class:has-unread={unreadNotifications.length} title="任务通知" aria-label="任务通知" on:click={() => notificationOpen = !notificationOpen}>!<i>{unreadNotifications.length || ''}</i></button><button type="button" title="刷新任务" aria-label="刷新任务" on:click={load}>↻</button></div></header>
  {#if createOpen}<form class="create-task" on:submit|preventDefault={createTask}><label>标题<input bind:value={draft.title} maxlength="160" required /></label><label>目标<textarea bind:value={draft.goal} maxlength="50000" required></textarea></label><label>执行 Agent<select bind:value={draft.assigned_agent_id}><option value="">稍后指派</option>{#each $agents as agent (agent.id)}<option value={agent.id}>{agent.name}</option>{/each}</select></label><div><button type="button" class="secondary" on:click={() => createOpen = false}>取消</button><button type="submit" disabled={createSaving}>{createSaving ? '正在创建...' : '创建任务'}</button></div></form>{/if}
  {#if notificationOpen}<section class="notifications" aria-label="任务通知中心"><div class="notification-heading"><h3>通知</h3>{#if typeof Notification !== 'undefined' && Notification.permission !== 'granted'}<button type="button" on:click={requestDesktopNotifications}>启用桌面提醒</button>{/if}</div>{#each notifications as notification (notification.id)}<button type="button" class:read={notification.read_at} on:click={() => openNotification(notification)}><strong>{notification.payload?.title || '任务通知'}</strong><small>{labels[notification.kind] || notification.kind}</small></button>{:else}<p class="muted">暂无通知</p>{/each}</section>{/if}
  <div class="filters" role="tablist" aria-label="任务筛选">
    {#each [['', '全部'], ['in_progress', '进行中'], ['awaiting_proposer_close', '待收尾'], ['attention_required', '需处理']] as [value, label]}
      <button type="button" class:active={filter === value} on:click={() => { filter = value; load(); }}>{label}</button>
    {/each}
  </div>
  {#if error}<p class="error">{error}</p>{/if}
  <div class="task-list">
    {#if loading}<p class="muted">正在同步任务...</p>
    {:else if !visible.length}<p class="muted">没有匹配的任务</p>
    {:else}{#each visible as task (task.id)}
      <button type="button" class="task-row" class:selected={selected?.id === task.id} style:padding-left={`${8 + task.depth * 16}px`} on:click={() => open(task)}>
        <strong>{task.title}{#if task.unread_count}<i class="unread">{task.unread_count}</i>{/if}</strong><span class:attention={task.state === 'attention_required'} class:waiting={task.state === 'awaiting_proposer_close'}>{labels[task.state] || task.state}</span>
        <small>{task.current_assignment?.executor_id ? `执行者：${task.current_assignment.executor_id}` : '等待分配'}{#if task.child_count} · {task.child_count} 个子任务{/if}</small>
        {#if task.last_dispatch_event?.summary}<small class="dispatch">{task.last_dispatch_event.summary}</small>{/if}
      </button>
    {/each}{/if}
  </div>
  {#if selected}
    <section class="detail"><div class="detail-heading"><h3>{selected.title}</h3><span>{labels[selected.state] || selected.state}</span></div>
      <p>{selected.goal}</p>
      {#if selected.result_summary}<section><h4>结果摘要</h4><p>{selected.result_summary}</p></section>{/if}
      <section><h4>提交结果</h4>{#each detail.results as result (result.id)}<p class="event"><small>{new Date(result.created_at).toLocaleString()}{result.risk_summary ? ` · 风险：${result.risk_summary}` : ''}</small>{result.result}</p>{:else}<p class="muted">执行者尚未提交结果</p>{/each}</section>
      <section><h4>执行记录</h4>{#each detail.assignments as assignment (assignment.id)}<p class="meta">尝试 {assignment.attempt} · {assignment.state}</p>{:else}<p class="muted">尚未指派</p>{/each}</section>
      <section><h4>工具确认</h4>{#each detail.confirmations as confirmation (confirmation.id)}<p class="meta">{confirmation.tool_name} · {confirmation.state}</p>{:else}<p class="muted">暂无工具确认</p>{/each}</section>
      <section><h4>Run 轨迹</h4>{#each detail.runs as run (run.id)}<details on:toggle={() => loadTrace(run.id)}><summary>{run.state} · 尝试 {run.attempt || 0} · {new Date(run.created_at).toLocaleString()}</summary>{#if run.final_content}<p class="final">{run.final_content}</p>{/if}{#if detail.traces[run.id]}<ol>{#each detail.traces[run.id] as event (event.sequence)}<li><small>{event.type}</small>{event.payload?.summary || event.payload?.error || ''}</li>{/each}</ol>{:else}<p class="muted">展开后加载轨迹...</p>{/if}</details>{:else}<p class="muted">暂无 Run 记录</p>{/each}</section>
      <section><h4>工作上下文</h4>{#each detail.context as event (event.id)}<p class="event"><small>{event.kind}</small>{event.content}</p>{:else}<p class="muted">暂无工作事件</p>{/each}</section>
      {#if canManage && (selected.state === 'awaiting_proposer_close' || selected.state === 'attention_required')}<div class="actions">
        {#if selected.state === 'awaiting_proposer_close'}<button type="button" on:click={() => action('close')} disabled={closing}>验收收尾</button>{/if}
        <button type="button" on:click={() => action('reopen')} disabled={closing}>继续处理</button><button type="button" class="danger" on:click={() => action('cancel')} disabled={closing}>取消</button>
      </div>{/if}
    </section>
  {/if}
</aside>

<style>
  .tasks {
    width: 348px;
    flex: 0 0 348px;
    overflow: auto;
    padding: 16px;
    border: 1px solid var(--color-border);
    border-radius: 26px;
    background: linear-gradient(180deg, rgba(255, 255, 255, .04), transparent 18%), var(--color-panel);
    backdrop-filter: blur(18px);
    box-shadow: var(--shadow-soft);
    box-sizing: border-box;
  }

  header, .detail-heading, .actions, .notification-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  header { padding: 4px 2px 14px; }
  header p, h2 { margin: 0; }
  header p { color: var(--color-primary); font-size: 10px; font-weight: 700; letter-spacing: .8px; }
  h2 { margin-top: 4px; font-size: 20px; }
  header button { display: grid; width: 32px; height: 32px; place-items: center; padding: 0; border: 1px solid transparent; border-radius: 10px; background: transparent; color: var(--color-text-muted); cursor: pointer; font-size: 17px; }
  header button:hover { border-color: var(--color-border); background: var(--color-hover); color: var(--color-text); }
  .header-actions { display: flex; gap: 4px; }
  .header-actions button { position: relative; }
  .header-actions i { position: absolute; top: -5px; right: -5px; min-width: 16px; height: 16px; padding: 0 3px; border: 2px solid var(--color-panel-strong); border-radius: 999px; background: var(--color-danger); color: #fff; font-size: 9px; font-style: normal; line-height: 12px; }

  .notifications, .create-task {
    display: grid;
    gap: 9px;
    margin-top: 8px;
    padding: 12px;
    border: 1px solid var(--color-border);
    border-radius: 16px;
    background: var(--color-input);
    animation: reveal var(--duration-normal) var(--ease-soft);
  }

  .notifications h3 { font-size: 13px; }
  .notifications button { display: grid; gap: 3px; padding: 9px 8px; border: 0; border-left: 2px solid transparent; border-radius: 8px; background: transparent; color: var(--color-text); cursor: pointer; text-align: left; }
  .notifications button:hover { background: var(--color-hover); }
  .notifications button:not(.read) { border-left-color: var(--color-primary); background: var(--color-active); }
  .notifications button.read { opacity: .58; }
  .notification-heading button { width: auto; height: auto; padding: 0; border: 0; color: var(--color-primary); font-size: 11px; }

  .create-task label { display: grid; gap: 6px; color: var(--color-text-muted); font-size: 11px; font-weight: 600; }
  .create-task input, .create-task textarea, .create-task select { width: 100%; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-panel-strong); color: var(--color-text); font: inherit; padding: 9px 10px; }
  .create-task textarea { min-height: 86px; resize: vertical; }
  .create-task > div { display: flex; justify-content: flex-end; gap: 8px; }
  .create-task button, .actions button { min-height: 34px; padding: 0 11px; border: 1px solid var(--color-primary); border-radius: 10px; background: var(--color-active); color: var(--color-primary); cursor: pointer; font-size: 12px; font-weight: 600; }
  .create-task button:hover, .actions button:hover { background: var(--color-active-strong); }
  .create-task button.secondary { border-color: var(--color-border); background: transparent; color: var(--color-text-muted); }

  .filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 16px 0 12px; padding: 5px; border: 1px solid var(--color-border); border-radius: 14px; background: var(--color-input); }
  .filters button { flex: 1; min-height: 30px; padding: 0 8px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--color-text-muted); cursor: pointer; font-size: 11px; white-space: nowrap; }
  .filters button:hover { color: var(--color-text); background: var(--color-hover); }
  .filters button.active { border-color: color-mix(in srgb, var(--color-primary) 40%, var(--color-border)); background: var(--color-active); color: var(--color-primary); }

  .task-list { display: grid; gap: 6px; }
  .task-row { display: grid; gap: 5px; width: 100%; padding: 11px 10px; border: 1px solid transparent; border-radius: 14px; background: transparent; color: var(--color-text); cursor: pointer; text-align: left; }
  .task-row:hover { transform: translateY(-1px); border-color: var(--color-border); background: var(--color-hover); }
  .task-row.selected { border-color: color-mix(in srgb, var(--color-primary) 42%, var(--color-border)); background: var(--color-active); box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .03); }
  .task-row strong { overflow: hidden; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
  .task-row span, .meta, small { color: var(--color-text-muted); font-size: 11px; }
  .task-row span { width: max-content; max-width: 100%; padding: 3px 7px; border-radius: 999px; background: var(--color-hover); color: var(--color-text-soft); }
  .task-row .attention { background: var(--color-danger-bg); color: var(--color-danger); }
  .task-row .waiting { background: var(--color-warning-bg); color: var(--color-warning); }
  .dispatch { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .unread { display: inline-grid; min-width: 17px; height: 17px; place-items: center; margin-left: 5px; padding: 0 4px; border-radius: 999px; background: var(--color-primary); color: #fff; font-size: 9px; font-style: normal; line-height: 1; }

  .detail { margin-top: 16px; padding: 16px; border: 1px solid var(--color-border); border-radius: 18px; background: var(--color-panel-strong); animation: reveal var(--duration-slow) var(--ease-soft); }
  .detail-heading { align-items: flex-start; }
  .detail-heading h3 { max-width: 220px; font-size: 15px; line-height: 1.35; }
  .detail-heading > span { flex-shrink: 0; padding: 4px 7px; border-radius: 999px; background: var(--color-active); color: var(--color-primary); font-size: 10px; }
  h3 { margin: 0; }
  h4 { margin: 18px 0 7px; color: var(--color-text-soft); font-size: 11px; letter-spacing: .4px; }
  .detail p { white-space: pre-wrap; font-size: 12px; line-height: 1.65; }
  .event { padding: 8px 0 8px 10px; border-left: 2px solid var(--color-border-strong); }
  .event small { display: block; margin-bottom: 3px; }
  .detail details { padding: 9px 0; border-top: 1px solid var(--color-border); }
  .detail summary { cursor: pointer; color: var(--color-text-muted); font-size: 11px; }
  .detail ol { margin: 8px 0 0; padding-left: 18px; }
  .detail li { margin: 6px 0; white-space: pre-wrap; font-size: 11px; line-height: 1.5; }
  .detail li small { display: block; color: var(--color-text-muted); }
  .final { padding-left: 10px; border-left: 2px solid var(--color-primary); }
  .actions { flex-wrap: wrap; margin-top: 16px; }
  .actions .danger { border-color: var(--color-danger); background: var(--color-danger-bg); color: var(--color-danger); }
  .muted, .error { padding: 10px 2px; color: var(--color-text-muted); font-size: 12px; }
  .error { color: var(--color-danger); }

  @keyframes reveal {
    from { opacity: 0; transform: translateY(-5px); }
    to { opacity: 1; transform: translateY(0); }
  }

  @media (max-width: 980px) {
    .tasks { width: 100%; max-height: 40vh; flex-basis: auto; }
  }
</style>
