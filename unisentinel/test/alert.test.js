const { expect } = require('chai');
const SqliteDB = require('../src/sqliteDb');
const AlertService = require('../src/alertService');

describe('AlertService persistence', function() {
  it('persists alerts to sqlite and lists them', async function() {
    // use in-memory sqlite for tests
    const db = new SqliteDB(':memory:');
    const alertSvc = new AlertService({ db });

    const created = await alertSvc.createAlert({ level: 'high', type: 'test', message: 'test alert', meta: { foo: 'bar' } });
    expect(created).to.have.property('id');
    expect(created).to.have.property('timestamp');

    const list = await db.listAlerts(10);
    expect(list).to.be.an('array');
    expect(list.length).to.be.greaterThan(0);
    const first = list[0];
    expect(first.type).to.equal('test');
    expect(first.meta).to.deep.equal({ foo: 'bar' });
  });
});
