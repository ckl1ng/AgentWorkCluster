import fs from 'node:fs/promises';
import path from 'node:path';
import { assertPrivateFile, ensurePrivateDirectory } from './permissions.js';

async function request(apiUrl, method, pathname, body) {
  const response = await fetch(new URL(pathname, apiUrl), {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  return data;
}

async function authorizedRequest(apiUrl, method, pathname, accessToken, body) {
  const response = await fetch(new URL(pathname, apiUrl), {
    method,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  return data;
}

export async function saveCredential(dataDir, credential) {
  await ensurePrivateDirectory(dataDir);
  const target = path.join(dataDir, 'device-credential.json');
  const temporary = `${target}.${process.pid}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(credential)}\n`, { mode: 0o600 });
  await fs.rename(temporary, target);
}

export async function loadCredential(dataDir) {
  const target = path.join(dataDir, 'device-credential.json');
  await assertPrivateFile(target);
  try { return JSON.parse(await fs.readFile(target, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

export async function beginPairing(apiUrl, device) {
  return request(apiUrl, 'POST', '/api/v1/local-agent/pairings', device);
}

export async function claimPairing(apiUrl, pairingId, pairingSecret) {
  return request(apiUrl, 'POST', `/api/v1/local-agent/pairings/${pairingId}/claim`, { pairing_secret: pairingSecret });
}

export async function refreshAccessToken(apiUrl, refreshToken) {
  return request(apiUrl, 'POST', '/api/v1/local-agent/token/refresh', { refresh_token: refreshToken });
}

export async function registerWorkspace(apiUrl, accessToken, workspace) {
  return authorizedRequest(apiUrl, 'POST', '/api/v1/local-agent/workspaces', accessToken, workspace);
}

export async function registerLocalModel(apiUrl, accessToken, model) {
  return authorizedRequest(apiUrl, 'POST', '/api/v1/local-agent/models', accessToken, model);
}

export async function removeLocalModel(apiUrl, accessToken, agentId) {
  return authorizedRequest(apiUrl, 'DELETE', `/api/v1/local-agent/models/${agentId}`, accessToken);
}
