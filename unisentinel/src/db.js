const fs = require('fs');
const path = require('path');
const SqliteDB = require('./sqliteDb');

const DB_FILE = process.env.UNISENTINEL_DB_FILE || path.join(__dirname, '..', 'data', 'alerts.json');

function ensureDir(filePath) {
  const dir = path.dirname(filePath);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

// Backwards-compatible adapter factory: choose 'sqlite' or 'file' via env
function createDb() {
  const driver = (process.env.UNISENTINEL_DB_DRIVER || 'file').toLowerCase();
  if (driver === 'sqlite') {
    try {
      return new SqliteDB(process.env.UNISENTINEL_DB_FILE);
    } catch (e) {
      console.warn('Failed to open sqlite DB, falling back to file DB', e && e.message);
    }
  }

  // File-backed DB (fallback)
  const filePath = DB_FILE;
  ensureDir(filePath);
  class FileDB {
    constructor(filePathLocal = filePath) {
      this.filePath = filePathLocal;
      ensureDir(this.filePath);
      if (!fs.existsSync(this.filePath)) fs.writeFileSync(this.filePath, JSON.stringify({ alerts: [] }, null, 2));
    }

    read() {
      const raw = fs.readFileSync(this.filePath, 'utf8');
      try {
        return JSON.parse(raw);
      } catch (e) {
        console.warn('FileDB: failed to parse JSON, resetting');
        const init = { alerts: [] };
        fs.writeFileSync(this.filePath, JSON.stringify(init, null, 2));
        return init;
      }
    }

    write(data) {
      fs.writeFileSync(this.filePath, JSON.stringify(data, null, 2));
    }

    insertAlert(alert) {
      const db = this.read();
      alert.id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      alert.timestamp = new Date().toISOString();
      db.alerts.unshift(alert);
      // keep a reasonable history
      db.alerts = db.alerts.slice(0, 1000);
      this.write(db);
      return alert;
    }

    listAlerts(limit = 50) {
      const db = this.read();
      return db.alerts.slice(0, limit);
    }

    acknowledgeAlert(id) {
      const db = this.read();
      const idx = db.alerts.findIndex((a) => a.id === id);
      if (idx === -1) return false;
      db.alerts[idx].acknowledged = true;
      this.write(db);
      return true;
    }
  }

  return new FileDB(filePath);
}

module.exports = createDb;
