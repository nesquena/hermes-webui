const Database = require('sqlite3').verbose();
const path = require('path');

class SqliteDB {
  constructor(dbPath) {
    this.dbPath = dbPath || path.join(__dirname, '..', 'data', 'unisentinel.db');
    this.db = new Database.Database(this.dbPath);
    this._init();
  }

  _init() {
    const create = `CREATE TABLE IF NOT EXISTS alerts (
      id TEXT PRIMARY KEY,
      level TEXT,
      type TEXT,
      message TEXT,
      meta TEXT,
      timestamp TEXT,
      acknowledged INTEGER DEFAULT 0
    )`;
    this.db.serialize(() => {
      this.db.run(create);
      this.db.run('CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp)');
      this.db.run('CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(type)');
    });
  }

  insertAlert(alert) {
    const id = alert.id || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const timestamp = alert.timestamp || new Date().toISOString();
    const stmt = this.db.prepare('INSERT INTO alerts (id, level, type, message, meta, timestamp, acknowledged) VALUES (?, ?, ?, ?, ?, ?, ?)');
    stmt.run(id, alert.level, alert.type, alert.message, JSON.stringify(alert.meta || {}), timestamp, alert.acknowledged ? 1 : 0);
    stmt.finalize();
    return { ...alert, id, timestamp };
  }

  listAlerts(limit = 50) {
    const sql = 'SELECT id, level, type, message, meta, timestamp, acknowledged FROM alerts ORDER BY timestamp DESC LIMIT ?';
    return new Promise((resolve, reject) => {
      this.db.all(sql, [limit], (err, rows) => {
        if (err) return reject(err);
        const mapped = rows.map((r) => ({
          id: r.id,
          level: r.level,
          type: r.type,
          message: r.message,
          meta: JSON.parse(r.meta || '{}'),
          timestamp: r.timestamp,
          acknowledged: !!r.acknowledged,
        }));
        resolve(mapped);
      });
    });
  }

  write() {
    // no-op compatibility method for FileDB interchange
    return true;
  }

  acknowledgeAlert(id) {
    const sql = 'UPDATE alerts SET acknowledged = 1 WHERE id = ?';
    return new Promise((resolve, reject) => {
      this.db.run(sql, [id], function (err) {
        if (err) return reject(err);
        resolve(this.changes > 0);
      });
    });
  }
}

module.exports = SqliteDB;
