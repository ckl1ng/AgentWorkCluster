<script>
  import { auth } from '../lib/store.js';
  import { api } from '../lib/api.js';
  import { encryptSecretKeyForPassword, generateKeyPair } from '../lib/crypto.js';
  import { base64ToBytes, bytesToBase64 } from '../lib/utils.js';

  let username = '';
  let password = '';
  let error = '';
  let loading = false;
  let mode = 'register';  // 'register' | 'login'

  /** 注册新用户 */
  async function handleRegister() {
    error = '';
    if (!username.trim()) {
      error = '请输入用户名';
      return;
    }
    if (username.length > 32) {
      error = '用户名需为 1-32 字符';
      return;
    }

    if (password.length < 8) {
      error = '密码至少需要 8 个字符';
      return;
    }
    loading = true;
    try {
      // 生成密钥对
      const kp = generateKeyPair();
      const publicKeyB64 = bytesToBase64(kp.publicKey);
      const encryptedSecretKey = await encryptSecretKeyForPassword(kp.secretKey, password);

      // 注册
      const data = await api.register(username.trim(), password, publicKeyB64, encryptedSecretKey);

      // 保存到 localStorage
      const authData = {
        id: data.id,
        username: data.username,
        token: data.token,
        publicKey: bytesToBase64(kp.publicKey),
        secretKey: bytesToBase64(kp.secretKey),
      };
      localStorage.setItem('chat_auth', JSON.stringify(authData));
      localStorage.setItem('chat_token', data.token);

      // 更新 store
      auth.set({
        id: data.id,
        username: data.username,
        token: data.token,
        publicKey: kp.publicKey,
        secretKey: kp.secretKey,
      });
    } catch (e) {
      error = e.message || '注册失败';
    } finally {
      loading = false;
    }
  }

  /** 使用用户名和密码登录；聊天私钥仅保留在当前设备。 */
  async function handleLogin() {
    error = '';
    if (!username.trim() || !password) {
      error = '请输入用户名和密码';
      return;
    }

    loading = true;
    try {
      let localSecretKey = null;
      try {
        const stored = JSON.parse(localStorage.getItem('chat_auth') || 'null');
        if (stored?.username === username.trim() && stored.secretKey) {
          const key = base64ToBytes(stored.secretKey);
          if (key.length === 32) localSecretKey = key;
        }
      } catch { /* A missing local key must not prevent account login. */ }
      const data = await api.login(username.trim(), password);
      localStorage.setItem('chat_token', data.token);
      const me = await api.getMe();
      const publicKey = base64ToBytes(me.public_key);

      const authData = {
        id: data.id,
        username: data.username,
        token: data.token,
        publicKey: bytesToBase64(publicKey),
        ...(localSecretKey ? { secretKey: bytesToBase64(localSecretKey) } : {}),
      };
      localStorage.setItem('chat_auth', JSON.stringify(authData));

      localStorage.setItem('chat_token', data.token);
      auth.set({
        id: data.id,
        username: data.username,
        token: data.token,
        publicKey,
        secretKey: localSecretKey,
      });
    } catch (e) {
      error = e.message || '登录失败';
      localStorage.removeItem('chat_token');
    } finally {
      loading = false;
    }
  }

  function handleKeydown(e, handler) {
    if (e.key === 'Enter') handler();
  }
</script>

<div class="login-page">
  <div class="login-card">
    <h1>Chat Server</h1>
    <p class="subtitle">端到端加密聊天</p>

    <div class="tabs">
      <button
        class:active={mode === 'register'}
        on:click={() => { mode = 'register'; error = ''; }}
      >注册</button>
      <button
        class:active={mode === 'login'}
        on:click={() => { mode = 'login'; error = ''; }}
      >登录</button>
    </div>

    <div class="form">
      {#if mode === 'register'}
        <input
          type="text"
          placeholder="用户名"
          bind:value={username}
          maxlength="32"
          disabled={loading}
          on:keydown={(e) => handleKeydown(e, handleRegister)}
        />
        <input
          type="password"
          placeholder="密码（至少 8 位）"
          bind:value={password}
          minlength="8"
          disabled={loading}
          on:keydown={(e) => handleKeydown(e, handleRegister)}
        />
        <p class="hint">注册时将自动生成 Curve25519 密钥对</p>
        <button
          class="primary"
          disabled={loading}
          on:click={handleRegister}
        >
          {loading ? '注册中...' : '注册'}
        </button>
      {:else}
        <input
          type="text"
          placeholder="用户名"
          bind:value={username}
          disabled={loading}
          on:keydown={(e) => handleKeydown(e, handleLogin)}
        />
        <input
          type="password"
          placeholder="密码"
          bind:value={password}
          disabled={loading}
          on:keydown={(e) => handleKeydown(e, handleLogin)}
        />
        <button
          class="primary"
          disabled={loading}
          on:click={handleLogin}
        >
          {loading ? '登录中...' : '登录'}
        </button>
      {/if}

      {#if error}
        <p class="error">{error}</p>
      {/if}
    </div>
  </div>
</div>

<style>
  .login-page {
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--color-bg, #0f0f0f);
  }

  .login-card {
    width: 360px;
    padding: 32px;
    border-radius: var(--radius-lg, 16px);
    background: var(--color-surface, #1a1a1a);
    border: 1px solid var(--color-border, #2a2a2a);
  }

  h1 {
    text-align: center;
    font-size: 24px;
    color: var(--color-primary, #4a9eff);
    margin-bottom: 4px;
  }

  .subtitle {
    text-align: center;
    color: var(--color-text-muted, #888);
    font-size: var(--font-sm, 12px);
    margin-bottom: 24px;
  }

  .tabs {
    display: flex;
    gap: 0;
    margin-bottom: 20px;
    border-radius: var(--radius-md, 8px);
    overflow: hidden;
    border: 1px solid var(--color-border, #2a2a2a);
  }

  .tabs button {
    flex: 1;
    padding: 8px;
    border: none;
    background: transparent;
    color: var(--color-text-muted, #888);
    cursor: pointer;
    font-size: var(--font-md, 14px);
    transition: 0.15s;
  }

  .tabs button.active {
    background: var(--color-primary, #4a9eff);
    color: #fff;
  }

  .form {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  input {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--color-border, #2a2a2a);
    border-radius: var(--radius-md, 8px);
    background: var(--color-bg, #0f0f0f);
    color: var(--color-text, #e0e0e0);
    font-size: var(--font-md, 14px);
    outline: none;
    transition: border-color 0.15s;
  }

  input:focus {
    border-color: var(--color-primary, #4a9eff);
  }

  .hint {
    font-size: var(--font-sm, 12px);
    color: var(--color-text-muted, #888);
    line-height: 1.4;
  }

  .primary {
    padding: 10px;
    border: none;
    border-radius: var(--radius-md, 8px);
    background: var(--color-primary, #4a9eff);
    color: #fff;
    cursor: pointer;
    font-size: var(--font-md, 14px);
    font-weight: 500;
    transition: background 0.15s;
  }

  .primary:hover:not(:disabled) {
    background: var(--color-primary-dim, #2a6ecc);
  }

  .primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .error {
    padding: 8px 12px;
    border-radius: var(--radius-sm, 4px);
    background: rgba(255, 82, 82, 0.15);
    color: var(--color-error, #ff5252);
    font-size: var(--font-sm, 12px);
  }
</style>
