import argparse
import json
import sys
import tempfile
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from blockchain.blockchain import Blockchain
from denarius_crypto import address_from_public_key


def main(argv=None):
    parser = argparse.ArgumentParser(description='Benchmark Denarius long-chain replay and reload')
    parser.add_argument('--blocks', type=int, default=1000)
    args = parser.parse_args(argv)
    if args.blocks < 1:
        parser.error('--blocks must be positive')

    blockchain = Blockchain()
    public_key = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    blockchain.node_address = address_from_public_key(public_key)
    started = perf_counter()
    for _ in range(args.blocks):
        block = blockchain.mine_pending_transactions(relay=False, persist=False)
        if block is False:
            raise RuntimeError('unable to mine benchmark block')
    mining_seconds = perf_counter() - started

    with tempfile.TemporaryDirectory() as tmpdir:
        database = Path(tmpdir) / 'benchmark.db'
        blockchain.STATE_PATH = database
        save_started = perf_counter()
        blockchain.save_everything()
        save_seconds = perf_counter() - save_started
        load_started = perf_counter()
        restored = Blockchain().load_everything(database)
        load_seconds = perf_counter() - load_started

    print(json.dumps({
        'blocks': len(restored.chain) - 1,
        'mining_seconds': round(mining_seconds, 4),
        'snapshot_seconds': round(save_seconds, 4),
        'reload_seconds': round(load_seconds, 4),
        'blocks_per_second': round(args.blocks / mining_seconds, 2),
    }, indent=2))


if __name__ == '__main__':
    main()
