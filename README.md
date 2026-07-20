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
- JSON state persistence instead of Python pickle files.
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



To run blockchain node:

```bash
python blockchain/blockchain.py -p 5000
```

which also supports restoring to previous state with `-r path\to\blockchain.json`.
The default state file is stored in `states\blockchain.json`.

The first visit to the node UI creates a local administrator. Administrative
actions such as mining, changing the reward address, adding peers, and manually
resolving the chain require that signed-in session. Passwords must be at least
10 characters and are held as salted hashes for the lifetime of the process.
Set `DENARIUS_SECRET_KEY` in a managed deployment instead of relying on the
random session-signing secret generated at startup. Administrator registration
is intentionally local and must currently be repeated after a process restart.

Phase 1 uses protocol version 2, account nonces, transaction IDs, Merkle roots,
and deterministic proof-of-work targets. State files created by Phase 0 or
older versions use a different consensus format and are intentionally rejected.
Archive an old state file and start without `-r` to create a fresh Phase 1
chain.

To run blockchain client:

```bash
python blockchain_client/blockchain_client.py -p 8080
```

The wallet UI generates an Ed25519 private key, raw public key, and checked
Denarius address. Use the checked address for sender, recipient, and miner
fields. The wallet UI accepts ordinary DEN amounts and normalizes them to atomic
units before signing. Before creating a signature, the wallet reads the sender's
next nonce from the selected node. The node's `/transactions/new` endpoint
expects `sender_address`, `recipient_address`, `amount` (in atomic units),
`nonce`, `signature`, and `transaction_id`. Account balance and nonce state are
available from `GET /accounts/<address>`.

To run the tests:

```bash
pytest
```

The regression tests cover monetary policy, canonical transaction IDs, account
nonces, forged signatures, invalid amounts, pending double-spends, invalid
genesis blocks, incorrect and duplicate coinbase rewards, malformed peer
responses, Merkle commitments, deterministic targets, exact chainwork, JSON
state loading, and Denarii display formatting.

## Security notes

Denarius remains educational software. The node binds to `127.0.0.1` by default.
Do not expose the Flask development server directly to the internet. Private
wallet and TLS key files are ignored and must never be committed. The historical
example keys previously included in this repository should be considered public
and must not be reused.
