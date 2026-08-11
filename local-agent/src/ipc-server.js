import net from 'node:net';
import fs from 'node:fs/promises';
import { jsonRpcError, jsonRpcResult } from './protocol.js';

export class IpcServer {
  constructor(socketPath, handler) { this.socketPath = socketPath; this.handler = handler; this.server = null; this.connections = new Set(); }
  async listen() {
    await fs.mkdir(new URL('.', `file://${this.socketPath}`).pathname, { recursive: true, mode: 0o700 }).catch(() => {});
    await fs.unlink(this.socketPath).catch((error) => { if (error.code !== 'ENOENT') throw error; });
    this.server = net.createServer((socket) => {
      this.connections.add(socket);
      socket.once('close', () => this.connections.delete(socket));
      socket.setEncoding('utf8'); let buffer = '';
      socket.on('data', (chunk) => {
        buffer += chunk;
        let index;
        while ((index = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, index); buffer = buffer.slice(index + 1); this.dispatch(socket, line);
        }
      });
    });
    await new Promise((resolve, reject) => { this.server.once('error', reject); this.server.listen(this.socketPath, () => { this.server.off('error', reject); resolve(); }); });
    await fs.chmod(this.socketPath, 0o600);
  }
  async dispatch(socket, line) {
    let request;
    try { request = JSON.parse(line); } catch { socket.write(`${JSON.stringify(jsonRpcError(null, -32700, 'invalid JSON'))}\n`); return; }
    try {
      if (request.jsonrpc !== '2.0' || typeof request.method !== 'string') throw Object.assign(new Error('invalid JSON-RPC request'), { code: -32600 });
      const result = await this.handler(request.method, request.params || {});
      socket.write(`${JSON.stringify(jsonRpcResult(request.id ?? null, result))}\n`);
    } catch (error) { socket.write(`${JSON.stringify(jsonRpcError(request.id ?? null, error.code || -32000, error.message))}\n`); }
  }
  async close() {
    if (!this.server) return;
    for (const socket of this.connections) socket.destroy();
    await new Promise((resolve) => this.server.close(resolve));
    await fs.unlink(this.socketPath).catch(() => {});
    this.connections.clear(); this.server = null;
  }
}
