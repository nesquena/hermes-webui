const express = require('express');
const dotenv = require('dotenv');
const Web3Service = require('./web3Service');

dotenv.config();

const app = express();
const port = process.env.PORT || 3000;

// Basic health and alerts endpoints (stubs for early work)
app.get('/health', (req, res) => {
  res.json({ status: 'ok', uptime: process.uptime() });
});

app.get('/api/alerts', (req, res) => {
  res.json({ alerts: [] });
});

const FileDB = require('./db');
const AlertService = require('./alertService');

const providerUrl = process.env.ETHEREUM_RPC_URL;
const web3 = new Web3Service(providerUrl);
const db = new FileDB(process.env.UNISENTINEL_DB_FILE);
const alerts = new AlertService({ db, webhookUrl: process.env.WEBHOOK_URL });

web3.start();
web3.on('block', async (bn) => {
  console.log(`New block ${bn}`);
  // Example: create a low-severity heartbeat alert every new block (placeholder)
  await alerts.createAlert({ level: 'info', type: 'block', message: `New block ${bn}`, meta: { block: bn } });
});

app.get('/api/alerts', (req, res) => {
  const list = db.listAlerts(50);
  res.json({ alerts: list });
});

app.listen(port, () => {
  console.log(`UniSentinel listening on http://localhost:${port}`);
});
