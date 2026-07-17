import binascii
import hashlib
import json
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
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
        self.miner_name = name
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
                self.MINING_DIFFICULTY = max(1, int(round(self.MINING_DIFFICULTY * adjustment)))

        if last_index % (6 * 30 * 24 * 6) == 0 and last_index != 0:
            self.MINING_REWARD = self.block_reward(last_index + 1)

    def normalize_node(self, node_url):
        parsed_url = urlparse(node_url)
        if parsed_url.netloc:
            return parsed_url.netloc
        if parsed_url.path:
            return parsed_url.path
        raise ValueError('Invalid URL')

    def peer_url(self, node, path):
        return 'http://' + node + path

    def register_node(self, node_url):
        """
        Add a new node to the list of nodes
        """
        if len(self.nodes) >= self.MAX_PEERS:
            raise ValueError('Peer limit reached')
        self.nodes.add(self.normalize_node(node_url))

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
        for i, c in reversed(list(enumerate(self.chain))):
            for j, t in enumerate(c['transactions']):
                if t['sender_address'] == address and t['recipient_address'] == address:
                    pass
                elif t['recipient_address'] == address:
                    balance += self.parse_atomic_value(t['value']) or 0
                elif t['sender_address'] == address:
                    balance -= self.parse_atomic_value(t['value']) or 0
        return self.format_amount(balance)

    def get_atomic_balance(self, address, include_pending=False):
        balance = 0
        for c in self.chain:
            for t in c['transactions']:
                atomic_value = self.parse_atomic_value(t['value']) or 0
                if t['sender_address'] == address and t['recipient_address'] == address:
                    continue
                if t['recipient_address'] == address:
                    balance += atomic_value
                if t['sender_address'] == address:
                    balance -= atomic_value

        if include_pending:
            for t in self.transactions:
                atomic_value = self.parse_atomic_value(t['value']) or 0
                if t['sender_address'] == address:
                    balance -= atomic_value
        return balance

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
        if len(self.transactions) >= self.MAX_PENDING_TRANSACTIONS:
            return False

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
        enough_balance = self.verify_enough_balance(sender_address, atomic_value)
        if transaction_verification and enough_balance:
            signed_transaction = OrderedDict(transaction)
            signed_transaction['signature'] = signature
            if signed_transaction in self.transactions:
                return False
            self.transactions.append(signed_transaction)
            if relay:
                self.broadcast_transaction(signed_transaction)
            return len(self.chain)
        return False

    def create_coinbase_transaction(self):
        return OrderedDict({'sender_address': self.COINBASE_SENDER,
                            'recipient_address': self.node_address,
                            'value': str(self.block_reward(len(self.chain)))})

    def create_block(self, nonce, previous_hash):
        """
        Add a block of transactions to the blockchain
        """
        if len(self.transactions) > self.MAX_TRANSACTIONS_PER_BLOCK:
            return False

        block = {'block_number': len(self.chain),
                 'timestamp': time(),
                 'transactions': self.transactions,
                 'nonce': nonce,
                 'previous_hash': previous_hash,
                 'difficulty': int(self.MINING_DIFFICULTY)}

        # Reset the current list of transactions
        self.transactions = []

        self.chain.append(block)

        # Update hyperparameters.
        self.update_hyperparameters()

        return block

    def hash(self, block):
        """
        Create a SHA-256 hash of a block
        """
        # We must make sure that the Dictionary is Ordered, or we'll have inconsistent hashes
        block_string = json.dumps(block, sort_keys=True).encode()

        return hashlib.sha256(block_string).hexdigest()

    def proof_of_work(self):
        """
        Proof of work algorithm
        """
        last_block = self.chain[-1]
        last_hash = self.hash(last_block)

        nonce = 0
        transactions = self.proof_transactions(self.transactions)
        while self.valid_proof(transactions, last_hash, nonce, self.MINING_DIFFICULTY) is False:
            nonce += 1

        return nonce

    def proof_transactions(self, transactions):
        transaction_elements = ['sender_address', 'recipient_address', 'value', 'signature']
        return [OrderedDict((k, transaction[k]) for k in transaction_elements if k in transaction)
                for transaction in transactions]

    def valid_proof(self, transactions, last_hash, nonce, difficulty=None):
        """
        Check if a hash value satisfies the mining conditions. This function is used within the proof_of_work function.
        """
        guess = (str(transactions) + str(last_hash) + str(nonce)).encode()
        guess_hash = hashlib.sha256(guess).hexdigest()
        difficulty = int(difficulty if difficulty is not None else self.MINING_DIFFICULTY)
        return guess_hash[:difficulty] == '0' * difficulty

    def apply_transaction(self, transaction, ledger, require_signature=True):
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
        if not chain or chain[0] != self.GENESIS_BLOCK:
            return False

        ledger = {}
        seen_transactions = set()
        last_block = chain[0]
        current_index = 1

        while current_index < len(chain):
            block = chain[current_index]
            # print(last_block)
            # print(block)
            # print("\n-----------\n")
            # Check that the hash of the block is correct
            if block['previous_hash'] != self.hash(last_block):
                return False

            if block.get('block_number') != current_index:
                return False

            difficulty = block.get('difficulty')
            if not isinstance(difficulty, int) or difficulty < 1:
                return False

            if not block.get('transactions'):
                return False

            if len(block.get('transactions', [])) > self.MAX_TRANSACTIONS_PER_BLOCK:
                return False

            # Check that the Proof of Work is correct
            # Delete the reward transaction
            transactions = block['transactions'][:-1]
            coinbase_transaction = block['transactions'][-1]
            # Need to make sure that the dictionary is ordered. Otherwise we'll get a different hash
            transactions = self.proof_transactions(transactions)

            if not self.valid_proof(transactions, block['previous_hash'], block['nonce'], difficulty):
                return False

            for transaction in transactions:
                transaction_key = json.dumps(transaction, sort_keys=True, separators=(',', ':'))
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
        if block.get('block_number') != len(self.chain):
            return False
        if block.get('previous_hash') != self.hash(self.chain[-1]):
            return False
        if len(block.get('transactions', [])) > self.MAX_TRANSACTIONS_PER_BLOCK:
            return False

        candidate_chain = self.chain + [block]
        if not self.valid_chain(candidate_chain):
            return False

        self.chain.append(block)
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
        neighbours = self.nodes
        new_chain = None

        # Prefer the valid chain with the greatest accumulated work.
        max_work = self.chainwork(self.chain)

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
                chain_work = self.chainwork(chain)

                # Check if the peer chain contains more accumulated proof-of-work.
                if chain_work > max_work:
                    max_work = chain_work
                    new_chain = chain

        # Replace our chain if we discovered a new, valid chain longer than ours
        if new_chain:
            self.chain = new_chain
            return True

        return False


    def save_everything(self):
        state = {
            'chain': self.chain,
            'transactions': self.transactions,
            'nodes': sorted(self.nodes),
            'node_address': self.node_address,
            'miner_name': self.miner_name,
            'MINING_DIFFICULTY': self.MINING_DIFFICULTY,
            'MINING_REWARD': self.MINING_REWARD,
        }
        self.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with self.STATE_PATH.open('w', encoding='utf8') as f:
            json.dump(state, f, sort_keys=True)


    def load_everything(self, path):
        try:
            with open(path, 'r', encoding='utf8') as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return self

        self.chain = state.get('chain', self.chain)
        self.transactions = state.get('transactions', [])
        self.nodes = set(state.get('nodes', []))
        self.node_address = state.get('node_address', self.node_address)
        self.miner_name = state.get('miner_name', self.miner_name)
        self.MINING_DIFFICULTY = state.get('MINING_DIFFICULTY', self.MINING_DIFFICULTY)
        self.MINING_REWARD = state.get('MINING_REWARD', self.MINING_REWARD)
        return self



app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
app.secret_key = 'your_secret_key'  # Replace with a strong key
CORS(app)

blockchain = Blockchain()
registered_user = None  # To keep track of the first registered user


def login_required(func):
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# Instantiate the Blockchain
blockchain = Blockchain()


@app.route('/')
def index():
    if not registered_user:
        return redirect(url_for('register'))
    return render_template('index.html')


@app.route('/configure')
def configure():
    return render_template('./configure.html')


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
    transactions = blockchain.transactions

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
    response = {
        'chain': blockchain.chain,
        'length': len(blockchain.chain),
    }
    return jsonify(response), 200


@app.route('/mine', methods=['GET'])
def mine():
    if blockchain.public_key_from_address(blockchain.node_address) is None:
        response = {'message': 'Please register a valid Denarius miner address before mining'}
        return jsonify(response), 400

    # We run the proof of work algorithm to get the next proof...
    last_block = blockchain.chain[-1]
    nonce = blockchain.proof_of_work()

    # We must receive a reward for finding the proof.
    blockchain.transactions.append(blockchain.create_coinbase_transaction())

    # Forge the new Block by adding it to the chain
    previous_hash = blockchain.hash(last_block)
    block = blockchain.create_block(nonce, previous_hash)
    if block is False:
        response = {'message': 'Block has too many transactions'}
        return jsonify(response), 406

    blockchain.broadcast_block(block)

    # save this state
    blockchain.save_everything()

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
def register_miner():
    values = request.form
    address = values.get('address')
    name = values.get('name')

    if address is None or name is None:
        return "Error: Please add valid address and name", 400

    try:
        blockchain.set_miner_info(name, address)
    except ValueError:
        return "Error: Please add a valid Denarius address", 400

    response = {
        'message': 'Miner information has been updated',
        'miner': address,
    }
    return jsonify(response), 201


@app.route('/nodes/register', methods=['POST'])
def register_nodes():
    values = request.form
    submitted_nodes = values.get('nodes')

    if not submitted_nodes:
        return "Error: Please supply a valid list of nodes", 400

    nodes = submitted_nodes.replace(" ", "").split(',')

    for node in nodes:
        if node:
            blockchain.register_node(node)

    blockchain.exchange_peer_table()

    response = {
        'message': 'New nodes have been added',
        'total_nodes': [node for node in blockchain.nodes],
    }
    return jsonify(response), 201


@app.route('/nodes/resolve', methods=['GET'])
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
    nodes = list(blockchain.nodes)
    response = {'nodes': nodes}
    return jsonify(response), 200


@app.route('/miner/get', methods=['GET'])
def get_miner_info():
    response = {'name': blockchain.miner_name,
                'address': blockchain.node_address,
                'balance': blockchain.get_balance(blockchain.node_address)
                }
    return jsonify(response), 200




@app.route('/register', methods=['GET', 'POST'])
def register():
    global registered_user
    if registered_user:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        registered_user = {'username': username, 'password': password}
        session['user'] = username
        return redirect(url_for('index'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if registered_user and username == registered_user['username'] and password == registered_user['password']:
            session['user'] = username
            return redirect(url_for('index'))
        return 'Invalid credentials', 403
    return render_template('login.html')


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
        blockchain = blockchain.load_everything(path)

    # Run with SSL support
    #app.run(host='127.0.0.1', port=port, ssl_context=("../certificates/cert.pem", "../certificates/key.pem"))

    # Run without SSL support
    app.run(host='127.0.0.1', port=port)
