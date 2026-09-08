const { expect } = require('chai');
const sinon = require('sinon');
const ethers = require('ethers');

const SlippageService = require('../src/slippageService');

describe('SlippageService (unit)', function() {
  it('computes v2 price from reserves', async function() {
    const fakeProvider = {};
    const alertService = { createAlert: sinon.spy() };
    const svc = new SlippageService({ provider: fakeProvider, alertService, pairs: [] });

    // stub ethers.Contract for a specific address
    const contractStub = {
      getReserves: sinon.stub().resolves([1000, 500, 0]),
      token0: sinon.stub().resolves('0xToken0'),
      token1: sinon.stub().resolves('0xToken1')
    };

    svc.contractFactory = (address, abi, provider) => contractStub;

    svc.addPair({ id: 'p1', pairAddress: '0xPair', pairType: 'v2', token0: '0xToken0', token1: '0xToken1' });
    await svc.updateAllPairs();

    expect(svc.pairs.find(p=>p.id==='p1').currentPrice).to.equal(500/1000);

    sinon.restore();
  });

  it('computes v3 price from slot0 sqrtPriceX96', async function() {
    const fakeProvider = {};
    const alertService = { createAlert: sinon.spy() };
    const svc = new SlippageService({ provider: fakeProvider, alertService, pairs: [] });

    const slot0 = [
      // sqrtPriceX96 ~ 79228162514264337593543950336 (2**96)
      { toString: () => '79228162514264337593543950336' },
      0,0,0,0,0,false
    ];

    const contractStub = {
      slot0: sinon.stub().resolves(slot0),
      token0: sinon.stub().resolves('0xToken0'),
      token1: sinon.stub().resolves('0xToken1')
    };

    svc.contractFactory = (address, abi, provider) => contractStub;

    svc.addPair({ id: 'p2', pairAddress: '0xPool', pairType: 'v3', token0: '0xToken0', token1: '0xToken1' });
    await svc.updateAllPairs();

    const p = svc.pairs.find(p=>p.id==='p2');
    expect(p.currentPrice).to.be.a('number');
    expect(p.currentPrice).to.be.closeTo(1.0, 0.1);

    sinon.restore();
  });
});
