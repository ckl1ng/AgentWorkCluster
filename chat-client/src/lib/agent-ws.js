function agentWsUrl(token) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/agent/ws?token=${encodeURIComponent(token)}`;
}

/** A small reconnecting socket dedicated to one Agent run subscription. */
export class AgentWebSocket {
  constructor(token, onEvent, onState = () => {}) {
    this.token = token;
    this.onEvent = onEvent;
    this.onState = onState;
    this.socket = null;
    this.running = false;
    this.runId = null;
    this.afterSequence = 0;
    this.delay = 1000;
    this.timer = null;
  }

  connect(runId, afterSequence = 0) {
    this.running = true;
    this.runId = runId;
    this.afterSequence = afterSequence;
    this.open();
  }

  disconnect() {
    this.running = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
      this.socket = null;
    }
  }

  open() {
    if (!this.running || !this.runId) return;
    const socket = new WebSocket(agentWsUrl(this.token));
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket || !this.running) return socket.close();
      this.delay = 1000;
      this.onState('connected');
      socket.send(JSON.stringify({
        type: 'agent.subscribe',
        run_id: this.runId,
        after_sequence: this.afterSequence,
      }));
    };
    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (typeof data.sequence === 'number') this.afterSequence = Math.max(this.afterSequence, data.sequence);
        this.onEvent(data);
      } catch { /* Ignore malformed events. */ }
    };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.onState('disconnected');
      if (!this.running) return;
      this.timer = setTimeout(() => this.open(), this.delay);
      this.delay = Math.min(this.delay * 2, 30000);
    };
  }
}
