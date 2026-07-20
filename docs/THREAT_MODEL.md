# Denarius Threat Model

## Assets

Denarius must protect private keys, signed transactions, spendable balances,
consensus history, node administration, account credentials, and operator
availability. Loss of an encrypted wallet or its password may permanently make
funds unavailable.

## Trust boundaries

- Wallet software must not trust a node or console with private key material.
- Nodes must treat every transaction, block, peer response, and advertised peer
  address as hostile input.
- Console users must not inherit node-administrator authority.
- Local databases may be corrupted by crashes, storage failure, rollback, or
  unauthorized modification.
- Release artifacts and dependency updates are part of the security boundary.

## Primary adversaries

- A miner with majority or burst hash power attempting reorganizations,
  double-spends, timestamp manipulation, or accelerated issuance.
- A Sybil operator attempting to eclipse nodes with attacker-controlled peers.
- A remote client exhausting CPU, memory, disk, sockets, worker threads, or
  mempool capacity.
- A malicious peer returning inconsistent headers, blocks, peer tables, or
  protocol metadata.
- A compromised console origin attempting to read passwords or private keys.
- An attacker with a stolen account password or administration token.
- A compromised build dependency or release account.

## Required security properties

- Consensus validity is deterministic and independent of local configuration.
- Routine block acceptance is incremental and bounded by the new block, not by
  total chain history.
- Chain state and its undo information commit atomically with each accepted
  block.
- Reorganizations preserve valid disconnected transactions and cannot spend
  immature mining rewards.
- Wallet keys are generated, encrypted, decrypted, and used for signing only on
  the user's device.
- Peer work is bounded, asynchronous, scored, and diversified across network
  groups.
- Public endpoints have request limits, rate limits, and observable failure
  behavior.
- Backups are restorable and releases are reproducible enough to audit.

## Explicit non-goals for Testnet v3

Testnet v3 does not promise economic finality, resistance to majority hash
power, anonymous network transport, or recovery of lost wallet secrets. It must
not carry funds represented as real value.

## Mainnet launch gates

- Independent consensus, cryptography, wallet, and web-security reviews have no
  unresolved critical or high-severity findings.
- A public adversarial testnet completes long-chain, reorganization, crash,
  restore, eclipse, and sustained-load exercises.
- The production consensus-security source is measured and independently
  operated; assumed hash power is not sufficient.
- Wallet signing is local-only and backup recovery is tested across supported
  browsers or wallet applications.
- Operators have monitoring, alerting, backup, restore, incident response, and
  emergency upgrade procedures.
