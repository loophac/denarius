import binascii
import copy
import hmac
import ipaddress
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
from flask import Flask, Response, jsonify, request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from denarius_protocol import (
    ATOMIC_UNITS,
    BLOCK_FIELDS,
    BLOCK_HEADER_FIELDS,
    COINBASE_MATURITY,
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
    MIN_TRANSACTION_FEE_ATOMIC,
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
from denarius_ledger import ChainState
from denarius_network import PeerNetwork, protocol_identity
from denarius_operations import (
    configure_json_logging,
    configure_trusted_proxy,
    install_runtime_controls,
)
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
    MAX_DISCOVERED_PER_PEER = 8
    MAX_PEERS_PER_NETWORK_GROUP = 4
    MAX_HEADERS_PER_REQUEST = 512
    MAX_BLOCKS_PER_REQUEST = 32
    MAX_CHAIN_API_BLOCKS = 250
    MAX_SYNC_HEADERS = 100000
    MAX_PENDING_TRANSACTIONS = 1000
    MAX_PENDING_PER_SENDER = 64
    MAX_TRANSACTIONS_PER_BLOCK = 1000
    AUTOMINE_INTERVAL_SECONDS = 2
    GENESIS_BLOCK = GENESIS_BLOCK
    STATE_PATH = state_path('denarius-testnet-v3.db')

    def __init__(self, name="THE BLOCKCHAIN"):

        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._sync_stop = threading.Event()
        self._sync_wakeup = threading.Event()
        self._sync_thread = None
        self._automine_stop = threading.Event()
        self._automine_thread = None
        self._automine_started_at = None
        self._automine_blocks_mined = 0
        self._automine_last_block = None
        self._automine_last_error = None
        self.transactions = []
        self.chain = []
        self.undo_records = {}
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
        self.chain_state = ChainState()
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

        candidate = node_url if '://' in node_url else self.network.scheme + '://' + node_url
        parsed_url = urlparse(candidate)
        if parsed_url.scheme != self.network.scheme or parsed_url.username or parsed_url.password:
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

    def peer_host(self, node):
        parsed = urlparse('http://' + node)
        return parsed.hostname

    def peer_network_group(self, node):
        hostname = self.peer_host(node)
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            labels = hostname.lower().rstrip('.').split('.')
            return 'dns:' + '.'.join(labels[-2:])
        if address.is_loopback:
            return 'loopback'
        if address.version == 4:
            network = ipaddress.ip_network(str(address) + '/16', strict=False)
            return 'ipv4:' + str(network.network_address) + '/16'
        network = ipaddress.ip_network(str(address) + '/32', strict=False)
        return 'ipv6:' + str(network.network_address) + '/32'

    def validate_discovered_peer(self, peer, source):
        candidate_host = self.peer_host(peer)
        source_host = self.peer_host(source)
        try:
            candidate_address = ipaddress.ip_address(candidate_host)
            source_address = ipaddress.ip_address(source_host)
        except ValueError as exc:
            raise ValueError('Peer gossip may only advertise IP addresses') from exc
        if (
            not source_address.is_private
            and not source_address.is_loopback
            and (candidate_address.is_private or candidate_address.is_loopback)
        ):
            raise ValueError('Public peers may not advertise private network addresses')

        group = self.peer_network_group(peer)
        with self._lock:
            group_count = sum(
                self.peer_network_group(existing) == group
                for existing in self.nodes
            )
        if group_count >= self.MAX_PEERS_PER_NETWORK_GROUP:
            raise ValueError('Peer network group limit reached')

    def register_node(self, node_url, discovered_from=None):
        """
        Add a new node to the list of nodes
        """
        normalized_node = self.normalize_node(node_url)
        if discovered_from is not None:
            self.validate_discovered_peer(normalized_node, discovered_from)
        with self._lock:
            if normalized_node == self.advertised_node:
                raise ValueError('Cannot register the current node as a peer')
            if len(self.nodes) >= self.MAX_PEERS and normalized_node not in self.nodes:
                raise ValueError('Peer limit reached')
            before = len(self.nodes)
            self.nodes.add(normalized_node)
            return len(self.nodes) > before

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
                self.network.health.record_misbehavior(node, 'Peer returned an invalid peer table')
                continue
            if len(discovered) > self.MAX_PEERS:
                self.network.health.record_misbehavior(node, 'Peer advertised an oversized peer table')
            for peer in discovered[:self.MAX_DISCOVERED_PER_PEER]:
                try:
                    self.register_node(peer, discovered_from=node)
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
            self.ensure_chain_state()
            height = len(self.chain) - 1
            tip_hash = self.hash(self.chain[-1])
            chainwork = self.chain_state.chainwork
        status = protocol_identity()
        status.update({
            'node': self.advertised_node,
            'peer_transport': self.network.scheme,
            'height': height,
            'tip_hash': tip_hash,
            'chainwork': str(chainwork),
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
        return self.format_amount(self.get_atomic_balance(address))

    def get_atomic_balance(self, address, include_pending=False):
        with self._lock:
            self.ensure_chain_state()
            balance = self.chain_state.balances.get(address, 0)
            if include_pending:
                for transaction in self.transactions:
                    if transaction.get('sender_address') == address:
                        balance -= (
                            self.parse_atomic_value(transaction.get('amount_atomic')) or 0
                        ) + (
                            self.parse_atomic_value(transaction.get('fee_atomic')) or 0
                        )
        return balance

    def get_immature_balance(self, address):
        with self._lock:
            self.ensure_chain_state()
            return self.chain_state.immature_balance(address)

    def get_confirmed_nonce(self, address, chain=None):
        if chain is not None and chain is not self.chain:
            state = self.build_chain_state(chain)
            return 0 if state is None else state.nonces.get(address, 0)
        with self._lock:
            self.ensure_chain_state()
            return self.chain_state.nonces.get(address, 0)

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
        if chain is not None and chain is not self.chain:
            state = self.build_chain_state(chain)
            return set() if state is None else set(state.confirmed_transactions)
        with self._lock:
            self.ensure_chain_state()
            return set(self.chain_state.confirmed_transactions)

    def verify_enough_balance(self, address, atomic_value, fee_atomic=0):
        """
        Check that the sender has enough balance in his wallet.
        :param sender_address: address of the sender
        :param value: value to be sent
        :return: True if sender has enough balance else False
        """
        return self.get_atomic_balance(address, include_pending=True) >= atomic_value + fee_atomic

    def submit_transaction(
        self,
        sender_address,
        recipient_address,
        value,
        nonce,
        signature,
        transaction_id,
        fee=MIN_TRANSACTION_FEE_ATOMIC,
        relay=True,
    ):
        """
        Add a transaction to transactions array if the signature verified
        """
        atomic_value = self.parse_atomic_value(value)
        if atomic_value is None:
            return False
        fee_atomic = self.parse_atomic_value(fee)
        if fee_atomic is None or fee_atomic < MIN_TRANSACTION_FEE_ATOMIC:
            return False
        if atomic_value + fee_atomic > self.TOTAL_AMOUNT:
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
            fee_atomic,
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
            self.ensure_chain_state()
            if len(self.transactions) >= self.MAX_PENDING_TRANSACTIONS:
                return False
            sender_pending = [
                pending
                for pending in self.transactions
                if pending.get('sender_address') == sender_address
            ]
            if len(sender_pending) >= self.MAX_PENDING_PER_SENDER:
                return False
            if nonce != self.get_next_nonce(sender_address):
                return False
            if not self.verify_enough_balance(sender_address, atomic_value, fee_atomic):
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

    def create_coinbase_transaction(self, height=None, recipient_address=None, fees_atomic=0):
        height = len(self.chain) if height is None else height
        recipient_address = self.node_address if recipient_address is None else recipient_address
        return coinbase_transaction(
            recipient_address,
            self.block_reward(height) + fees_atomic,
            height,
        )

    def create_candidate_block(self):
        with self._lock:
            height = len(self.chain)
            previous_hash = self.hash(self.chain[-1])
            pending_limit = max(0, self.MAX_TRANSACTIONS_PER_BLOCK - 1)
            pending_snapshot = copy.deepcopy(self.transactions[:pending_limit])
            recipient_address = self.node_address
            target = self.expected_target(self.chain, height)
            timestamp = max(int(time()), self.median_time_past(self.chain) + 1)

        fees_atomic = sum(
            self.parse_atomic_value(transaction.get('fee_atomic')) or 0
            for transaction in pending_snapshot
        )
        coinbase = self.create_coinbase_transaction(height, recipient_address, fees_atomic)
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

    def proof_of_work(self, candidate_block, stop_event=None):
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
        if stop_event is not None and stop_event.is_set():
            return None
        while not self.valid_proof(block):
            if stop_event is not None and stop_event.is_set():
                return None
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
            fee_atomic = self.parse_atomic_value(transaction.get('fee_atomic'))
            if fee_atomic is None or fee_atomic < MIN_TRANSACTION_FEE_ATOMIC:
                return False
            nonce = transaction.get('nonce')
            if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce < 0:
                return False
            payload = transaction_signing_payload(
                transaction.get('sender_address'),
                transaction.get('recipient_address'),
                atomic_value,
                nonce,
                fee_atomic,
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

    def mine_pending_transactions(self, relay=True, persist=True, stop_event=None):
        if self.public_key_from_address(self.node_address) is None:
            return False

        candidate = self.create_candidate_block()
        block = self.proof_of_work(candidate, stop_event=stop_event)
        if block is None:
            return False
        if stop_event is not None and stop_event.is_set():
            return False

        with self._lock:
            self.ensure_chain_state()
            if block['block_number'] != len(self.chain):
                return False
            if block['previous_hash'] != self.hash(self.chain[-1]):
                return False

            height = len(self.chain)
            undo = self.create_block_undo(self.chain_state, block, height)
            next_state = self.validate_next_block(block, self.chain, self.chain_state)
            if next_state is None:
                return False

            self.chain.append(block)
            self.chain_state = next_state
            self.undo_records[height] = undo
            self.remove_confirmed_transactions(block)
            self.update_hyperparameters()
            if persist:
                self.persist_appended_block(block, undo)

        if relay:
            self.broadcast_block(block)
        return block

    def automining_status(self):
        with self._lock:
            running = self._automine_thread is not None and self._automine_thread.is_alive()
            return {
                'running': running,
                'interval_seconds': self.AUTOMINE_INTERVAL_SECONDS,
                'started_at': self._automine_started_at,
                'blocks_mined': self._automine_blocks_mined,
                'last_block': self._automine_last_block,
                'last_error': self._automine_last_error,
            }

    def start_automining(self):
        if self.public_key_from_address(self.node_address) is None:
            return False
        with self._lock:
            if self._automine_thread is not None and self._automine_thread.is_alive():
                return self.automining_status()
            self._automine_stop.clear()
            self._automine_started_at = int(time())
            self._automine_blocks_mined = 0
            self._automine_last_block = None
            self._automine_last_error = None

            def worker():
                while not self._automine_stop.is_set():
                    try:
                        block = self.mine_pending_transactions(
                            stop_event=self._automine_stop,
                        )
                    except Exception as exc:
                        with self._lock:
                            self._automine_last_error = str(exc)[:240]
                        if self._automine_stop.wait(self.AUTOMINE_INTERVAL_SECONDS):
                            break
                        continue
                    if block is not False:
                        with self._lock:
                            self._automine_blocks_mined += 1
                            self._automine_last_block = block['block_number']
                            self._automine_last_error = None
                    if self._automine_stop.wait(self.AUTOMINE_INTERVAL_SECONDS):
                        break

            self._automine_thread = threading.Thread(
                target=worker,
                name='denarius-autominer',
                daemon=True,
            )
            self._automine_thread.start()
            return self.automining_status()

    def stop_automining(self):
        self._automine_stop.set()
        thread = self._automine_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(5, self.PEER_REQUEST_TIMEOUT + 1))
        return self.automining_status()

    def apply_transaction(self, transaction, ledger, nonces=None, require_signature=True):
        if not isinstance(transaction, dict):
            return False
        if set(transaction) != set(SIGNED_TRANSACTION_FIELDS):
            return False

        atomic_value = self.parse_atomic_value(transaction.get('amount_atomic'))
        if atomic_value is None:
            return False
        fee_atomic = self.parse_atomic_value(transaction.get('fee_atomic'))
        if fee_atomic is None or fee_atomic < MIN_TRANSACTION_FEE_ATOMIC:
            return False
        if atomic_value + fee_atomic > self.TOTAL_AMOUNT:
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
            fee_atomic,
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
        if ledger.get(sender_address, 0) < atomic_value + fee_atomic:
            return False

        ledger[sender_address] = ledger.get(sender_address, 0) - atomic_value - fee_atomic
        ledger[recipient_address] = ledger.get(recipient_address, 0) + atomic_value
        nonces[sender_address] = nonce + 1
        return True

    def apply_coinbase_transaction(self, transaction, state, height, fees_atomic):
        if not isinstance(transaction, dict):
            return False
        if set(transaction) != set(COINBASE_FIELDS):
            return False

        atomic_value = self.parse_atomic_value(transaction.get('amount_atomic'))
        if atomic_value is None:
            return False

        if transaction.get('sender_address') != self.COINBASE_SENDER:
            return False
        subsidy = self.block_reward(height)
        if atomic_value != subsidy + fees_atomic:
            return False

        recipient_address = transaction.get('recipient_address')
        if self.public_key_from_address(recipient_address) is None:
            return False

        if transaction != coinbase_transaction(recipient_address, atomic_value, height):
            return False

        state.issued_atomic += subsidy
        if state.issued_atomic > self.TOTAL_AMOUNT:
            return False
        state.immature_rewards.append({
            'height': height,
            'matures_at': height + COINBASE_MATURITY,
            'address': recipient_address,
            'amount_atomic': atomic_value,
        })
        return True

    def state_matches_chain(self):
        return (
            self.chain_state.tip_height == len(self.chain) - 1
            and self.chain_state.tip_hash == self.hash(self.chain[-1])
        )

    def ensure_chain_state(self):
        if self.state_matches_chain():
            return self.chain_state
        rebuilt = self.build_chain_state(self.chain)
        if rebuilt is None:
            raise ValueError('Current chain cannot produce a valid chain state')
        self.chain_state = rebuilt
        return rebuilt

    def validate_next_block(self, block, chain, state):
        height = len(chain)
        if not isinstance(block, dict) or set(block) != set(BLOCK_FIELDS):
            return None
        if block.get('version') != self.PROTOCOL_VERSION or block.get('network') != self.NETWORK_ID:
            return None
        if block.get('block_number') != height:
            return None
        if not isinstance(block.get('nonce'), int) or isinstance(block.get('nonce'), bool):
            return None
        if block['nonce'] < 0:
            return None
        if not isinstance(block.get('timestamp'), int) or isinstance(block.get('timestamp'), bool):
            return None
        if block['timestamp'] <= self.median_time_past(chain):
            return None
        if block['timestamp'] > int(time()) + MAX_FUTURE_BLOCK_SECONDS:
            return None
        if block.get('previous_hash') != self.hash(chain[-1]):
            return None
        try:
            expected_target = target_to_hex(self.expected_target(chain, height))
        except (KeyError, TypeError, ValueError):
            return None
        if block.get('target') != expected_target:
            return None

        transactions = block.get('transactions')
        if not isinstance(transactions, list) or not transactions:
            return None
        if len(transactions) > self.MAX_TRANSACTIONS_PER_BLOCK:
            return None
        if not self.valid_proof(block):
            return None

        working = state.clone()
        working.mature_rewards(height)
        fees_atomic = 0
        for transaction in transactions[1:]:
            if not isinstance(transaction, dict):
                return None
            try:
                transaction_key = self.transaction_key(transaction)
            except ValueError:
                return None
            if transaction_key in working.confirmed_transactions:
                return None
            if not self.apply_transaction(
                transaction,
                working.balances,
                working.nonces,
                require_signature=True,
            ):
                return None
            working.confirmed_transactions.add(transaction_key)
            working.transaction_heights[transaction_key] = height
            fees_atomic += self.parse_atomic_value(transaction.get('fee_atomic')) or 0

        if not self.apply_coinbase_transaction(
            transactions[0],
            working,
            height,
            fees_atomic,
        ):
            return None

        working.chainwork += work_for_target(target_from_hex(block['target']))
        working.tip_height = height
        working.tip_hash = self.hash(block)
        return working

    def create_block_undo(self, state, block, height=None):
        height = len(self.chain) if height is None else height
        touched_addresses = {
            reward['address']
            for reward in state.immature_rewards
            if reward['matures_at'] <= height
        }
        touched_nonces = set()
        added_transactions = []
        for transaction in block.get('transactions', [])[1:]:
            sender = transaction.get('sender_address')
            recipient = transaction.get('recipient_address')
            if sender:
                touched_addresses.add(sender)
                touched_nonces.add(sender)
            if recipient:
                touched_addresses.add(recipient)
            transaction_id = transaction.get('transaction_id')
            if transaction_id:
                added_transactions.append(transaction_id)
        return {
            'height': height,
            'block_hash': self.hash(block),
            'balances_before': {
                address: state.balances.get(address)
                for address in sorted(touched_addresses)
            },
            'nonces_before': {
                address: state.nonces.get(address)
                for address in sorted(touched_nonces)
            },
            'confirmed_transactions_added': added_transactions,
            'immature_rewards_before': copy.deepcopy(state.immature_rewards),
            'issued_atomic_before': state.issued_atomic,
            'chainwork_before': str(state.chainwork),
            'tip_height_before': state.tip_height,
            'tip_hash_before': state.tip_hash,
        }

    def restore_block_undo(self, state, undo):
        restored = state.clone()
        for address, previous in undo['balances_before'].items():
            if previous is None:
                restored.balances.pop(address, None)
            else:
                restored.balances[address] = previous
        for address, previous in undo['nonces_before'].items():
            if previous is None:
                restored.nonces.pop(address, None)
            else:
                restored.nonces[address] = previous
        for transaction_id in undo['confirmed_transactions_added']:
            restored.confirmed_transactions.discard(transaction_id)
            restored.transaction_heights.pop(transaction_id, None)
        restored.immature_rewards = copy.deepcopy(undo['immature_rewards_before'])
        restored.issued_atomic = int(undo['issued_atomic_before'])
        restored.chainwork = int(undo['chainwork_before'])
        restored.tip_height = int(undo['tip_height_before'])
        restored.tip_hash = undo['tip_hash_before']
        return restored

    def replay_chain(self, chain):
        if not isinstance(chain, list) or not chain or chain[0] != self.GENESIS_BLOCK:
            return None, None
        state = ChainState()
        undo_records = {}
        history = [chain[0]]
        for block in chain[1:]:
            undo = self.create_block_undo(state, block, len(history))
            state = self.validate_next_block(block, history, state)
            if state is None:
                return None, None
            undo_records[len(history)] = undo
            history.append(block)
        return state, undo_records

    def build_chain_state(self, chain):
        state, _ = self.replay_chain(chain)
        return state

    def valid_chain(self, chain):
        return self.build_chain_state(chain) is not None

    def chainwork(self, chain):
        if chain is self.chain:
            with self._lock:
                self.ensure_chain_state()
                return self.chain_state.chainwork
        state = self.build_chain_state(chain)
        return 0 if state is None else state.chainwork

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
            self.ensure_chain_state()
            if block.get('block_number') != len(self.chain):
                return False
            if block.get('previous_hash') != self.hash(self.chain[-1]):
                return False

            height = len(self.chain)
            undo = self.create_block_undo(self.chain_state, block, height)
            next_state = self.validate_next_block(block, self.chain, self.chain_state)
            if next_state is None:
                return False

            self.chain.append(copy.deepcopy(block))
            self.chain_state = next_state
            self.undo_records[height] = undo
            self.remove_confirmed_transactions(block)
            self.update_hyperparameters()
            self.persist_appended_block(block, undo)

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
        candidate_state, candidate_undo = self.replay_chain(candidate_chain)
        if candidate_state is None:
            self.network.health.record_failure(best_peer, 'Peer block data did not match its valid headers')
            return False

        with self._lock:
            self.ensure_chain_state()
            if candidate_state.chainwork <= self.chain_state.chainwork:
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
            self.chain_state = candidate_state
            self.undo_records = candidate_undo
            self.transactions = []
            for transaction in disconnected_transactions + pending_transactions:
                self.submit_transaction(
                    transaction.get('sender_address'),
                    transaction.get('recipient_address'),
                    transaction.get('amount_atomic'),
                    transaction.get('nonce'),
                    transaction.get('signature'),
                    transaction.get('transaction_id'),
                    fee=transaction.get('fee_atomic'),
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
            if added_peers or not replaced:
                self.persist_peer_state()
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
        thread = self._sync_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(5, self.PEER_REQUEST_TIMEOUT + 1))
            if not thread.is_alive():
                self._sync_thread = None

    def synchronization_status(self):
        return {
            'running': self._sync_thread is not None and self._sync_thread.is_alive(),
            'in_progress': self._sync_lock.locked(),
            'last_sync': getattr(self, 'last_sync_at', None),
            'last_error': getattr(self, 'last_sync_error', None),
        }


    def save_everything(self):
        with self._lock:
            self.ensure_chain_state()
            state = {
                'chain': copy.deepcopy(self.chain),
                'chain_state': self.chain_state.as_dict(),
                'undo_records': copy.deepcopy(self.undo_records),
                'transactions': copy.deepcopy(self.transactions),
                'nodes': sorted(self.nodes),
                'peer_states': self.network.health.export_state(self.nodes),
                'node_address': self.node_address,
                'miner_name': self.miner_name,
                'mining_target': target_to_hex(self.MINING_TARGET),
            }
        DenariusStorage(self.STATE_PATH).save_state(state)

    def persist_peer_state(self):
        storage = DenariusStorage(self.STATE_PATH)
        if not storage.path.exists():
            self.save_everything()
            return
        with self._lock:
            nodes = sorted(self.nodes)
            peer_states = self.network.health.export_state(nodes)
        storage.update_peers(nodes, peer_states)

    def persist_appended_block(self, block, undo):
        storage = DenariusStorage(self.STATE_PATH)
        if not storage.path.exists():
            self.save_everything()
            return
        state = {
            'chain_state': self.chain_state.as_dict(),
            'transactions': copy.deepcopy(self.transactions),
            'nodes': sorted(self.nodes),
            'node_address': self.node_address,
            'miner_name': self.miner_name,
            'mining_target': target_to_hex(self.MINING_TARGET),
        }
        storage.append_block(state, block, undo)


    def load_persisted_chain_state(self, chain, persisted_chain_state, undo_records):
        if not isinstance(chain, list) or not chain or chain[0] != self.GENESIS_BLOCK:
            raise ValueError('State database has an invalid genesis block')
        try:
            chain_state = ChainState.from_dict(persisted_chain_state)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('State database chain index is invalid') from exc
        if chain_state.tip_height != len(chain) - 1:
            raise ValueError('State database chain index has an invalid height')
        if chain_state.tip_hash != self.hash(chain[-1]):
            raise ValueError('State database chain index has an invalid tip')
        if chain_state.issued_atomic < 0 or chain_state.issued_atomic > self.TOTAL_AMOUNT:
            raise ValueError('State database chain index has invalid issuance')
        if any(balance < 0 for balance in chain_state.balances.values()):
            raise ValueError('State database chain index has a negative balance')
        if any(nonce < 0 for nonce in chain_state.nonces.values()):
            raise ValueError('State database chain index has a negative nonce')
        expected_undo_heights = set(range(1, len(chain)))
        if set(undo_records) != expected_undo_heights:
            raise ValueError('State database reorganization undo index is incomplete')

        if len(chain) == 1:
            if chain_state.as_dict() != ChainState().as_dict():
                raise ValueError('State database genesis index is invalid')
            return chain_state

        height = len(chain) - 1
        undo = undo_records[height]
        if undo.get('height') != height or undo.get('block_hash') != self.hash(chain[-1]):
            raise ValueError('State database tip undo record is invalid')
        try:
            previous_state = self.restore_block_undo(chain_state, undo)
            verified_state = self.validate_next_block(
                chain[-1],
                chain[:-1],
                previous_state,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('State database tip state is invalid') from exc
        if verified_state is None or verified_state.as_dict() != chain_state.as_dict():
            raise ValueError('State database tip state does not match its block')
        return chain_state

    def load_everything(self, path, reindex=False):
        state_path = Path(path).resolve()
        state = DenariusStorage(state_path).load_state()

        chain = state.get('chain')
        persisted_chain_state = state.get('chain_state')
        undo_records = state.get('undo_records') or {}
        rewrite_indexes = reindex or persisted_chain_state is None
        if rewrite_indexes:
            chain_state, undo_records = self.replay_chain(chain)
            if chain_state is None:
                raise ValueError('State file contains an invalid Denarius chain')
        else:
            chain_state = self.load_persisted_chain_state(
                chain,
                persisted_chain_state,
                undo_records,
            )

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
        pending_validator.chain_state = chain_state.clone()
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
                fee=transaction.get('fee_atomic'),
                relay=False,
            )

        with self._lock:
            self.STATE_PATH = state_path
            self.chain = copy.deepcopy(chain)
            self.chain_state = chain_state
            self.undo_records = undo_records
            self.transactions = copy.deepcopy(pending_validator.transactions)
            self.nodes = nodes
            self.network.health.import_state(state.get('peer_states'))
            self.node_address = node_address
            self.miner_name = miner_name.strip()
            self.MINING_TARGET = mining_target
        if rewrite_indexes:
            self.save_everything()
        return self



app = Flask(__name__)
configure_trusted_proxy(app)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
SECURE_TRANSPORT = os.environ.get('DENARIUS_SECURE_TRANSPORT', '').lower() in ('1', 'true', 'yes')
metrics = install_runtime_controls(
    app,
    'denarius-node',
    policies={
        'healthz': None,
        'readyz': None,
        'new_transaction': (20, 60),
        'receive_transaction': (240, 60),
        'receive_block': (120, 60),
        'mine': (10, 60),
        'start_automining': (10, 60),
        'stop_automining': (10, 60),
        'register_nodes': (20, 60),
        'consensus': (10, 60),
    },
    secure_transport=SECURE_TRANSPORT,
)

blockchain = Blockchain()
ADMIN_TOKEN_ENV = 'DENARIUS_ADMIN_TOKEN'
METRICS_TOKEN_ENV = 'DENARIUS_METRICS_TOKEN'


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


def metrics_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        expected_token = os.environ.get(METRICS_TOKEN_ENV) or os.environ.get(ADMIN_TOKEN_ENV)
        submitted_token = request.headers.get('X-Denarius-Metrics-Token')
        if not expected_token or not submitted_token or not hmac.compare_digest(
            expected_token,
            submitted_token,
        ):
            return jsonify({'message': 'Metrics authentication is required'}), 403
        return func(*args, **kwargs)
    return wrapper


@app.route('/healthz', methods=['GET'])
def healthz():
    return jsonify({'status': 'alive'}), 200


@app.route('/readyz', methods=['GET'])
def readyz():
    with blockchain._lock:
        ready = bool(blockchain.chain) and blockchain.chain[0] == blockchain.GENESIS_BLOCK
    return jsonify({'status': 'ready' if ready else 'not-ready'}), 200 if ready else 503


@app.route('/metrics', methods=['GET'])
@metrics_required
def prometheus_metrics():
    with blockchain._lock:
        gauges = {
            'chain_height': len(blockchain.chain) - 1,
            'chainwork': blockchain.chain_state.chainwork,
            'mempool_transactions': len(blockchain.transactions),
            'configured_peers': len(blockchain.nodes),
        }
    return Response(metrics.render(gauges), content_type='text/plain; version=0.0.4')


@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    values = request.form

    # Check that the required fields are in the POST'ed data
    required = [
        'sender_address', 'recipient_address', 'amount', 'fee', 'nonce',
        'signature', 'transaction_id',
    ]
    if not all(k in values for k in required):
        return 'Missing values', 400
    # Create a new Transaction
    transaction_result = blockchain.submit_transaction(
        values['sender_address'], values['recipient_address'], values['amount'],
        values['nonce'], values['signature'], values['transaction_id'],
        fee=values['fee'],
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
        'sender_address', 'recipient_address', 'amount', 'fee', 'nonce',
        'signature', 'transaction_id',
    ]
    if not all(k in values for k in required):
        return 'Missing values', 400

    if blockchain.has_seen_transaction(values['transaction_id']):
        return jsonify({'message': 'Transaction already known'}), 200

    transaction_result = blockchain.submit_transaction(
        values['sender_address'], values['recipient_address'], values['amount'],
        values['nonce'], values['signature'], values['transaction_id'],
        fee=values['fee'],
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
        'immature_balance': blockchain.format_amount(blockchain.get_immature_balance(address)),
        'immature_balance_atomic': str(blockchain.get_immature_balance(address)),
        'next_nonce': blockchain.get_next_nonce(address),
    }), 200


@app.route('/chain', methods=['GET'])
def full_chain():
    try:
        limit = int(request.args.get('limit', blockchain.MAX_CHAIN_API_BLOCKS))
        submitted_start = request.args.get('start')
        start = int(submitted_start) if submitted_start is not None else None
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid chain range'}), 400
    if limit < 1 or limit > blockchain.MAX_CHAIN_API_BLOCKS or (start is not None and start < 0):
        return jsonify({'message': 'Invalid chain range'}), 400
    with blockchain._lock:
        chain_length = len(blockchain.chain)
        start = max(0, chain_length - limit) if start is None else start
        chain = copy.deepcopy(blockchain.chain[start:start + limit])
    response = {
        'chain': chain,
        'length': chain_length,
        'start': start,
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


@app.route('/mining/auto', methods=['GET'])
@admin_required
def get_automining():
    return jsonify(blockchain.automining_status()), 200


@app.route('/mining/auto/start', methods=['POST'])
@admin_required
def start_automining():
    status = blockchain.start_automining()
    if status is False:
        return jsonify({'message': 'Configure a valid miner before starting automining'}), 406
    return jsonify({
        'message': 'Automining started',
        **status,
    }), 200


@app.route('/mining/auto/stop', methods=['POST'])
@admin_required
def stop_automining():
    status = blockchain.stop_automining()
    return jsonify({
        'message': 'Automining stopped' if not status['running'] else 'Automining is stopping',
        **status,
    }), 200


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
        chain_length = len(blockchain.chain)
        blocks = copy.deepcopy(blockchain.chain[start:start + limit])
    headers = blockchain.headers_for_chain(blocks)
    return jsonify({
        'protocol': blockchain.protocol_status(),
        'length': chain_length,
        'start': start,
        'headers': headers,
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
        chain_length = len(blockchain.chain)
        blocks = copy.deepcopy(blockchain.chain[start:start + limit])
    return jsonify({
        'protocol': blockchain.protocol_status(),
        'length': chain_length,
        'start': start,
        'blocks': blocks,
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
        '--reindex',
        action='store_true',
        help='fully replay and rewrite chain indexes before serving',
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
    parser.add_argument(
        '--peer-scheme',
        choices=('http', 'https'),
        default=os.environ.get('DENARIUS_PEER_SCHEME', 'http'),
        help='peer transport; HTTPS verifies peer certificates (default: http)',
    )
    parser.add_argument(
        '--development-server',
        action='store_true',
        help='use Flask development serving for local debugging only',
    )
    parser.add_argument('--threads', type=int, default=8, help='Waitress worker threads')
    args = parser.parse_args(argv)
    port = args.port
    database_path = Path(args.database).resolve()
    if args.migrate_json:
        try:
            migrate_json_state(args.migrate_json, database_path)
        except ValueError as exc:
            parser.error(str(exc))
    blockchain.STATE_PATH = database_path
    blockchain.network.scheme = args.peer_scheme
    if database_path.exists():
        try:
            blockchain = blockchain.load_everything(database_path, reindex=args.reindex)
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
    configure_json_logging('denarius-node', os.environ.get('DENARIUS_LOG_LEVEL', 'INFO'))
    try:
        if args.development_server:
            app.run(host=args.host, port=port)
        else:
            from waitress import serve
            serve(app, host=args.host, port=port, threads=max(4, args.threads))
    finally:
        blockchain.stop_automining()
        blockchain.stop_background_sync()
        try:
            blockchain.persist_peer_state()
        finally:
            blockchain.network.close()


if __name__ == '__main__':
    main()
