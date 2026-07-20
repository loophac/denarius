# Denarius Network

Denarius is an educational proof-of-work blockchain prototype and the conceptual
foundation for a lightweight cryptocurrency. It is intentionally compact, but it
should still be treated as demonstration software rather than production money.

This work is based on :

- [adilmoujahid/blockchain-python-tutorial](https://github.com/adilmoujahid/blockchain-python-tutorial)  
- [asuith/blockchain-in-python](https://github.com/asuith/blockchain-in-python)


## Novel Features from asuith/blockchain-in-python

Compared with the original one, we now introduce:

- Denarii (coin name).
- Constant wealth (`1e8` coin in total).
- Setting miner's information.
- Balance check before every transaction.
- Integer atomic units (`1 DEN = 100,000,000` atomic units) for consensus
  accounting.
- Internally generated coinbase rewards only.
- Proof-of-work commits to the complete block, including its coinbase reward.
- Confirmed transaction replay protection.
- Canonical genesis block.
- Ed25519 wallet keys, signatures, and checked addresses.
- Transactional SQLite state persistence instead of Python pickle files.
- Password-encrypted wallet files using scrypt and AES-256-GCM.
- Separate node API and unified local Denarius Console processes.
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

Integer subsidy rounding keeps scheduled issuance below the hard cap. The
consensus values and canonical serialization rules live in
`denarius_protocol.py` so the node and wallet sign and validate identical data.


## Requirements

In order to run this code, you'll need:

- Python 3
- cryptography
- Flask
- Requests
- pytest, for running the regression tests

To install run:

```
pip install -r requirements.txt
```


## Usage

Start the node API and unified local console together:

```bash
python run_denarius.py
```

The launcher creates a temporary administration token, shares it only between
the node and console processes, and starts:

- Node API: `http://127.0.0.1:5000`
- Denarius Console: `http://127.0.0.1:8080`

The console combines node status, wallet management, sending, transaction
activity, mining, and peer configuration behind one local login.

The node checks peers every 30 seconds by default. Set a different interval
without changing consensus data:

```bash
python run_denarius.py --sync-interval 60
```

Node state is stored transactionally in `states/denarius.db`. SQLite keeps
blocks, pending transactions, peers, and node metadata in separate tables and
uses full synchronous writes with write-ahead logging.

The two services can also be run independently:

```bash
export DENARIUS_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python blockchain/blockchain.py --port 5000 --database states/denarius.db
python blockchain_client/blockchain_client.py --port 8080
```

Use the same `DENARIUS_ADMIN_TOKEN` for the node and console when starting
them independently. The browser never receives this token. The console keeps
its local login and CSRF checks, then forwards authorized administration calls
to the node. Set `DENARIUS_SECRET_KEY` for a stable console session key.

Phase 1 uses protocol version 2, account nonces, transaction IDs, Merkle roots,
and deterministic proof-of-work targets. State files created by Phase 0 or
older versions use a different consensus format and are intentionally rejected.
Archive an older state file and start without it to create a fresh Phase 1
chain. Phases 2 through 4 do not change consensus or require another chain reset. A valid
Phase 1 JSON state can be migrated into SQLite:

```bash
python blockchain/blockchain.py --migrate-json states/blockchain.json --database states/denarius.db
```

The wallet creates an Ed25519 key, encrypts it with scrypt and AES-256-GCM, and
saves the encrypted wallet document in the browser's local storage. Raw private
keys are never returned to the web page or stored in the browser. To send DEN,
select a saved sender wallet and enter its password; the local console process
decrypts it in memory, reads the next account nonce from the selected node, and
uses the plaintext key only for that signing request. `.denwallet` backup files
can be exported and imported, but they are not required for routine payments.

## Peer networking

Configured nodes exchange a versioned peer handshake before relaying data or
synchronizing. Compatibility requires the Denarius network identifier,
consensus protocol version, canonical genesis hash, peer API version, and the
header, block, and relay capabilities used by Phase 4.

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

To run the tests:

```bash
pytest
```

The regression tests cover monetary policy, canonical transaction IDs, account
nonces, forged signatures, invalid amounts, pending double-spends, invalid
genesis blocks, incorrect and duplicate coinbase rewards, malformed peer
responses, Merkle commitments, deterministic targets, exact chainwork, SQLite
state loading and migration, authenticated wallet encryption, process
separation, Denarii display formatting, protocol compatibility, relay
deduplication, peer health, background synchronization, and header-first chain
resolution.

## Security notes

Denarius remains educational software. The node binds to `127.0.0.1` by default.
Do not expose the Flask development servers directly to the internet. Encrypted
wallet files, SQLite state files, private keys, and TLS key files are ignored
and must never be committed. A `.denwallet` file still controls funds when paired
with its password; back up both separately. Historical example keys previously
included in this repository should be considered public and must not be reused.
