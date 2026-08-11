import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { WebSocketServer } from 'ws';
import { sanitize, StreamSanitizer } from '../src/sanitizer.js';
import { resolveWorkspacePath } from '../src/workspace.js';
import { LocalAgentDaemon, defaultPaths } from '../src/daemon.js';
import { call } from '../src/ipc-client.js';
import { LocalModelStore } from '../src/model-store.js';
import { saveCredential } from '../src/registry-client.js';
import { LocalAgentTransport } from '../src/transport.js';

async function waitFor(predicate, timeout = 1000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error('timed out waiting for condition');
}

test('sanitizer redacts secrets and workspace paths', () => {
  const result = sanitize('Authorization: Bearer abc123 TOKEN=xyz /home/user/project/a.js', { workspaceRoot: '/home/user/project', homeDir: '/home/user' });
  assert.match(result.value, /\[REDACTED\]/); assert.doesNotMatch(result.value, /abc123|xyz|\/home\/user\/project/); assert.ok(result.redactionCount >= 3);
});

test('stream sanitizer handles a secret split over chunks', () => {
  const sanitizer = new StreamSanitizer(); sanitizer.push('Authorization: Bearer abc'); const result = sanitizer.flush(); assert.match(result.value, /\[REDACTED\]/);
});

test('local model store encrypts credentials with the device secret', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const file = path.join(dir, 'models.json'); const store = new LocalModelStore(file);
  await store.set('agent-1', { base_url: 'https://model.example/v1', model_id: 'test-model', api_key: 'local-model-secret' }, 'device-refresh-secret');
  assert.doesNotMatch(await fs.readFile(file, 'utf8'), /local-model-secret/);
  assert.deepEqual(await store.get('agent-1', 'device-refresh-secret'), { base_url: 'https://model.example/v1', model_id: 'test-model', api_key: 'local-model-secret' });
  await assert.rejects(store.get('agent-1', 'wrong-device-secret'), /unable to decrypt/); assert.equal(await store.remove('agent-1'), true);
});

test('workspace path resolution rejects traversal and symlink escapes', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const root = path.join(dir, 'root'); const outside = path.join(dir, 'outside'); await fs.mkdir(root); await fs.mkdir(outside); await fs.writeFile(path.join(outside, 'secret'), 'x'); await fs.symlink(outside, path.join(root, 'link'));
  await assert.rejects(resolveWorkspacePath(root, '../outside/secret'), /escapes/); await assert.rejects(resolveWorkspacePath(root, 'link/secret'), /escapes/);
  assert.equal(await resolveWorkspacePath(root, 'new.txt', { allowMissing: true }), path.join(root, 'new.txt'));
});

test('workspace path resolution rejects FIFO special files', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const root = path.join(dir, 'root'); const fifo = path.join(root, 'pipe'); await fs.mkdir(root);
  await new Promise((resolve, reject) => { const child = spawn('mkfifo', [fifo]); child.once('error', reject); child.once('exit', (code) => code === 0 ? resolve() : reject(new Error('mkfifo failed'))); });
  await assert.rejects(resolveWorkspacePath(root, 'pipe'), /special files/);
});

test('daemon exposes authenticated local IPC methods and journals runs', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const workspace = path.join(dir, 'workspace'); await fs.mkdir(workspace); const daemon = await new LocalAgentDaemon(defaultPaths(path.join(dir, 'state'))).start();
  const added = await call(daemon.paths.socket, 'workspace.add', { path: workspace, name: 'test' }); const run = await call(daemon.paths.socket, 'run.create', { workspace_id: added.id, prompt: 'status' }); const events = await call(daemon.paths.socket, 'run.events', { run_id: run.run_id }); assert.equal(run.state, 'queued'); assert.equal(events.length, 1); await daemon.stop();
});

test('daemon restores journalled runs after restart', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const workspace = path.join(dir, 'workspace'); const paths = defaultPaths(path.join(dir, 'state')); await fs.mkdir(workspace);
  let daemon = await new LocalAgentDaemon(paths).start(); const added = await call(paths.socket, 'workspace.add', { path: workspace, name: 'test' }); const run = await call(paths.socket, 'run.create', { workspace_id: added.id, prompt: 'resume me' }); await daemon.stop();
  daemon = await new LocalAgentDaemon(paths).start(); const runs = await call(paths.socket, 'run.list'); assert.deepEqual(runs.map(({ run_id, prompt }) => ({ run_id, prompt })), [{ run_id: run.run_id, prompt: 'resume me' }]); await daemon.stop();
});

test('daemon accepts a local model key over IPC without exposing it in status', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const paths = defaultPaths(path.join(dir, 'state')); await saveCredential(paths.dataDir, { device_id: 'device-1', refresh_token: 'device-refresh-secret', api_url: 'http://127.0.0.1:9011' });
  const daemon = await new LocalAgentDaemon(paths).start(); const configured = await call(paths.socket, 'model.configure', { agent_id: 'agent-1', base_url: 'https://model.example/v1', model_id: 'test-model', api_key: 'daemon-only-secret' }); const models = await call(paths.socket, 'model.list');
  assert.equal(configured.agent_id, 'agent-1'); assert.deepEqual(models.map(({ agent_id, configured: ready }) => ({ agent_id, ready })), [{ agent_id: 'agent-1', ready: true }]); assert.doesNotMatch(await fs.readFile(paths.models, 'utf8'), /daemon-only-secret/); await daemon.stop();
});

test('daemon runs a local model stream and journals sanitized output', async () => {
  let authorization = ''; const server = createServer((request, response) => { authorization = request.headers.authorization || ''; response.writeHead(200, { 'Content-Type': 'text/event-stream' }); response.write('data: {"choices":[{"delta":{"content":"Hello TOKEN=secret-value"}}]}\n\n'); response.end('data: [DONE]\n\n'); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve)); const address = server.address(); const baseUrl = `http://127.0.0.1:${address.port}/v1`;
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const paths = defaultPaths(path.join(dir, 'state')); await saveCredential(paths.dataDir, { device_id: 'device-1', refresh_token: 'device-refresh-secret', api_url: 'http://127.0.0.1:9011' });
  const workspace = path.join(dir, 'workspace'); await fs.mkdir(workspace); const daemon = await new LocalAgentDaemon(paths).start(); const added = await call(paths.socket, 'workspace.add', { path: workspace, name: 'test' }); await call(paths.socket, 'model.configure', { agent_id: 'agent-1', base_url: baseUrl, model_id: 'test-model', api_key: 'daemon-only-secret' }); const run = await call(paths.socket, 'run.create', { workspace_id: added.id, agent_id: 'agent-1', prompt: 'say hello' });
  for (let attempt = 0; attempt < 20; attempt += 1) { const current = (await call(paths.socket, 'run.list')).find((item) => item.run_id === run.run_id); if (current?.state === 'completed') break; await new Promise((resolve) => setTimeout(resolve, 20)); }
  const events = await call(paths.socket, 'run.events', { run_id: run.run_id }); assert.equal(authorization, 'Bearer daemon-only-secret'); assert.match(JSON.stringify(events), /Hello TOKEN=\[REDACTED\]/); assert.doesNotMatch(JSON.stringify(events), /secret-value|daemon-only-secret/); await daemon.stop(); await new Promise((resolve) => server.close(resolve));
});

test('daemon rejects a state directory with broad permissions', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const state = path.join(dir, 'state'); await fs.mkdir(state); await fs.chmod(state, 0o755);
  await assert.rejects(new LocalAgentDaemon(defaultPaths(state)).start(), /permissions are too broad/);
});

test('failed startup removes its daemon lock', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-')); const paths = defaultPaths(path.join(dir, 'state')); await fs.mkdir(paths.dataDir, { mode: 0o700 }); await fs.writeFile(paths.journal, ''); await fs.chmod(paths.journal, 0o644);
  await assert.rejects(new LocalAgentDaemon(paths).start(), /permissions are too broad/); await assert.rejects(fs.access(paths.lock));
});

test('transport authenticates WSS with a device token and handles nested offers', async () => {
  const received = []; let authorization = ''; const offers = [];
  const server = createServer((request, response) => {
    if (request.method === 'POST' && request.url === '/api/v1/local-agent/token/refresh') {
      response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify({ access_token: 'device-access' })); return;
    }
    response.writeHead(404); response.end();
  });
  const wss = new WebSocketServer({ noServer: true });
  server.on('upgrade', (request, socket, head) => {
    authorization = request.headers.authorization || '';
    wss.handleUpgrade(request, socket, head, (websocket) => wss.emit('connection', websocket));
  });
  wss.on('connection', (websocket) => websocket.on('message', (data) => {
    const message = JSON.parse(data.toString()); received.push(message);
    if (message.type === 'hello') websocket.send(JSON.stringify({ protocol_version: 1, message_id: 'offer-1', type: 'run.offer', payload: { run_id: 'run-1', lease_id: 'lease-1' } }));
  }));
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve)); const address = server.address(); const dataDir = await fs.mkdtemp(path.join(os.tmpdir(), 'local-agent-'));
  await saveCredential(dataDir, { device_id: 'device-1', refresh_token: 'refresh-secret', api_url: `http://127.0.0.1:${address.port}` });
  const transport = new LocalAgentTransport(dataDir, { offer: (offer) => offers.push(offer), claimed: () => {}, cancel: () => {} });
  await transport.start(); await waitFor(() => offers.length === 1);
  transport.send('run.claim', { run_id: 'run-1', lease_id: 'lease-1', local_session_id: 'session-1' }); await waitFor(() => received.some((message) => message.type === 'run.claim'));
  assert.equal(authorization, 'Bearer device-access'); assert.deepEqual(offers, [{ run_id: 'run-1', lease_id: 'lease-1' }]);
  assert.deepEqual(received.find((message) => message.type === 'run.claim').payload, { run_id: 'run-1', lease_id: 'lease-1', local_session_id: 'session-1' });
  await transport.stop(); await new Promise((resolve) => wss.close(resolve)); await new Promise((resolve) => server.close(resolve));
});
