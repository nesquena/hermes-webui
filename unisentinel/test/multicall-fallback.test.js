const { expect } = require('chai');
const sinon = require('sinon');
const SlippageService = require('../src/slippageService');

describe('SlippageService multicall fallback', function() {
  it('falls back to per-pair reads when multicall fails', async function() {
    const alertService = { createAlert: sinon.spy() };
    const multicallAddress = '0xMulti';

    // multicall contract throws
    const contractFactory = (address, abi, provider) => {
      if (address === multicallAddress) {
        return { aggregate: sinon.stub().rejects(new Error('multicall down')) };
      }
      return {
        getReserves: sinon.stub().resolves([100, 200, 0])
      };
    };

    const pairs = [ { id: 'p1', pairAddress: '0xPair1', pairType: 'v2' } ];
    const svc = new SlippageService({ provider: null, alertService, pairs, contractFactory, multicallAddress });
    await svc.updateAllPairs();
    const p = svc.pairs.find(p=>p.id==='p1');
    expect(p.currentPrice).to.equal(200/100);
    const m = svc.getMetrics();
    expect(m.multicallCalls).to.equal(1);
    expect(m.multicallFailures).to.equal(1);
  });

  it('handles malformed multicall return data gracefully', async function() {
    const alertService = { createAlert: sinon.spy() };
    const multicallAddress = '0xMulti';

    const badReturn = ['0xdeadbeef'];
    const contractFactory = (address, abi, provider) => {
      if (address === multicallAddress) {
        return { aggregate: sinon.stub().resolves([123, badReturn]) };
      }
      return {
        getReserves: sinon.stub().resolves([0,0,0])
      };
    };

    const pairs = [ { id: 'p2', pairAddress: '0xPair2', pairType: 'v2' } ];
    const svc = new SlippageService({ provider: null, alertService, pairs, contractFactory, multicallAddress });
    // should not throw
    await svc.updateAllPairs();
    const p = svc.pairs.find(p=>p.id==='p2');
    // malformed data leads to no update, price remains default 0
    expect(p.currentPrice).to.equal(0);
  });
});
