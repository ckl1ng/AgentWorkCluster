import { StreamSanitizer } from './sanitizer.js';

function endpoint(baseUrl) {
  const value = baseUrl.replace(/\/+$/, '');
  return value.endsWith('/chat/completions') ? value : `${value}/chat/completions`;
}

export async function executeTextRun({ run, workspace, model, signal, emit }) {
  await emit('agent.run.started', { summary: '开始调用本机配置的模型' });
  const response = await fetch(endpoint(model.base_url), {
    method: 'POST', signal,
    headers: { Authorization: `Bearer ${model.api_key}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: model.model_id, stream: true, messages: run.messages || [
      { role: 'system', content: run.system_prompt || 'You are a helpful local coding assistant. Do not request or reveal credentials.' },
      { role: 'user', content: run.prompt },
    ] }),
  });
  if (!response.ok || !response.body) throw new Error(`model request failed: HTTP ${response.status}`);
  const sanitizer = new StreamSanitizer({ workspaceRoot: workspace.root }); let buffer = ''; let final = ''; let sanitized = '';
  for await (const chunk of response.body) {
    buffer += Buffer.from(chunk).toString('utf8'); let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).trim(); buffer = buffer.slice(index + 1);
      if (!line.startsWith('data:')) continue;
      const data = line.slice(5).trim(); if (data === '[DONE]') continue;
      try {
        const content = JSON.parse(data).choices?.[0]?.delta?.content;
        if (!content) continue;
        final += String(content); const safe = sanitizer.push(content);
        if (safe.value) { sanitized += safe.value; await emit('agent.message.delta', { content: safe.value, redaction_count: safe.redactionCount }); }
      } catch { /* Ignore malformed model stream chunks. */ }
    }
  }
  const remainder = sanitizer.flush(); if (remainder.value) { sanitized += remainder.value; await emit('agent.message.delta', { content: remainder.value, redaction_count: remainder.redactionCount }); }
  return { content: final, sanitized_content: sanitized };
}
