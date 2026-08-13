import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { IpcServer } from './ipc-server.js';
import { Journal } from './journal.js';
import { WorkspaceRegistry, resolveWorkspacePath } from './workspace.js';
import { assertPrivateFile, ensurePrivateDirectory } from './permissions.js';
import { LocalModelStore } from './model-store.js';
import { loadCredential } from './registry-client.js';
import { executeTextRun } from './harness.js';
import { LocalAgentTransport } from './transport.js';
import crypto from 'node:crypto';

export function defaultPaths(dataDir = path.join(os.homedir(), '.local-agent')) {
  return { dataDir, lock: path.join(dataDir, 'daemon.lock'), socket: path.join(dataDir, 'daemon.sock'), journal: path.join(dataDir, 'journal.jsonl'), workspaces: path.join(dataDir, 'workspaces.json'), models: path.join(dataDir, 'models.json'), secret: path.join(dataDir, 'state.key') };
}

async function loadOrCreateLocalSecret(file) {
  try {
    await assertPrivateFile(file);
    const value = await fs.readFile(file, 'utf8');
    if (!value.trim()) throw new Error('local state key is empty');
    return value.trim();
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    const value = crypto.randomBytes(32).toString('base64url');
    await fs.writeFile(file, `${value}\n`, { mode: 0o600 });
    return value;
  }
}

export class LocalAgentDaemon {
  constructor(paths = defaultPaths()) { this.paths = paths; this.lockHandle = null; this.ipc = null; this.localSecret = null; this.journal = new Journal(paths.journal); this.workspaces = new WorkspaceRegistry(paths.workspaces); this.models = new LocalModelStore(paths.models); this.runs = new Map(); this.controllers = new Map(); this.pendingOffers = new Map(); this.transport = new LocalAgentTransport(paths.dataDir, { offer: (offer) => this.offerRemoteRun(offer), claimed: (claim) => this.claimRemoteRun(claim), cancel: (runId) => this.cancelRemoteRun(runId) }); }
  async start() {
    await ensurePrivateDirectory(this.paths.dataDir);
    try { this.lockHandle = await fs.open(this.paths.lock, 'wx', 0o600); await this.lockHandle.writeFile(`${process.pid}\n`); } catch { throw new Error('local-agent daemon is already running'); }
    try {
      this.localSecret = await loadOrCreateLocalSecret(this.paths.secret); await this.journal.init(); await this.workspaces.load();
      this.runs = new Map((await this.journal.runs()).map((run) => [run.run_id, run]));
      this.ipc = new IpcServer(this.paths.socket, (method, params) => this.handle(method, params)); await this.ipc.listen();
      void this.transport.start();
      return this;
    } catch (error) {
      await this.ipc?.close(); await this.lockHandle?.close(); await fs.unlink(this.paths.lock).catch(() => {}); this.lockHandle = null;
      throw error;
    }
  }
  async stop() { for (const controller of this.controllers.values()) controller.abort(); await this.transport.stop(); await this.ipc?.close(); await this.lockHandle?.close(); await fs.unlink(this.paths.lock).catch(() => {}); this.lockHandle = null; }
  async handle(method, params) {
    if (method === 'daemon.status') return { protocol_version: 1, pid: process.pid, platform: process.platform, version: '0.1.0', connected: Boolean(this.transport.socket), workspaces: this.workspaces.list(), models: await this.models.list(), runs: [...this.runs.values()] };
    if (method === 'workspace.list') return this.workspaces.list();
    if (method === 'workspace.add') return this.workspaces.add(params.path, params.name);
    if (method === 'workspace.set-remote-id') return this.workspaces.setRemoteId(params.workspace_id, params.remote_id);
    if (method === 'workspace.remove') return this.workspaces.remove(params.workspace_id);
    if (method === 'model.list') return this.models.list();
    if (method === 'model.configure') {
      return this.models.set(params.agent_id, { base_url: params.base_url, model_id: params.model_id, api_key: params.api_key }, this.localSecret);
    }
    if (method === 'model.remove') return { removed: await this.models.remove(params.agent_id) };
    if (method === 'run.list') return [...this.runs.values()];
    if (method === 'run.create') {
      const workspace = this.workspaces.get(params.workspace_id); if (!workspace) throw new Error('workspace not found');
      const run = { run_id: `run_${crypto.randomUUID()}`, workspace_id: workspace.id, agent_id: params.agent_id || null, origin: params.origin || 'terminal', sync_mode: params.sync || 'full', prompt: String(params.prompt || ''), state: 'queued', created_at: new Date().toISOString(), last_sequence: 0 };
      this.runs.set(run.run_id, run);
      const event = await this.journal.append({ run_id: run.run_id, type: 'run.created', payload: { workspace_id: run.workspace_id, agent_id: run.agent_id, origin: run.origin, sync_mode: run.sync_mode, prompt: run.prompt } });
      run.last_sequence = event.sequence;
      if (run.agent_id) void this.executeRun(run, workspace);
      return run;
    }
    if (method === 'run.cancel') { const run = this.runs.get(params.run_id); if (!run) throw new Error('run not found'); this.controllers.get(run.run_id)?.abort(); run.state = 'cancelled'; const event = await this.journal.append({ run_id: run.run_id, type: 'run.cancelled', payload: {} }); run.last_sequence = event.sequence; return run; }
    if (method === 'run.events') return this.journal.events(params.run_id, Number(params.after_sequence || 0));
    throw Object.assign(new Error(`unknown method: ${method}`), { code: -32601 });
  }
  async executeRun(run, workspace, onEvent = null) {
    const emit = async (type, payload) => { const event = await this.journal.append({ run_id: run.run_id, type, payload }); run.last_sequence = event.sequence; if (onEvent) await onEvent(event); };
    const controller = new AbortController(); this.controllers.set(run.run_id, controller); run.state = 'running';
    try {
      let model = null;
      try { model = await this.models.get(run.agent_id, this.localSecret); } catch (error) {
        const credential = await loadCredential(this.paths.dataDir);
        if (credential?.refresh_token) model = await this.models.get(run.agent_id, credential.refresh_token);
        else throw error;
      }
      if (!model) throw new Error('no local model/profile is configured for this run');
      const result = await executeTextRun({ run, workspace, model, signal: controller.signal, emit }); run.result = result.content; run.sanitized_result = result.sanitized_content;
      if (run.state !== 'cancelled') { run.state = 'completed'; await emit('agent.run.completed', { summary: '模型响应已完成' }); }
    } catch (error) {
      if (run.state !== 'cancelled') { run.state = 'failed'; run.error = error.name === 'AbortError' ? 'run cancelled' : error.message; await emit('agent.run.failed', { summary: run.error }); }
    } finally { this.controllers.delete(run.run_id); }
    return run;
  }
  async offerRemoteRun(offer) { this.pendingOffers.set(offer.run_id, offer); this.transport.send('run.claim', { run_id: offer.run_id, lease_id: offer.lease_id, local_session_id: `sess_${crypto.randomUUID()}` }); }
  async claimRemoteRun(claim) {
    const offer = this.pendingOffers.get(claim.run_id); this.pendingOffers.delete(claim.run_id); if (!offer || !claim.claimed) return;
    const workspace = this.workspaces.getByRemoteId(offer.workspace_id);
    if (!workspace) { this.transport.send('run.finish', { run_id: offer.run_id, lease_id: offer.lease_id, state: 'failed', error: 'workspace is not registered on this daemon' }); return; }
    const run = { run_id: offer.run_id, lease_id: offer.lease_id, workspace_id: workspace.id, agent_id: offer.profile || offer.agent_id, origin: 'web', sync_mode: 'full', prompt: offer.messages?.at(-1)?.content || '', messages: [{ role: 'system', content: offer.system_prompt }, ...(offer.messages || [])], state: 'running', created_at: new Date().toISOString(), last_sequence: 0 };
    this.runs.set(run.run_id, run); this.transport.trackLease(run.run_id, run.lease_id);
    const finished = await this.executeRun(run, workspace, (event) => this.transport.send('run.event', { run_id: run.run_id, lease_id: run.lease_id, sequence: event.sequence, event_type: event.type, payload: event.payload }));
    this.transport.send('run.finish', { run_id: run.run_id, lease_id: run.lease_id, state: finished.state, content: finished.sanitized_result || '', error: finished.error || '' }); this.transport.untrackLease(run.run_id);
  }
  async cancelRemoteRun(runId) { const run = this.runs.get(runId); if (run) await this.handle('run.cancel', { run_id: runId }); }
}

export async function ensureWorkspacePath(workspaceRoot, input, options) { return resolveWorkspacePath(workspaceRoot, input, options); }
