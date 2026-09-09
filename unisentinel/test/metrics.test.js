const http = require('http');
const { expect } = require('chai');

// prefer sqlite in tests to avoid writing large JSON files
process.env.UNISENTINEL_DB_DRIVER = 'sqlite';
// require server to start it (server main() auto-invokes)
require('../src/server');

function fetchMetrics(retries = 20, delay = 100) {
  return new Promise((resolve, reject) => {
    let attempt = 0;
    const tryFetch = () => {
      attempt += 1;
      http.get('http://localhost:3000/metrics', (res) => {
        let body = '';
        res.on('data', (c) => body += c);
        res.on('end', () => resolve({ statusCode: res.statusCode, body }));
      }).on('error', (err) => {
        if (attempt >= retries) return reject(err);
        setTimeout(tryFetch, delay);
      });
    };
    tryFetch();
  });
}

describe('/metrics endpoint', function() {
  this.timeout(10000);

  it('responds with Prometheus-style metrics', async function() {
    const res = await fetchMetrics(40, 100);
    expect(res.statusCode).to.equal(200);
    expect(res.body).to.include('unisentinel_slippage_multicall_calls');
    expect(res.body).to.include('unisentinel_slippage_single_calls');
  });
});
