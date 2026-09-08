const ethers = require('ethers');

class SlippageService {
  constructor({ alertService, provider = null, pairs = [], contractFactory = null } = {}) {
    this.alertService = alertService;
    this.provider = provider;
    this.contractFactory = contractFactory;

    // allow external configuration of pairs (id, pairAddress, threshold)
    if (pairs && pairs.length > 0) {
      this.pairs = pairs.map((p) => ({
        id: p.id,
        pairAddress: p.pairAddress || null,
        tokenA: p.tokenA || null,
        tokenB: p.tokenB || null,
        currentPrice: p.currentPrice || 0,
        previousPrice: p.previousPrice || 1,
        threshold: p.threshold || 5,
        status: 'unknown',
        lastAlertAt: null,
      }));
    } else {
      // fallback sample pairs
      this.pairs = [
        { id: 'WETH-USDC', pairAddress: null, tokenA: 'WETH', tokenB: 'USDC', currentPrice: 2820.42, previousPrice: 2804.9, threshold: 5, status: 'healthy', lastAlertAt: null },
        { id: 'UNI-WETH', pairAddress: null, tokenA: 'UNI', tokenB: 'WETH', currentPrice: 0.0824, previousPrice: 0.0756, threshold: 6, status: 'warning', lastAlertAt: null },
        { id: 'DAI-USDC', pairAddress: null, tokenA: 'DAI', tokenB: 'USDC', currentPrice: 1.003, previousPrice: 1.0002, threshold: 3, status: 'healthy', lastAlertAt: null },
      ];
    }

    // Minimal UniswapV2 pair ABI for on-chain reserve reads
    this.pairAbi = [
      'function getReserves() view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)',
      'function token0() view returns (address)',
      'function token1() view returns (address)'
    ];

    // Uniswap V3 minimal ABI (slot0 + token0/token1)
    this.v3PoolAbi = [
      'function slot0() view returns (uint160 sqrtPriceX96, int24 tick, uint16 observationIndex, uint16 observationCardinality, uint16 observationCardinalityNext, uint8 feeProtocol, bool unlocked)',
      'function token0() view returns (address)',
      'function token1() view returns (address)'
    ];
  }

  computeSlippage(pair) {
    // avoid division by zero
    if (!pair.previousPrice || pair.previousPrice === 0) return 0;
    const change = ((pair.currentPrice - pair.previousPrice) / pair.previousPrice) * 100;
    return Number(Math.abs(change).toFixed(3));
  }

  async updateOnChainPrices() {
    if (!this.provider) return;
    for (const pair of this.pairs.filter(p => !p.pairType || p.pairType === 'v2')) {
      if (!pair.pairAddress) continue;
      try {
        const contract = this.contractFactory ? this.contractFactory(pair.pairAddress, this.pairAbi, this.provider) : new ethers.Contract(pair.pairAddress, this.pairAbi, this.provider);
        const [reserve0, reserve1] = await contract.getReserves();
        // determine token0/token1 order to compute price as token1 per token0
        const token0 = await contract.token0();
        const token1 = await contract.token1();
        // compute price: price of token0 in terms of token1 = reserve1 / reserve0
        const r0 = Number(reserve0.toString());
        const r1 = Number(reserve1.toString());
        if (r0 > 0) {
          const price = r1 / r0;
          pair.previousPrice = pair.currentPrice || price;
          pair.currentPrice = price;
          pair.status = 'updated';
        }
      } catch (e) {
        console.warn('SlippageService: on-chain price update failed for', pair.id, e && e.message);
      }
    }

    // support Uniswap V3 pools when pair.pairType === 'v3'
    for (const pair of this.pairs.filter(p => p.pairType === 'v3' && p.pairAddress)) {
      try {
        const contract = this.contractFactory ? this.contractFactory(pair.pairAddress, this.v3PoolAbi, this.provider) : new ethers.Contract(pair.pairAddress, this.v3PoolAbi, this.provider);
        const slot0 = await contract.slot0();
        // slot0.sqrtPriceX96 is BigInt-like; use precise conversion helper
        const { sqrtPriceX96ToNumber } = require('./v3Math');
        const sqrtPriceVal = slot0[0].toString ? slot0[0].toString() : slot0.sqrtPriceX96.toString();
        const approx = sqrtPriceX96ToNumber(sqrtPriceVal, 12);
        pair.previousPrice = pair.currentPrice || approx;
        pair.currentPrice = approx;
        // also keep string form for higher precision storage
        pair.currentPriceString = require('./v3Math').sqrtPriceX96ToPriceString(sqrtPriceVal, 18);
        pair.status = 'updated';
      } catch (e) {
        console.warn('SlippageService: v3 on-chain price update failed for', pair.id, e && e.message);
      }
    }
  }

  async updateAllPairs() {
    await this.updateOnChainPrices();
    return this.pairs;
  }

  listPairs() {
    return this.pairs.map((pair) => ({
      ...pair,
      slippage: this.computeSlippage(pair),
    }));
  }

  getPairById(id) {
    return this.listPairs().find((pair) => pair.id === id) || null;
  }

  addPair({ id, pairAddress = null, tokenA = null, tokenB = null, token0 = null, token1 = null, pairType = 'v2', threshold = 5 } = {}) {
    if (!id) throw new Error('pair id required');
    const exists = this.pairs.find((p) => p.id === id);
    if (exists) return exists;
    const entry = {
      id,
      pairAddress,
      pairType,
      tokenA,
      tokenB,
      token0,
      token1,
      currentPrice: 0,
      previousPrice: 1,
      threshold,
      status: 'unknown',
      lastAlertAt: null,
    };
    this.pairs.push(entry);
    return entry;
  }

  updateThreshold(id, threshold) {
    const pair = this.pairs.find((p) => p.id === id);
    if (!pair) return null;
    pair.threshold = threshold;
    return pair;
  }

  async scan() {
    // try on-chain price update first
    if (this.provider) {
      await this.updateOnChainPrices().catch(() => {});
    }

    const detections = [];
    for (const pair of this.pairs) {
      const slippage = this.computeSlippage(pair);
      const thresholdExceeded = slippage > pair.threshold;
      pair.status = thresholdExceeded ? 'warning' : (pair.status === 'updated' ? 'healthy' : pair.status);
      if (thresholdExceeded) {
        detections.push({
          pairId: pair.id,
          slippage,
          threshold: pair.threshold,
          message: `${pair.id} exceeded slippage threshold (${slippage}% > ${pair.threshold}%)`,
        });

        if (this.alertService && !pair.lastAlertAt) {
          this.alertService.createAlert({
            level: 'medium',
            type: 'slippage',
            message: `${pair.id} exceeded slippage threshold (${slippage}% > ${pair.threshold}%)`,
            meta: { pair: pair.id, slippage, threshold: pair.threshold },
          }).catch(() => {});
          pair.lastAlertAt = new Date().toISOString();
        }
      }
    }
    return { detections, pairs: this.listPairs() };
  }
}

module.exports = SlippageService;
