import { spawn } from 'node:child_process';
import { StreamSanitizer } from './sanitizer.js';
import { executeTextRun } from './harness.js';

/**
 * Executor abstraction for local runs.
 *
 * A run may be executed either by the built-in local text-model harness
 * ('model', the only executor before external agents were supported) or by an
 * external CLI agent such as OpenAI Codex ('codex'). The server, snapshot and
 * Web UI never need to know which executor ran a run: they only observe the
 * monotonic trace-event sequence and the terminal state, both produced through
 * the emit(type, payload) convention shared by every executor below.
 *
 * Executor event contract (implemented via the emit hook):
 *   - emit('agent.run.started',   { summary })                    before starting work
 *   - emit('agent.message.delta', { content, redaction_count })   for each sanitized text increment
 *   - emit('agent.run.completed', { summary })                    on success
 *   - emit('agent.run.failed',    { summary })                    on failure
 *
 * Executors must never write to the WebSocket or journal directly. Everything
 * observable goes through emit, which keeps them pure and unit-testable.
 */

export async function executeExecutorRun({ run, workspace, model, signal, emit, codex }) {
  const executor = run.executor || run.executor_kind || 'model';
  if (executor === 'codex') {
    return executeCodexRun({ run, workspace, signal, emit, codex });
  }
  if (executor === 'model') {
    return executeTextRun({ run, workspace, model, signal, emit });
  }
  throw new Error('unknown executor: ' + executor);
}

/**
 * Execute a run with OpenAI Codex (external CLI agent).
 *
 * Codex is a self-contained agent loop with its own tools. This platform treats
 * it as a black-box executor bound to a registered workspace: Codex's internal
 * tool calls are NOT re-executed by, or visible to, the platform governance
 * pipeline. Only sanitized text/reasoning increments are surfaced as trace
 * events, and the process runs with the workspace root as its cwd.
 */
export async function executeCodexRun({ run, workspace, signal, emit, codex }) {
  await emit('agent.run.started', { summary: '启动外部 Code agent（Codex）' });
  const adapter = normalizeCodexAdapter(codex);
  const prompt = String(run.prompt || run.messages?.at(-1)?.content || '').trim();

  return new Promise((resolve) => {
    const child = spawn(adapter.bin, adapter.args(prompt), {
      cwd: workspace.root,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    // Keep stdin closed so Codex can never pause waiting for an interactive reply.
    child.stdin.end();

    const sanitizer = new StreamSanitizer({ workspaceRoot: workspace.root, homeDir: process.env.HOME });
    let final = '';
    let sanitized = '';
    let failed = null;

    const controller = signal || new AbortController().signal;
    const onAbort = () => { try { child.kill('SIGTERM'); } catch { /* ignore */ } };
    if (controller.aborted) onAbort(); else controller.addEventListener('abort', onAbort, { once: true });

    child.stdout.on('data', (chunk) => {
      const text = chunk.toString('utf8');
      final += text;
      const data = sanitizer.push(text);
      if (data.value) sanitized += data.value;
      void emit('agent.message.delta', { content: data.value, redaction_count: data.redactionCount });
    });
    child.stderr.on('data', () => { /* Codex logs to stderr; not surfaced to avoid noise/secrets */ });
    child.on('error', (error) => { failed = error.message || 'codex failed to start'; });
    child.on('close', (code) => void finish(code));

    const finish = async (code) => {
      if (controller.aborted) failed = 'run cancelled';
      if (signal) signal.removeEventListener('abort', onAbort);
      const remainder = sanitizer.flush();
      if (remainder.value) { sanitized += remainder.value; void emit('agent.message.delta', { content: remainder.value, redaction_count: remainder.redactionCount }); }
      if (failed) {
        await emit('agent.run.failed', { summary: failed });
        resolve({ content: final, sanitized_content: sanitized, error: failed, exit_code: code });
        return;
      }
      if (code !== 0 && !sanitized.trim()) {
        const message = 'Codex exited with code ' + code + ' and produced no output';
        await emit('agent.run.failed', { summary: message });
        resolve({ content: final, sanitized_content: sanitized, error: message, exit_code: code });
        return;
      }
      await emit('agent.run.completed', { summary: sanitized.trim() ? '外部 Code agent 已完成' : 'Codex finished (exit ' + code + ') without visible output' });
      resolve({ content: final, sanitized_content: sanitized, exit_code: code });
    };
  });
}

function normalizeCodexAdapter(codex) {
  const adapter = codex || {};
  const bin = adapter.bin || 'codex';
  const buildArgs = adapter.buildArgs || ((prompt) => ['exec', '-C', '.', '--json', '--skip-git-repo-check', prompt]);
  return { bin, args: (prompt) => buildArgs(prompt || '') };
}

export { executeTextRun };
