<script>
  import { createEventDispatcher } from 'svelte';
  import { api } from '../lib/api.js';

  const dispatch = createEventDispatcher();
  let saving = false;
  let error = '';
  let form = {
    name: '',
    description: '',
    model_display_name: 'OpenAI compatible',
    base_url: 'https://api.openai.com/v1',
    api_key: '',
    model_id: '',
    temperature: 0.4,
    max_tokens: 2048,
    timeout_seconds: 60,
    system_prompt: 'You are a helpful assistant.',
    max_concurrent_runs: 2,
    max_tool_calls: 6,
    context_window: 32768,
  };

  async function save() {
    error = '';
    saving = true;
    try {
      const agent = await api.createAgent({
        ...form, name: form.name.trim(), description: form.description.trim(),
        run_policy: {
          max_concurrent_runs: Number(form.max_concurrent_runs), max_tool_calls: Number(form.max_tool_calls),
          context_window: Number(form.context_window), daily_token_budget: 0, monthly_token_budget: 0,
        },
      });
      dispatch('created', { agent });
    } catch (e) {
      error = e.message || '无法创建 Agent';
    } finally {
      saving = false;
    }
  }
</script>

<section class="workspace" aria-label="创建 Agent">
  <div class="heading">
    <div><p class="kicker">AGENT</p><h2>创建个人 Agent</h2></div>
    <button class="close" type="button" title="返回会话" aria-label="返回会话" on:click={() => dispatch('cancel')}>×</button>
  </div>

  <form on:submit|preventDefault={save}>
    <section class="form-section">
      <h3>基础信息</h3>
      <label>名称<input required maxlength="80" bind:value={form.name} placeholder="例如：研究助手" /></label>
      <label>用途<textarea rows="2" maxlength="280" bind:value={form.description} placeholder="它应该帮助你完成什么？"></textarea></label>
    </section>

    <section class="form-section">
      <h3>模型连接</h3>
      <div class="two-col">
        <label>连接名称<input required maxlength="80" bind:value={form.model_display_name} /></label>
        <label>模型 ID<input required maxlength="160" bind:value={form.model_id} placeholder="gpt-4.1-mini" /></label>
      </div>
      <label>Base URL<input required type="url" bind:value={form.base_url} placeholder="https://api.example.com/v1" /></label>
      <label>API Key<input required type="password" autocomplete="off" bind:value={form.api_key} placeholder="仅加密保存在服务端" /></label>
      <div class="three-col">
        <label>温度<input type="number" min="0" max="2" step="0.1" bind:value={form.temperature} /></label>
        <label>最大输出 Token<input type="number" min="1" max="32768" step="1" bind:value={form.max_tokens} /></label>
        <label>超时（秒）<input type="number" min="5" max="300" step="1" bind:value={form.timeout_seconds} /></label>
      </div>
    </section>

    <section class="form-section">
      <h3>角色提示词</h3>
      <label>系统提示词<textarea required rows="8" maxlength="32000" bind:value={form.system_prompt}></textarea></label>
      <div class="three-col">
        <label>上下文窗口<input type="number" min="2048" bind:value={form.context_window} /></label>
        <label>并发运行<input type="number" min="1" max="10" bind:value={form.max_concurrent_runs} /></label>
        <label>工具调用上限<input type="number" min="0" max="20" bind:value={form.max_tool_calls} /></label>
      </div>
    </section>

    <p class="notice">保存后即可运行。输入会发送到此 Agent 配置的模型服务；API Key 不会返回到浏览器。</p>
    {#if error}<p class="error" role="status">{error}</p>{/if}
    <div class="actions"><button type="button" class="secondary" on:click={() => dispatch('cancel')}>取消</button><button type="submit" disabled={saving}>{saving ? '正在保存...' : '保存并运行'}</button></div>
  </form>
</section>

<style>
  .workspace { flex: 1; overflow-y: auto; padding: 28px max(20px, 7vw) 48px; }
  .heading { display: flex; align-items: flex-start; justify-content: space-between; max-width: 760px; margin-bottom: 24px; }
  .kicker { color: var(--color-primary); font-size: 11px; font-weight: 700; letter-spacing: 1px; }
  h2 { margin-top: 4px; font-size: 24px; } h3 { margin-bottom: 16px; font-size: 15px; }
  form { max-width: 760px; } .form-section { padding: 20px 0; border-top: 1px solid var(--color-border); }
  label { display: grid; gap: 7px; margin-top: 13px; color: var(--color-text-muted); font-size: 12px; font-weight: 600; }
  input, textarea { width: 100%; border: 1px solid var(--color-border); border-radius: 5px; background: var(--color-input); color: var(--color-text); font: inherit; font-size: 14px; font-weight: 400; outline: none; padding: 10px 11px; resize: vertical; }
  input:focus, textarea:focus { border-color: var(--color-primary); }
  .two-col, .three-col { display: grid; gap: 12px; } .two-col { grid-template-columns: 1fr 1fr; } .three-col { grid-template-columns: repeat(3, 1fr); }
  .notice { margin-top: 4px; padding: 10px 12px; border-left: 3px solid var(--color-primary); background: var(--color-active); color: var(--color-text-muted); font-size: 12px; line-height: 1.6; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; } button { min-height: 36px; padding: 0 14px; border: 1px solid var(--color-primary); border-radius: 4px; background: var(--color-primary); color: #fff; cursor: pointer; font-size: 13px; font-weight: 700; } button:disabled { opacity: .65; cursor: wait; } .secondary, .close { border-color: var(--color-border); background: transparent; color: var(--color-text-muted); } .close { display: grid; width: 36px; height: 36px; place-items: center; padding: 0; font-size: 24px; font-weight: 400; }
  .error { margin-top: 12px; color: var(--color-error); font-size: 13px; }
  @media (max-width: 600px) { .workspace { padding: 22px 16px 36px; } .two-col, .three-col { grid-template-columns: 1fr; } }
</style>
