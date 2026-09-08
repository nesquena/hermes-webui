class SlippageService {
  constructor({ alertService } = {}) {
    this.alertService = alertService;
    this.pairs = [
      {
        id: 'WETH-USDC',
        tokenA: 'WETH',
        tokenB: 'USDC',
        currentPrice: 2820.42,
        previousPrice: 2804.9,
        threshold: 5,
        status: 'healthy',
        lastAlertAt: null,
      },
      {
        id: 'UNI-WETH',
        tokenA: 'UNI',
        tokenB: 'WETH',
        currentPrice: 0.0824,
        previousPrice: 0.0756,
        threshold: 6,
        status: 'warning',
        lastAlertAt: null,
      },
      {
        id: 'DAI-USDC',
        tokenA: 'DAI',
        tokenB: 'USDC',
        currentPrice: 1.003,
        previousPrice: 1.0002,
        threshold: 3,
        status: 'healthy',
        lastAlertAt: null,
      },
    ];
  }

  computeSlippage(pair) {
    const change = ((pair.currentPrice - pair.previousPrice) / pair.previousPrice) * 100;
    return Number(Math.abs(change).toFixed(3));
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

  scan() {
    const detections = [];
    for (const pair of this.pairs) {
      const slippage = this.computeSlippage(pair);
      const thresholdExceeded = slippage > pair.threshold;
      pair.status = thresholdExceeded ? 'warning' : 'healthy';
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
