# Denarius Consensus

## Current network

Denarius protocol version 3 is a public test network named
`denarius-testnet-v3`. It is not a mainnet and DEN on this network must not be
represented as having monetary value.

The network uses standalone SHA-256 proof of work. This preserves the existing
Denarius design while its consensus engine, chain state, wallet, networking,
and operations are hardened. It is not an assertion that standalone proof of
work is secure enough for a future production network.

## Production decision gate

A production genesis block must not be created until one of these paths has an
independent security review:

1. Standalone proof of work with measured, independently operated hash power
   sufficient for the documented reorganization threat model.
2. Merge mining with a named parent network and a reviewed auxiliary-proof
   protocol.

Validator and federated consensus are not current Denarius goals. Adopting
either would require a new architecture decision, protocol specification,
network identifier, and genesis block.

## Compatibility

Consensus is identified by the protocol version, network identifier, and
genesis hash. A node must reject peers or databases that differ on any of these
values.

Consensus upgrades activate at deterministic block heights listed in
`CONSENSUS_UPGRADES`. Wall-clock activation, local feature flags, and operator
configuration must never change block or transaction validity.

Protocol version 2 databases belong to the retired demonstration chain. They
are intentionally not migrated because version 3 creates a new consensus
history. Operators should archive old databases and allow Denarius to create
`states/denarius-testnet-v3.db`.

## Current monetary constants

- Atomic units: `100,000,000` per DEN
- Maximum scheduled supply: `100,000,000 DEN`
- Target block interval: two minutes
- Subsidy halving interval: `1,051,200` blocks
- Initial subsidy: `47.56468797 DEN`
- Minimum transaction fee: `0.0001 DEN`
- Coinbase maturity: `10` blocks
- Testnet initial proof target: approximately one accepted hash per `4,096`
  attempts

Every signed transaction commits to its fee. A block coinbase must equal the
scheduled subsidy plus the fees of its non-coinbase transactions. Subsidies and
collected fees remain immature for ten blocks. The node enforces global and
per-sender mempool limits in addition to the fee floor.

## Reorganizations

Nodes select the fully validated compatible chain with the greatest exact
accumulated proof of work. Equal-work candidates do not replace the local
chain. A replacement is validated from genesis before activation, indexed state
is rebuilt, and valid transactions disconnected from the former branch are
reconsidered for the mempool. Testnet v3 has no checkpoint or maximum reorg
depth; production finality and emergency recovery policy require independent
review before launch.

Testnet difficulty is calibrated for local and small multi-node testing, not
economic security. A production initial target and adjustment algorithm require
measured hash-rate data and independent review.
