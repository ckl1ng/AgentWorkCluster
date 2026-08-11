import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { assertPrivateFile } from './permissions.js';

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

export async function resolveWorkspacePath(workspaceRoot, input, { allowMissing = false } = {}) {
  if (typeof input !== 'string' || !input || input.includes('\0')) throw new Error('invalid workspace path');
  const root = await fs.realpath(workspaceRoot);
  const absolute = path.resolve(root, input);
  let candidate;
  try {
    candidate = await fs.realpath(absolute);
  } catch (error) {
    if (!allowMissing || error.code !== 'ENOENT') throw new Error('workspace path does not exist');
    const parent = await fs.realpath(path.dirname(absolute));
    candidate = path.join(parent, path.basename(absolute));
  }
  if (!isInside(root, candidate)) throw new Error('path escapes workspace');
  const stat = await fs.lstat(candidate).catch(() => null);
  if (stat?.isBlockDevice() || stat?.isCharacterDevice() || stat?.isSocket() || stat?.isFIFO()) throw new Error('special files are not allowed');
  return candidate;
}

export class WorkspaceRegistry {
  constructor(file) { this.file = file; this.items = new Map(); this.locks = new Map(); }
  async load() {
    await assertPrivateFile(this.file);
    try {
      const data = JSON.parse(await fs.readFile(this.file, 'utf8'));
      for (const item of data) this.items.set(item.id, item);
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    return this;
  }
  async save() {
    await fs.mkdir(path.dirname(this.file), { recursive: true, mode: 0o700 });
    const tmp = `${this.file}.${process.pid}.tmp`;
    await fs.writeFile(tmp, `${JSON.stringify([...this.items.values()], null, 2)}\n`, { mode: 0o600 });
    await fs.rename(tmp, this.file);
  }
  async add(input, name = path.basename(path.resolve(input))) {
    const root = await fs.realpath(input);
    const stat = await fs.stat(root);
    if (!stat.isDirectory()) throw new Error('workspace must be a directory');
    const item = { id: `ws_${randomUUID()}`, name: String(name).slice(0, 120), root, policy_version: 1, created_at: new Date().toISOString() };
    this.items.set(item.id, item); await this.save(); return item;
  }
  async remove(id) { if (!this.items.delete(id)) return false; await this.save(); return true; }
  async setRemoteId(id, remoteId) {
    const item = this.items.get(id);
    if (!item) throw new Error('workspace not found');
    item.remote_id = remoteId;
    await this.save();
    return item;
  }
  list() { return [...this.items.values()].map(({ root, ...safe }) => safe); }
  get(id) { return this.items.get(id); }
  getByRemoteId(remoteId) { return [...this.items.values()].find((item) => item.remote_id === remoteId); }
  async lock(id, mode = 'write') {
    const current = this.locks.get(id);
    if (current) throw new Error(`workspace lock is held (${current.mode})`);
    const lock = { id: randomUUID(), mode, release: () => this.locks.delete(id) };
    this.locks.set(id, lock); return lock;
  }
}
