# Blockchain Python tutorial

This work is based on [adilmoujahid/blockchain-python-tutorial](https://github.com/adilmoujahid/blockchain-python-tutorial).


## Features

Compared with the original one, we now introduce:

- Constant wealth (1e8 coin in total).
- Setting miner's information.
- Balance check before every transaction.
- Dynamic `difficulty` update every 2 weeks.
- SSL support.



(Risky, not recommended) If you need SSL support, add certificate(inside `certificates` folder) to your system(`cert.pem`) or your browser(`cert.p12`). 

## Usage

To run blockchain node:

```bash
python blockchain/blockchain.py -p 5000
```

To run blockchain client:

```bash
python blockchain_client/blockchain_client.py -p 8080
```
