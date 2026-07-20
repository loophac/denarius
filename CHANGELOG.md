# Changelog

All notable project changes are recorded here. Denarius follows semantic
versioning for application releases; consensus and peer protocol versions are
tracked separately in `denarius_protocol.py`.

## [Unreleased]

### Added

- Added administrator-controlled node-side automining with clean cancellation,
  live Overview status, and an explicit start/stop control below manual mining.
- Hardened Waitress serving, request rate limits, secure session defaults,
  request IDs, JSON logs, health/readiness endpoints, protected Prometheus
  metrics, verified online backup/restore tooling, and incident runbooks.
- Configurable verified HTTPS peer transport, persistent peer penalties,
  discovery diversity controls, and explicit network security guidance.
- Generated property tests, malformed-input fuzzing, SQLite crash tests, backup
  tamper tests, a long-chain benchmark gate, an opt-in synchronization soak,
  and an independent security review gate.

### Changed

- Began Phase 6 by replacing the premature mainnet identifier with
  `denarius-testnet-v3`, protocol version 3, and a new canonical genesis block.
- Bound SQLite state to the network identifier and genesis hash and moved the
  default node database to `states/denarius-testnet-v3.db`.
- Documented the standalone proof-of-work decision, threat model, deterministic
  upgrade activation, and production launch gates.
- Added minimum signed transaction fees, coinbase maturity, bounded mempool
  policy, indexed balances/nonces/transaction IDs/chainwork, per-block SQLite
  commits, and persisted reorganization undo records.
- Moved wallet generation, encryption, decryption, and signing entirely to Web
  Crypto in the user's browser and removed Python private-key wallet operations.

### Compatibility

- Protocol version 2 databases remain valid records of the retired
  demonstration chain but cannot be loaded into Testnet v3. No automatic
  consensus-history migration is provided.
- Server-generated wallet format 1 is retired with the old chain. Testnet v3
  uses browser wallet format 2; old backups are not imported into the console.

## [0.5.0] - 2026-07-20

### Added

- Full-block proof commitments, replay protection, canonical headers,
  deterministic targets, account nonces, transaction IDs, and strict block
  validation.
- SQLite persistence for chain, mempool, peer, miner, and console account state.
- A unified Denarius Console with encrypted browser-scoped wallets and role-based
  administrator and standard-user access.
- Deduplicated relay, peer compatibility checks, health tracking, background
  synchronization, and header-first chain resolution.
- Cross-platform process integration tests, pinned dependencies, Python package
  metadata, Windows/Linux CI, contributor guidance, and a security policy.

### Compatibility

- Protocol version 2 state remains current. Phase 5 does not change consensus
  and does not require a chain reset.
