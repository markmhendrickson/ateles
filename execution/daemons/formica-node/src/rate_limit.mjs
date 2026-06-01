export class HourlyRateLimiter {
  /**
   * @param {number} maxPerHour
   */
  constructor(maxPerHour = 5) {
    this.maxPerHour = maxPerHour;
    /** @type {number[]} */
    this.timestamps = [];
  }

  _prune() {
    const now = Date.now();
    this.timestamps = this.timestamps.filter((t) => now - t < 60 * 60 * 1000);
  }

  canConsume() {
    this._prune();
    return this.timestamps.length < this.maxPerHour;
  }

  consume() {
    this.timestamps.push(Date.now());
  }
}
