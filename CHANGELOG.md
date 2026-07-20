# Changelog

All notable project changes are recorded here. Denarius follows semantic
versioning for application releases; consensus and peer protocol versions are
tracked separately in `denarius_protocol.py`.

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
