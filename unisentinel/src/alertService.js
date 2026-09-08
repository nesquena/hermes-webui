const EventEmitter = require('events');

class AlertService extends EventEmitter {
  constructor(options = {}) {
    super();
    this.webhookUrl = options.webhookUrl || process.env.WEBHOOK_URL || null;
    this.emailEnabled = options.emailEnabled || (process.env.ENABLE_EMAIL_ALERTS === 'true');
    this.db = options.db; // simple file DB instance
  }

  async sendWebhook(alert) {
    if (!this.webhookUrl) return false;
    try {
      // Node 18+ has global fetch
      await fetch(this.webhookUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(alert),
        timeout: 5000,
      });
      return true;
    } catch (err) {
      console.warn('AlertService: webhook send failed', err && err.message);
      return false;
    }
  }

  async createAlert({ level = 'info', type = 'generic', message = '', meta = {} } = {}) {
    const alert = { level, type, message, meta };
    if (this.db) {
      try {
        this.db.insertAlert(alert);
      } catch (e) {
        console.warn('AlertService: failed to persist alert', e && e.message);
      }
    }
    // fire-and-forget webhook
    this.sendWebhook(alert).catch(() => {});
    this.emit('alert', alert);
    return alert;
  }
}

module.exports = AlertService;
