import WebSocket from 'ws';
import { loadCredential, refreshAccessToken } from './registry-client.js';
import { envelope } from './protocol.js';

function websocketUrl(apiUrl) {
  const url = new URL('/local-agent/ws', apiUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

export class LocalAgentTransport {
  constructor(dataDir, handlers) { this.dataDir = dataDir; this.handlers = handlers; this.socket = null; this.retry = null; this.heartbeat = null; this.stopped = false; this.leases = new Map(); }
  async start() { this.stopped = false; await this.connect(); }
  async connect() {
    if (this.stopped) return;
    try {
      const credential = await loadCredential(this.dataDir); if (!credential) { this.scheduleReconnect(); return; }
      const access = await refreshAccessToken(credential.api_url, credential.refresh_token);
      const socket = this.socket = new WebSocket(websocketUrl(credential.api_url), { headers: { Authorization: `Bearer ${access.access_token}` } });
      socket.on('open', () => { this.send('hello', { device_id: credential.device_id }); this.heartbeat = setInterval(() => this.renewLeases(), 15_000); });
      socket.on('message', (data) => this.receive(data.toString()));
      socket.on('error', () => {});
      socket.on('close', () => { if (this.socket === socket) this.socket = null; clearInterval(this.heartbeat); this.heartbeat = null; this.scheduleReconnect(); });
    } catch { this.scheduleReconnect(); }
  }
  scheduleReconnect() { if (!this.stopped && !this.retry) this.retry = setTimeout(() => { this.retry = null; void this.connect(); }, 2_000); }
  send(type, payload) { if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(envelope(type, { payload }))); }
  trackLease(runId, leaseId) { this.leases.set(runId, leaseId); }
  untrackLease(runId) { this.leases.delete(runId); }
  renewLeases() { for (const [run_id, lease_id] of this.leases) this.send('lease.renew', { run_id, lease_id }); }
  receive(raw) {
    let message; try { message = JSON.parse(raw); } catch { return; }
    const payload = message.payload || {};
    if (message.type === 'run.offer') void this.handlers.offer(payload);
    if (message.type === 'run.claimed') void this.handlers.claimed(payload);
    if (message.type === 'lease.ack' && payload.cancelled) void this.handlers.cancel(payload.run_id);
  }
  async stop() { this.stopped = true; clearTimeout(this.retry); clearInterval(this.heartbeat); this.retry = null; this.heartbeat = null; this.socket?.close(); this.socket = null; this.leases.clear(); }
}
