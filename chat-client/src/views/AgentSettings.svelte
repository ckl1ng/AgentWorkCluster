<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import { api } from '../lib/api.js';
  import AgentGovernance from './AgentGovernance.svelte';

  export let agent;
  const dispatch = createEventDispatcher();
  let saving = false;
  let error = '';
  let devices = [];
  let workspaces = [];
  let loadingLocal = true;
  let bindingLocal = false;
  let approvingPairing = false;
  let qqLoading = true;
  let qqConnecting = false;
  let qq = { configured: false, status: 'disconnected', bot_id: '', last_error: '' };
  let qqForm = { appId: '', appSecret: '', botId: '', intents: 33554432 };
  let pairing = { id: '', code: '' };
  let local = {
    deviceId: agent.default_device_id || '',
    workspaceId: agent.default_workspace_id || '',
    modelMode: agent.model_mode || 'server_proxy',
  };
  let form = {
    name: agent.name,
    description: agent.description || '',
    model_display_name: agent.model?.display_name || 'OpenAI compatible',
    base_url: agent.model?.base_url || '',
    api_key: '',
    model_id: agent.model?.model_id || '',
    temperature: agent.model?.temperature ?? 0.4,
    max_tokens: agent.model?.max_tokens ?? 2048,
    timeout_seconds: agent.model?.timeout_seconds ?? 60,
    system_prompt: agent.system_prompt || 'You are a helpful assistant.',
    max_tool_calls: agent.run_policy?.max_tool_calls ?? 6,
    max_concurrent_runs: agent.run_policy?.max_concurrent_runs ?? 2,
    daily_token_budget: agent.run_policy?.daily_token_budget ?? 0,
    monthly_token_budget: agent.run_policy?.monthly_token_budget ?? 0,
    context_window: agent.run_policy?.context_window ?? 32768,
  };
  let activeTab = 'general';
  let localLoaded = false;
  let governanceDirty = false;

  onMount(loadQqConnection);

  async function selectTab(tab) {
    if (activeTab === 'governance' && tab !== 'governance' && governanceDirty
      && !confirm('工具授权尚未保存，确定要放弃这些更改吗？')) return;
    activeTab = tab;
    if (tab === 'local' && !localLoaded) {
      localLoaded = true;
      await loadLocalDevices();
    }
  }

  function requestCancel() {
    if (!governanceDirty || confirm('工具授权尚未保存，确定要放弃这些更改吗？')) dispatch('cancel');
  }

  async function loadQqConnection() {
    qqLoading = true;
    try {
      qq = await api.getAgentQqConnection(agent.id);
    } catch (e) {
      qq = { configured: false, status: 'gateway_unavailable', bot_id: '', last_error: e.message || 'QQ Gateway 不可用' };
    } finally {
      qqLoading = false;
    }
  }

  async function connectQq() {
    if (!qqForm.appId.trim() || !qqForm.appSecret) {
      error = '请输入 QQ AppID 和 AppSecret';
      return;
    }
    qqConnecting = true;
    error = '';
    try {
      qq = await api.connectAgentQq(agent.id, {
        app_id: qqForm.appId.trim(), client_secret: qqForm.appSecret,
        bot_id: qqForm.botId.trim() || undefined, intents: Number(qqForm.intents) || 33554432,
      });
      qqForm.appSecret = '';
      window.setTimeout(loadQqConnection, 1500);
    } catch (e) {
      error = e.message || 'QQ Bot 连接失败';
    } finally {
      qqConnecting = false;
    }
  }

  async function disconnectQq() {
    qqConnecting = true;
    error = '';
    try {
      qq = await api.disconnectAgentQq(agent.id);
    } catch (e) {
      error = e.message || 'QQ Bot 断开失败';
    } finally {
      qqConnecting = false;
    }
  }

  async function loadLocalDevices() {
    loadingLocal = true;
    try {
      devices = await api.listLocalDevices();
      await loadLocalWorkspaces();
    } catch (e) {
      error = e.message || '无法加载本地设备';
    } finally {
      loadingLocal = false;
    }
  }

  async function loadLocalWorkspaces() {
    workspaces = local.deviceId ? await api.listLocalWorkspaces(local.deviceId) : [];
    if (!workspaces.some(item => item.id === local.workspaceId)) local.workspaceId = '';
  }

  async function selectLocalDevice() {
    try {
      await loadLocalWorkspaces();
    } catch (e) {
      error = e.message || '无法加载本地工作区';
    }
  }

  async function bindLocal() {
    if (!local.deviceId || !local.workspaceId) return;
    bindingLocal = true;
    error = '';
    try {
      const updated = await api.bindLocalAgent(agent.id, {
        device_id: local.deviceId,
        workspace_id: local.workspaceId,
        model_mode: local.modelMode,
      });
      agent = { ...agent, ...updated };
      dispatch('updated', { agent });
    } catch (e) {
      error = e.message || '无法绑定本地 Agent';
    } finally {
      bindingLocal = false;
    }
  }

  async function approvePairing() {
    if (!pairing.id.trim() || !pairing.code.trim()) return;
    approvingPairing = true;
    error = '';
    try {
      await api.approveLocalPairing(pairing.id.trim(), pairing.code.trim());
      pairing = { id: '', code: '' };
      await loadLocalDevices();
    } catch (e) {
      error = e.message || '无法批准设备配对';
    } finally {
      approvingPairing = false;
    }
  }

  async function revokeLocalDevice() {
    const device = devices.find(item => item.id === local.deviceId);
    if (!device || !confirm(`撤销「${device.display_name}」后，该设备不能再领取本地任务。`)) return;
    bindingLocal = true;
    error = '';
    try {
      await api.revokeLocalDevice(device.id);
      local.deviceId = '';
      local.workspaceId = '';
      await loadLocalDevices();
    } catch (e) {
      error = e.message || '无法撤销本地设备';
    } finally {
      bindingLocal = false;
    }
  }

  async function save() {
    saving = true; error = '';
    try {
      const payload = {
        name: form.name.trim(), description: form.description.trim(),
        model_display_name: form.model_display_name.trim(), base_url: form.base_url.trim(),
        model_id: form.model_id.trim(), temperature: Number(form.temperature),
        max_tokens: Number(form.max_tokens), timeout_seconds: Number(form.timeout_seconds),
        system_prompt: form.system_prompt,
        run_policy: {
          max_tool_calls: Number(form.max_tool_calls), max_concurrent_runs: Number(form.max_concurrent_runs),
          daily_token_budget: Number(form.daily_token_budget), monthly_token_budget: Number(form.monthly_token_budget),
          context_window: Number(form.context_window),
        },
      };
      if (form.api_key) payload.api_key = form.api_key;
      const updated = await api.updateAgent(agent.id, payload);
      dispatch('saved', { agent: { ...updated, tool_ids: agent.tool_ids || [] } });
    } catch (e) { error = e.message || '无法保存 Agent 配置'; }
    finally { saving = false; }
  }

  async function toggleState() {
    try {
      const updated = agent.state === 'active' ? await api.pauseAgent(agent.id) : await api.resumeAgent(agent.id);
      agent = { ...agent, ...updated };
    } catch (e) { error = e.message || '无法更新 Agent 状态'; }
  }
</script>

<section class="settings" aria-label="Agent 配置">
  <header><div><p>AGENT CONFIG</p><h2>{agent.name}</h2></div><button type="button" class="close" title="关闭配置" aria-label="关闭配置" on:click={requestCancel}>×</button></header>
  <nav class="tabs" aria-label="Agent 配置页签"><button type="button" class:active={activeTab === 'general'} on:click={() => selectTab('general')}>基础配置</button><button type="button" class:active={activeTab === 'local'} on:click={() => selectTab('local')}>本地执行</button><button type="button" class:active={activeTab === 'governance'} on:click={() => selectTab('governance')}>工具与记忆</button></nav>
  {#if activeTab === 'general'}<form on:submit|preventDefault={save} novalidate>
    <section class="qq-execution"><div class="section-title"><div><h3>QQ Bot 连接</h3><p class="muted">AppSecret 只提交到服务端加密的 QQ Gateway，不会保存到浏览器。</p></div><span class="qq-status" class:connected={qq.status === 'connected'} class:error-status={qq.status === 'error'}>{qqLoading ? '读取中' : qq.status === 'connected' ? '已连接' : qq.status === 'connecting' ? '连接中' : qq.status === 'gateway_unavailable' ? 'Gateway 不可用' : '未连接'}</span></div><div class="two"><label>AppID<input required maxlength="128" bind:value={qqForm.appId} placeholder="QQ 开放平台 AppID" autocomplete="off" /></label><label>AppSecret<input required type="password" maxlength="256" bind:value={qqForm.appSecret} placeholder="QQ 开放平台 AppSecret" autocomplete="new-password" /></label></div><div class="two"><label>Bot ID（可选）<input maxlength="128" bind:value={qqForm.botId} placeholder="留空则连接后自动识别" /></label><label>事件 Intents<input type="number" min="1" max="2147483647" bind:value={qqForm.intents} /></label></div><div class="local-actions"><button type="button" on:click={connectQq} disabled={qqConnecting || qqLoading}>{qqConnecting ? '正在连接...' : '一键连接 QQ Bot'}</button><button type="button" class="danger" on:click={disconnectQq} disabled={qqConnecting || !qq.configured}>断开连接</button></div>{#if qq.bot_id}<p class="muted">已识别 Bot ID：{qq.bot_id}</p>{/if}{#if qq.last_error}<p class="error" role="status">{qq.last_error}</p>{/if}</section>
    <section><div class="section-title"><h3>运行状态</h3><button type="button" class:danger={agent.state === 'active'} on:click={toggleState}>{agent.state === 'active' ? '暂停 Agent' : '恢复 Agent'}</button></div></section>
    <section><h3>基础信息</h3><div class="two"><label>名称<input required maxlength="80" bind:value={form.name} /></label><label>用途<input maxlength="280" bind:value={form.description} /></label></div></section>
    <section><h3>模型连接</h3><div class="two"><label>连接名称<input required bind:value={form.model_display_name} /></label><label>模型 ID<input required bind:value={form.model_id} /></label></div><label>Base URL<input required type="url" bind:value={form.base_url} /></label><label>替换 API Key<input type="password" autocomplete="new-password" bind:value={form.api_key} placeholder="留空则保持现有密钥" /></label><div class="three"><label>温度<input type="number" min="0" max="2" step="0.1" bind:value={form.temperature} /></label><label>输出 Token<input type="number" min="1" max="32768" bind:value={form.max_tokens} /></label><label>超时（秒）<input type="number" min="5" max="300" bind:value={form.timeout_seconds} /></label></div></section>
    <section><h3>上下文与预算</h3><div class="three"><label>上下文窗口<input type="number" min="2048" bind:value={form.context_window} /></label><label>并发运行<input type="number" min="1" max="10" bind:value={form.max_concurrent_runs} /></label><label>工具调用上限<input type="number" min="0" max="20" bind:value={form.max_tool_calls} /></label></div><div class="two"><label>每日 Token（0 不限）<input type="number" min="0" bind:value={form.daily_token_budget} /></label><label>每月 Token（0 不限）<input type="number" min="0" bind:value={form.monthly_token_budget} /></label></div><label>系统提示词<textarea required rows="7" maxlength="32000" bind:value={form.system_prompt}></textarea></label></section>
    {#if error}<p class="error" role="status">{error}</p>{/if}
    <footer><button type="button" class="secondary" on:click={requestCancel}>取消</button><button type="submit" disabled={saving}>{saving ? '正在保存...' : '保存配置'}</button></footer>
  </form>{:else if activeTab === 'local'}<section class="local-execution"><h3>本地设备与工作区</h3><p class="muted">服务端仅保存设备和工作区名称，不保存本机绝对路径。</p><div class="two"><label>配对会话 ID<input bind:value={pairing.id} placeholder="由 local-agent auth login 输出" /></label><label>配对码<input bind:value={pairing.code} placeholder="LA-000000" pattern="LA-[0-9]{6}" /></label></div><button type="button" class="secondary" on:click={approvePairing} disabled={approvingPairing || !pairing.id.trim() || !pairing.code.trim()}>{approvingPairing ? '正在批准...' : '批准本地设备'}</button>{#if loadingLocal}<p class="muted">正在加载...</p>{:else if !devices.length}<p class="muted">批准配对后，设备会出现在此处。</p>{:else}<div class="two"><label>设备<select bind:value={local.deviceId} on:change={selectLocalDevice}><option value="">选择设备</option>{#each devices as device (device.id)}<option value={device.id} disabled={device.status === 'revoked'}>{device.display_name} · {device.platform || 'unknown'} · {device.status}</option>{/each}</select></label><label>工作区<select bind:value={local.workspaceId} disabled={!local.deviceId || !workspaces.length}><option value="">选择工作区</option>{#each workspaces as workspace (workspace.id)}<option value={workspace.id}>{workspace.display_name} · 策略 v{workspace.policy_version}</option>{/each}</select></label></div><label>模型模式<select bind:value={local.modelMode}><option value="server_proxy">服务端代理模型</option><option value="local_direct">本机直连模型</option></select></label><div class="local-actions"><button type="button" on:click={bindLocal} disabled={bindingLocal || !local.deviceId || !local.workspaceId}>{bindingLocal ? '正在绑定...' : '绑定本地执行'}</button><button type="button" class="danger" on:click={revokeLocalDevice} disabled={bindingLocal || !local.deviceId}>撤销设备</button></div>{#if agent.execution_target === 'local'}<p class="muted">当前 Agent 的新 run 不会进入云端 Worker。</p>{/if}{/if}{#if error}<p class="error" role="status">{error}</p>{/if}</section>{:else}<AgentGovernance {agent} on:updated={event => dispatch('updated', event.detail)} on:dirty={event => governanceDirty = event.detail.tools} />{/if}
</section>

<style>
  .settings { flex: 1; min-width: 0; overflow-y: auto; padding: 26px max(18px, 6vw) 48px; }.settings > header, form, .tabs, .local-execution, .settings :global(.governance) { max-width: 860px; }.settings > header { display: flex; justify-content: space-between; margin-bottom: 20px; }.settings > header p { color: var(--color-primary); font-size: 10px; font-weight: 700; letter-spacing: 1px; }h2 { margin-top: 3px; font-size: 22px; }.tabs { display:flex; gap:6px; margin-bottom:8px; border-bottom:1px solid var(--color-border); }.tabs button { margin-bottom:-1px; border-color:transparent; border-radius:0; background:transparent; color:var(--color-text-muted); }.tabs button.active { border-bottom-color:var(--color-primary); color:var(--color-primary); }section section, .local-execution { padding: 18px 0; border-top: 1px solid var(--color-border); }h3 { font-size: 14px; }.section-title { display: flex; align-items: center; justify-content: space-between; }.two,.three { display: grid; gap: 12px; }.two { grid-template-columns: repeat(2,minmax(0,1fr)); }.three { grid-template-columns: repeat(3,minmax(0,1fr)); }label { display: grid; gap: 6px; margin-top: 12px; color: var(--color-text-muted); font-size: 12px; font-weight: 600; }input,textarea,select { width: 100%; min-width: 0; padding: 9px 10px; border: 1px solid var(--color-border); border-radius: 5px; background: var(--color-input); color: var(--color-text); font: inherit; font-size: 13px; outline: none; resize: vertical; }input:focus,textarea:focus,select:focus { border-color: var(--color-primary); }button { min-height: 34px; padding: 0 12px; border: 1px solid var(--color-primary); border-radius: 4px; background: var(--color-primary); color: #fff; cursor: pointer; font-size: 12px; font-weight: 700; }.close { display:grid; width:34px; place-items:center; padding:0; border-color:var(--color-border); background:transparent; color:var(--color-text-muted); font-size:22px; }.secondary { border-color:var(--color-border); background:transparent; color:var(--color-text-muted); }.danger { border-color:var(--color-error); background:transparent; color:var(--color-error); }.local-execution > button { margin-top:16px; }.local-actions { display:flex; gap:8px; margin-top:16px; }.local-actions button { margin:0; }footer { display:flex; justify-content:flex-end; gap:8px; padding-top:18px; }.error { margin-top:12px; color:var(--color-error); font-size:12px; }button:disabled { opacity:.55; cursor:default; }
  @media(max-width:650px){.settings{padding:20px 14px 36px}.two,.three{grid-template-columns:1fr}}
  .qq-execution { padding: 18px 0; border-top: 1px solid var(--color-border); }.qq-execution .section-title { align-items: flex-start; }.qq-status { padding: 4px 8px; border: 1px solid var(--color-border); border-radius: 4px; color: var(--color-text-muted); font-size: 11px; font-weight: 700; }.qq-status.connected { border-color: var(--color-online); color: var(--color-online); }.qq-status.error-status { border-color: var(--color-error); color: var(--color-error); }
</style>
