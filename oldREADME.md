# Blockchain in Python

This work is based on [adilmoujahid/blockchain-python-tutorial](https://github.com/adilmoujahid/blockchain-python-tutorial).


## Novel Features

Compared with the original one, we now introduce:

- Denarii (coin name).
- Constant wealth (`1e8` coin in total).
- Setting miner's information.
- Balance check before every transaction.
- Transaction failure alert.
- Dynamic `difficulty` update every 2 weeks.
- Save running states.


The historical bundled certificate and private-key examples have been removed.
Do not commit wallet or transport private keys.


## Requirements

In order to run this code, you'll need:

- Python 3
- cryptography
- Flask

To install cryptography and Flask, run:

```
pip install cryptography
pip install -U Flask
```


## Usage



To run blockchain node:

```bash
python blockchain/blockchain.py -p 5000
```

which we also support restoring to previous state with `-r path\to\file.pkl`.
The default file of state is stored in `states\blockchain.pkl`.

To run blockchain client:

```bash
python blockchain_client/blockchain_client.py -p 8080
```
