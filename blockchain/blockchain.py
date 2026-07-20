import binascii
import copy
import hmac
import os
import sys
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
from flask import Flask, jsonify, request
from flask_cors import CORS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from denarius_protocol import (
    ATOMIC_UNITS,
    BLOCK_FIELDS,
    BLOCK_HEADER_FIELDS,
    COINBASE_FIELDS,
    COINBASE_SENDER,
    GENESIS_BLOCK,
    GENESIS_HASH,
    HALVING_INTERVAL,
    INITIAL_BLOCK_REWARD,
    INITIAL_TARGET,
    MAX_FUTURE_BLOCK_SECONDS,
    MAX_SUPPLY_ATOMIC,
    MAX_TARGET,
    MEDIAN_TIME_BLOCKS,
    NETWORK_ID,
    PEER_API_VERSION,
    PROTOCOL_VERSION,
    RETARGET_INTERVAL,
    RETARGET_TIMESPAN,
    SIGNED_TRANSACTION_FIELDS,
    block_hash,
    block_header,
    block_reward,
    calculate_merkle_root,
    canonical_json_bytes,
    coinbase_transaction,
    signed_transaction_id,
    target_from_hex,
    target_to_hex,
    transaction_signing_payload,
    work_for_target,
)
from denarius_crypto import address_from_public_key, public_key_from_address
from denarius_network import PeerNetwork, protocol_identity
from denarius_paths import state_path
from denarius_storage import DenariusStorage, migrate_json_state

class Blockchain:
    PROTOCOL_VERSION = PROTOCOL_VERSION
    NETWORK_ID = NETWORK_ID
    ATOMIC_UNITS = ATOMIC_UNITS
    TOTAL_AMOUNT = MAX_SUPPLY_ATOMIC
    REWARD_HALVING_INTERVAL = HALVING_INTERVAL
    INITIAL_BLOCK_REWARD = INITIAL_BLOCK_REWARD
    COINBASE_SENDER = COINBASE_SENDER
    PEER_REQUEST_TIMEOUT = 3
    MAX_PEERS = 128
    MAX_HEADERS_PER_REQUEST = 512
    MAX_BLOCKS_PER_REQUEST = 32
    MAX_SYNC_HEADERS = 100000
    MAX_PENDING_TRANSACTIONS = 1000
    MAX_TRANSACTIONS_PER_BLOCK = 1000
    GENESIS_BLOCK = GENESIS_BLOCK
    STATE_PATH = state_path('denarius.db')

    def __init__(self, name="THE BLOCKCHAIN"):

        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._sync_stop = threading.Event()
        self._sync_wakeup = threading.Event()
        self._sync_thread = None
        self.transactions = []
        self.chain = []
        self.nodes = set()
        self.advertised_node = None
        self.network = PeerNetwork(
            timeout=self.PEER_REQUEST_TIMEOUT,
            requests_module=requests,
        )
        # Generate random number to be used as node_id
        self.node_address = str(uuid1()).replace('-', '')
        # Create genesis block
        self.chain.append(dict(self.GENESIS_BLOCK))
        self.miner_name = name
        self.MINING_TARGET = INITIAL_TARGET

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
        return block_reward(height)

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
        self.MINING_TARGET = self.expected_target(self.chain, len(self.chain))

    def expected_target(self, chain, height):
        if height <= 0:
            return INITIAL_TARGET

        previous_target = target_from_hex(chain[height - 1]['target'])
        if height % RETARGET_INTERVAL != 0:
            return previous_target

        first_block = chain[height - RETARGET_INTERVAL]
        last_block = chain[height - 1]
        elapsed = last_block['timestamp'] - first_block['timestamp']
        elapsed = max(RETARGET_TIMESPAN // 4, min(RETARGET_TIMESPAN * 4, elapsed))
        adjusted_target = previous_target * elapsed // RETARGET_TIMESPAN
        return max(1, min(MAX_TARGET, adjusted_target))

    def median_time_past(self, chain):
        timestamps = [
            block['timestamp']
            for block in chain[-MEDIAN_TIME_BLOCKS:]
        ]
        timestamps.sort()
        return timestamps[len(timestamps) // 2]

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
        return self.network.peer_url(node, path)

    def register_node(self, node_url):
        """
        Add a new node to the list of nodes
        """
        normalized_node = self.normalize_node(node_url)
        with self._lock:
            if normalized_node == self.advertised_node:
                raise ValueError('Cannot register the current node as a peer')
            if len(self.nodes) >= self.MAX_PEERS and normalized_node not in self.nodes:
                raise ValueError('Peer limit reached')
            self.nodes.add(normalized_node)

    def exchange_peer_table(self):
        with self._lock:
            peers = list(self.nodes)
            original_count = len(self.nodes)
        for node in peers:
            payload = self.network.get_json(node, '/nodes/get')
            if payload is None:
                continue
            discovered = payload.get('nodes')
            if not isinstance(discovered, list):
                self.network.health.record_failure(node, 'Peer returned an invalid peer table')
                continue
            for peer in discovered:
                try:
                    self.register_node(peer)
                except ValueError:
                    continue
                with self._lock:
                    if len(self.nodes) >= self.MAX_PEERS:
                        break
        with self._lock:
            return len(self.nodes) - original_count

    def broadcast_transaction(self, transaction):
        self.network.seen_transactions.add(transaction.get('transaction_id'))
        with self._lock:
            peers = list(self.nodes)
        self.network.relay_transaction(peers, transaction)

    def broadcast_block(self, block):
        try:
            block_id = self.hash(block)
        except (KeyError, TypeError, ValueError):
            return
        self.network.seen_blocks.add(block_id)
        with self._lock:
            peers = list(self.nodes)
        self.network.relay_block(peers, block)

    def has_seen_transaction(self, transaction_id):
        if self.network.seen_transactions.contains(transaction_id):
            return True
        with self._lock:
            if any(tx.get('transaction_id') == transaction_id for tx in self.transactions):
                return True
            return transaction_id in self.confirmed_transaction_keys()

    def has_seen_block(self, block_id):
        if self.network.seen_blocks.contains(block_id):
            return True
        with self._lock:
            return any(self.hash(block) == block_id for block in self.chain)

    def peer_health(self):
        with self._lock:
            peers = list(self.nodes)
        return self.network.peer_health(peers)

    def protocol_status(self):
        with self._lock:
            chain = copy.deepcopy(self.chain)
        status = protocol_identity()
        status.update({
            'node': self.advertised_node,
            'height': len(chain) - 1,
            'tip_hash': self.hash(chain[-1]),
            'chainwork': str(sum(
                work_for_target(target_from_hex(block['target']))
                for block in chain[1:]
            )),
        })
        return status

    def canonical_transaction_bytes(self, transaction):
        return canonical_json_bytes(transaction)

    def address_from_public_key(self, public_key_bytes):
        return address_from_public_key(public_key_bytes)

    def public_key_from_address(self, address):
        return public_key_from_address(address)

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
                    balance += self.parse_atomic_value(t['amount_atomic']) or 0
                elif t['sender_address'] == address:
                    balance -= self.parse_atomic_value(t['amount_atomic']) or 0
        return self.format_amount(balance)

    def get_atomic_balance(self, address, include_pending=False):
        balance = 0
        with self._lock:
            chain = copy.deepcopy(self.chain)
            pending_transactions = copy.deepcopy(self.transactions) if include_pending else []

        for c in chain:
            for t in c['transactions']:
                atomic_value = self.parse_atomic_value(t['amount_atomic']) or 0
                if t['sender_address'] == address and t['recipient_address'] == address:
                    continue
                if t['recipient_address'] == address:
                    balance += atomic_value
                if t['sender_address'] == address:
                    balance -= atomic_value

        if include_pending:
            for t in pending_transactions:
                atomic_value = self.parse_atomic_value(t['amount_atomic']) or 0
                if t['sender_address'] == address and t['recipient_address'] == address:
                    continue
                if t['sender_address'] == address:
                    balance -= atomic_value
        return balance

    def get_confirmed_nonce(self, address, chain=None):
        nonce = 0
        blocks = chain if chain is not None else self.chain
        for block in blocks:
            for transaction in block.get('transactions', []):
                if transaction.get('sender_address') == address:
                    nonce += 1
        return nonce

    def get_next_nonce(self, address):
        with self._lock:
            nonce = self.get_confirmed_nonce(address)
            nonce += sum(
                transaction.get('sender_address') == address
                for transaction in self.transactions
            )
        return nonce

    def transaction_key(self, transaction):
        transaction_id = transaction.get('transaction_id')
        if not isinstance(transaction_id, str) or len(transaction_id) != 64:
            raise ValueError('Invalid transaction ID')
        return transaction_id

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

    def submit_transaction(
        self,
        sender_address,
        recipient_address,
        value,
        nonce,
        signature,
        transaction_id,
        relay=True,
    ):
        """
        Add a transaction to transactions array if the signature verified
        """
        atomic_value = self.parse_atomic_value(value)
        if atomic_value is None:
            return False

        try:
            nonce = int(str(nonce))
        except (TypeError, ValueError):
            return False
        if nonce < 0:
            return False

        transaction = transaction_signing_payload(
            sender_address,
            recipient_address,
            atomic_value,
            nonce,
        )

        if sender_address == self.COINBASE_SENDER:
            return False
        if self.public_key_from_address(recipient_address) is None:
            return False

        transaction_verification = self.verify_transaction_signature(sender_address, signature, transaction)
        if not transaction_verification:
            return False

        expected_transaction_id = signed_transaction_id(transaction, signature)
        if not isinstance(transaction_id, str):
            return False
        if not hmac.compare_digest(transaction_id, expected_transaction_id):
            return False

        signed_transaction = OrderedDict(transaction)
        signed_transaction['signature'] = signature
        signed_transaction['transaction_id'] = expected_transaction_id

        with self._lock:
            if len(self.transactions) >= self.MAX_PENDING_TRANSACTIONS:
                return False
            if nonce != self.get_next_nonce(sender_address):
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

        self.network.seen_transactions.add(expected_transaction_id)
        if relay:
            self.broadcast_transaction(signed_transaction)
        return next_block_number

    def create_coinbase_transaction(self, height=None, recipient_address=None):
        height = len(self.chain) if height is None else height
        recipient_address = self.node_address if recipient_address is None else recipient_address
        return coinbase_transaction(recipient_address, self.block_reward(height), height)

    def create_candidate_block(self):
        with self._lock:
            height = len(self.chain)
            previous_hash = self.hash(self.chain[-1])
            pending_limit = max(0, self.MAX_TRANSACTIONS_PER_BLOCK - 1)
            pending_snapshot = copy.deepcopy(self.transactions[:pending_limit])
            recipient_address = self.node_address
            target = self.expected_target(self.chain, height)
            timestamp = max(int(time()), self.median_time_past(self.chain) + 1)

        coinbase = self.create_coinbase_transaction(height, recipient_address)
        transactions = [coinbase] + pending_snapshot
        block = {
            'version': self.PROTOCOL_VERSION,
            'network': self.NETWORK_ID,
            'block_number': height,
            'timestamp': timestamp,
            'merkle_root': calculate_merkle_root(transactions),
            'nonce': 0,
            'previous_hash': previous_hash,
            'target': target_to_hex(target),
            'transactions': transactions,
        }
        return block

    def hash(self, block):
        """
        Create a SHA-256 hash of a block
        """
        return block_hash(block)

    def proof_of_work(self, candidate_block):
        """
        Find a nonce for a complete candidate block. Every consensus field,
        including the coinbase reward, is committed by the proof.
        """
        if not isinstance(candidate_block, dict):
            raise ValueError('Invalid candidate block')
        try:
            target_from_hex(candidate_block.get('target'))
        except ValueError as exc:
            raise ValueError('Invalid candidate block target') from exc
        transactions = candidate_block.get('transactions')
        if not isinstance(transactions, list):
            raise ValueError('Invalid candidate block transactions')
        if not all(self.has_valid_transaction_id(tx) for tx in transactions):
            raise ValueError('Invalid candidate transaction ID')
        try:
            if candidate_block.get('merkle_root') != calculate_merkle_root(transactions):
                raise ValueError('Invalid candidate Merkle root')
        except (TypeError, ValueError) as exc:
            raise ValueError('Invalid candidate Merkle root') from exc

        block = copy.deepcopy(candidate_block)
        nonce = 0
        block['nonce'] = nonce
        while not self.valid_proof(block):
            nonce += 1
            block['nonce'] = nonce
        return block

    def has_valid_transaction_id(self, transaction):
        if not isinstance(transaction, dict):
            return False
        try:
            atomic_value = self.parse_atomic_value(transaction.get('amount_atomic'))
            if atomic_value is None:
                return False
            if transaction.get('sender_address') == self.COINBASE_SENDER:
                if set(transaction) != set(COINBASE_FIELDS):
                    return False
                expected = coinbase_transaction(
                    transaction.get('recipient_address'),
                    atomic_value,
                    transaction.get('height'),
                )
                return transaction == expected

            if set(transaction) != set(SIGNED_TRANSACTION_FIELDS):
                return False
            nonce = transaction.get('nonce')
            if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce < 0:
                return False
            payload = transaction_signing_payload(
                transaction.get('sender_address'),
                transaction.get('recipient_address'),
                atomic_value,
                nonce,
            )
            if any(transaction.get(field) != value for field, value in payload.items()):
                return False
            expected_id = signed_transaction_id(payload, transaction.get('signature'))
            return transaction.get('transaction_id') == expected_id
        except (TypeError, ValueError):
            return False

    def valid_proof(self, block):
        """
        Check the transaction commitment and header hash against the target.
        """
        if not isinstance(block, dict):
            return False
        try:
            target = target_from_hex(block.get('target'))
            if not all(self.has_valid_transaction_id(tx) for tx in block.get('transactions')):
                return False
            if block.get('merkle_root') != calculate_merkle_root(block.get('transactions')):
                return False
            proof_hash = self.hash(block)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return False
        return int(proof_hash, 16) <= target

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

    def apply_transaction(self, transaction, ledger, nonces=None, require_signature=True):
        if not isinstance(transaction, dict):
            return False
        if set(transaction) != set(SIGNED_TRANSACTION_FIELDS):
            return False

        atomic_value = self.parse_atomic_value(transaction.get('amount_atomic'))
        if atomic_value is None:
            return False

        sender_address = transaction.get('sender_address')
        recipient_address = transaction.get('recipient_address')
        nonce = transaction.get('nonce')
        if not sender_address or not recipient_address or sender_address == self.COINBASE_SENDER:
            return False
        if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce < 0:
            return False
        if self.public_key_from_address(recipient_address) is None:
            return False

        normalized_transaction = transaction_signing_payload(
            sender_address,
            recipient_address,
            atomic_value,
            nonce,
        )
        if any(transaction.get(field) != value for field, value in normalized_transaction.items()):
            return False

        signature = transaction.get('signature')
        if not isinstance(signature, str):
            return False
        expected_transaction_id = signed_transaction_id(normalized_transaction, signature)
        if transaction.get('transaction_id') != expected_transaction_id:
            return False

        if require_signature:
            if not self.verify_transaction_signature(sender_address, signature, normalized_transaction):
                return False

        nonces = {} if nonces is None else nonces
        if nonce != nonces.get(sender_address, 0):
            return False
        if ledger.get(sender_address, 0) < atomic_value:
            return False

        ledger[sender_address] = ledger.get(sender_address, 0) - atomic_value
        ledger[recipient_address] = ledger.get(recipient_address, 0) + atomic_value
        nonces[sender_address] = nonce + 1
        return True

    def apply_coinbase_transaction(self, transaction, ledger, height):
        if not isinstance(transaction, dict):
            return False
        if set(transaction) != set(COINBASE_FIELDS):
            return False

        atomic_value = self.parse_atomic_value(transaction.get('amount_atomic'))
        if atomic_value is None:
            return False

        if transaction.get('sender_address') != self.COINBASE_SENDER:
            return False
        if atomic_value != self.block_reward(height):
            return False

        recipient_address = transaction.get('recipient_address')
        if self.public_key_from_address(recipient_address) is None:
            return False

        if transaction != coinbase_transaction(recipient_address, atomic_value, height):
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
        nonces = {}
        seen_transactions = set()
        last_block = chain[0]
        current_index = 1

        while current_index < len(chain):
            block = chain[current_index]
            if not isinstance(block, dict) or set(block) != set(BLOCK_FIELDS):
                return False
            if block.get('version') != self.PROTOCOL_VERSION or block.get('network') != self.NETWORK_ID:
                return False
            if block['block_number'] != current_index:
                return False
            if not isinstance(block['nonce'], int) or isinstance(block['nonce'], bool) or block['nonce'] < 0:
                return False
            if not isinstance(block['timestamp'], int) or isinstance(block['timestamp'], bool):
                return False
            if block['timestamp'] <= self.median_time_past(chain[:current_index]):
                return False
            if block['timestamp'] > int(time()) + MAX_FUTURE_BLOCK_SECONDS:
                return False
            if block['previous_hash'] != self.hash(last_block):
                return False
            try:
                if block['target'] != target_to_hex(self.expected_target(chain[:current_index], current_index)):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
            if not isinstance(block['transactions'], list) or not block['transactions']:
                return False
            if len(block['transactions']) > self.MAX_TRANSACTIONS_PER_BLOCK:
                return False
            if not self.valid_proof(block):
                return False

            coinbase = block['transactions'][0]
            transactions = block['transactions'][1:]

            for transaction in transactions:
                if not isinstance(transaction, dict):
                    return False
                transaction_key = self.transaction_key(transaction)
                if transaction_key in seen_transactions:
                    return False
                seen_transactions.add(transaction_key)
                if not self.apply_transaction(transaction, ledger, nonces, require_signature=True):
                    return False

            if not self.apply_coinbase_transaction(coinbase, ledger, current_index):
                return False

            last_block = block
            current_index += 1

        return True

    def chainwork(self, chain):
        if not self.valid_chain(chain):
            return 0
        return sum(work_for_target(target_from_hex(block['target'])) for block in chain[1:])

    def headers_for_chain(self, chain=None):
        blocks = self.chain if chain is None else chain
        headers = []
        for block in blocks:
            header = dict(block_header(block))
            header['hash'] = self.hash(block)
            headers.append(header)
        return headers

    def valid_header_chain(self, headers):
        if not isinstance(headers, list) or not headers or len(headers) > self.MAX_SYNC_HEADERS:
            return False

        expected_fields = set(BLOCK_HEADER_FIELDS) | {'hash'}
        header_chain = []
        for height, header in enumerate(headers):
            if not isinstance(header, dict) or set(header) != expected_fields:
                return False
            if header.get('block_number') != height:
                return False
            if header.get('version') != self.PROTOCOL_VERSION or header.get('network') != self.NETWORK_ID:
                return False
            if not isinstance(header.get('timestamp'), int) or isinstance(header.get('timestamp'), bool):
                return False
            if not isinstance(header.get('nonce'), int) or isinstance(header.get('nonce'), bool):
                return False
            if header['nonce'] < 0:
                return False
            merkle_root = header.get('merkle_root')
            if not isinstance(merkle_root, str) or len(merkle_root) != 64:
                return False
            try:
                bytes.fromhex(merkle_root)
            except ValueError:
                return False
            if merkle_root != merkle_root.lower():
                return False

            header_without_hash = {field: header[field] for field in BLOCK_HEADER_FIELDS}
            calculated_hash = block_hash(header_without_hash)
            if header.get('hash') != calculated_hash:
                return False

            if height == 0:
                if header_without_hash != dict(block_header(self.GENESIS_BLOCK)):
                    return False
                if calculated_hash != GENESIS_HASH:
                    return False
                header_chain.append(header_without_hash)
                continue

            if header.get('previous_hash') != headers[height - 1]['hash']:
                return False
            if header['timestamp'] <= self.median_time_past(header_chain):
                return False
            if header['timestamp'] > int(time()) + MAX_FUTURE_BLOCK_SECONDS:
                return False
            try:
                target = target_from_hex(header.get('target'))
            except ValueError:
                return False
            if target != self.expected_target(header_chain, height):
                return False
            if int(calculated_hash, 16) > target:
                return False
            header_chain.append(header_without_hash)
        return True

    def header_chainwork(self, headers):
        if not self.valid_header_chain(headers):
            return 0
        return sum(work_for_target(target_from_hex(header['target'])) for header in headers[1:])

    def fetch_peer_header_batch(self, peer, start, limit, expected_length=None):
        payload = self.network.get_json(
            peer,
            '/headers?start=' + str(start) + '&limit=' + str(limit),
        )
        if payload is None:
            return None
        batch = payload.get('headers')
        response_length = payload.get('length')
        if not isinstance(batch, list) or len(batch) > limit:
            self.network.health.record_failure(peer, 'Peer returned an invalid header batch')
            return None
        if not isinstance(response_length, int) or isinstance(response_length, bool):
            self.network.health.record_failure(peer, 'Peer returned an invalid chain length')
            return None
        if response_length < 1 or response_length > self.MAX_SYNC_HEADERS:
            self.network.health.record_failure(peer, 'Peer chain exceeds synchronization limits')
            return None
        if start > response_length or start + len(batch) > response_length:
            self.network.health.record_failure(peer, 'Peer returned headers outside its declared chain')
            return None
        if expected_length is not None and response_length != expected_length:
            self.network.health.record_failure(peer, 'Peer chain changed during header synchronization')
            return None
        expected_fields = set(BLOCK_HEADER_FIELDS) | {'hash'}
        for offset, header in enumerate(batch):
            height = start + offset
            if not isinstance(header, dict) or set(header) != expected_fields:
                return None
            if header.get('block_number') != height:
                return None
            try:
                if block_hash(header) != header.get('hash'):
                    return None
                if height > 0 and int(header['hash'], 16) > target_from_hex(header.get('target')):
                    return None
            except (KeyError, TypeError, ValueError):
                return None
        return batch, response_length

    def fetch_peer_headers(self, peer, local_headers=None):
        local_headers = self.headers_for_chain() if local_headers is None else local_headers
        probe_height = max(0, len(local_headers) - 1)
        probe_result = self.fetch_peer_header_batch(peer, probe_height, 1)
        if probe_result is None:
            return None
        probe_batch, declared_length = probe_result
        if probe_height < declared_length and len(probe_batch) != 1:
            self.network.health.record_failure(peer, 'Peer returned an incomplete tip header')
            return None

        common_height = -1
        low = 0
        high = min(len(local_headers), declared_length) - 1
        located_headers = {}
        if probe_batch:
            located_headers[probe_height] = probe_batch[0]
            if probe_batch[0]['hash'] == local_headers[probe_height]['hash']:
                common_height = probe_height
                low = high + 1
        while low <= high:
            middle = (low + high) // 2
            peer_header = located_headers.get(middle)
            if peer_header is None:
                result = self.fetch_peer_header_batch(peer, middle, 1, declared_length)
                if result is None or len(result[0]) != 1:
                    return None
                peer_header = result[0][0]
                located_headers[middle] = peer_header
            if peer_header['hash'] == local_headers[middle]['hash']:
                common_height = middle
                low = middle + 1
            else:
                high = middle - 1

        if common_height < 0:
            self.network.health.record_incompatible(peer, 'Peer does not share the Denarius genesis block')
            return None

        headers = copy.deepcopy(local_headers[:common_height + 1])
        while len(headers) < declared_length:
            start = len(headers)
            limit = min(self.MAX_HEADERS_PER_REQUEST, declared_length - start)
            result = self.fetch_peer_header_batch(peer, start, limit, declared_length)
            if result is None or len(result[0]) != limit:
                self.network.health.record_failure(peer, 'Peer returned an incomplete header chain')
                return None
            headers.extend(result[0])

        if len(headers) != declared_length or not self.valid_header_chain(headers):
            self.network.health.record_failure(peer, 'Peer returned an invalid header chain')
            return None
        work = self.header_chainwork(headers)
        self.network.health.update_tip(peer, len(headers) - 1, work)
        return headers, common_height

    def fetch_peer_blocks(self, peer, headers, start):
        blocks = []
        expected_count = len(headers) - start
        while len(blocks) < expected_count:
            batch_start = start + len(blocks)
            batch_limit = min(self.MAX_BLOCKS_PER_REQUEST, expected_count - len(blocks))
            payload = self.network.get_json(
                peer,
                '/blocks?start=' + str(batch_start) + '&limit=' + str(batch_limit),
            )
            if payload is None:
                return None
            batch = payload.get('blocks')
            if not isinstance(batch, list) or len(batch) != batch_limit:
                self.network.health.record_failure(peer, 'Peer returned an incomplete block batch')
                return None
            for offset, block in enumerate(batch):
                height = batch_start + offset
                if not isinstance(block, dict):
                    return None
                try:
                    if self.hash(block) != headers[height]['hash']:
                        return None
                    if dict(block_header(block)) != {
                        field: headers[height][field] for field in BLOCK_HEADER_FIELDS
                    }:
                        return None
                except (KeyError, TypeError, ValueError):
                    return None
            blocks.extend(batch)
        return blocks

    def remove_confirmed_transactions(self, block):
        confirmed_transactions = block.get('transactions', [])[1:]
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

        self.network.seen_blocks.add(self.hash(block))
        if relay:
            self.broadcast_block(block)
        return True

    def resolve_conflicts(self):
        """
        Resolve conflicts between blockchain's nodes
        by replacing our chain with the greatest-work chain in the network.
        """
        with self._lock:
            neighbours = list(self.nodes)
            local_chain = copy.deepcopy(self.chain)
        local_headers = self.headers_for_chain(local_chain)
        best_headers = None
        best_peer = None
        best_common_height = None
        best_work = self.header_chainwork(local_headers)

        for node in neighbours:
            peer_headers = self.fetch_peer_headers(node, local_headers)
            if peer_headers is None:
                continue
            headers, common_height = peer_headers
            peer_work = self.header_chainwork(headers)
            if peer_work > best_work:
                best_work = peer_work
                best_headers = headers
                best_peer = node
                best_common_height = common_height

        if best_headers is None:
            return False

        suffix = self.fetch_peer_blocks(best_peer, best_headers, best_common_height + 1)
        if suffix is None:
            return False
        candidate_chain = local_chain[:best_common_height + 1] + suffix
        if not self.valid_chain(candidate_chain):
            self.network.health.record_failure(best_peer, 'Peer block data did not match its valid headers')
            return False

        with self._lock:
            if self.chainwork(candidate_chain) <= self.chainwork(self.chain):
                return False
            current_chain = copy.deepcopy(self.chain)
            pending_transactions = copy.deepcopy(self.transactions)
            current_hashes = [self.hash(block) for block in current_chain]
            candidate_hashes = [self.hash(block) for block in candidate_chain]
            shared_height = -1
            for height in range(min(len(current_hashes), len(candidate_hashes))):
                if current_hashes[height] != candidate_hashes[height]:
                    break
                shared_height = height

            disconnected_transactions = []
            for block in current_chain[shared_height + 1:]:
                disconnected_transactions.extend(block.get('transactions', [])[1:])

            self.chain = copy.deepcopy(candidate_chain)
            self.transactions = []
            for transaction in disconnected_transactions + pending_transactions:
                self.submit_transaction(
                    transaction.get('sender_address'),
                    transaction.get('recipient_address'),
                    transaction.get('amount_atomic'),
                    transaction.get('nonce'),
                    transaction.get('signature'),
                    transaction.get('transaction_id'),
                    relay=False,
                )
            self.MINING_TARGET = self.expected_target(self.chain, len(self.chain))
            self.save_everything()

        for block in candidate_chain:
            self.network.seen_blocks.add(self.hash(block))
        return True

    def synchronize_network(self):
        if not self._sync_lock.acquire(blocking=False):
            return False
        try:
            added_peers = self.exchange_peer_table()
            replaced = self.resolve_conflicts()
            if added_peers and not replaced:
                self.save_everything()
            self.last_sync_at = int(time())
            self.last_sync_error = None
            return replaced
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self.last_sync_at = int(time())
            self.last_sync_error = str(exc)
            return False
        finally:
            self._sync_lock.release()

    def start_background_sync(self, interval=30):
        interval = max(1, int(interval))
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return self._sync_thread

        self._sync_stop.clear()
        self._sync_wakeup.clear()

        def worker():
            while not self._sync_stop.is_set():
                self.synchronize_network()
                self._sync_wakeup.wait(interval)
                self._sync_wakeup.clear()

        self._sync_thread = threading.Thread(
            target=worker,
            name='denarius-network-sync',
            daemon=True,
        )
        self._sync_thread.start()
        return self._sync_thread

    def trigger_background_sync(self):
        self._sync_wakeup.set()

    def stop_background_sync(self):
        self._sync_stop.set()
        self._sync_wakeup.set()

    def synchronization_status(self):
        return {
            'running': self._sync_thread is not None and self._sync_thread.is_alive(),
            'in_progress': self._sync_lock.locked(),
            'last_sync': getattr(self, 'last_sync_at', None),
            'last_error': getattr(self, 'last_sync_error', None),
        }


    def save_everything(self):
        with self._lock:
            state = {
                'chain': copy.deepcopy(self.chain),
                'transactions': copy.deepcopy(self.transactions),
                'nodes': sorted(self.nodes),
                'node_address': self.node_address,
                'miner_name': self.miner_name,
                'mining_target': target_to_hex(self.MINING_TARGET),
            }
        DenariusStorage(self.STATE_PATH).save_state(state)


    def load_everything(self, path):
        state_path = Path(path).resolve()
        state = DenariusStorage(state_path).load_state()

        chain = state.get('chain')
        if not self.valid_chain(chain):
            raise ValueError('State file contains an invalid Denarius chain')

        expected_mining_target = self.expected_target(chain, len(chain))
        encoded_target = state.get('mining_target', target_to_hex(expected_mining_target))
        try:
            mining_target = target_from_hex(encoded_target)
        except ValueError as exc:
            raise ValueError('State file contains an invalid mining target') from exc
        if mining_target != expected_mining_target:
            raise ValueError('State file contains an unexpected mining target')

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
                transaction.get('amount_atomic'),
                transaction.get('nonce'),
                transaction.get('signature'),
                transaction.get('transaction_id'),
                relay=False,
            )

        with self._lock:
            self.STATE_PATH = state_path
            self.chain = copy.deepcopy(chain)
            self.transactions = copy.deepcopy(pending_validator.transactions)
            self.nodes = nodes
            self.node_address = node_address
            self.miner_name = miner_name.strip()
            self.MINING_TARGET = mining_target
        return self



app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
CORS(app, resources={
    r'/chain': {'origins': '*'},
    r'/miner/get': {'origins': '*'},
    r'/nodes/get': {'origins': '*'},
    r'/transactions/get': {'origins': '*'},
    r'/transactions/new': {'origins': '*'},
    r'/accounts/.*': {'origins': '*'},
})

blockchain = Blockchain()
ADMIN_TOKEN_ENV = 'DENARIUS_ADMIN_TOKEN'


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        expected_token = os.environ.get(ADMIN_TOKEN_ENV)
        if not expected_token or len(expected_token) < 32:
            return jsonify({'message': 'Node administration is not configured'}), 503
        submitted_token = request.headers.get('X-Denarius-Admin-Token')
        if not submitted_token or not hmac.compare_digest(expected_token, submitted_token):
            return jsonify({'message': 'Invalid node administration token'}), 403
        return func(*args, **kwargs)
    return wrapper


def compatible_peer_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        compatible = (
            request.headers.get('X-Denarius-Protocol-Version') == str(PROTOCOL_VERSION)
            and request.headers.get('X-Denarius-Network') == NETWORK_ID
            and request.headers.get('X-Denarius-Peer-API-Version') == str(PEER_API_VERSION)
        )
        if not compatible:
            return jsonify({
                'message': 'Incompatible Denarius peer protocol',
                'protocol': blockchain.protocol_status(),
            }), 409
        return func(*args, **kwargs)
    return wrapper


@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    values = request.form

    # Check that the required fields are in the POST'ed data
    required = [
        'sender_address', 'recipient_address', 'amount', 'nonce',
        'signature', 'transaction_id',
    ]
    if not all(k in values for k in required):
        return 'Missing values', 400
    # Create a new Transaction
    transaction_result = blockchain.submit_transaction(
        values['sender_address'], values['recipient_address'], values['amount'],
        values['nonce'], values['signature'], values['transaction_id'],
    )

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
@compatible_peer_required
def receive_transaction():
    values = request.form

    required = [
        'sender_address', 'recipient_address', 'amount', 'nonce',
        'signature', 'transaction_id',
    ]
    if not all(k in values for k in required):
        return 'Missing values', 400

    if blockchain.has_seen_transaction(values['transaction_id']):
        return jsonify({'message': 'Transaction already known'}), 200

    transaction_result = blockchain.submit_transaction(
        values['sender_address'], values['recipient_address'], values['amount'],
        values['nonce'], values['signature'], values['transaction_id'],
        relay=True,
    )

    if transaction_result == False:
        if blockchain.has_seen_transaction(values['transaction_id']):
            return jsonify({'message': 'Transaction already known'}), 200
        response = {'message': 'Invalid Transaction!'}
        return jsonify(response), 406

    response = {'message': 'Transaction accepted from peer'}
    return jsonify(response), 201


@app.route('/accounts/<address>', methods=['GET'])
def get_account(address):
    if blockchain.public_key_from_address(address) is None:
        return jsonify({'message': 'Invalid Denarius address'}), 400
    return jsonify({
        'address': address,
        'balance': blockchain.get_balance(address),
        'balance_atomic': str(blockchain.get_atomic_balance(address)),
        'next_nonce': blockchain.get_next_nonce(address),
    }), 200


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
@admin_required
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


@app.route('/protocol', methods=['GET'])
def get_protocol():
    return jsonify(blockchain.protocol_status()), 200


@app.route('/headers', methods=['GET'])
@compatible_peer_required
def get_headers():
    try:
        start = int(request.args.get('start', 0))
        limit = int(request.args.get('limit', blockchain.MAX_HEADERS_PER_REQUEST))
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid header range'}), 400
    if start < 0 or limit < 1 or limit > blockchain.MAX_HEADERS_PER_REQUEST:
        return jsonify({'message': 'Invalid header range'}), 400
    with blockchain._lock:
        chain = copy.deepcopy(blockchain.chain)
    headers = blockchain.headers_for_chain(chain)
    return jsonify({
        'protocol': blockchain.protocol_status(),
        'length': len(headers),
        'start': start,
        'headers': headers[start:start + limit],
    }), 200


@app.route('/blocks', methods=['GET'])
@compatible_peer_required
def get_blocks():
    try:
        start = int(request.args.get('start', 0))
        limit = int(request.args.get('limit', blockchain.MAX_BLOCKS_PER_REQUEST))
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid block range'}), 400
    if start < 0 or limit < 1 or limit > blockchain.MAX_BLOCKS_PER_REQUEST:
        return jsonify({'message': 'Invalid block range'}), 400
    with blockchain._lock:
        chain = copy.deepcopy(blockchain.chain)
    return jsonify({
        'protocol': blockchain.protocol_status(),
        'length': len(chain),
        'start': start,
        'blocks': chain[start:start + limit],
    }), 200


@app.route('/blocks/receive', methods=['POST'])
@compatible_peer_required
def receive_block():
    values = request.get_json(silent=True) or {}
    block = values.get('block')

    if not isinstance(block, dict):
        return jsonify({'message': 'Invalid block'}), 400

    try:
        block_id = blockchain.hash(block)
    except (KeyError, TypeError, ValueError):
        return jsonify({'message': 'Invalid block'}), 400

    if blockchain.has_seen_block(block_id):
        return jsonify({'message': 'Block already known'}), 200

    if blockchain.accept_block(block):
        return jsonify({'message': 'Block accepted'}), 201

    blockchain.trigger_background_sync()
    return jsonify({'message': 'Block did not extend the local chain; synchronization queued'}), 409


@app.route('/miner/register', methods=['POST'])
@admin_required
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
@admin_required
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

    blockchain.save_everything()
    blockchain.trigger_background_sync()

    response = {
        'message': 'New nodes have been added',
        'total_nodes': [node for node in blockchain.nodes],
    }
    return jsonify(response), 201


@app.route('/nodes/resolve', methods=['POST'])
@admin_required
def consensus():
    replaced = blockchain.synchronize_network()

    if replaced:
        response = {
            'message': 'Our chain was replaced',
        }
    else:
        response = {
            'message': 'Our chain is authoritative',
        }
    response['synchronization'] = blockchain.synchronization_status()
    return jsonify(response), 200


@app.route('/nodes/get', methods=['GET'])
def get_nodes():
    with blockchain._lock:
        nodes = sorted(blockchain.nodes)
    response = {
        'nodes': nodes,
        'peers': blockchain.peer_health(),
        'protocol': blockchain.protocol_status(),
        'synchronization': blockchain.synchronization_status(),
    }
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

def main(argv=None):
    global blockchain

    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-p', '--port', default=5000, type=int, help='port to listen on')
    parser.add_argument('--host', default='127.0.0.1', help='interface to listen on')
    parser.add_argument(
        '-d', '--database',
        default=str(Blockchain.STATE_PATH),
        help='SQLite state database path',
    )
    parser.add_argument(
        '--migrate-json',
        default=None,
        help='migrate a Phase 1 JSON state file into the selected database',
    )
    parser.add_argument(
        '--sync-interval',
        default=30,
        type=int,
        help='seconds between background peer synchronization passes',
    )
    parser.add_argument(
        '--advertise-address',
        default=None,
        help='host and port shared with peers (defaults to 127.0.0.1 and --port)',
    )
    args = parser.parse_args(argv)
    port = args.port
    database_path = Path(args.database).resolve()
    if args.migrate_json:
        try:
            migrate_json_state(args.migrate_json, database_path)
        except ValueError as exc:
            parser.error(str(exc))
    blockchain.STATE_PATH = database_path
    if database_path.exists():
        try:
            blockchain = blockchain.load_everything(database_path)
        except ValueError as exc:
            parser.error(str(exc))

    try:
        blockchain.advertised_node = blockchain.normalize_node(
            args.advertise_address or ('127.0.0.1:' + str(port))
        )
    except ValueError as exc:
        parser.error(str(exc))
    with blockchain._lock:
        blockchain.nodes.discard(blockchain.advertised_node)

    blockchain.start_background_sync(args.sync_interval)
    try:
        app.run(host=args.host, port=port)
    finally:
        blockchain.stop_background_sync()


if __name__ == '__main__':
    main()
