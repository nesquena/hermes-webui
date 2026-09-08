class GovernanceService {
  constructor() {
    this.proposals = [
      {
        id: 'UNI-42',
        title: 'Treasury diversification and liquidity incentives',
        status: 'active',
        votesFor: 61.2,
        votesAgainst: 18.4,
        quorum: 74,
        proposer: '0xA1B2...C3D4',
        endTime: '2026-09-15T18:00:00Z',
        summary: 'Expand treasury risk controls and improve liquidity incentives for stable pool deployments.',
      },
      {
        id: 'UNI-43',
        title: 'Fee switch implementation review',
        status: 'queued',
        votesFor: 33.7,
        votesAgainst: 16.6,
        quorum: 52,
        proposer: '0xD4E5...F6A7',
        endTime: '2026-09-18T18:00:00Z',
        summary: 'Review fee splitting and governance step-ups for v3 ecosystem fee routing.',
      },
      {
        id: 'UNI-44',
        title: 'Emergency budget rebalancing',
        status: 'executed',
        votesFor: 77.5,
        votesAgainst: 8.1,
        quorum: 89,
        proposer: '0xE7F8...A9B0',
        endTime: '2026-09-02T12:00:00Z',
        summary: 'Rebalanced grants and emergency operational reserves for migration support.',
      },
    ];
  }

  listProposals() {
    return this.proposals.map((proposal) => ({ ...proposal }));
  }

  getProposalById(id) {
    return this.proposals.find((proposal) => proposal.id === id) || null;
  }
}

module.exports = GovernanceService;
