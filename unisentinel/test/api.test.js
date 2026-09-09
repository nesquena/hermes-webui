const http = require('http');
const { expect } = require('chai');

// prefer sqlite in tests to avoid writing large JSON files
process.env.UNISENTINEL_DB_DRIVER = 'sqlite';
// ensure server is running
require('../src/server');

function httpGet(path, retries = 20, delay = 100) {
  return new Promise((resolve, reject) => {
    let attempt = 0;
    const tryReq = () => {
      attempt += 1;
      http.get({ hostname: 'localhost', port: 3000, path, agent: false }, (res) => {
        let body = '';
        res.on('data', (c) => body += c);
        res.on('end', () => resolve({ statusCode: res.statusCode, body }));
      }).on('error', (err) => {
        if (attempt >= retries) return reject(err);
        setTimeout(tryReq, delay);
      });
    };
    tryReq();
  });
}

function httpPost(path, json, retries = 20, delay = 100) {
  return new Promise((resolve, reject) => {
    let attempt = 0;
    const doReq = () => {
      attempt += 1;
      const data = JSON.stringify(json || {});
      const req = http.request({ hostname: 'localhost', port: 3000, path, method: 'POST', headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } }, (res) => {
        let body = '';
        res.on('data', (c) => body += c);
        res.on('end', () => resolve({ statusCode: res.statusCode, body }));
      });
      req.on('error', (e) => {
        if (attempt >= retries) return reject(e);
        setTimeout(doReq, delay);
      });
      req.write(data);
      req.end();
    };
    doReq();
  });
}

describe('API endpoints', function() {
  this.timeout(10000);

  it('lists alerts and can acknowledge an alert', async function() {
    const res = await httpGet('/api/alerts');
    expect(res.statusCode).to.equal(200);
    const parsed = JSON.parse(res.body);
    expect(parsed.alerts).to.be.an('array');
    if (parsed.alerts.length === 0) this.skip();
    const first = parsed.alerts[0];
    const ackRes = await httpGet(`/api/alerts/${first.id}/acknowledge`);
    expect(ackRes.statusCode).to.equal(200);
    const ackBody = JSON.parse(ackRes.body);
    expect(ackBody.ok).to.equal(true);
    expect(ackBody.alert).to.have.property('acknowledged');
    expect(ackBody.alert.acknowledged).to.equal(true);

    // verify persisted
    const after = await httpGet('/api/alerts');
    const afterParsed = JSON.parse(after.body);
    const found = afterParsed.alerts.find(a => a.id === first.id);
    expect(found).to.exist;
    expect(found.acknowledged).to.equal(true);
  });

  it('can add a monitored pair', async function() {
    const payload = { id: 'TEST-PAIR-1', pairAddress: null, tokenA: 'AAA', tokenB: 'BBB', threshold: 5 };
    const res = await httpPost('/api/pairs', payload);
    expect(res.statusCode).to.equal(201);
    const parsed = JSON.parse(res.body);
    expect(parsed.pair).to.include({ id: 'TEST-PAIR-1', tokenA: 'AAA', tokenB: 'BBB' });
  });
});
