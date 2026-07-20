# Denarius Network

Denarius is an educational proof-of-work blockchain prototype and the conceptual
foundation for a lightweight cryptocurrency. It is intentionally compact, but it
should still be treated as demonstration software rather than production money.

The active network is `denarius-testnet-v3`. Testnet DEN has no represented
monetary value. Denarius does not currently publish or operate a mainnet; see
[docs/CONSENSUS.md](docs/CONSENSUS.md) and
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the Phase 6 production gates.

This work is based on :

- [adilmoujahid/blockchain-python-tutorial](https://github.com/adilmoujahid/blockchain-python-tutorial)  
- [asuith/blockchain-in-python](https://github.com/asuith/blockchain-in-python)


## Novel Features from asuith/blockchain-in-python

Compared with the original one, we now introduce:

- Denarii (coin name).
- Constant wealth (`1e8` coin in total).
- Setting miner's information.
- Administrator-controlled automining with live console status.
- Balance check before every transaction.
- Integer atomic units (`1 DEN = 100,000,000` atomic units) for consensus
  accounting.
- Internally generated coinbase rewards only.
- Proof-of-work commits to the complete block, including its coinbase reward.
- Confirmed transaction replay protection.
- Canonical genesis block.
- Ed25519 wallet keys, signatures, and checked addresses.
- Transactional SQLite state persistence instead of Python pickle files.
- Browser-generated Ed25519 wallets encrypted with PBKDF2 and AES-256-GCM.
- Separate node API and unified local Denarius Console processes.
- Persistent console accounts with administrator and standard-user roles.
- Deduplicated transaction and block relay across compatible peers.
- Background, header-first synchronization that downloads blocks only from the
  strongest compatible header chain.
- Peer health tracking for reachability, latency, remote height, and protocol
  compatibility.
- Basic peer request timeouts, request size limits, and mempool/block limits.
- Password-hashed local node administration with CSRF-protected controls.
- Transaction failure alert.
- Deterministic proof-of-work target adjustment every 10,080 blocks.
- Exact accumulated-work comparison during chain resolution.
- Account nonces and stable transaction IDs for replay and ordering protection.
- Merkle-root commitments over each block's transactions.
- Save running states.


## Denarii monetary policy

Denarius is the network and project, Denarii is its currency, and `DEN` is the
currency notation. Consensus uses integer atomic units, with
`1 DEN = 100,000,000` atomic units.

- Maximum supply: `100,000,000 DEN`
- Target block time: `2 minutes`
- Subsidy halving interval: `1,051,200 blocks` (approximately four years)
- Initial block subsidy: `47.56468797 DEN`
- Difficulty target adjustment: every `10,080 blocks` (approximately two weeks)
- Minimum transaction fee: `0.0001 DEN`
- Mining reward maturity: `10 blocks`

Integer subsidy rounding keeps scheduled issuance below the hard cap. The
consensus values and canonical serialization rules live in
`denarius_protocol.py` so the node and wallet sign and validate identical data.
Testnet v3 uses an intentionally accessible development target; it does not
represent production proof-of-work security.


## Requirements

Denarius supports Python 3.10, 3.11, and 3.12 on Windows and Linux. Direct
runtime, development, and build dependencies are pinned to exact versions.

Install the application from a source checkout:

```bash
python -m venv .venv
```

Activate the environment with `.\.venv\Scripts\Activate.ps1` on PowerShell or
`source .venv/bin/activate` on Linux, then install Denarius:

```bash
python -m pip install .
```

Contributors should install the development tools instead:

```bash
python -m pip install -e ".[dev]"
```


## Usage

Start the installed node API and unified local console together:

```bash
denarius
```

The source checkout remains directly runnable without installation:

```bash
python run_denarius.py
```

The launcher creates a temporary administration token, shares it only between
the node and console processes, and starts:

- Node API: `http://127.0.0.1:5000`
- Denarius Console: `http://127.0.0.1:8080`

On the first visit, enter the one-time setup code printed by the launcher and
create the persistent node administrator. Later visitors can create standard
accounts or sign in without that code. Administrators can use the full node
console; standard accounts are limited to Wallets, Send, and Activity. Account
credentials are salted and hashed in `states/console-accounts.db`.

The node checks peers every 30 seconds by default. Set a different interval
without changing consensus data:

```bash
python run_denarius.py --sync-interval 60
```

Node state is stored transactionally in `states/denarius-testnet-v3.db`. SQLite keeps
blocks, pending transactions, peers, and node metadata in separate tables and
uses full synchronous writes with write-ahead logging.

By default, `states` is created beneath the directory where Denarius is started.
Set `DENARIUS_STATE_DIR` to use a stable absolute state directory when running
the installed commands as a service.

The two services can also be run independently:

```bash
export DENARIUS_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
denarius-node --port 5000 --database states/denarius-testnet-v3.db
denarius-console --port 8080 --accounts-database states/console-accounts.db
```

Use the same `DENARIUS_ADMIN_TOKEN` for the node and console when starting
them independently. The browser never receives this token. The console checks
the signed-in account's persisted role and CSRF token, then forwards authorized
administration calls to the node. Set `DENARIUS_SECRET_KEY` for a stable console
session key.

Phase 6 begins with protocol version 3 and a new Testnet v3 genesis block. State
from protocol version 2 belongs to the retired demonstration chain and is
intentionally rejected. Archive `states/denarius.db`; the default launcher will
create `states/denarius-testnet-v3.db`. JSON migration remains available only
for data already using the active protocol and genesis:

```bash
python blockchain/blockchain.py --migrate-json states/blockchain.json --database states/denarius-testnet-v3.db
```

The browser creates each Ed25519 key with Web Crypto, derives an encryption key
with PBKDF2-SHA256, and encrypts the private key with AES-256-GCM. The encrypted
document is saved in account-scoped browser local storage. Decryption and
transaction signing also happen in the browser; the console and node receive
only public addresses and signed transactions. `.denwallet` backups can be
exported and imported, but they are not required for routine payments.

Testnet v3 uses browser wallet format 2. The former server-generated format 1
belongs to the retired demonstration chain and is intentionally not imported;
the Python node and console contain no private-key wallet operations.

## Peer networking

Configured nodes exchange a versioned peer handshake before relaying data or
synchronizing. Compatibility requires the Denarius network identifier,
consensus protocol version, canonical genesis hash, peer API version, and the
header, block, and relay capabilities used by Phase 4.

Testnet v3 retains standalone SHA-256 proof of work while production foundations
are developed. This is a development decision, not evidence that the current
network has sufficient hash power for real value.

Synchronization locates the latest shared block, downloads and validates the
candidate header suffix, compares exact accumulated proof of work, and only
then requests the corresponding full blocks. Transactions from disconnected
blocks are reconsidered for the mempool after a valid reorganization. The
Network view reports each peer's current health and the background worker's
latest synchronization pass.

Nodes listen on and advertise `127.0.0.1:<port>` by default. For trusted local
network testing between machines, bind the node to the network interface and
advertise its reachable address:

```bash
python run_denarius.py --node-host 0.0.0.0 --advertise-address 192.168.1.25:5000
```

The web console remains local by default. For testing user accounts on a
trusted local network, complete administrator setup first, then add
`--console-host 0.0.0.0` and connect to port `8080`. Public deployments still
require a TLS-terminating reverse proxy and the hardening guidance in
`docs/OPERATIONS.md`.

Install the pinned development dependencies, run every regression and process
integration test, and build the release artifacts:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python -m build
```

The regression tests cover monetary policy, canonical transaction IDs, account
nonces, forged signatures, invalid amounts, pending double-spends, invalid
genesis blocks, incorrect and duplicate coinbase rewards, malformed peer
responses, Merkle commitments, deterministic targets, exact chainwork, SQLite
state loading and migration, authenticated wallet encryption, process
separation, Denarii display formatting, protocol compatibility, relay
deduplication, peer health, background synchronization, and header-first chain
resolution, persistent console accounts, role authorization, and account-scoped
wallet storage. The process integration suite starts real node and console
services to verify administrator setup, wallet creation, mining, persistence
across restart, standard-user restrictions, and two-node synchronization.

## Release quality

GitHub Actions runs the full suite on Windows and Linux with Python 3.10 and
3.12, builds and smoke-tests the wheel and source archive, and runs a separate
adversarial property, benchmark, and soak job. The stable `Release quality`
status is the required branch-protection check described in
[docs/BRANCH_PROTECTION.md](docs/BRANCH_PROTECTION.md).

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and protocol-change
guidelines, [docs/RELEASING.md](docs/RELEASING.md) for the release checklist,
and [CHANGELOG.md](CHANGELOG.md) for release history.

## Security notes

Denarius remains educational software. The node binds to `127.0.0.1` by default.
The services use Waitress by default, but must still sit behind HTTPS and the
controls in [docs/OPERATIONS.md](docs/OPERATIONS.md). Encrypted wallet files,
SQLite state files, private keys, and TLS key files are ignored and must never
be committed. A `.denwallet` file still controls funds when paired with its
password; back up both separately. Historical example keys previously included
in this repository should be considered public and must not be reused. Report
suspected vulnerabilities privately by following [SECURITY.md](SECURITY.md).

See [docs/NETWORK_SECURITY.md](docs/NETWORK_SECURITY.md) for peer transport,
[docs/INCIDENT_RESPONSE.md](docs/INCIDENT_RESPONSE.md) for containment and
recovery, [docs/ADVERSARIAL_TESTING.md](docs/ADVERSARIAL_TESTING.md) for stress
verification, and [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) for the
independent review required before any mainnet claim.
