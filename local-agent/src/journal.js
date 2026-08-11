import fs from 'node:fs/promises';
import path from 'node:path';
import { assertPrivateFile } from './permissions.js';

export class Journal {
  constructor(file) { this.file = file; this.sequence = new Map(); }
  async init() {
    await fs.mkdir(path.dirname(this.file), { recursive: true, mode: 0o700 });
    await assertPrivateFile(this.file);
    try {
      const lines = (await fs.readFile(this.file, 'utf8')).split('\n').filter(Boolean);
      for (const line of lines) { const row = JSON.parse(line); if (row.run_id) this.sequence.set(row.run_id, Math.max(this.sequence.get(row.run_id) || 0, row.sequence || 0)); }
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    return this;
  }
  async append(row) {
    const sequence = row.sequence ?? ((this.sequence.get(row.run_id) || 0) + 1);
    const event = { ...row, sequence, event_id: row.event_id || `evt_${crypto.randomUUID()}`, created_at: row.created_at || new Date().toISOString() };
    await fs.appendFile(this.file, `${JSON.stringify(event)}\n`, { mode: 0o600 });
    if (event.run_id) this.sequence.set(event.run_id, sequence);
    return event;
  }
  lastSequence(runId) { return this.sequence.get(runId) || 0; }
  async events(runId, after = 0) {
    try {
      const lines = (await fs.readFile(this.file, 'utf8')).split('\n').filter(Boolean);
      return lines.map((line) => JSON.parse(line)).filter((row) => row.run_id === runId && row.sequence > after);
    } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  }
  async runs() {
    const runs = new Map();
    try {
      const lines = (await fs.readFile(this.file, 'utf8')).split('\n').filter(Boolean);
      for (const line of lines) {
        const event = JSON.parse(line); const run = runs.get(event.run_id);
        if (event.type === 'run.created') {
          runs.set(event.run_id, {
            run_id: event.run_id, workspace_id: event.payload.workspace_id, origin: event.payload.origin,
            agent_id: event.payload.agent_id || null, sync_mode: event.payload.sync_mode, prompt: event.payload.prompt || '', state: 'queued',
            created_at: event.created_at, last_sequence: event.sequence,
          });
        } else if (run) {
          run.last_sequence = event.sequence;
          if (event.type === 'run.cancelled') run.state = 'cancelled';
          if (event.type === 'agent.run.completed') run.state = 'completed';
          if (event.type === 'agent.run.failed') run.state = 'failed';
        }
      }
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    return [...runs.values()];
  }
}
