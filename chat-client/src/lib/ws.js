/**
 * WebSocket 连接管理器
 *
 * 功能：
 * - 使用 token 建立 WebSocket 连接（query 参数，因为浏览器不支持自定义 header）
 * - 指数退避自动重连
 * - 30s 心跳 ping/pong
 * - 消息回调注册 / 分发
 */

// 解析当前页面的 host，用于 WebSocket 连接
function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

export class ChatWebSocket {
  /**
   * @param {string} token
   */
  constructor(token) {
    this.token = token;
    this.ws = null;
    this.handlers = [];
    this.reconnectDelay = 1000;
    this.maxReconnectDelay = 30000;
    this.running = false;
    this.heartbeatTimer = null;
    this.pongTimer = null;
    this.reconnectTimer = null;
  }

  /** 启动连接 */
  connect() {
    if (this.running) return;
    this.running = true;
    this._doConnect();
  }

  /** 停止连接（不重连） */
  disconnect() {
    this.running = false;
    this.reconnectDelay = 1000;
    this._clearTimers();
    this._clearReconnectTimer();
    if (this.ws) {
      this.ws.onclose = null; // 阻止触发重连
      this.ws.close();
      this.ws = null;
    }
  }

  /** 发送 JSON 消息 */
  send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify(msg));
        return true;
      } catch {
        return false;
      }
    }
    return false;
  }

  /** 注册消息处理器 */
  onMessage(handler) {
    this.handlers.push(handler);
    // 返回取消注册的函数
    return () => {
      this.handlers = this.handlers.filter(h => h !== handler);
    };
  }

  // ── 内部 ──

  _doConnect() {
    if (!this.running) return;

    const url = `${wsUrl()}?token=${this.token}`;
    const socket = new WebSocket(url);
    this.ws = socket;

    socket.onopen = () => {
      if (!this.running || this.ws !== socket) {
        socket.close();
        return;
      }
      this.reconnectDelay = 1000;
      this._startHeartbeat();
    };

    socket.onmessage = (event) => {
      if (this.ws !== socket) return;
      try {
        const msg = JSON.parse(event.data);
        // 内部处理 pong
        if (msg.type === 'pong') {
          this._clearPongTimer();
          return;
        }
        // 在线状态：connected 消息可以用来感知自己的连接
        for (const h of this.handlers) {
          try { h(msg); } catch (e) { /* 忽略 handler 异常 */ }
        }
      } catch {
        // 忽略非 JSON 消息
      }
    };

    socket.onclose = () => {
      if (this.ws !== socket) return;
      this._clearTimers();
      this.ws = null;
      if (!this.running) return;

      this._scheduleReconnect();
    };

    socket.onerror = () => {
      // onclose 会在 onerror 之后触发，重连只在 onclose 中调度。
    };
  }

  _scheduleReconnect() {
    if (!this.running || this.reconnectTimer) return;
    const delay = this.reconnectDelay;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this._doConnect();
    }, delay);
    this.reconnectDelay = Math.min(delay * 2, this.maxReconnectDelay);
  }

  _clearReconnectTimer() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  // ── 心跳 ──

  _startHeartbeat() {
    this._clearTimers();
    this.heartbeatTimer = setInterval(() => {
      const socket = this.ws;
      if (socket && socket.readyState === WebSocket.OPEN) {
        this._clearPongTimer();
        socket.send(JSON.stringify({ type: 'ping' }));
        // 期待 10 秒内收到 pong
        this.pongTimer = setTimeout(() => {
          if (this.ws === socket) socket.close();
        }, 10000);
      }
    }, 30000);
  }

  _clearPongTimer() {
    if (this.pongTimer) {
      clearTimeout(this.pongTimer);
      this.pongTimer = null;
    }
  }

  _clearTimers() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    this._clearPongTimer();
  }
}
