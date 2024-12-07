import binascii
import hashlib
import json
from collections import OrderedDict
from time import time
from urllib.parse import urlparse
from uuid import uuid1

import requests
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from flask import Flask, jsonify, request, render_template, redirect, session, url_for
from flask_cors import CORS

import pickle

class Blockchain:
    def __init__(self, name="THE BLOCKCHAIN"):

        self.transactions = []
        self.chain = []
        self.nodes = set()
        # Generate random number to be used as node_id
        self.node_address = str(uuid1()).replace('-', '')
        # Create genesis block
        self.create_block(0, '00')
        self.MINING_SENDER = name
        self.MINING_DIFFICULTY = 2
        self.TOTAL_AMOUNT = 100000000.0 # 1e8
        self.MINING_REWARD = self.TOTAL_AMOUNT / 100 / (6 * 30 * 24 * 6)

    def set_miner_info(self, name, address):
        """
        Set miner's infomation
        :param name: Miner's name.
        :param address: Miner's public key address.
        :return: None
        """
        self.MINING_SENDER = name
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
            time_diff = self.chain[last_index] - self.chain[last_index - 2016]
            two_week = 2 * 7 * 24 * 60 * 60 * 1.0
            self.MINING_DIFFICULTY *= (two_week / time_diff)

        if last_index % (6 * 30 * 24 * 6) == 0 and last_index != 0:
            self.MINING_REWARD *= 0.5

    def register_node(self, node_url):
        """
        Add a new node to the list of nodes
        """
        # Checking node_url has valid format
        parsed_url = urlparse(node_url)
        if parsed_url.netloc:
            self.nodes.add(parsed_url.netloc)
        elif parsed_url.path:
            # Accepts an URL without scheme like '192.168.0.5:5000'.
            self.nodes.add(parsed_url.path)
        else:
            raise ValueError('Invalid URL')

    def verify_transaction_signature(self, sender_address, signature, transaction):
        """
        Check that the provided signature corresponds to transaction
        signed by the public key (sender_address)
        """
        public_key = RSA.importKey(binascii.unhexlify(sender_address))
        verifier = PKCS1_v1_5.new(public_key)
        h = SHA.new(str(transaction).encode('utf8'))
        return verifier.verify(h, binascii.unhexlify(signature))

    def get_balance(self, address):
        """
        Get the balance noted on the address
        :param address: the address of the account
        :return: balance (Float)
        """
        balance = 0.0
        for i, c in reversed(list(enumerate(self.chain))):
            for j, t in enumerate(c['transactions']):
                if t['sender_address'] == address and t['recipient_address'] == address:
                    pass
                elif t['recipient_address'] == address:
                    balance += float(t['value'])
                elif t['sender_address'] == address:
                    balance -= float(t['value'])
        return balance

    def verify_enough_balance(self, address, value):
        """
        Check that the sender has enough balance in his wallet.
        Greedy search, in theory faster than get_balanece()
        :param sender_address: address of the sender
        :param value: value to be sent
        :return: True if sender has enough balance else False
        """
        # return True
        balance = 0.0
        for i, c in reversed(list(enumerate(self.chain))):
            for j, t in enumerate(c['transactions']):
                if t['sender_address'] == address and t['recipient_address'] == address:
                    pass
                elif t['recipient_address'] == address:
                    balance += float(t['value'])
                elif t['sender_address'] == address:
                    balance -= float(t['value'])
            if balance >= float(value):
                return True
        return False

    def submit_transaction(self, sender_address, recipient_address, value, signature):
        """
        Add a transaction to transactions array if the signature verified
        """
        transaction = OrderedDict({'sender_address': sender_address,
                                   'recipient_address': recipient_address,
                                   'value': value})

        # Reward for mining a block
        if sender_address == self.MINING_SENDER:
            self.transactions.append(transaction)
            return len(self.chain)
        # Manages transactions from wallet to another wallet
        else:
            transaction_verification = self.verify_transaction_signature(sender_address, signature, transaction)
            enough_balance = self.verify_enough_balance(sender_address, value)
            if transaction_verification and enough_balance:
                self.transactions.append(transaction)
                return len(self.chain)
            else:
                return False
                print('Ah Shit')

    def create_block(self, nonce, previous_hash):
        """
        Add a block of transactions to the blockchain
        """
        block = {'block_number': len(self.chain),
                 'timestamp': time(),
                 'transactions': self.transactions,
                 'nonce': nonce,
                 'previous_hash': previous_hash}

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
        while self.valid_proof(self.transactions, last_hash, nonce) is False:
            nonce += 1

        return nonce

    def valid_proof(self, transactions, last_hash, nonce):
        """
        Check if a hash value satisfies the mining conditions. This function is used within the proof_of_work function.
        """
        guess = (str(transactions) + str(last_hash) + str(nonce)).encode()
        guess_hash = hashlib.sha256(guess).hexdigest()
        difficulty = self.MINING_DIFFICULTY
        return guess_hash[:difficulty] == '0' * difficulty

    def valid_chain(self, chain):
        """
        check if a blockchain is valid
        """
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

            # Check that the Proof of Work is correct
            # Delete the reward transaction
            transactions = block['transactions'][:-1]
            # Need to make sure that the dictionary is ordered. Otherwise we'll get a different hash
            transaction_elements = ['sender_address', 'recipient_address', 'value']
            transactions = [OrderedDict((k, transaction[k]) for k in transaction_elements) for transaction in
                            transactions]

            if not self.valid_proof(transactions, block['previous_hash'], block['nonce']):
                return False

            last_block = block
            current_index += 1

        return True

    def resolve_conflicts(self):
        """
        Resolve conflicts between blockchain's nodes
        by replacing our chain with the longest one in the network.
        """
        neighbours = self.nodes
        new_chain = None

        # We're only looking for chains longer than ours
        max_length = len(self.chain)

        # Grab and verify the chains from all the nodes in our network
        for node in neighbours:
            print('http://' + node + '/chain')
            response = requests.get('http://' + node + '/chain', verify='../certificates/cert.pem')

            if response.status_code == 200:
                length = response.json()['length']
                chain = response.json()['chain']

                # Check if the length is longer and the chain is valid
                if length > max_length and self.valid_chain(chain):
                    max_length = length
                    new_chain = chain

        # Replace our chain if we discovered a new, valid chain longer than ours
        if new_chain:
            self.chain = new_chain
            return True

        return False


    def save_everything(self):
        with open('../states/blockchain.pkl', 'w+b') as f:
            pickle.dump(self, f)


    def load_everything(self, path):
        try:
            with open(path, 'rb') as f:
                return pickle.load(f)
        except FileNotFoundError:
            return self



app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
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


@app.route('/chain', methods=['GET'])
def full_chain():
    response = {
        'chain': blockchain.chain,
        'length': len(blockchain.chain),
    }
    return jsonify(response), 200


@app.route('/mine', methods=['GET'])
def mine():
    # We run the proof of work algorithm to get the next proof...
    last_block = blockchain.chain[-1]
    nonce = blockchain.proof_of_work()

    # We must receive a reward for finding the proof.
    blockchain.submit_transaction(sender_address=blockchain.MINING_SENDER, recipient_address=blockchain.node_address,
                                  value=blockchain.MINING_REWARD, signature="")

    # Forge the new Block by adding it to the chain
    previous_hash = blockchain.hash(last_block)
    block = blockchain.create_block(nonce, previous_hash)

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


@app.route('/miner/register', methods=['POST'])
def register_miner():
    values = request.form
    address = values.get('address')
    name = values.get('name')

    if address is None or name is None:
        return "Error: Please add valid address and name", 400

    blockchain.set_miner_info(name, address)

    response = {
        'message': 'Miner information has been updated',
        'miner': address,
    }
    return jsonify(response), 201


@app.route('/nodes/register', methods=['POST'])
def register_nodes():
    values = request.form
    nodes = values.get('nodes').replace(" ", "").split(',')

    if nodes is None:
        return "Error: Please supply a valid list of nodes", 400

    for node in nodes:
        blockchain.register_node(node)

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
    response = {'name': blockchain.MINING_SENDER,
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
