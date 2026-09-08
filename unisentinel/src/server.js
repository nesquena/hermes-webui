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

const providerUrl = process.env.ETHEREUM_RPC_URL;
const web3 = new Web3Service(providerUrl);
web3.start();
web3.on('block', (bn) => console.log(`New block ${bn}`));

app.listen(port, () => {
  console.log(`UniSentinel listening on http://localhost:${port}`);
});
