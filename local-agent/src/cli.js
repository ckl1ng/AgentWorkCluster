import path from 'node:path';
import os from 'node:os';
import { LocalAgentDaemon, defaultPaths } from './daemon.js';
import { call } from './ipc-client.js';
import { beginPairing, claimPairing, loadCredential, refreshAccessToken, registerLocalModel, registerWorkspace, removeLocalModel, saveCredential } from './registry-client.js';

function usage() {
  return `local-agent auth login [--api url] | auth status | daemon | status | workspace add <path> [--name name] | workspace list | model set <agent-id> --base-url url --model-id id [--api-key-env name] | model list | model remove <agent-id> | run <prompt> --workspace id --agent id | run list | run events <id> | run attach <id>`;
}

export async function runCli(argv, { dataDir = path.join(os.homedir(), '.local-agent'), stdout = process.stdout } = {}) {
  const [command, subcommand, ...rest] = argv; const paths = defaultPaths(dataDir);
  if (!command) throw new Error(usage());
  const apiIndex = rest.indexOf('--api');
  const apiUrl = apiIndex >= 0 ? rest[apiIndex + 1] : (process.env.LOCAL_AGENT_API_URL || 'http://127.0.0.1:9011');
  if (command === 'auth' && subcommand === 'status') {
    const credential = await loadCredential(dataDir);
    return stdout.write(`${JSON.stringify(credential ? { device_id: credential.device_id, api_url: credential.api_url } : { authenticated: false }, null, 2)}\n`);
  }
  if (command === 'auth' && subcommand === 'login') {
    const pairing = await beginPairing(apiUrl, { display_name: os.hostname(), platform: process.platform, cli_version: '0.1.0', capabilities: [] });
    stdout.write(`在 Web 端批准配对会话：${pairing.pairing_id}\n配对码：${pairing.code}\n`);
    const deadline = Date.parse(pairing.expires_at);
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const claimed = await claimPairing(apiUrl, pairing.pairing_id, pairing.pairing_secret);
      if (claimed.state !== 'approved') continue;
      await saveCredential(dataDir, { device_id: claimed.device_id, refresh_token: pairing.pairing_secret, api_url: apiUrl, created_at: new Date().toISOString() });
      return stdout.write(`已配对设备 ${claimed.device_id}\n`);
    }
    throw new Error('配对码已过期');
  }
  if (command === 'daemon') { const daemon = await new LocalAgentDaemon(paths).start(); stdout.write(`local-agent daemon listening on ${paths.socket}\n`); const shutdown = async () => { await daemon.stop(); process.exit(0); }; process.once('SIGINT', shutdown); process.once('SIGTERM', shutdown); return daemon; }
  const request = async (method, params) => call(paths.socket, method, params);
  if (command === 'status') return stdout.write(`${JSON.stringify(await request('daemon.status'), null, 2)}\n`);
  if (command === 'workspace' && subcommand === 'list') return stdout.write(`${JSON.stringify(await request('workspace.list'), null, 2)}\n`);
  if (command === 'workspace' && subcommand === 'add') {
    const nameIndex = rest.indexOf('--name');
    const result = await request('workspace.add', { path: rest[0], name: nameIndex >= 0 ? rest[nameIndex + 1] : undefined });
    const credential = await loadCredential(dataDir);
    if (!credential) return stdout.write(`${JSON.stringify(result)}\n本地工作区已添加；完成 local-agent auth login 后再同步到控制面。\n`);
    const access = await refreshAccessToken(credential.api_url, credential.refresh_token);
    const remote = await registerWorkspace(credential.api_url, access.access_token, { display_name: result.name, policy_version: result.policy_version, capabilities: [] });
    await request('workspace.set-remote-id', { workspace_id: result.id, remote_id: remote.id });
    return stdout.write(`${JSON.stringify({ ...result, remote_id: remote.id })}\n`);
  }
  if (command === 'model' && subcommand === 'list') return stdout.write(`${JSON.stringify(await request('model.list'), null, 2)}\n`);
  if (command === 'model' && subcommand === 'set') {
    const agentId = rest[0]; const baseUrlIndex = rest.indexOf('--base-url'); const modelIdIndex = rest.indexOf('--model-id'); const keyEnvIndex = rest.indexOf('--api-key-env');
    const keyEnv = keyEnvIndex >= 0 ? rest[keyEnvIndex + 1] : 'LOCAL_AGENT_MODEL_API_KEY'; const apiKey = process.env[keyEnv];
    if (!agentId || baseUrlIndex < 0 || modelIdIndex < 0 || !apiKey) throw new Error('model set requires an agent ID, --base-url, --model-id, and an API key environment variable');
    const credential = await loadCredential(dataDir); if (!credential) throw new Error('local model credentials require local-agent auth login');
    const result = await request('model.configure', { agent_id: agentId, base_url: rest[baseUrlIndex + 1], model_id: rest[modelIdIndex + 1], api_key: apiKey }); delete process.env[keyEnv];
    const access = await refreshAccessToken(credential.api_url, credential.refresh_token); await registerLocalModel(credential.api_url, access.access_token, { agent_id: agentId, base_url: result.base_url, model_id: result.model_id });
    return stdout.write(`${JSON.stringify(result)}\n`);
  }
  if (command === 'model' && subcommand === 'remove') {
    const agentId = rest[0]; if (!agentId) throw new Error('model remove requires an agent ID'); const credential = await loadCredential(dataDir); if (!credential) throw new Error('local model credentials require local-agent auth login');
    const result = await request('model.remove', { agent_id: agentId }); const access = await refreshAccessToken(credential.api_url, credential.refresh_token); await removeLocalModel(credential.api_url, access.access_token, agentId); return stdout.write(`${JSON.stringify(result)}\n`);
  }
  if (command === 'run' && subcommand === 'list') return stdout.write(`${JSON.stringify(await request('run.list'), null, 2)}\n`);
  if (command === 'run' && subcommand === 'events') return stdout.write(`${JSON.stringify(await request('run.events', { run_id: rest[0] }), null, 2)}\n`);
  if (command === 'run' && subcommand === 'attach') {
    const runId = rest[0]; if (!runId) throw new Error('run attach requires a run ID'); let after = 0;
    while (true) {
      const events = await request('run.events', { run_id: runId, after_sequence: after });
      for (const event of events) { after = Math.max(after, event.sequence || 0); if (event.payload?.content) stdout.write(event.payload.content); }
      const run = (await request('run.list')).find((item) => item.run_id === runId);
      if (!run || ['completed', 'failed', 'cancelled'].includes(run.state)) return stdout.write(`\n${run?.state || 'not found'}\n`);
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  }
  if (command === 'run') {
    const workspaceIndex = rest.indexOf('--workspace'); const agentIndex = rest.indexOf('--agent'); const optionValues = new Set([workspaceIndex, workspaceIndex + 1, agentIndex, agentIndex + 1]);
    const prompt = [subcommand, ...rest.filter((_, index) => !optionValues.has(index))].join(' '); const result = await request('run.create', { prompt, workspace_id: workspaceIndex >= 0 ? rest[workspaceIndex + 1] : undefined, agent_id: agentIndex >= 0 ? rest[agentIndex + 1] : undefined, origin: 'terminal' }); return stdout.write(`${JSON.stringify(result)}\n`);
  }
  throw new Error(usage());
}
