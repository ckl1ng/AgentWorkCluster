import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { assertPrivateFile } from './permissions.js';

function validate(agentId, config) {
  if (typeof agentId !== 'string' || !agentId || agentId.length > 128) throw new Error('invalid agent id');
  if (!config || typeof config.base_url !== 'string' || !/^https?:\/\//.test(config.base_url) || config.base_url.length > 1024) throw new Error('invalid model base URL');
  if (typeof config.model_id !== 'string' || !config.model_id || config.model_id.length > 160) throw new Error('invalid model id');
  if (typeof config.api_key !== 'string' || !config.api_key || config.api_key.length > 4096) throw new Error('invalid model API key');
}

export class LocalModelStore {
  constructor(file) { this.file = file; }

  async read() {
    await assertPrivateFile(this.file);
    try {
      const value = JSON.parse(await fs.readFile(this.file, 'utf8'));
      if (value.version !== 1 || typeof value.salt !== 'string' || typeof value.models !== 'object' || !value.models) throw new Error('invalid local model store');
      return value;
    } catch (error) {
      if (error.code === 'ENOENT') return { version: 1, salt: crypto.randomBytes(16).toString('base64'), models: {} };
      throw error;
    }
  }

  async write(value) {
    const temporary = `${this.file}.${process.pid}.tmp`;
    await fs.mkdir(path.dirname(this.file), { recursive: true, mode: 0o700 });
    await fs.writeFile(temporary, `${JSON.stringify(value)}\n`, { mode: 0o600 });
    await fs.rename(temporary, this.file);
  }

  key(secret, salt) { return crypto.scryptSync(secret, Buffer.from(salt, 'base64'), 32); }

  async set(agentId, config, deviceSecret) {
    validate(agentId, config);
    if (typeof deviceSecret !== 'string' || !deviceSecret) throw new Error('device credential is required for local model storage');
    const value = await this.read(); const key = this.key(deviceSecret, value.salt); const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
    const ciphertext = Buffer.concat([cipher.update(JSON.stringify(config), 'utf8'), cipher.final()]);
    value.models[agentId] = { iv: iv.toString('base64'), tag: cipher.getAuthTag().toString('base64'), ciphertext: ciphertext.toString('base64'), updated_at: new Date().toISOString() };
    await this.write(value);
    return { agent_id: agentId, base_url: config.base_url, model_id: config.model_id, updated_at: value.models[agentId].updated_at };
  }

  async get(agentId, deviceSecret) {
    const value = await this.read(); const entry = value.models[agentId];
    if (!entry) return null;
    try {
      const decipher = crypto.createDecipheriv('aes-256-gcm', this.key(deviceSecret, value.salt), Buffer.from(entry.iv, 'base64'));
      decipher.setAuthTag(Buffer.from(entry.tag, 'base64'));
      return JSON.parse(Buffer.concat([decipher.update(Buffer.from(entry.ciphertext, 'base64')), decipher.final()]).toString('utf8'));
    } catch { throw new Error('unable to decrypt local model credential'); }
  }

  async list() {
    const value = await this.read();
    return Object.entries(value.models).map(([agent_id, entry]) => ({ agent_id, configured: true, updated_at: entry.updated_at }));
  }

  async remove(agentId) {
    const value = await this.read();
    if (!value.models[agentId]) return false;
    delete value.models[agentId]; await this.write(value); return true;
  }
}
