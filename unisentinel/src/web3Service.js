const { ethers } = require('ethers');
const EventEmitter = require('events');

// Web3Service with basic failover and circuit-breaker behavior using ethers' FallbackProvider
class Web3Service extends EventEmitter {
  constructor(rpcUrlOrList) {
    super();
    // allow passing CSV or array of urls
    this.rpcUrls = [];
    if (!rpcUrlOrList) {
      this.rpcUrls = [];
    } else if (Array.isArray(rpcUrlOrList)) {
      this.rpcUrls = rpcUrlOrList;
    } else if (typeof rpcUrlOrList === 'string') {
      this.rpcUrls = rpcUrlOrList.split(',').map((s) => s.trim()).filter(Boolean);
    }

    this.provider = null;
    this.failureCounts = new Map();
    this.maxFailures = Number(process.env.RPC_MAX_FAILURES || 3);
    this.circuitTimeoutMs = Number(process.env.RPC_CIRCUIT_TIMEOUT_MS || 60_000);
    this.blackoutUntil = new Map();
  }

  _providerForUrl(url) {
    const p = new ethers.JsonRpcProvider(url);
    // instrument errors to track failing endpoints
    p._url = url;
    p.on('error', (err) => this._recordFailure(url, err));
    return p;
  }

  _recordFailure(url, err) {
    const now = Date.now();
    const count = (this.failureCounts.get(url) || 0) + 1;
    this.failureCounts.set(url, count);
    if (count >= this.maxFailures) {
      this.blackoutUntil.set(url, now + this.circuitTimeoutMs);
      console.warn(`Web3Service: blacklisting ${url} for ${this.circuitTimeoutMs}ms after ${count} failures`);
    }
  }

  _activeProviders() {
    const now = Date.now();
    const providers = [];
    for (const url of this.rpcUrls) {
      const black = this.blackoutUntil.get(url) || 0;
      if (black > now) continue;
      try {
        providers.push(this._providerForUrl(url));
      } catch (e) {
        console.warn('Web3Service: failed to construct provider for', url, e && e.message);
      }
    }
    return providers;
  }

  start() {
    if (!this.rpcUrls || this.rpcUrls.length === 0) {
      console.warn('Web3Service: no RPC URL(s) configured, skipping provider startup');
      return;
    }
    try {
      const providers = this._activeProviders();
      if (providers.length === 0) {
        console.warn('Web3Service: no healthy providers available at start');
        return;
      }
      // Use ethers FallbackProvider to manage multiple JSON-RPC endpoints and failover
      this.provider = new ethers.FallbackProvider(providers);

      // subscribe to block events via polling fallback (FallbackProvider forwards events)
      this.provider.on('block', (blockNumber) => this.emit('block', blockNumber));
      this.provider.getBlockNumber().then((n) => console.log('Web3Service connected, block', n)).catch(console.error);

      // periodic check to rebuild provider if endpoints were blacklisted and timeout passed
      this._rebuildInterval = setInterval(() => {
        const currentProviders = this._activeProviders();
        if (currentProviders.length > 0) {
          try {
            this.provider = new ethers.FallbackProvider(currentProviders);
          } catch (e) {
            console.warn('Web3Service: rebuild provider failed', e && e.message);
          }
        }
      }, Math.max(10_000, this.circuitTimeoutMs / 2));
    } catch (err) {
      console.error('Web3Service start error', err);
    }
  }

  stop() {
    if (this.provider && this.provider.removeAllListeners) {
      try { this.provider.removeAllListeners(); } catch (e) {}
    }
    if (this._rebuildInterval) clearInterval(this._rebuildInterval);
  }
}

module.exports = Web3Service;
