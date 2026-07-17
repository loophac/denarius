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
- Canonical genesis block.
- Ed25519 wallet keys, signatures, and checked addresses.
- JSON state persistence instead of Python pickle files.
- Peer table exchange, transaction relay, and newly mined block relay.
- Basic peer request timeouts, request size limits, and mempool/block limits.
- Transaction failure alert.
- Dynamic `difficulty` update every 2 weeks.
- SSL support.
- Save running states.


(Risky, not recommended) If you need SSL support, add certificate(inside `certificates` folder) to your system(`cert.pem`) or your browser(`cert.p12`). 


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

To run blockchain client:

```bash
python blockchain_client/blockchain_client.py -p 8080
```

The wallet UI generates an Ed25519 private key, raw public key, and checked
Denarius address. Use the checked address for sender, recipient, and miner
fields. The wallet UI accepts ordinary DEN amounts and normalizes them to atomic
units before signing. The node's `/transactions/new` endpoint expects the signed
atomic-unit value from the client.

To run the tests:

```bash
pytest
```

The regression tests cover the core consensus checks: forged signatures, invalid
amounts, pending double-spends, invalid genesis blocks, incorrect and duplicate
coinbase rewards, duplicate signed transactions, malformed peer responses,
oversized blocks, chainwork-based conflict resolution, JSON state loading, and
Denarii display formatting for atomic transaction values.
