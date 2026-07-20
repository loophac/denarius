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
- Separate node API, local administration dashboard, and wallet processes.
- Peer table exchange, transaction relay, and newly mined block relay.
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

Start the node API, administration dashboard, and encrypted wallet together:

```bash
python run_denarius.py
```

The launcher creates a temporary administration token, shares it only between
the node and dashboard processes, and starts:

- Node API: `http://127.0.0.1:5000`
- Administration dashboard: `http://127.0.0.1:5001`
- Encrypted wallet: `http://127.0.0.1:8080`

Node state is stored transactionally in `states/denarius.db`. SQLite keeps
blocks, pending transactions, peers, and node metadata in separate tables and
uses full synchronous writes with write-ahead logging.

The three services can also be run independently:

```bash
export DENARIUS_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python blockchain/blockchain.py --port 5000 --database states/denarius.db
python node_dashboard/dashboard.py --port 5001
python blockchain_client/blockchain_client.py --port 8080
```

Use the same `DENARIUS_ADMIN_TOKEN` for the node and dashboard when starting
them independently. The browser never receives this token. The dashboard keeps
its local login and CSRF checks, then forwards authorized administration calls
to the node. Set `DENARIUS_SECRET_KEY` for a stable dashboard session key.

Phase 1 uses protocol version 2, account nonces, transaction IDs, Merkle roots,
and deterministic proof-of-work targets. State files created by Phase 0 or
older versions use a different consensus format and are intentionally rejected.
Archive an older state file and start without it to create a fresh Phase 1
chain. Phase 2 does not change consensus or require another chain reset. A valid
Phase 1 JSON state can be migrated into SQLite:

```bash
python blockchain/blockchain.py --migrate-json states/blockchain.json --database states/denarius.db
```

The wallet creates an Ed25519 key, encrypts it with scrypt and AES-256-GCM, and
downloads a `.denwallet` file. Raw private keys are never returned to the web
page. To send DEN, select the encrypted wallet file and enter its password; the
local wallet process decrypts it in memory, reads the next account nonce from
the selected node, and uses the plaintext key only for that signing request.

To run the tests:

```bash
pytest
```

The regression tests cover monetary policy, canonical transaction IDs, account
nonces, forged signatures, invalid amounts, pending double-spends, invalid
genesis blocks, incorrect and duplicate coinbase rewards, malformed peer
responses, Merkle commitments, deterministic targets, exact chainwork, SQLite
state loading and migration, authenticated wallet encryption, process
separation, and Denarii display formatting.

## Security notes

Denarius remains educational software. The node binds to `127.0.0.1` by default.
Do not expose the Flask development servers directly to the internet. Encrypted
wallet files, SQLite state files, private keys, and TLS key files are ignored
and must never be committed. A `.denwallet` file still controls funds when paired
with its password; back up both separately. Historical example keys previously
included in this repository should be considered public and must not be reused.
