<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import { api } from '../lib/api.js';

  export let agent;
  const dispatch = createEventDispatcher();
  let error = '';
  let loading = true;
  let tools = [];
  let memories = [];
  let assigned = new Set(agent.tool_ids || []);
  let savedAssigned = new Set(agent.tool_ids || []);
  let toolAssignmentsDirty = false;
  let savingTools = false;
  let savingMemory = false;
  let tool = { name: '', description: '', kind: 'http', method: 'GET', url: '', command: '', args: '', headers: '{}', schema: '{"type":"object","properties":{}}', sideEffect: 'read', confirmationMode: 'none', rateLimit: 6, parameterLocations: '{}' };
  let openApi = { documentUrl: '', document: '', baseUrl: '' };
  let mcp = { url: '', headers: '{}' };
  let candidates = [];
  let candidateSource = '';
  let memory = { content: '', kind: 'fact', importance: 50 };

  onMount(load);

  async function load() {
    loading = true; error = '';
    try {
      const [available, current, storedMemories] = await Promise.all([
        api.listTools(), api.listAgentTools(agent.id), api.listMemories(agent.id),
      ]);
      tools = available;
      assigned = new Set(current.map(item => item.id));
      savedAssigned = new Set(assigned);
      toolAssignmentsDirty = false;
      memories = storedMemories;
    } catch (e) { error = e.message || '无法加载治理配置'; }
    finally { loading = false; }
  }

  function hasUnsavedToolAssignments() {
    return assigned.size !== savedAssigned.size || [...assigned].some(id => !savedAssigned.has(id));
  }

  function toggleTool(id) {
    const next = new Set(assigned);
    if (next.has(id)) next.delete(id); else next.add(id);
    assigned = next;
    toolAssignmentsDirty = hasUnsavedToolAssignments();
    error = '';
    dispatch('dirty', { tools: toolAssignmentsDirty });
  }

  async function saveToolAssignments() {
    if (savingTools) return;
    savingTools = true;
    error = '';
    try {
      const updated = await api.assignAgentTools(agent.id, [...assigned]);
      savedAssigned = new Set(assigned);
      toolAssignmentsDirty = false;
      dispatch('dirty', { tools: false });
      dispatch('updated', { agent: { ...agent, ...updated, tool_ids: [...assigned] } });
    } catch (e) { error = e.message || '无法更新工具分配'; }
    finally { savingTools = false; }
  }

  function normalizePolicy(value = tool) {
    if (value.sideEffect === 'destructive') value.confirmationMode = 'per_call';
    if (value.sideEffect === 'write' && value.confirmationMode === 'none') value.confirmationMode = 'per_call';
    if (value.kind !== 'mcp' && value.method !== 'GET' && value.method !== 'HEAD' && value.sideEffect === 'read') value.sideEffect = 'write';
  }

  function parseJsonObject(value, label) {
    let parsed;
    try { parsed = JSON.parse(value || '{}'); }
    catch { throw new Error(`${label} 必须是有效 JSON`); }
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
      throw new Error(`${label} 必须是 JSON 对象`);
    }
    return parsed;
  }

  function parseJson(value, label) {
    try { return JSON.parse(value); } catch { throw new Error(`${label} 必须是有效 JSON`); }
  }

  function buildToolPayload(value) {
    normalizePolicy(value);
    const name = value.name.trim();
    if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(name)) {
      throw new Error('名称必须以英文字母开头，且只能包含字母、数字和下划线');
    }
    if (value.kind === 'local' || value.kind === 'mcp_stdio') {
      if (!value.command.trim()) throw new Error('请填写本地命令');
    } else {
      if (!value.url.trim()) throw new Error('请填写工具 URL');
      try {
        const parsedUrl = new URL(value.url.trim());
        if (!['http:', 'https:'].includes(parsedUrl.protocol)) throw new Error();
      } catch { throw new Error('工具 URL 必须是有效的 HTTP 或 HTTPS 地址'); }
    }
    if ((value.kind === 'mcp' || value.kind === 'mcp_stdio') && !value.remoteToolName?.trim()) {
      throw new Error('MCP 工具必须填写远程工具名');
    }
    const rateLimit = Number(value.rateLimit);
    if (!Number.isInteger(rateLimit) || rateLimit < 1 || rateLimit > 100) {
      throw new Error('每运行限额必须是 1 到 100 的整数');
    }
    if (value.sideEffect === 'write' && value.confirmationMode === 'none') {
      throw new Error('写工具必须选择每运行确认或每次确认');
    }
    if (value.sideEffect === 'destructive' && value.confirmationMode !== 'per_call') {
      throw new Error('破坏性工具必须选择每次确认');
    }
    return {
      name, description: value.description.trim(), kind: value.kind,
      config: value.kind === 'local' || value.kind === 'mcp_stdio'
        ? { command: value.command.trim(), args: (() => { const args = parseJson(value.args || '[]', '参数'); if (!Array.isArray(args) || args.some(item => typeof item !== 'string')) throw new Error('参数必须是字符串数组'); return args; })(), ...(value.kind === 'mcp_stdio' ? { remote_tool_name: value.remoteToolName.trim() } : {}) }
        : { url: value.url.trim(), headers: parseJsonObject(value.headers, 'Headers'), method: value.method, parameter_locations: parseJsonObject(value.parameterLocations, '参数位置'), ...(value.kind === 'mcp' ? { remote_tool_name: value.remoteToolName.trim() } : {}) },
      input_schema: parseJsonObject(value.schema, '输入 Schema'),
      side_effect: value.sideEffect, confirmation_mode: value.confirmationMode,
      rate_limit_per_run: rateLimit,
    };
  }

  async function createTool(value = tool) {
    error = '';
    try {
      const created = await api.createTool(buildToolPayload(value));
      tools = [created, ...tools];
      if (!assigned.has(created.id)) {
        assigned = new Set([...assigned, created.id]);
        toolAssignmentsDirty = true;
        dispatch('dirty', { tools: true });
      }
      tool = { name: '', description: '', kind: 'http', method: 'GET', url: '', headers: '{}', schema: '{"type":"object","properties":{}}', sideEffect: 'read', confirmationMode: 'none', rateLimit: 6, parameterLocations: '{}' };
    } catch (e) { error = e.message || '无法创建工具'; }
  }

  async function importOpenApi() {
    error = ''; candidates = [];
    try {
      const payload = { base_url: openApi.baseUrl.trim() || undefined };
      if (openApi.document.trim()) payload.document = JSON.parse(openApi.document);
      else payload.document_url = openApi.documentUrl.trim();
      const result = await api.importOpenApi(payload);
      candidates = result.candidates || [];
      candidateSource = result.base_url || openApi.baseUrl.trim();
    } catch (e) { error = e instanceof SyntaxError ? 'OpenAPI 文档不是有效 JSON' : (e.message || '无法导入 OpenAPI'); }
  }

  async function discoverMcp() {
    error = ''; candidates = [];
    try {
      const result = await api.discoverMcp({ url: mcp.url.trim(), headers: JSON.parse(mcp.headers || '{}') });
      candidates = (result.candidates || []).map(item => ({ ...item, mcpHeaders: mcp.headers, mcpUrl: mcp.url.trim() }));
      candidateSource = 'MCP';
    } catch (e) { error = e instanceof SyntaxError ? 'MCP Headers 不是有效 JSON' : (e.message || '无法发现 MCP 工具'); }
  }

  function candidateToTool(candidate) {
    const isMcp = candidate.kind === 'mcp';
    const path = candidate.path || '';
    const url = isMcp ? candidate.mcpUrl : `${candidateSource.replace(/\/$/, '')}${path}`;
    tool = {
      name: candidate.name, description: candidate.description || '', kind: isMcp ? 'mcp' : 'http',
      method: candidate.method || 'GET', url, headers: isMcp ? candidate.mcpHeaders : '{}',
      schema: JSON.stringify(candidate.input_schema || { type: 'object', properties: {} }, null, 2),
      // A discovered MCP descriptor has no trustworthy HTTP verb. Require an
      // explicit approval by default until its owner has classified it.
      sideEffect: isMcp ? 'write' : (candidate.side_effect || 'read'),
      confirmationMode: isMcp ? 'per_call' : (candidate.confirmation_mode || 'none'),
      rateLimit: 6, parameterLocations: '{}', remoteToolName: candidate.config?.remote_tool_name || candidate.name,
    };
    candidates = [];
  }

  async function toggleMemory() {
    savingMemory = true; error = '';
    try {
      const updated = await api.updateAgent(agent.id, { memory_enabled: !agent.memory_enabled });
      agent = { ...agent, ...updated };
      dispatch('updated', { agent });
    } catch (e) { error = e.message || '无法更新记忆授权'; }
    finally { savingMemory = false; }
  }

  async function addMemory() {
    error = '';
    try {
      const created = await api.createMemory(agent.id, { content: memory.content.trim(), kind: memory.kind, importance: Number(memory.importance) });
      memories = [created, ...memories];
      memory = { content: '', kind: 'fact', importance: 50 };
    } catch (e) { error = e.message || '无法保存记忆'; }
  }

  async function deleteMemory(id) {
    try { await api.deleteMemory(agent.id, id); memories = memories.filter(item => item.id !== id); }
    catch (e) { error = e.message || '无法删除记忆'; }
  }
</script>

<section class="governance" aria-label="工具和记忆治理">
  <section>
    <div class="heading"><div><h3>工具治理</h3><p>只有分配给此 Agent 的工具才会进入模型请求。</p></div></div>
    {#if loading}<p class="muted">正在加载工具...</p>
    {:else}<div class="tool-list">
      {#each tools as item (item.id)}
        <label class="tool-row"><input type="checkbox" checked={assigned.has(item.id)} on:change={() => toggleTool(item.id)} />
          <span><strong>{item.name}{item.builtin ? ' · 预设' : ''}</strong><small>{item.kind} · {item.side_effect} · {item.confirmation_mode} · 每运行 {item.rate_limit_per_run} 次</small></span>
        </label>
      {:else}<p class="muted">尚未创建工具</p>{/each}
    </div>{/if}
    <div class="tool-save">
      <span class:dirty={toolAssignmentsDirty}>{toolAssignmentsDirty ? '有未保存的工具授权' : '工具授权已保存'}</span>
      <button type="button" on:click={saveToolAssignments} disabled={savingTools}>{savingTools ? '正在保存...' : '保存工具授权'}</button>
    </div>
  </section>

  <section>
    <h3>创建受控工具</h3>
    <div class="grid"><label>名称<input bind:value={tool.name} pattern="[A-Za-z][A-Za-z0-9_]*" /></label><label>说明<input bind:value={tool.description} /></label></div>
    <div class="grid three"><label>类型<select bind:value={tool.kind}><option value="http">HTTP</option><option value="mcp">远程 MCP</option><option value="local">本地命令</option><option value="mcp_stdio">本地 MCP STDIO</option></select></label><label>HTTP 方法<select bind:value={tool.method} disabled={tool.kind === 'local' || tool.kind === 'mcp_stdio'} on:change={normalizePolicy}><option>GET</option><option>HEAD</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option></select></label><label>副作用<select bind:value={tool.sideEffect} on:change={normalizePolicy}><option value="read">read</option><option value="write">write</option><option value="destructive">destructive</option></select></label></div>
    {#if tool.kind === 'local' || tool.kind === 'mcp_stdio'}<div class="grid"><label>命令<input bind:value={tool.command} placeholder="python3" /></label><label>参数（JSON 数组）<input bind:value={tool.args} placeholder='["tool.py"]' /></label></div>{:else}<label>{tool.kind === 'mcp' ? 'MCP 服务 URL' : 'URL'}<input type="url" bind:value={tool.url} placeholder="https://api.example.com/..." /></label>{/if}
    {#if tool.kind === 'mcp' || tool.kind === 'mcp_stdio'}<label>远程工具名<input bind:value={tool.remoteToolName} /></label>{/if}
    <div class="grid three"><label>确认策略<select bind:value={tool.confirmationMode} disabled={tool.sideEffect === 'destructive'}><option value="none">无需确认</option><option value="per_run">每运行确认</option><option value="per_call">每次确认</option></select></label><label>每运行限额<input type="number" min="1" max="100" bind:value={tool.rateLimit} /></label><label>参数位置 JSON<textarea rows="2" bind:value={tool.parameterLocations}></textarea></label></div>
    <div class="grid"><label>Headers JSON<textarea rows="3" bind:value={tool.headers}></textarea></label><label>输入 Schema JSON<textarea rows="3" bind:value={tool.schema}></textarea></label></div>
    <button type="button" on:click={() => createTool()} disabled={!tool.name.trim() || ((tool.kind === 'local' || tool.kind === 'mcp_stdio') ? !tool.command.trim() : !tool.url.trim())}>创建并加入授权草稿</button>
  </section>

  <section>
    <h3>候选导入</h3>
    <div class="grid"><label>OpenAPI 文档 URL<input type="url" bind:value={openApi.documentUrl} /></label><label>基础 URL（可选）<input type="url" bind:value={openApi.baseUrl} /></label></div>
    <label>或粘贴 OpenAPI JSON<textarea rows="3" bind:value={openApi.document}></textarea></label><button type="button" class="secondary" on:click={importOpenApi} disabled={!openApi.documentUrl.trim() && !openApi.document.trim()}>导入 OpenAPI 候选</button>
    <div class="grid"><label>MCP 服务 URL<input type="url" bind:value={mcp.url} /></label><label>MCP Headers JSON<textarea rows="2" bind:value={mcp.headers}></textarea></label></div><button type="button" class="secondary" on:click={discoverMcp} disabled={!mcp.url.trim()}>发现远程 MCP</button>
    {#if candidates.length}<div class="candidates">{#each candidates as candidate, index (`${candidate.name}:${index}`)}<article><div><strong>{candidate.name}</strong><p>{candidate.description}</p><small>{candidate.method || 'MCP'} · 默认 {candidate.side_effect}</small></div><button type="button" on:click={() => candidateToTool(candidate)}>审阅</button></article>{/each}</div>{/if}
  </section>

  <section>
    <div class="heading"><div><h3>长期记忆</h3><p>仅保存明确创建的事实；冲突不会自动覆盖。</p></div><button type="button" class:enabled={agent.memory_enabled} on:click={toggleMemory} disabled={savingMemory}>{agent.memory_enabled ? '已启用' : '默认关闭'}</button></div>
    {#if agent.memory_enabled}<div class="grid three"><label>类型<select bind:value={memory.kind}><option value="fact">事实</option><option value="preference">偏好</option><option value="profile">档案</option><option value="constraint">约束</option><option value="experience">经验</option></select></label><label>重要度<input type="number" min="0" max="100" bind:value={memory.importance} /></label><label>记忆内容<input bind:value={memory.content} /></label></div><button type="button" on:click={addMemory} disabled={!memory.content.trim()}>保存记忆</button>{/if}
    <div class="memory-list">{#each memories as item (item.id)}<article class:conflict={item.conflict_state === 'conflicted'}><div><strong>{item.kind}</strong><p>{item.content}</p><small>{item.conflict_state} · 重要度 {item.importance} · 使用 {item.access_count} 次</small></div><button type="button" class="danger" on:click={() => deleteMemory(item.id)}>删除</button></article>{:else}<p class="muted">没有已保存记忆</p>{/each}</div>
  </section>
  {#if error}<p class="error" role="status">{error}</p>{/if}
</section>

<style>
  .governance section { padding:20px 0; border-top:1px solid var(--color-border); }.heading { display:flex; justify-content:space-between; gap:16px; align-items:start; } h3 { margin:0; font-size:14px; } p { margin:5px 0 0; color:var(--color-text-muted); font-size:12px; }.grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:12px; }.grid.three { grid-template-columns:repeat(3,minmax(0,1fr)); } label { display:grid; gap:6px; color:var(--color-text-muted); font-size:12px; font-weight:600; } input,select,textarea { width:100%; min-width:0; padding:9px 10px; border:1px solid var(--color-border); border-radius:5px; background:var(--color-input); color:var(--color-text); font:inherit; font-size:13px; outline:none; resize:vertical; } input:focus,select:focus,textarea:focus { border-color:var(--color-primary); } button { min-height:34px; margin-top:12px; padding:0 12px; border:1px solid var(--color-primary); border-radius:4px; background:var(--color-primary); color:#fff; cursor:pointer; font-size:12px; font-weight:700; } button.secondary { border-color:var(--color-border); background:transparent; color:var(--color-text-muted); }.heading button { margin:0; }.heading button.enabled { border-color:var(--color-online); color:var(--color-online); background:transparent; }.tool-list,.memory-list,.candidates { margin-top:12px; border:1px solid var(--color-border); border-radius:5px; }.tool-row,.memory-list article,.candidates article { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:10px; }.tool-row + .tool-row,.memory-list article + article,.candidates article + article { border-top:1px solid var(--color-border); }.tool-row { justify-content:start; }.tool-row input { width:16px; }.tool-row strong,.tool-row small,.memory-list strong,.memory-list small,.candidates small { display:block; }.tool-row small,.memory-list small,.candidates small,.muted { color:var(--color-text-muted); font-size:11px; }.tool-save { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:12px; }.tool-save span { color:var(--color-text-muted); font-size:12px; }.tool-save span.dirty { color:var(--color-warning); }.tool-save button { margin:0; }.memory-list article.conflict { border-left:3px solid var(--color-error); }.memory-list article p,.candidates article p { overflow-wrap:anywhere; }.memory-list button,.candidates button { flex:0 0 auto; margin:0; }.danger { border-color:var(--color-error); background:transparent; color:var(--color-error); }.error { color:var(--color-error); } button:disabled { opacity:.55; cursor:default; } @media(max-width:650px){.grid,.grid.three{grid-template-columns:1fr}.heading{align-items:center}.tool-save{align-items:start;flex-direction:column}.candidates article{align-items:start}}
</style>
