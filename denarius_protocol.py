import hashlib
import json
from collections import OrderedDict


PROTOCOL_VERSION = 2
NETWORK_ID = 'denarius-mainnet-v2'
COINBASE_SENDER = 'DENARIUS_COINBASE'

# The peer API can evolve without changing consensus serialization.
PEER_API_VERSION = 1
PEER_CAPABILITIES = (
    'blocks-v1',
    'headers-v1',
    'relay-v1',
)

ATOMIC_UNITS = 100_000_000
MAX_SUPPLY_DEN = 100_000_000
MAX_SUPPLY_ATOMIC = MAX_SUPPLY_DEN * ATOMIC_UNITS

TARGET_BLOCK_SECONDS = 2 * 60
HALVING_INTERVAL = 4 * 365 * 24 * 60 * 60 // TARGET_BLOCK_SECONDS
INITIAL_BLOCK_REWARD = MAX_SUPPLY_ATOMIC // (2 * HALVING_INTERVAL)

RETARGET_TIMESPAN = 14 * 24 * 60 * 60
RETARGET_INTERVAL = RETARGET_TIMESPAN // TARGET_BLOCK_SECONDS
MAX_FUTURE_BLOCK_SECONDS = 2 * 60 * 60
MEDIAN_TIME_BLOCKS = 11

MAX_HASH = (1 << 256) - 1
INITIAL_TARGET = MAX_HASH >> 8
MAX_TARGET = INITIAL_TARGET
MIN_TARGET = 1

TRANSACTION_SIGNING_FIELDS = (
    'version',
    'network',
    'sender_address',
    'recipient_address',
    'amount_atomic',
    'nonce',
)
SIGNED_TRANSACTION_FIELDS = TRANSACTION_SIGNING_FIELDS + (
    'signature',
    'transaction_id',
)
COINBASE_FIELDS = (
    'version',
    'network',
    'sender_address',
    'recipient_address',
    'amount_atomic',
    'height',
    'transaction_id',
)
BLOCK_HEADER_FIELDS = (
    'version',
    'network',
    'block_number',
    'timestamp',
    'merkle_root',
    'nonce',
    'previous_hash',
    'target',
)
BLOCK_FIELDS = BLOCK_HEADER_FIELDS + ('transactions',)


def canonical_json_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf8')


def sha256_hex(value):
    return hashlib.sha256(value).hexdigest()


def target_to_hex(target):
    if not isinstance(target, int) or isinstance(target, bool):
        raise ValueError('Target must be an integer')
    if target < MIN_TARGET or target > MAX_TARGET:
        raise ValueError('Target is outside protocol bounds')
    return format(target, '064x')


def target_from_hex(encoded_target):
    if not isinstance(encoded_target, str) or len(encoded_target) != 64:
        raise ValueError('Target must be a 64-character hexadecimal string')
    if encoded_target != encoded_target.lower():
        raise ValueError('Target must use lowercase hexadecimal')
    try:
        target = int(encoded_target, 16)
    except ValueError as exc:
        raise ValueError('Target must be hexadecimal') from exc
    if target < MIN_TARGET or target > MAX_TARGET:
        raise ValueError('Target is outside protocol bounds')
    return target


def block_reward(height):
    if not isinstance(height, int) or isinstance(height, bool) or height < 1:
        return 0
    halvings = (height - 1) // HALVING_INTERVAL
    return INITIAL_BLOCK_REWARD >> halvings


def transaction_signing_payload(
    sender_address,
    recipient_address,
    amount_atomic,
    nonce,
):
    return OrderedDict({
        'version': PROTOCOL_VERSION,
        'network': NETWORK_ID,
        'sender_address': sender_address,
        'recipient_address': recipient_address,
        'amount_atomic': str(amount_atomic),
        'nonce': nonce,
    })


def signed_transaction_id(signing_payload, signature):
    signed_payload = OrderedDict(signing_payload)
    signed_payload['signature'] = signature
    return sha256_hex(canonical_json_bytes(signed_payload))


def coinbase_transaction(recipient_address, amount_atomic, height):
    payload = OrderedDict({
        'version': PROTOCOL_VERSION,
        'network': NETWORK_ID,
        'sender_address': COINBASE_SENDER,
        'recipient_address': recipient_address,
        'amount_atomic': str(amount_atomic),
        'height': height,
    })
    payload['transaction_id'] = sha256_hex(canonical_json_bytes(payload))
    return payload


def calculate_merkle_root(transactions):
    if not transactions:
        return sha256_hex(b'')

    level = []
    for transaction in transactions:
        transaction_id = transaction.get('transaction_id')
        if not isinstance(transaction_id, str) or len(transaction_id) != 64:
            raise ValueError('Transaction ID must be a SHA-256 hexadecimal string')
        try:
            level.append(bytes.fromhex(transaction_id))
        except ValueError as exc:
            raise ValueError('Transaction ID must be hexadecimal') from exc

    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def block_header(block):
    return OrderedDict((field, block[field]) for field in BLOCK_HEADER_FIELDS)


def block_hash(block):
    return sha256_hex(canonical_json_bytes(block_header(block)))


def work_for_target(target):
    return (1 << 256) // (target + 1)


EMPTY_MERKLE_ROOT = calculate_merkle_root([])
GENESIS_BLOCK = {
    'version': PROTOCOL_VERSION,
    'network': NETWORK_ID,
    'block_number': 0,
    'timestamp': 1546300800,
    'merkle_root': EMPTY_MERKLE_ROOT,
    'nonce': 0,
    'previous_hash': '00',
    'target': target_to_hex(INITIAL_TARGET),
    'transactions': [],
}
GENESIS_HASH = block_hash(GENESIS_BLOCK)
