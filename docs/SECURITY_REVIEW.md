# Independent Security Review Gate

No independent security review has been completed. Denarius must not describe
itself as production-ready or launch a mainnet until an unaffiliated reviewer
has assessed at least:

- Consensus validation, proof-of-work binding, difficulty adjustment, chainwork,
  timestamp rules, coinbase maturity, fees, and reorganizations.
- Transaction canonicalization, nonce handling, signature validation, replay
  resistance, amount bounds, mempool policy, and denial-of-service behavior.
- Incremental chain-state and undo correctness across crashes and deep reorgs.
- Browser wallet entropy, Web Crypto usage, encrypted backup compatibility,
  password handling, signing UX, recovery, and supply-chain exposure.
- Peer discovery, eclipse and Sybil resistance, scoring, durable bans, relay
  bounds, synchronization, and authenticated transport.
- Account authentication, session and CSRF controls, authorization boundaries,
  rate limits, reverse-proxy assumptions, logging, backup, and restoration.

Every high or critical finding must be fixed and retested. Medium findings need
a documented disposition. The final report, tested commit, reviewer identity,
scope exclusions, and remediation evidence belong in the release record.
