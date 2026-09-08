const { ethers } = require('ethers');
const EventEmitter = require('events');

class Web3Service extends EventEmitter {
  constructor(rpcUrl) {
    super();
    this.rpcUrl = rpcUrl;
    this.provider = null;
  }

  start() {
    if (!this.rpcUrl) {
      console.warn('Web3Service: no RPC URL configured, skipping provider startup');
      return;
    }
    try {
      this.provider = new ethers.JsonRpcProvider(this.rpcUrl);
      // Subscribe to new blocks — a small prototype for monitoring
      this.provider.on('block', (blockNumber) => this.emit('block', blockNumber));
      this.provider.getBlockNumber().then((n) => console.log('Web3Service connected, block', n)).catch(console.error);
    } catch (err) {
      console.error('Web3Service start error', err);
    }
  }

  stop() {
    if (this.provider && this.provider.removeAllListeners) {
      this.provider.removeAllListeners();
    }
  }
}

module.exports = Web3Service;
