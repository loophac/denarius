import binascii
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from time import time
from urllib.parse import urlparse
from uuid import uuid1

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from flask import Flask, jsonify, request, render_template, redirect, session, url_for
from flask_cors import CORS

class Blockchain:
    ATOMIC_UNITS = 100000000
    TOTAL_AMOUNT = 100000000 * ATOMIC_UNITS
    REWARD_HALVING_INTERVAL = 6 * 30 * 24 * 6
    INITIAL_MINING_REWARD = TOTAL_AMOUNT // 100 // REWARD_HALVING_INTERVAL
    COINBASE_SENDER = "DENARIUS_COINBASE"
    PEER_REQUEST_TIMEOUT = 3
    MAX_PEERS = 128
    MAX_PENDING_TRANSACTIONS = 1000
    MAX_TRANSACTIONS_PER_BLOCK = 1000
    MAX_DIFFICULTY = 16
    GENESIS_BLOCK = {
        'block_number': 0,
        'timestamp': 1546300800,
        'transactions': [],
        'nonce': 0,
        'previous_hash': '00',
        'difficulty': 2,
    }
    STATE_PATH = Path(__file__).resolve().parents[1] / 'states' / 'blockchain.json'

    def __init__(self, name="THE BLOCKCHAIN"):

        self._lock = threading.RLock()
        self.transactions = []
        self.chain = []
        self.nodes = set()
        # Generate random number to be used as node_id
        self.node_address = str(uuid1()).replace('-', '')
        # Create genesis block
        self.chain.append(dict(self.GENESIS_BLOCK))
        self.miner_name = name
        self.MINING_DIFFICULTY = 2
        self.MINING_REWARD = self.block_reward(1)

    def parse_amount(self, value):
        """
        Convert a user-facing DEN amount to integer atomic units.
        1 DEN = 100,000,000 atomic units.
        """
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

        if not amount.is_finite() or amount <= 0:
            return None

        atomic_amount = amount * self.ATOMIC_UNITS
        if atomic_amount != atomic_amount.to_integral_value():
            return None

        atomic_amount = int(atomic_amount)
        if atomic_amount <= 0 or atomic_amount > self.TOTAL_AMOUNT:
            return None
        return atomic_amount

    def parse_atomic_value(self, value):
        try:
            atomic_value = int(str(value))
        except (TypeError, ValueError):
            return None

        if atomic_value <= 0 or atomic_value > self.TOTAL_AMOUNT:
            return None
        return atomic_value

    def format_amount(self, atomic_value):
        amount = Decimal(int(atomic_value)) / Decimal(self.ATOMIC_UNITS)
        return format(amount.normalize(), 'f')

    def block_reward(self, height):
        halvings = height // self.REWARD_HALVING_INTERVAL
        reward = self.INITIAL_MINING_REWARD
        for _ in range(halvings):
            reward //= 2
        return reward

    def set_miner_info(self, name, address):
        """
        Set miner's infomation
        :param name: Miner's name.
        :param address: Miner's public key address.
        :return: None
        """
        if self.public_key_from_address(address) is None:
            raise ValueError('Invalid miner address')
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError('Invalid miner name')
        with self._lock:
            self.miner_name = name.strip()
            self.node_address = address

    def update_hyperparameters(self):
        """
        Generate Block every 2 minutes.
        Update difficulty every 2016 block (should be in exactly 2 weeks' time)
        Update mining reward every six months.
        :return:
        """
        # Update difficulty
        last_block = self.chain[-1]
        last_index = last_block['block_number']
        if last_index % 2016 == 0 and last_index != 0:
            time_diff = self.chain[last_index]['timestamp'] - self.chain[last_index - 2016]['timestamp']
            two_week = 2 * 7 * 24 * 60 * 60 * 1.0
            if time_diff > 0:
                adjustment = two_week / time_diff
                adjustment = max(0.25, min(4.0, adjustment))
                self.MINING_DIFFICULTY = min(
                    self.MAX_DIFFICULTY,
                    max(1, int(round(self.MINING_DIFFICULTY * adjustment))),
                )

        if last_index % (6 * 30 * 24 * 6) == 0 and last_index != 0:
            self.MINING_REWARD = self.block_reward(last_index + 1)

    def normalize_node(self, node_url):
        if not isinstance(node_url, str):
            raise ValueError('Invalid URL')

        node_url = node_url.strip()
        if not node_url or len(node_url) > 255:
            raise ValueError('Invalid URL')

        candidate = node_url if '://' in node_url else 'http://' + node_url
        parsed_url = urlparse(candidate)
        if parsed_url.scheme != 'http' or parsed_url.username or parsed_url.password:
            raise ValueError('Invalid URL')
        if parsed_url.path not in ('', '/') or parsed_url.query or parsed_url.fragment:
            raise ValueError('Invalid URL')

        try:
            port = parsed_url.port
        except ValueError as exc:
            raise ValueError('Invalid URL') from exc

        hostname = parsed_url.hostname
        if not hostname or any(character.isspace() for character in hostname):
            raise ValueError('Invalid URL')

        if ':' in hostname:
            hostname = '[' + hostname + ']'
        return hostname + (':' + str(port) if port is not None else '')

    def peer_url(self, node, path):
        return 'http://' + node + path

    def register_node(self, node_url):
        """
        Add a new node to the list of nodes
        """
        normalized_node = self.normalize_node(node_url)
        with self._lock:
            if len(self.nodes) >= self.MAX_PEERS and normalized_node not in self.nodes:
                raise ValueError('Peer limit reached')
            self.nodes.add(normalized_node)

    def exchange_peer_table(self):
        for node in list(self.nodes):
            try:
                response = requests.get(
                    self.peer_url(node, '/nodes/get'),
                    timeout=self.PEER_REQUEST_TIMEOUT,
                )
                if response.status_code != 200:
                    continue
                for peer in response.json().get('nodes', []):
                    if len(self.nodes) >= self.MAX_PEERS:
                        return
                    self.register_node(peer)
            except (requests.RequestException, ValueError, KeyError):
                continue

    def broadcast_transaction(self, transaction):
        for node in list(self.nodes):
            try:
                requests.post(
                    self.peer_url(node, '/transactions/receive'),
                    data={
                        'sender_address': transaction['sender_address'],
                        'recipient_address': transaction['recipient_address'],
                        'amount': transaction['value'],
                        'signature': transaction['signature'],
                    },
                    timeout=self.PEER_REQUEST_TIMEOUT,
                )
            except requests.RequestException:
                continue

    def broadcast_block(self, block):
        for node in list(self.nodes):
            try:
                requests.post(
                    self.peer_url(node, '/blocks/receive'),
                    json={'block': block},
                    timeout=self.PEER_REQUEST_TIMEOUT,
                )
            except requests.RequestException:
                continue

    def canonical_transaction_bytes(self, transaction):
        return json.dumps(transaction, sort_keys=True, separators=(',', ':')).encode('utf8')

    def address_from_public_key(self, public_key_bytes):
        public_key_hex = binascii.hexlify(public_key_bytes).decode('ascii')
        checksum = hashlib.sha256(('DENARIUS:' + public_key_hex).encode('ascii')).hexdigest()[:8]
        return 'dn' + public_key_hex + checksum

    def public_key_from_address(self, address):
        if not isinstance(address, str) or len(address) != 74 or not address.startswith('dn'):
            return None

        public_key_hex = address[2:66]
        checksum = address[66:]
        expected_checksum = hashlib.sha256(('DENARIUS:' + public_key_hex).encode('ascii')).hexdigest()[:8]
        if checksum != expected_checksum:
            return None

        try:
            return binascii.unhexlify(public_key_hex)
        except (ValueError, TypeError, binascii.Error):
            return None

    def verify_transaction_signature(self, sender_address, signature, transaction):
        """
        Check that the provided signature corresponds to transaction
        signed by the public key (sender_address)
        """
        public_key_bytes = self.public_key_from_address(sender_address)
        if public_key_bytes is None:
            return False

        try:
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
            public_key.verify(binascii.unhexlify(signature), self.canonical_transaction_bytes(transaction))
            return True
        except (InvalidSignature, ValueError, TypeError, binascii.Error):
            return False

    def get_balance(self, address):
        """
        Get the balance noted on the address
        :param address: the address of the account
        :return: balance (Float)
        """
        balance = 0
        with self._lock:
            chain = copy.deepcopy(self.chain)
        for c in chain:
            for t in c['transactions']:
                if t['sender_address'] == address and t['recipient_address'] == address:
                    continue
                if t['recipient_address'] == address:
                    balance += self.parse_atomic_value(t['value']) or 0
                elif t['sender_address'] == address:
                    balance -= self.parse_atomic_value(t['value']) or 0
        return self.format_amount(balance)

    def get_atomic_balance(self, address, include_pending=False):
        balance = 0
        with self._lock:
            chain = copy.deepcopy(self.chain)
            pending_transactions = copy.deepcopy(self.transactions) if include_pending else []

        for c in chain:
            for t in c['transactions']:
                atomic_value = self.parse_atomic_value(t['value']) or 0
                if t['sender_address'] == address and t['recipient_address'] == address:
                    continue
                if t['recipient_address'] == address:
                    balance += atomic_value
                if t['sender_address'] == address:
                    balance -= atomic_value

        if include_pending:
            for t in pending_transactions:
                atomic_value = self.parse_atomic_value(t['value']) or 0
                if t['sender_address'] == address:
                    balance -= atomic_value
        return balance

    def transaction_key(self, transaction):
        return hashlib.sha256(self.canonical_transaction_bytes(transaction)).hexdigest()

    def confirmed_transaction_keys(self, chain=None):
        transaction_keys = set()
        for block in chain if chain is not None else self.chain:
            for transaction in block.get('transactions', []):
                if transaction.get('sender_address') != self.COINBASE_SENDER:
                    transaction_keys.add(self.transaction_key(transaction))
        return transaction_keys

    def verify_enough_balance(self, address, atomic_value):
        """
        Check that the sender has enough balance in his wallet.
        :param sender_address: address of the sender
        :param value: value to be sent
        :return: True if sender has enough balance else False
        """
        return self.get_atomic_balance(address, include_pending=True) >= atomic_value

    def submit_transaction(self, sender_address, recipient_address, value, signature, relay=True):
        """
        Add a transaction to transactions array if the signature verified
        """
        atomic_value = self.parse_atomic_value(value)
        if atomic_value is None:
            return False

        transaction = OrderedDict({'sender_address': sender_address,
                                   'recipient_address': recipient_address,
                                   'value': str(atomic_value)})

        if sender_address == self.COINBASE_SENDER:
            return False
        if self.public_key_from_address(recipient_address) is None:
            return False

        transaction_verification = self.verify_transaction_signature(sender_address, signature, transaction)
        if not transaction_verification:
            return False

        signed_transaction = OrderedDict(transaction)
        signed_transaction['signature'] = signature

        with self._lock:
            if len(self.transactions) >= self.MAX_PENDING_TRANSACTIONS:
                return False
            if not self.verify_enough_balance(sender_address, atomic_value):
                return False

            transaction_key = self.transaction_key(signed_transaction)
            if transaction_key in self.confirmed_transaction_keys():
                return False
            if any(self.transaction_key(pending) == transaction_key for pending in self.transactions):
                return False

            self.transactions.append(signed_transaction)
            next_block_number = len(self.chain)

        if relay:
            self.broadcast_transaction(signed_transaction)
        return next_block_number

    def create_coinbase_transaction(self, height=None, recipient_address=None):
        height = len(self.chain) if height is None else height
        recipient_address = self.node_address if recipient_address is None else recipient_address
        return OrderedDict({'sender_address': self.COINBASE_SENDER,
                            'recipient_address': recipient_address,
                            'value': str(self.block_reward(height))})

    def create_candidate_block(self):
        with self._lock:
            height = len(self.chain)
            previous_hash = self.hash(self.chain[-1])
            pending_limit = max(0, self.MAX_TRANSACTIONS_PER_BLOCK - 1)
            pending_snapshot = copy.deepcopy(self.transactions[:pending_limit])
            recipient_address = self.node_address
            difficulty = int(self.MINING_DIFFICULTY)

        coinbase = self.create_coinbase_transaction(height, recipient_address)
        block = {
            'block_number': height,
            'timestamp': int(time()),
            'transactions': pending_snapshot + [coinbase],
            'nonce': 0,
            'previous_hash': previous_hash,
            'difficulty': difficulty,
        }
        return block

    def hash(self, block):
        """
        Create a SHA-256 hash of a block
        """
        block_string = json.dumps(
            block,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf8')

        return hashlib.sha256(block_string).hexdigest()

    def proof_of_work(self, candidate_block):
        """
        Find a nonce for a complete candidate block. Every consensus field,
        including the coinbase reward, is committed by the proof.
        """
        if not isinstance(candidate_block, dict):
            raise ValueError('Invalid candidate block')
        difficulty = candidate_block.get('difficulty')
        if (not isinstance(difficulty, int) or isinstance(difficulty, bool)
                or difficulty < 1 or difficulty > self.MAX_DIFFICULTY):
            raise ValueError('Invalid candidate block difficulty')

        block = copy.deepcopy(candidate_block)
        nonce = 0
        block['nonce'] = nonce
        while not self.valid_proof(block):
            nonce += 1
            block['nonce'] = nonce
        return block

    def valid_proof(self, block):
        """
        Check whether a complete block hash satisfies its declared difficulty.
        """
        if not isinstance(block, dict):
            return False
        difficulty = block.get('difficulty')
        if not isinstance(difficulty, int) or isinstance(difficulty, bool):
            return False
        if difficulty < 1 or difficulty > self.MAX_DIFFICULTY:
            return False
        try:
            block_hash = self.hash(block)
        except (TypeError, ValueError, OverflowError):
            return False
        return block_hash.startswith('0' * difficulty)

    def mine_pending_transactions(self, relay=True, persist=True):
        if self.public_key_from_address(self.node_address) is None:
            return False

        candidate = self.create_candidate_block()
        block = self.proof_of_work(candidate)

        with self._lock:
            if block['block_number'] != len(self.chain):
                return False
            if block['previous_hash'] != self.hash(self.chain[-1]):
                return False

            candidate_chain = self.chain + [block]
            if not self.valid_chain(candidate_chain):
                return False

            self.chain.append(block)
            self.remove_confirmed_transactions(block)
            self.update_hyperparameters()
            if persist:
                self.save_everything()

        if relay:
            self.broadcast_block(block)
        return block

    def apply_transaction(self, transaction, ledger, require_signature=True):
        if not isinstance(transaction, dict):
            return False
        if set(transaction) != {'sender_address', 'recipient_address', 'value', 'signature'}:
            return False

        atomic_value = self.parse_atomic_value(transaction.get('value'))
        if atomic_value is None:
            return False

        sender_address = transaction.get('sender_address')
        recipient_address = transaction.get('recipient_address')
        if not sender_address or not recipient_address or sender_address == self.COINBASE_SENDER:
            return False
        if self.public_key_from_address(recipient_address) is None:
            return False

        normalized_transaction = OrderedDict({'sender_address': sender_address,
                                              'recipient_address': recipient_address,
                                              'value': str(atomic_value)})
        if require_signature:
            signature = transaction.get('signature', '')
            if not self.verify_transaction_signature(sender_address, signature, normalized_transaction):
                return False

        if ledger.get(sender_address, 0) < atomic_value:
            return False

        ledger[sender_address] = ledger.get(sender_address, 0) - atomic_value
        ledger[recipient_address] = ledger.get(recipient_address, 0) + atomic_value
        return True

    def apply_coinbase_transaction(self, transaction, ledger, height):
        if not isinstance(transaction, dict):
            return False
        if set(transaction) != {'sender_address', 'recipient_address', 'value'}:
            return False

        atomic_value = self.parse_atomic_value(transaction.get('value'))
        if atomic_value is None:
            return False

        if transaction.get('sender_address') != self.COINBASE_SENDER:
            return False
        if atomic_value != self.block_reward(height):
            return False

        recipient_address = transaction.get('recipient_address')
        if self.public_key_from_address(recipient_address) is None:
            return False

        ledger[recipient_address] = ledger.get(recipient_address, 0) + atomic_value
        if sum(ledger.values()) > self.TOTAL_AMOUNT:
            return False
        return True

    def valid_chain(self, chain):
        """
        check if a blockchain is valid
        """
        if not isinstance(chain, list) or not chain or chain[0] != self.GENESIS_BLOCK:
            return False

        ledger = {}
        seen_transactions = set()
        last_block = chain[0]
        current_index = 1
        required_block_fields = {
            'block_number',
            'timestamp',
            'transactions',
            'nonce',
            'previous_hash',
            'difficulty',
        }

        while current_index < len(chain):
            block = chain[current_index]
            if not isinstance(block, dict) or set(block) != required_block_fields:
                return False
            if block['block_number'] != current_index:
                return False
            if not isinstance(block['nonce'], int) or isinstance(block['nonce'], bool) or block['nonce'] < 0:
                return False
            if not isinstance(block['timestamp'], int) or isinstance(block['timestamp'], bool):
                return False
            if block['previous_hash'] != self.hash(last_block):
                return False
            if not isinstance(block['transactions'], list) or not block['transactions']:
                return False
            if len(block['transactions']) > self.MAX_TRANSACTIONS_PER_BLOCK:
                return False
            if not self.valid_proof(block):
                return False

            transactions = block['transactions'][:-1]
            coinbase_transaction = block['transactions'][-1]

            for transaction in transactions:
                if not isinstance(transaction, dict):
                    return False
                transaction_key = self.transaction_key(transaction)
                if transaction_key in seen_transactions:
                    return False
                seen_transactions.add(transaction_key)
                if not self.apply_transaction(transaction, ledger, require_signature=True):
                    return False

            if not self.apply_coinbase_transaction(coinbase_transaction, ledger, current_index):
                return False

            last_block = block
            current_index += 1

        return True

    def chainwork(self, chain):
        if not self.valid_chain(chain):
            return 0
        return sum(16 ** int(block.get('difficulty', 0)) for block in chain[1:])

    def remove_confirmed_transactions(self, block):
        confirmed_transactions = block.get('transactions', [])[:-1]
        self.transactions = [transaction for transaction in self.transactions
                             if transaction not in confirmed_transactions]

    def accept_block(self, block, relay=True):
        if not isinstance(block, dict):
            return False
        with self._lock:
            if block.get('block_number') != len(self.chain):
                return False
            if block.get('previous_hash') != self.hash(self.chain[-1]):
                return False

            candidate_chain = self.chain + [block]
            if not self.valid_chain(candidate_chain):
                return False

            self.chain.append(copy.deepcopy(block))
            self.remove_confirmed_transactions(block)
            self.update_hyperparameters()
            self.save_everything()

        if relay:
            self.broadcast_block(block)
        return True

    def resolve_conflicts(self):
        """
        Resolve conflicts between blockchain's nodes
        by replacing our chain with the longest one in the network.
        """
        with self._lock:
            neighbours = list(self.nodes)
            current_chain = copy.deepcopy(self.chain)
        new_chain = None

        # Prefer the valid chain with the greatest accumulated work.
        max_work = self.chainwork(current_chain)

        # Grab and verify the chains from all the nodes in our network
        for node in neighbours:
            try:
                response = requests.get(
                    self.peer_url(node, '/chain'),
                    timeout=self.PEER_REQUEST_TIMEOUT,
                )
            except requests.RequestException:
                continue

            if response.status_code == 200:
                try:
                    chain = response.json()['chain']
                except (ValueError, KeyError, TypeError):
                    continue
                try:
                    chain_work = self.chainwork(chain)
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue

                # Check if the peer chain contains more accumulated proof-of-work.
                if chain_work > max_work:
                    max_work = chain_work
                    new_chain = chain

        # Replace our chain if we discovered a new, valid chain longer than ours
        if new_chain:
            with self._lock:
                if self.chainwork(new_chain) <= self.chainwork(self.chain):
                    return False
                pending_transactions = copy.deepcopy(self.transactions)
                self.chain = copy.deepcopy(new_chain)
                self.transactions = []
                for transaction in pending_transactions:
                    self.submit_transaction(
                        transaction.get('sender_address'),
                        transaction.get('recipient_address'),
                        transaction.get('value'),
                        transaction.get('signature'),
                        relay=False,
                    )
                self.MINING_DIFFICULTY = int(self.chain[-1].get('difficulty', self.MINING_DIFFICULTY))
                self.MINING_REWARD = self.block_reward(len(self.chain))
                self.save_everything()
            return True

        return False


    def save_everything(self):
        with self._lock:
            state = {
                'chain': copy.deepcopy(self.chain),
                'transactions': copy.deepcopy(self.transactions),
                'nodes': sorted(self.nodes),
                'node_address': self.node_address,
                'miner_name': self.miner_name,
                'MINING_DIFFICULTY': self.MINING_DIFFICULTY,
                'MINING_REWARD': self.MINING_REWARD,
            }

        self.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.STATE_PATH.with_suffix(self.STATE_PATH.suffix + '.tmp')
        with temporary_path.open('w', encoding='utf8') as state_file:
            json.dump(state, state_file, sort_keys=True, allow_nan=False)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, self.STATE_PATH)


    def load_everything(self, path):
        state_path = Path(path).resolve()
        try:
            with state_path.open('r', encoding='utf8') as f:
                state = json.load(f)
        except FileNotFoundError as exc:
            raise ValueError('State file does not exist') from exc
        except json.JSONDecodeError as exc:
            raise ValueError('State file is not valid JSON') from exc
        if not isinstance(state, dict):
            raise ValueError('State file must contain a JSON object')

        chain = state.get('chain')
        if not self.valid_chain(chain):
            raise ValueError('State file contains an invalid Denarius chain')

        difficulty = state.get('MINING_DIFFICULTY', chain[-1].get('difficulty', 2))
        if not isinstance(difficulty, int) or isinstance(difficulty, bool):
            raise ValueError('State file contains an invalid mining difficulty')
        if difficulty < 1 or difficulty > self.MAX_DIFFICULTY:
            raise ValueError('State file contains an invalid mining difficulty')

        nodes = set()
        for node in state.get('nodes', []):
            if len(nodes) >= self.MAX_PEERS:
                break
            nodes.add(self.normalize_node(node))

        node_address = state.get('node_address', self.node_address)
        if self.public_key_from_address(node_address) is None:
            node_address = self.node_address

        miner_name = state.get('miner_name', self.miner_name)
        if not isinstance(miner_name, str) or not miner_name.strip() or len(miner_name.strip()) > 80:
            raise ValueError('State file contains an invalid miner name')

        pending_validator = Blockchain(miner_name.strip())
        pending_validator.chain = copy.deepcopy(chain)
        pending_validator.node_address = node_address
        for transaction in state.get('transactions', []):
            if not isinstance(transaction, dict):
                continue
            pending_validator.submit_transaction(
                transaction.get('sender_address'),
                transaction.get('recipient_address'),
                transaction.get('value'),
                transaction.get('signature'),
                relay=False,
            )

        with self._lock:
            self.STATE_PATH = state_path
            self.chain = copy.deepcopy(chain)
            self.transactions = copy.deepcopy(pending_validator.transactions)
            self.nodes = nodes
            self.node_address = node_address
            self.miner_name = miner_name.strip()
            self.MINING_DIFFICULTY = difficulty
            self.MINING_REWARD = self.block_reward(len(self.chain))
        return self



app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.secret_key = os.environ.get('DENARIUS_SECRET_KEY') or secrets.token_hex(32)
CORS(app, resources={
    r'/chain': {'origins': '*'},
    r'/miner/get': {'origins': '*'},
    r'/nodes/get': {'origins': '*'},
    r'/transactions/get': {'origins': '*'},
    r'/transactions/new': {'origins': '*'},
})

blockchain = Blockchain()
registered_user = None
PASSWORD_ITERATIONS = 240000


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), salt, PASSWORD_ITERATIONS)
    return salt.hex() + ':' + digest.hex()


def verify_password(password, encoded_password):
    try:
        salt_hex, expected_hex = encoded_password.split(':', 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (AttributeError, TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf8'), salt, PASSWORD_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def ensure_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if registered_user is None:
            return redirect(url_for('register'))
        if 'user' not in session:
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    return wrapper


def csrf_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        submitted_token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        session_token = session.get('csrf_token')
        if not session_token or not submitted_token or not hmac.compare_digest(session_token, submitted_token):
            return jsonify({'message': 'Invalid CSRF token'}), 403
        return func(*args, **kwargs)
    return wrapper


@app.route('/')
@login_required
def index():
    return render_template('index.html', csrf_token=ensure_csrf_token())


@app.route('/configure')
@login_required
def configure():
    return render_template('./configure.html', csrf_token=ensure_csrf_token())


@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    values = request.form

    # Check that the required fields are in the POST'ed data
    required = ['sender_address', 'recipient_address', 'amount', 'signature']
    if not all(k in values for k in required):
        return 'Missing values', 400
    # Create a new Transaction
    transaction_result = blockchain.submit_transaction(values['sender_address'], values['recipient_address'],
                                                       values['amount'], values['signature'])

    if transaction_result == False:
        response = {'message': 'Invalid Transaction!'}
        return jsonify(response), 406
    else:
        response = {'message': 'Transaction will be added to Block ' + str(transaction_result)}
        return jsonify(response), 201


@app.route('/transactions/get', methods=['GET'])
def get_transactions():
    # Get transactions from transactions pool
    with blockchain._lock:
        transactions = copy.deepcopy(blockchain.transactions)

    response = {'transactions': transactions}
    return jsonify(response), 200


@app.route('/transactions/receive', methods=['POST'])
def receive_transaction():
    values = request.form

    required = ['sender_address', 'recipient_address', 'amount', 'signature']
    if not all(k in values for k in required):
        return 'Missing values', 400

    transaction_result = blockchain.submit_transaction(values['sender_address'], values['recipient_address'],
                                                       values['amount'], values['signature'])

    if transaction_result == False:
        response = {'message': 'Invalid Transaction!'}
        return jsonify(response), 406

    response = {'message': 'Transaction accepted from peer'}
    return jsonify(response), 201


@app.route('/chain', methods=['GET'])
def full_chain():
    with blockchain._lock:
        chain = copy.deepcopy(blockchain.chain)
    response = {
        'chain': chain,
        'length': len(chain),
    }
    return jsonify(response), 200


@app.route('/mine', methods=['POST'])
@login_required
@csrf_required
def mine():
    block = blockchain.mine_pending_transactions()
    if block is False:
        response = {'message': 'Unable to mine: configure a valid miner or retry after the chain changes'}
        return jsonify(response), 406

    response = {
        'message': "New Block Forged",
        'block_number': block['block_number'],
        'transactions': block['transactions'],
        'nonce': block['nonce'],
        'previous_hash': block['previous_hash'],
    }
    return jsonify(response), 200


@app.route('/blocks/receive', methods=['POST'])
def receive_block():
    values = request.get_json(silent=True) or {}
    block = values.get('block')

    if not isinstance(block, dict):
        return jsonify({'message': 'Invalid block'}), 400

    if blockchain.accept_block(block):
        return jsonify({'message': 'Block accepted'}), 201

    blockchain.resolve_conflicts()
    return jsonify({'message': 'Block rejected'}), 406


@app.route('/miner/register', methods=['POST'])
@login_required
@csrf_required
def register_miner():
    values = request.form
    address = values.get('address')
    name = values.get('name')

    if address is None or name is None:
        return "Error: Please add valid address and name", 400

    try:
        blockchain.set_miner_info(name, address)
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400

    blockchain.save_everything()

    response = {
        'message': 'Miner information has been updated',
        'miner': address,
    }
    return jsonify(response), 201


@app.route('/nodes/register', methods=['POST'])
@login_required
@csrf_required
def register_nodes():
    values = request.form
    submitted_nodes = values.get('nodes')

    if not submitted_nodes:
        return "Error: Please supply a valid list of nodes", 400

    nodes = submitted_nodes.replace(" ", "").split(',')

    try:
        for node in nodes:
            if node:
                blockchain.register_node(node)
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400

    blockchain.exchange_peer_table()
    blockchain.save_everything()

    response = {
        'message': 'New nodes have been added',
        'total_nodes': [node for node in blockchain.nodes],
    }
    return jsonify(response), 201


@app.route('/nodes/resolve', methods=['POST'])
@login_required
@csrf_required
def consensus():
    replaced = blockchain.resolve_conflicts()

    if replaced:
        response = {
            'message': 'Our chain was replaced',
            'new_chain': blockchain.chain
        }
    else:
        response = {
            'message': 'Our chain is authoritative',
            'chain': blockchain.chain
        }
    return jsonify(response), 200


@app.route('/nodes/get', methods=['GET'])
def get_nodes():
    with blockchain._lock:
        nodes = sorted(blockchain.nodes)
    response = {'nodes': nodes}
    return jsonify(response), 200


@app.route('/miner/get', methods=['GET'])
def get_miner_info():
    with blockchain._lock:
        miner_name = blockchain.miner_name
        miner_address = blockchain.node_address
    response = {'name': miner_name,
                'address': miner_address,
                'balance': blockchain.get_balance(miner_address)
                }
    return jsonify(response), 200




@app.route('/register', methods=['GET', 'POST'])
def register():
    global registered_user
    if registered_user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        submitted_token = request.form.get('csrf_token')
        if not submitted_token or not hmac.compare_digest(ensure_csrf_token(), submitted_token):
            return 'Invalid CSRF token', 403
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or len(username) > 64 or len(password) < 10:
            return 'Username is required and password must be at least 10 characters', 400
        registered_user = {'username': username, 'password_hash': hash_password(password)}
        session.clear()
        session['user'] = username
        ensure_csrf_token()
        return redirect(url_for('index'))
    return render_template('register.html', csrf_token=ensure_csrf_token())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        submitted_token = request.form.get('csrf_token')
        if not submitted_token or not hmac.compare_digest(ensure_csrf_token(), submitted_token):
            return 'Invalid CSRF token', 403
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if (registered_user and username == registered_user['username']
                and verify_password(password, registered_user['password_hash'])):
            session.clear()
            session['user'] = username
            ensure_csrf_token()
            return redirect(url_for('index'))
        return 'Invalid credentials', 403
    return render_template('login.html', csrf_token=ensure_csrf_token())


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))



if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-p', '--port', default=5000, type=int, help='port to listen on')
    parser.add_argument('-r', '--resume', default=None, type=str, help='resume to a certain state')
    args = parser.parse_args()
    port = args.port
    path = args.resume

    # resume to the state
    if path:
        try:
            blockchain = blockchain.load_everything(path)
        except ValueError as exc:
            parser.error(str(exc))

    app.run(host='127.0.0.1', port=port)
