// WebSocket client with automatic reconnect. The server sends a full picture (network, state, last frame, results)
// on every (re)connect, so a reload or a dropped connection never leaves the page stale.
export class NetClient {
  constructor(path = '/ws') {
    this.path = path;
    this.handlers = {};
    this.connected = false;
    this.onconnection = () => {};
    this._connect();
  }

  on(type, fn) { this.handlers[type] = fn; return this; }

  send(cmd, extra = {}) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ cmd, ...extra }));
  }

  _connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}${this.path}`);
    this.ws = ws;
    ws.onopen = () => { this.connected = true; this.onconnection(true); };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      msg._rx = performance.now();
      const h = this.handlers[msg.type];
      if (h) h(msg);
    };
    ws.onclose = () => {
      this.connected = false;
      this.onconnection(false);
      setTimeout(() => this._connect(), 1000);
    };
    ws.onerror = () => ws.close();
  }
}
