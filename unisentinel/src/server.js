const express = require('express');
const dotenv = require('dotenv');
const path = require('path');
const Web3Service = require('./web3Service');
const FileDB = require('./db');
const AlertService = require('./alertService');
const GovernanceService = require('./governanceService');
const SlippageService = require('./slippageService');

dotenv.config();

const app = express();
const port = process.env.PORT || 3000;
const db = new FileDB(process.env.UNISENTINEL_DB_FILE);
const alerts = new AlertService({ db, webhookUrl: process.env.WEBHOOK_URL });
const governance = new GovernanceService();
const slippage = new SlippageService({ alertService: alerts });

function seedDefaultAlerts() {
  const current = db.listAlerts();
  if (current.length > 0) return;

  db.insertAlert({
    level: 'info',
    type: 'governance',
    message: 'GovernorBravo proposal queue refreshed',
    meta: { proposal: 'UNI-42', status: 'active' },
  });

  db.insertAlert({
    level: 'medium',
    type: 'slippage',
    message: 'WETH/USDC pool moved 8.4% above threshold',
    meta: { pair: 'WETH/USDC', slippage: 8.4 },
  });
}

seedDefaultAlerts();

app.use(express.static(path.join(__dirname, '..', 'public')));
app.get('/health', (req, res) => {
  res.json({ status: 'ok', uptime: process.uptime(), rpc: !!process.env.ETHEREUM_RPC_URL });
});

app.get('/api/stats', (req, res) => {
  const list = db.listAlerts(50);
  const pairs = slippage.listPairs();
  const proposals = governance.listProposals();
  res.json({
    watchers: 12,
    activeProposals: proposals.filter((p) => p.status === 'active').length,
    monitoredPairs: pairs.length,
    alertsToday: list.length,
    rpcStatus: process.env.ETHEREUM_RPC_URL ? 'online' : 'offline',
  });
});

app.get('/api/alerts', (req, res) => {
  const list = db.listAlerts(50);
  res.json({ alerts: list });
});

app.get('/api/alerts/:id/acknowledge', (req, res) => {
  const list = db.listAlerts(1000);
  const alert = list.find((entry) => entry.id === req.params.id);
  if (!alert) {
    return res.status(404).json({ error: 'alert not found' });
  }
  alert.acknowledged = true;
  const data = { alerts: list.filter((entry) => entry.id !== alert.id).concat(alert) };
  db.write(data);
  res.json({ ok: true, alert });
});

app.get('/api/governance/proposals', (req, res) => {
  res.json({ proposals: governance.listProposals() });
});

app.get('/api/governance/proposals/:id', (req, res) => {
  const proposal = governance.getProposalById(req.params.id);
  if (!proposal) return res.status(404).json({ error: 'proposal not found' });
  res.json({ proposal });
});

app.get('/api/pairs', (req, res) => {
  res.json({ pairs: slippage.listPairs() });
});

app.get('/api/pairs/:id', (req, res) => {
  const pair = slippage.getPairById(req.params.id);
  if (!pair) return res.status(404).json({ error: 'pair not found' });
  res.json({ pair });
});

const providerUrl = process.env.ETHEREUM_RPC_URL;
const web3 = new Web3Service(providerUrl);
web3.start();
web3.on('block', async (bn) => {
  console.log(`New block ${bn}`);
  const level = bn % 2 === 0 ? 'info' : 'medium';
  await alerts.createAlert({
    level,
    type: 'block',
    message: `New Ethereum block ${bn} observed`,
    meta: { block: bn },
  });
});

setInterval(() => {
  const result = slippage.scan();
  if (result.detections.length > 0) {
    console.log('Slippage detection:', result.detections);
  }
}, 20000);

app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, '..', 'public', 'index.html'));
});

app.listen(port, () => {
  console.log(`UniSentinel listening on http://localhost:${port}`);
});
