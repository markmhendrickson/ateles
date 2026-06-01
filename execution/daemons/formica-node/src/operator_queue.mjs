/**
 * Per-issue FIFO for operator follow-ups (Telegram, HTTP, Neotoma poll).
 */
export class OperatorQueue {
  constructor() {
    /** @type {Map<string, string[]>} */
    this._queues = new Map();
    /** @type {Map<string, Array<(m: string) => void>>} */
    this._waiters = new Map();
  }

  /**
   * @param {string} issueEntityId
   * @param {string} text
   */
  enqueue(issueEntityId, text) {
    const id = String(issueEntityId);
    const waiters = this._waiters.get(id);
    if (waiters && waiters.length > 0) {
      const resolve = waiters.shift();
      if (!waiters.length) this._waiters.delete(id);
      resolve(text);
      return;
    }
    const q = this._queues.get(id) || [];
    q.push(text);
    this._queues.set(id, q);
  }

  /**
   * @param {string} issueEntityId
   * @returns {Promise<string | null>}
   */
  waitLine(issueEntityId, timeoutMs = 120_000) {
    const id = String(issueEntityId);
    const q = this._queues.get(id);
    if (q && q.length > 0) {
      const m = q.shift();
      if (!q.length) this._queues.delete(id);
      else this._queues.set(id, q);
      return Promise.resolve(m || null);
    }
    return new Promise((resolve) => {
      const t = setTimeout(() => {
        const arr = this._waiters.get(id) || [];
        const idx = arr.indexOf(done);
        if (idx >= 0) arr.splice(idx, 1);
        if (!arr.length) this._waiters.delete(id);
        resolve(null);
      }, timeoutMs);

      /** @param {string} m */
      const done = (m) => {
        clearTimeout(t);
        resolve(m);
      };
      const arr = this._waiters.get(id) || [];
      arr.push(done);
      this._waiters.set(id, arr);
    });
  }
}
