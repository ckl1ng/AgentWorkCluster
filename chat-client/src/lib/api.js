/**
 * REST API 封装
 *
 * - 自动附加 Authorization: Bearer header
 * - 统一错误处理
 * - 所有请求返回解析后的 JSON
 */

const BASE = '/api/v1';

function getToken() {
  return localStorage.getItem('chat_token');
}

function formatApiError(data, status) {
  const detail = data?.detail ?? data?.error;
  if (Array.isArray(detail)) {
    const messages = detail.map((issue) => {
      if (!issue || typeof issue !== 'object') return String(issue);
      const location = Array.isArray(issue.loc)
        ? issue.loc.filter((part) => part !== 'body').join('.')
        : '';
      const message = issue.msg || issue.message || '字段校验失败';
      return location ? `${location}: ${message}` : message;
    }).filter(Boolean);
    if (messages.length) return messages.join('；');
  }
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return `HTTP ${status}`;
}

/**
 * 内部请求函数
 */
async function request(method, path, body, { signal, timeout = 15000 } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', abortFromCaller, { once: true });
  }
  const timeoutId = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout);

  const opts = { method, headers, signal: controller.signal };
  if (body) {
    opts.body = JSON.stringify(body);
  }

  try {
    const res = await fetch(`${BASE}${path}`, opts);
    const raw = await res.text();
    let data = null;
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch {
        data = { error: raw };
      }
    }

    if (!res.ok) {
      const error = new Error(formatApiError(data, res.status));
      error.status = res.status;
      error.data = data;
      throw error;
    }

    return data;
  } catch (error) {
    if (timedOut) throw new Error('请求超时，请检查服务器连接后重试');
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    signal?.removeEventListener('abort', abortFromCaller);
  }
}

// ── 用户 ──

export const api = {
  /** POST /register */
  register(username, password, publicKey, encryptedSecretKey) {
    return request('POST', '/register', {
      username,
      password,
      public_key: publicKey,
      encrypted_secret_key: encryptedSecretKey,
    });
  },

  /** POST /login */
  login(username, password, encryptedSecretKey) {
    return request('POST', '/login', {
      username,
      password,
      ...(encryptedSecretKey ? { encrypted_secret_key: encryptedSecretKey } : {}),
    });
  },

  /** GET /users */
  getUsers() {
    return request('GET', '/users');
  },

  getFriends() {
    return request('GET', '/friends');
  },

  searchUsers(query) {
    return request('GET', `/users/search?q=${encodeURIComponent(query)}`);
  },

  getFriendRequests() {
    return request('GET', '/friends/requests');
  },

  sendFriendRequest(userId) {
    return request('POST', '/friends/requests', { user_id: userId });
  },

  acceptFriendRequest(userId) {
    return request('POST', `/friends/requests/${userId}/accept`);
  },

  /** GET /users/me */
  getMe() {
    return request('GET', '/users/me');
  },

  updateAvatar(avatar) {
    return request('PUT', '/users/me/avatar', { avatar });
  },

  /** GET /users/:id */
  getUser(id) {
    return request('GET', `/users/${id}`);
  },

  /** GET /users/:id/public_key */
  getPublicKey(userId) {
    return request('GET', `/users/${userId}/public_key`);
  },

  // ── 群组 ──

  /** POST /groups */
  createGroup(name, memberIds, encryptedKeys) {
    return request('POST', '/groups', {
      name,
      member_ids: memberIds,
      encrypted_group_keys: encryptedKeys,
    });
  },

  /** GET /groups/list */
  listGroups() {
    return request('GET', '/groups/list');
  },

  /** GET /groups/:id/members */
  getGroupMembers(groupId) {
    return request('GET', `/groups/${groupId}/members`);
  },

  /** POST /groups/:id/join */
  joinGroup(groupId, encryptedKey) {
    return request('POST', `/groups/${groupId}/join`, {
      encrypted_key: encryptedKey,
    });
  },

  addGroupMember(groupId, userId, encryptedKey) {
    return request('POST', `/groups/${groupId}/members`, {
      user_id: userId,
      encrypted_key: encryptedKey,
    });
  },

  // ── 消息历史 ──

  /**
   * GET /messages/:user_id — 私聊历史
   * @param {number} userId - 对方的用户 ID
   * @param {{ limit?: number, beforeId?: number, afterId?: number }} opts
   */
  getPrivateMessages(userId, { limit, beforeId, afterId, signal } = {}) {
    const params = new URLSearchParams();
    if (limit) params.set('limit', limit);
    if (beforeId) params.set('before_id', beforeId);
    if (afterId) params.set('after_id', afterId);
    const qs = params.toString();
    return request('GET', `/messages/${userId}${qs ? '?' + qs : ''}`, undefined, { signal });
  },

  /**
   * GET /groups/:id/messages — 群聊历史
   * @param {number} groupId
   * @param {{ limit?: number, beforeId?: number, afterId?: number }} opts
   */
  getGroupMessages(groupId, { limit, beforeId, afterId, signal } = {}) {
    const params = new URLSearchParams();
    if (limit) params.set('limit', limit);
    if (beforeId) params.set('before_id', beforeId);
    if (afterId) params.set('after_id', afterId);
    const qs = params.toString();
    return request('GET', `/groups/${groupId}/messages${qs ? '?' + qs : ''}`, undefined, { signal });
  },

  // ── Agent ──

  listAgents() {
    return request('GET', '/agents');
  },

  getAgent(agentId) {
    return request('GET', `/agents/${agentId}`);
  },

  createAgent(payload) {
    return request('POST', '/agents', payload, { timeout: 30000 });
  },

  updateAgent(agentId, payload) {
    return request('PUT', `/agents/${agentId}`, payload, { timeout: 30000 });
  },

  deleteAgent(agentId) {
    return request('DELETE', `/agents/${agentId}`);
  },

  listAgentConversations(agentId) {
    return request('GET', `/agents/${agentId}/conversations`);
  },

  createAgentConversation(agentId, title = '新会话') {
    return request('POST', `/agents/${agentId}/conversations`, { title });
  },

  getAgentConversation(conversationId) {
    return request('GET', `/agent-conversations/${conversationId}`);
  },

  createAgentRun(conversationId, content) {
    return request('POST', `/agent-conversations/${conversationId}/runs`, { content }, { timeout: 30000 });
  },

  listLocalDevices() {
    return request('GET', '/local-agent/devices');
  },

  revokeLocalDevice(deviceId) {
    return request('DELETE', `/local-agent/devices/${deviceId}`);
  },

  approveLocalPairing(pairingId, code) {
    return request('POST', `/local-agent/pairings/${pairingId}/approve`, { code });
  },

  listLocalWorkspaces(deviceId) {
    return request('GET', `/local-agent/devices/${deviceId}/workspaces`);
  },

  bindLocalAgent(agentId, payload) {
    return request('POST', `/agents/${agentId}/local-bind`, payload);
  },

  clearAgentContext(conversationId) {
    return request('POST', `/agent-conversations/${conversationId}/clear-context`);
  },

  cancelAgentRun(runId) {
    return request('POST', `/agent-runs/${runId}/cancel`);
  },

  getAgentTrace(runId, afterSequence = 0) {
    return request('GET', `/agent-runs/${runId}/trace?after_sequence=${afterSequence}`);
  },

  getAgentRun(runId) {
    return request('GET', `/agent-runs/${runId}`);
  },

  listAgentRuns({ agentId, state, limit = 100 } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (agentId) params.set('agent_id', agentId);
    if (state) params.set('state', state);
    return request('GET', `/agent-runs?${params}`);
  },

  listRunConfirmations(runId) {
    return request('GET', `/agent-runs/${runId}/confirmations`);
  },

  decideToolConfirmation(runId, confirmationId, payload) {
    return request('POST', `/agent-runs/${runId}/confirmations/${confirmationId}`, payload);
  },

  listTools() {
    return request('GET', '/tools');
  },

  createTool(payload) {
    return request('POST', '/tools', payload);
  },

  importOpenApi(payload) {
    return request('POST', '/tools/openapi/import', payload, { timeout: 30000 });
  },

  discoverMcp(payload) {
    return request('POST', '/tools/mcp/discover', payload, { timeout: 30000 });
  },

  discoverMcpStdio(payload) {
    return request('POST', '/tools/mcp/discover-stdio', payload, { timeout: 30000 });
  },

  validateTool(toolId) {
    return request('POST', `/tools/${toolId}/validate`);
  },

  listAgentTools(agentId) {
    return request('GET', `/agents/${agentId}/tools`);
  },

  assignAgentTools(agentId, toolIds) {
    return request('PUT', `/agents/${agentId}/tools`, { tool_ids: toolIds });
  },

  pauseAgent(agentId) {
    return request('POST', `/agents/${agentId}/pause`);
  },

  resumeAgent(agentId) {
    return request('POST', `/agents/${agentId}/resume`);
  },

  listMemories(agentId) {
    return request('GET', `/agents/${agentId}/memories`);
  },

  createMemory(agentId, payload) {
    return request('POST', `/agents/${agentId}/memories`, payload);
  },

  deleteMemory(agentId, memoryId) {
    return request('DELETE', `/agents/${agentId}/memories/${memoryId}`);
  },

  listTasks(state) {
    return request('GET', `/tasks${state ? `?state=${encodeURIComponent(state)}` : ''}`);
  },

  createTask(payload) { return request('POST', '/tasks', payload); },
  getTask(taskId) { return request('GET', `/tasks/${taskId}`); },
  getTaskContext(taskId) { return request('GET', `/tasks/${taskId}/context`); },
  getTaskAssignments(taskId) { return request('GET', `/tasks/${taskId}/assignments`); },
  getTaskResults(taskId) { return request('GET', `/tasks/${taskId}/results`); },
  getTaskRuns(taskId) { return request('GET', `/tasks/${taskId}/runs`); },
  getTaskConfirmations(taskId) { return request('GET', `/tasks/${taskId}/confirmations`); },
  closeTask(taskId, resultSummary) { return request('POST', `/tasks/${taskId}/close`, { result_summary: resultSummary }); },
  reopenTask(taskId) { return request('POST', `/tasks/${taskId}/reopen`); },
  cancelTask(taskId) { return request('POST', `/tasks/${taskId}/cancel`); },
  listNotifications(unreadOnly = false) {
    return request('GET', `/notifications${unreadOnly ? '?unread_only=true' : ''}`);
  },
  markNotificationRead(notificationId) {
    return request('POST', `/notifications/${notificationId}/read`);
  },

  listEvaluationRuns() {
    return request('GET', '/evaluations/runs');
  },

  getEvaluationRun(runId) {
    return request('GET', `/evaluations/runs/${runId}`);
  },

  compareEvaluationRuns(baselineId, candidateId) {
    const params = new URLSearchParams({ baseline_id: baselineId, candidate_id: candidateId });
    return request('GET', `/evaluations/compare?${params}`);
  },
};
