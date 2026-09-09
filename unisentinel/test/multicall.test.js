const { expect } = require('chai');
const sinon = require('sinon');
const ethers = require('ethers');

const SlippageService = require('../src/slippageService');

describe('SlippageService multicall batching', function() {
  it('uses multicall aggregate to fetch multiple v2 pair results', async function() {
    const alertService = { createAlert: sinon.spy() };

    // prepare fake return data encoded as multicall would return
    const pairAbi = [
      'function getReserves() view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)',
      'function token0() view returns (address)',
      'function token1() view returns (address)'
    ];
    const ifacePair = new ethers.Interface(pairAbi);

    // we'll create two pairs
    const p1Res = ifacePair.encodeFunctionResult('getReserves', ['1000', '500', 0]);
    const p1T0 = ifacePair.encodeFunctionResult('token0', ['0x0000000000000000000000000000000000000001']);
    const p1T1 = ifacePair.encodeFunctionResult('token1', ['0x0000000000000000000000000000000000000002']);

    const p2Res = ifacePair.encodeFunctionResult('getReserves', ['400', '800', 0]);
    const p2T0 = ifacePair.encodeFunctionResult('token0', ['0x0000000000000000000000000000000000000011']);
    const p2T1 = ifacePair.encodeFunctionResult('token1', ['0x0000000000000000000000000000000000000012']);

    const returnData = [p1Res, p1T0, p1T1, p2Res, p2T0, p2T1];

    // contractFactory: return a fake multicall contract when multicall address requested
    const multicallAddress = '0xMulticall';
    const contractFactory = (address, abi, provider) => {
      if (address === multicallAddress) {
        return {
          aggregate: sinon.stub().resolves([123, returnData])
        };
      }
      // shouldn't be called for pair contracts in multicall path; provide fallback
      return {
        getReserves: sinon.stub().resolves([0,0,0]),
        token0: sinon.stub().resolves('0x0'),
        token1: sinon.stub().resolves('0x0')
      };
    };

    const pairs = [
      { id: 'p1', pairAddress: '0xPair1', pairType: 'v2' },
      { id: 'p2', pairAddress: '0xPair2', pairType: 'v2' }
    ];

    const svc = new SlippageService({ provider: null, alertService, pairs, contractFactory, multicallAddress });
    await svc.updateAllPairs();

    const p1 = svc.pairs.find(p=>p.id==='p1');
    const p2 = svc.pairs.find(p=>p.id==='p2');

    expect(p1.currentPrice).to.equal(500/1000);
    expect(p2.currentPrice).to.equal(800/400);
  });
});
