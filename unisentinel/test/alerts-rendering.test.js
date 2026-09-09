const { expect } = require('chai');
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

describe('Alerts UI rendering', function () {
  it('renders persisted alert content as text and does not execute/script-insert payloads', async function () {
    // Load the client script
    const appJs = fs.readFileSync(path.join(__dirname, '..', 'public', 'app.js'), 'utf8');

    // Prepare a malicious-looking alert payload (simulates stored XSS vector)
    const payloadMessage = "Pool <img src=x onerror=alert('xss')> attacked";
    const payload = {
      alerts: [
        {
          message: payloadMessage,
          type: 'slippage',
          timestamp: Date.now(),
          level: 'high'
        }
      ]
    };

    // Create an HTML document with expected elements
    const dom = new JSDOM(`<!doctype html>
      <html><body>
      <div id="stat-pairs"></div>
      <div id="stat-proposals"></div>
      <div id="stat-alerts"></div>
      <div id="stat-rpc"></div>
      <ul id="alert-list"></ul>
      </body></html>`, { runScripts: 'dangerously', resources: 'usable' });

    // Install a fetch mock into the window before running the script
    dom.window.fetch = (url) => {
      if (url === '/api/alerts') {
        return Promise.resolve({ json: () => Promise.resolve(payload) });
      }
      if (url === '/api/stats') {
        return Promise.resolve({ json: () => Promise.resolve({ monitoredPairs: 0, activeProposals: 0, alertsToday: 0, rpcStatus: 'ok' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({}) });
    };

    // Evaluate the app.js in the JSDOM window context
    const scriptEl = dom.window.document.createElement('script');
    scriptEl.textContent = appJs;
    dom.window.document.body.appendChild(scriptEl);

    // Wait for the async IIFE in app.js to run (loadAlerts/loadStats)
    await new Promise((resolve) => setTimeout(resolve, 100));

    const list = dom.window.document.getElementById('alert-list');
    expect(list).to.exist;
    const item = list.querySelector('.alert-item');
    expect(item).to.exist;

    // The message must be present as text, and no <script> nodes should be inserted
    expect(item.textContent).to.include('Pool');
    expect(item.textContent).to.include("attacked");

    const scriptNode = item.querySelector('script');
    expect(scriptNode).to.be.null;

    // Ensure the dangerous attribute was not interpreted as HTML
    expect(item.innerHTML).to.not.include("onerror");
  });
});
