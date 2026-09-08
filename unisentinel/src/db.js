const fs = require('fs');
const path = require('path');

const DB_FILE = process.env.UNISENTINEL_DB_FILE || path.join(__dirname, '..', 'data', 'alerts.json');

function ensureDir(filePath) {
  const dir = path.dirname(filePath);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

class FileDB {
  constructor(filePath = DB_FILE) {
    this.filePath = filePath;
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
}

module.exports = FileDB;
