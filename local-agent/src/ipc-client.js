import net from 'node:net';

export function call(socketPath, method, params = {}) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath); socket.setEncoding('utf8'); let buffer = '';
    const id = `r_${crypto.randomUUID()}`;
    const timer = setTimeout(() => { socket.destroy(); reject(new Error('daemon request timed out')); }, 15000);
    socket.on('connect', () => socket.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`));
    socket.on('data', (chunk) => { buffer += chunk; const index = buffer.indexOf('\n'); if (index < 0) return; clearTimeout(timer); socket.end(); const response = JSON.parse(buffer.slice(0, index)); if (response.error) reject(Object.assign(new Error(response.error.message), { code: response.error.code })); else resolve(response.result); });
    socket.on('error', (error) => { clearTimeout(timer); reject(error); });
  });
}
