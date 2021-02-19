# Denarius Network

This work is based on :

- [adilmoujahid/blockchain-python-tutorial](https://github.com/adilmoujahid/blockchain-python-tutorial)  
- [asuith/blockchain-in-python](https://github.com/asuith/blockchain-in-python)


## Novel Features from asuith/blockchain-in-python

Compared with the original one, we now introduce:

- Denarii (coin name).
- Constant wealth (`1e8` coin in total).
- Setting miner's information.
- Balance check before every transaction.
- Transaction failure alert.
- Dynamic `difficulty` update every 2 weeks.
- SSL support.
- Save running states.


(Risky, not recommended) If you need SSL support, add certificate(inside `certificates` folder) to your system(`cert.pem`) or your browser(`cert.p12`). 


## Requirements

In order to run this code, you'll need:

- Python 3
- pycrypto
- Flask
- Requests

To install run:

```
pip install -r requirements.txt
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
