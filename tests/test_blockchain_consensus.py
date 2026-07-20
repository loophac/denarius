from collections import OrderedDict
import binascii
import copy
from decimal import Decimal
import importlib.util
import json
import os
import random
from pathlib import Path
import sqlite3
import tempfile
import sys
import threading
import types
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "blockchain" / "blockchain.py"
CLIENT_MODULE_PATH = ROOT / "blockchain_client" / "blockchain_client.py"
DASHBOARD_MODULE_PATH = ROOT / "node_dashboard" / "dashboard.py"
STUBBED_MODULE_NAMES = ("flask", "flask_cors", "requests")
ORIGINAL_MODULES = {
    name: sys.modules.get(name)
    for name in STUBBED_MODULE_NAMES
}


def install_flask_stubs():
    flask_stub = types.ModuleType("flask")

    class Flask:
        def __init__(self, *args, **kwargs):
            self.config = {}
            self.secret_key = None

        def route(self, *args, **kwargs):
            return lambda func: func

        def before_request(self, func):
            return func

        def after_request(self, func):
            return func

        def run(self, *args, **kwargs):
            return None

    class Response:
        def __init__(self, response=None, content_type=None):
            self.response = response
            self.content_type = content_type
            self.status_code = 200
            self.headers = {}

    class Session(dict):
        permanent = False

    flask_stub.Flask = Flask
    flask_stub.Response = Response
    flask_stub.jsonify = lambda *args, **kwargs: args[0] if args else kwargs
    flask_stub.request = types.SimpleNamespace(
        args={},
        form={},
        files={},
        headers={},
        method="GET",
        path="",
        remote_addr="127.0.0.1",
        get_json=lambda silent=True: {},
    )
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.redirect = lambda value: value
    flask_stub.g = types.SimpleNamespace()
    flask_stub.session = Session()
    flask_stub.url_for = lambda endpoint: endpoint

    flask_cors_stub = types.ModuleType("flask_cors")
    flask_cors_stub.CORS = lambda app, **kwargs: app

    sys.modules.setdefault("flask", flask_stub)
    sys.modules.setdefault("flask_cors", flask_cors_stub)


def install_requests_stub():
    requests_stub = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    requests_stub.RequestException = RequestException
    requests_stub.get = Mock(side_effect=RequestException("requests stub"))
    requests_stub.post = Mock(side_effect=RequestException("requests stub"))
    sys.modules.setdefault("requests", requests_stub)


install_flask_stubs()
install_requests_stub()

spec = importlib.util.spec_from_file_location("denarius_blockchain", MODULE_PATH)
denarius_blockchain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(denarius_blockchain)

client_spec = importlib.util.spec_from_file_location("denarius_client", CLIENT_MODULE_PATH)
denarius_client = importlib.util.module_from_spec(client_spec)
client_spec.loader.exec_module(denarius_client)

dashboard_spec = importlib.util.spec_from_file_location("denarius_dashboard", DASHBOARD_MODULE_PATH)
denarius_dashboard = importlib.util.module_from_spec(dashboard_spec)
dashboard_spec.loader.exec_module(denarius_dashboard)

for module_name, original_module in ORIGINAL_MODULES.items():
    if original_module is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = original_module

Blockchain = denarius_blockchain.Blockchain

import denarius_protocol
import denarius_crypto
import denarius_operations
import denarius_storage
from denarius_storage import migrate_json_state
from denarius_accounts import DenariusAccountStore
from denarius_network import PeerNetwork
from denarius_admin import create_backup, restore_backup, verify_backup
from denarius_operations import OperationalMetrics, SlidingWindowRateLimiter


def mine_block(blockchain):
    block = blockchain.mine_pending_transactions(relay=False, persist=False)
    assert block is not False
    return block


def mine_candidate(blockchain, mutate=None):
    candidate = blockchain.create_candidate_block()
    if mutate:
        mutate(candidate)
    return blockchain.proof_of_work(candidate)


def mine_empty_block(blockchain):
    return mine_block(blockchain)


def wallet(blockchain):
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        blockchain.address_from_public_key(public_bytes),
        binascii.hexlify(private_bytes).decode("ascii"),
    )


def signed_transaction(
    sender_address,
    private_key,
    recipient_address,
    value,
    nonce=0,
    fee="0.0001",
):
    amount_atomic = int(Decimal(str(value)) * denarius_protocol.ATOMIC_UNITS)
    fee_atomic = int(Decimal(str(fee)) * denarius_protocol.ATOMIC_UNITS)
    return signed_atomic_transaction(
        sender_address,
        private_key,
        recipient_address,
        amount_atomic,
        nonce,
        fee_atomic,
    )


def signed_atomic_transaction(
    sender_address,
    private_key,
    recipient_address,
    atomic_value,
    nonce=0,
    fee_atomic=denarius_protocol.MIN_TRANSACTION_FEE_ATOMIC,
):
    transaction = denarius_blockchain.transaction_signing_payload(
        sender_address, recipient_address, atomic_value, nonce, fee_atomic
    )
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(binascii.unhexlify(private_key))
    signature = binascii.hexlify(
        private_key.sign(denarius_blockchain.canonical_json_bytes(transaction))
    ).decode("ascii")
    transaction["signature"] = signature
    transaction["transaction_id"] = denarius_blockchain.signed_transaction_id(
        transaction, signature
    )
    return transaction, signature


def submit_signed(blockchain, transaction, relay=False):
    return blockchain.submit_transaction(
        transaction["sender_address"],
        transaction["recipient_address"],
        transaction["amount_atomic"],
        transaction["nonce"],
        transaction["signature"],
        transaction["transaction_id"],
        fee=transaction["fee_atomic"],
        relay=relay,
    )


FUNDED_CHAIN = None


def funded_blockchain():
    global FUNDED_CHAIN
    if FUNDED_CHAIN is None:
        funded = Blockchain()
        address, private_key = wallet(funded)
        funded.node_address = address
        for _ in range(denarius_protocol.COINBASE_MATURITY + 1):
            mine_block(funded)
        FUNDED_CHAIN = (copy.deepcopy(funded.chain), address, private_key)

    chain, address, private_key = FUNDED_CHAIN
    blockchain = Blockchain()
    blockchain.chain = copy.deepcopy(chain)
    blockchain.node_address = address
    blockchain.chain_state, blockchain.undo_records = blockchain.replay_chain(blockchain.chain)
    return blockchain, address, private_key


def test_public_transactions_cannot_create_coinbase_rewards():
    blockchain = Blockchain()
    recipient_address, _ = wallet(blockchain)

    result = blockchain.submit_transaction(
        blockchain.COINBASE_SENDER,
        recipient_address,
        str(blockchain.block_reward(1)),
        0,
        "",
        "",
    )

    assert result is False
    assert blockchain.transactions == []


def test_rejects_non_positive_and_non_finite_amounts():
    blockchain = Blockchain()
    sender_address, private_key = wallet(blockchain)
    recipient_address, _ = wallet(blockchain)

    for amount in ("0", "-1", "NaN", "Infinity"):
        transaction, _ = signed_transaction(sender_address, private_key, recipient_address, "1")
        assert blockchain.submit_transaction(
            sender_address,
            recipient_address,
            amount,
            transaction["nonce"],
            transaction["signature"],
            transaction["transaction_id"],
        ) is False


def test_pending_transactions_are_counted_against_balance():
    blockchain, sender_address, private_key = funded_blockchain()
    bob_address, _ = wallet(blockchain)
    carol_address, _ = wallet(blockchain)

    reward = blockchain.block_reward(1) - denarius_protocol.MIN_TRANSACTION_FEE_ATOMIC
    bob_transaction, bob_signature = signed_transaction(
        sender_address,
        private_key,
        bob_address,
        blockchain.format_amount(reward),
    )
    carol_transaction, carol_signature = signed_transaction(
        sender_address, private_key, carol_address, "1", nonce=1
    )

    assert submit_signed(blockchain, bob_transaction) == len(blockchain.chain)
    assert submit_signed(blockchain, carol_transaction) is False


def test_ed25519_signature_and_address_checksum_are_required():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    invalid_sender = sender_address[:-1] + ("0" if sender_address[-1] != "0" else "1")
    invalid_recipient = recipient_address[:-1] + ("0" if recipient_address[-1] != "0" else "1")

    signing_payload = denarius_blockchain.transaction_signing_payload(
        sender_address,
        recipient_address,
        transaction["amount_atomic"],
        transaction["nonce"],
    )
    assert blockchain.verify_transaction_signature(sender_address, signature, signing_payload) is True
    assert blockchain.verify_transaction_signature(invalid_sender, signature, signing_payload) is False
    assert blockchain.submit_transaction(
        sender_address,
        invalid_recipient,
        transaction["amount_atomic"],
        transaction["nonce"],
        signature,
        transaction["transaction_id"],
    ) is False


def test_forged_signature_is_rejected():
    blockchain, sender_address, _ = funded_blockchain()
    recipient_address, recipient_private_key = wallet(blockchain)
    transaction, _ = signed_atomic_transaction(
        sender_address,
        recipient_private_key,
        recipient_address,
        blockchain.ATOMIC_UNITS,
    )

    assert submit_signed(blockchain, transaction) is False


def test_valid_chain_requires_canonical_genesis_block():
    blockchain = Blockchain()
    candidate_chain = [dict(blockchain.chain[0])]
    candidate_chain[0]["timestamp"] += 1

    assert blockchain.valid_chain(candidate_chain) is False


def test_valid_chain_rejects_incorrect_coinbase_reward():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    blockchain.node_address = miner_address
    def set_incorrect_reward(candidate):
        candidate["transactions"][0] = denarius_blockchain.coinbase_transaction(
            miner_address,
            blockchain.block_reward(1) + 1,
            1,
        )
        candidate["merkle_root"] = denarius_blockchain.calculate_merkle_root(
            candidate["transactions"]
        )

    block = mine_candidate(blockchain, set_incorrect_reward)
    blockchain.chain.append(block)

    assert blockchain.valid_chain(blockchain.chain) is False


def test_valid_chain_rejects_multiple_coinbase_transactions():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    blockchain.node_address = miner_address
    def add_second_coinbase(candidate):
        candidate["transactions"].append(blockchain.create_coinbase_transaction())
        candidate["merkle_root"] = denarius_blockchain.calculate_merkle_root(
            candidate["transactions"]
        )

    block = mine_candidate(blockchain, add_second_coinbase)
    blockchain.chain.append(block)

    assert blockchain.valid_chain(blockchain.chain) is False


def test_valid_chain_rejects_invalid_target():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    blockchain.node_address = miner_address
    mine_block(blockchain)
    blockchain.chain[-1]["target"] = "0" * 64

    assert blockchain.valid_chain(blockchain.chain) is False


def test_coinbase_requires_valid_denarius_address():
    blockchain = Blockchain()
    blockchain.chain.append(mine_candidate(blockchain))

    assert blockchain.valid_chain(blockchain.chain) is False


def test_miner_registration_requires_valid_denarius_address():
    blockchain = Blockchain()

    try:
        blockchain.set_miner_info("miner", "not-a-denarius-address")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid miner address was accepted")


def test_valid_chain_replays_transaction_balances():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_atomic_transaction(
        sender_address,
        private_key,
        recipient_address,
        blockchain.TOTAL_AMOUNT,
    )

    blockchain.transactions.append(transaction)
    blockchain.chain.append(mine_candidate(blockchain))

    assert blockchain.valid_chain(blockchain.chain) is False


def test_submit_rejects_confirmed_transaction_replay():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")

    assert submit_signed(blockchain, transaction)
    mine_block(blockchain)

    assert submit_signed(blockchain, transaction) is False


def test_miner_will_not_append_a_replayed_transaction():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    blockchain.transactions.append(copy.deepcopy(transaction))
    mine_block(blockchain)
    chain_length = len(blockchain.chain)
    blockchain.transactions.append(copy.deepcopy(transaction))

    assert blockchain.mine_pending_transactions(relay=False, persist=False) is False
    assert len(blockchain.chain) == chain_length


def test_coinbase_recipient_is_bound_to_proof_of_work():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    thief_address, _ = wallet(blockchain)
    blockchain.node_address = miner_address
    block = mine_block(blockchain)
    altered_block = copy.deepcopy(block)
    altered_block["transactions"][0]["recipient_address"] = thief_address

    assert blockchain.valid_proof(altered_block) is False
    assert blockchain.valid_chain([blockchain.chain[0], altered_block]) is False


def test_block_metadata_is_bound_to_proof_of_work():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    block = mine_block(blockchain)
    altered_block = copy.deepcopy(block)
    altered_block["timestamp"] += 1

    assert blockchain.valid_proof(altered_block) is False


def test_state_is_saved_and_loaded_from_sqlite():
    blockchain, miner_address, _ = funded_blockchain()
    blockchain.miner_name = "miner"
    blockchain.nodes.add("127.0.0.1:5001")

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "denarius.db"
        blockchain.STATE_PATH = state_path
        blockchain.save_everything()

        restored = Blockchain().load_everything(state_path)

        connection = sqlite3.connect(state_path)
        try:
            tables = {
                name for name, in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()

    assert restored.chain == blockchain.chain
    assert restored.node_address == miner_address
    assert restored.miner_name == "miner"
    assert restored.nodes == {"127.0.0.1:5001"}

    assert {"metadata", "blocks", "pending_transactions", "peers"} <= tables


def test_state_loader_rejects_a_tampered_chain():
    blockchain, _, _ = funded_blockchain()

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "denarius.db"
        blockchain.STATE_PATH = state_path
        blockchain.save_everything()
        connection = sqlite3.connect(state_path)
        try:
            block_json = connection.execute(
                "SELECT block_json FROM blocks WHERE height = 1"
            ).fetchone()[0]
            block = json.loads(block_json)
            block["timestamp"] += 1
            connection.execute(
                "UPDATE blocks SET block_json = ? WHERE height = 1",
                (json.dumps(block),),
            )
            connection.commit()
        finally:
            connection.close()

        try:
            Blockchain().load_everything(state_path)
        except ValueError:
            pass
        else:
            raise AssertionError("tampered state chain was loaded")


def test_mining_reserves_space_for_coinbase_and_leaves_overflow_pending():
    blockchain, sender_address, private_key = funded_blockchain()
    blockchain.MAX_TRANSACTIONS_PER_BLOCK = 2
    first_recipient, _ = wallet(blockchain)
    second_recipient, _ = wallet(blockchain)
    first, first_signature = signed_transaction(sender_address, private_key, first_recipient, "1")
    second, second_signature = signed_transaction(
        sender_address, private_key, second_recipient, "1", nonce=1
    )

    assert submit_signed(blockchain, first)
    assert submit_signed(blockchain, second)

    block = mine_block(blockchain)

    assert len(block["transactions"]) == 2
    assert len(blockchain.transactions) == 1
    assert blockchain.valid_chain(blockchain.chain) is True


def test_local_admin_passwords_are_salted_and_verified():
    password = "correct horse battery staple"
    first_hash = denarius_dashboard.hash_password(password)
    second_hash = denarius_dashboard.hash_password(password)

    assert first_hash != second_hash
    assert denarius_dashboard.verify_password(password, first_hash) is True
    assert denarius_dashboard.verify_password("wrong password", first_hash) is False


def test_console_accounts_persist_with_first_account_as_administrator():
    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "accounts.db"
        store = DenariusAccountStore(database_path)
        administrator = store.create_account("NodeAdmin", "admin-password-hash")
        member = store.create_account("alice", "member-password-hash")

        assert administrator["role"] == "admin"
        assert member["role"] == "user"
        assert DenariusAccountStore(database_path).find_by_username("nodeadmin") == administrator
        assert DenariusAccountStore(database_path).find_by_id(member["id"]) == member


def test_console_account_names_are_case_insensitively_unique():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = DenariusAccountStore(Path(tmpdir) / "accounts.db")
        store.create_account("Alice", "first-password-hash")

        try:
            store.create_account("alice", "second-password-hash")
        except ValueError as exc:
            assert "already registered" in str(exc)
        else:
            raise AssertionError("duplicate console account was created")


def test_first_console_administrator_requires_the_launcher_setup_code():
    original_store = denarius_client.account_store
    original_form = denarius_client.request.form
    original_method = denarius_client.request.method
    original_session = dict(denarius_client.session)
    try:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"DENARIUS_SETUP_TOKEN": "one-time-setup-code"},
            clear=False,
        ):
            store = DenariusAccountStore(Path(tmpdir) / "accounts.db")
            denarius_client.account_store = store
            denarius_client.session.clear()
            csrf_token = denarius_client.ensure_csrf_token()
            denarius_client.request.method = "POST"
            registration = {
                "csrf_token": csrf_token,
                "setup_token": "incorrect-code",
                "username": "nodeadmin",
                "password": "administrator password",
                "password_confirm": "administrator password",
            }
            denarius_client.request.form = registration

            _, status = denarius_client.register()
            assert status == 400
            assert store.has_admin() is False

            registration["setup_token"] = "one-time-setup-code"
            assert denarius_client.register() == "index"
            assert store.find_by_username("nodeadmin")["role"] == "admin"
    finally:
        denarius_client.account_store = original_store
        denarius_client.request.form = original_form
        denarius_client.request.method = original_method
        denarius_client.session.clear()
        denarius_client.session.update(original_session)


def test_standard_console_accounts_cannot_access_node_administration():
    original_store = denarius_client.account_store
    original_path = denarius_client.request.path
    original_session = dict(denarius_client.session)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DenariusAccountStore(Path(tmpdir) / "accounts.db")
            administrator = store.create_account("admin", "admin-password-hash")
            member = store.create_account("member", "member-password-hash")
            denarius_client.account_store = store
            denarius_client.session.clear()
            denarius_client.session["account_id"] = member["id"]

            denarius_client.request.path = "/network"
            _, page_status = denarius_client.network()

            assert page_status == 403
            assert denarius_client.wallets() == ""
            assert denarius_client.send() == ""
            assert denarius_client.activity() == ""
            assert denarius_client.index() == "wallets"

            restricted_apis = (
                ("/api/miner", denarius_client.api_miner),
                ("/api/miner", denarius_client.api_register_miner),
                ("/api/nodes", denarius_client.api_nodes),
                ("/api/nodes", denarius_client.api_register_nodes),
                ("/api/mine", denarius_client.api_mine),
                ("/api/automine", denarius_client.api_automine),
                ("/api/resolve", denarius_client.api_resolve),
            )
            for path, endpoint in restricted_apis:
                denarius_client.request.path = path
                _, api_status = endpoint()
                assert api_status == 403

            denarius_client.session["account_id"] = administrator["id"]
            denarius_client.request.path = "/network"
            assert denarius_client.network() == ""
    finally:
        denarius_client.account_store = original_store
        denarius_client.request.path = original_path
        denarius_client.session.clear()
        denarius_client.session.update(original_session)


def test_node_mining_requires_admin_token():
    original_headers = denarius_blockchain.request.headers
    try:
        denarius_blockchain.request.headers = {}
        with patch.dict(os.environ, {}, clear=True):
            _, status = denarius_blockchain.mine()
            assert status == 503

        token = "a" * 32
        with patch.dict(os.environ, {"DENARIUS_ADMIN_TOKEN": token}, clear=False):
            denarius_blockchain.request.headers = {"X-Denarius-Admin-Token": "wrong"}
            _, status = denarius_blockchain.mine()
            assert status == 403

            denarius_blockchain.request.headers = {"X-Denarius-Admin-Token": token}
            with patch.object(denarius_blockchain.blockchain, "mine_pending_transactions", return_value=False):
                _, status = denarius_blockchain.mine()
            assert status == 406
    finally:
        denarius_blockchain.request.headers = original_headers


def test_node_urls_reject_paths_and_markup():
    blockchain = Blockchain()

    for node in ("http://127.0.0.1:5001/path", '127.0.0.1:5001"><script>'):
        try:
            blockchain.register_node(node)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe node URL was accepted")


def test_node_rejects_its_advertised_address_as_a_peer():
    blockchain = Blockchain()
    blockchain.advertised_node = "127.0.0.1:5000"

    try:
        blockchain.register_node("http://127.0.0.1:5000")
    except ValueError as exc:
        assert "current node" in str(exc)
    else:
        raise AssertionError("node registered itself as a peer")


def test_submit_transaction_relays_to_known_peers():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    peer_node = "127.0.0.1:5001"
    blockchain.nodes.add(peer_node)
    blockchain.network.health.record_compatible(peer_node, blockchain.protocol_status())

    with patch.object(denarius_blockchain.requests, "post") as post:
        post.return_value = Mock(status_code=201)
        result = submit_signed(blockchain, transaction, relay=True)
        blockchain.network.wait_for_relays(timeout=1)

    assert result == len(blockchain.chain)
    post.assert_called_once()
    assert post.call_args.kwargs["data"]["amount"] == transaction["amount_atomic"]
    assert post.call_args.kwargs["data"]["nonce"] == transaction["nonce"]
    assert post.call_args.kwargs["data"]["transaction_id"] == transaction["transaction_id"]
    assert post.call_args.kwargs["timeout"] == blockchain.PEER_REQUEST_TIMEOUT
    assert post.call_args.kwargs["headers"]["X-Denarius-Network"] == blockchain.NETWORK_ID


def test_transaction_relay_skips_incompatible_peers():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    peer_node = "127.0.0.1:5001"
    blockchain.nodes.add(peer_node)
    incompatible = blockchain.protocol_status()
    incompatible["network"] = "another-network"
    response = Mock(status_code=200)
    response.json.return_value = incompatible

    with patch.object(denarius_blockchain.requests, "get", return_value=response), patch.object(denarius_blockchain.requests, "post") as post:
        assert submit_signed(blockchain, transaction, relay=True)
        blockchain.network.wait_for_relays(timeout=1)

    post.assert_not_called()
    health = blockchain.peer_health()[0]
    assert health["status"] == "incompatible"
    assert health["compatible"] is False


def test_received_transaction_is_relayed_once():
    local, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(local)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    original_headers = denarius_blockchain.request.headers
    original_form = denarius_blockchain.request.form
    denarius_blockchain.request.headers = {
        "X-Denarius-Protocol-Version": str(local.PROTOCOL_VERSION),
        "X-Denarius-Network": local.NETWORK_ID,
        "X-Denarius-Peer-API-Version": "1",
    }
    denarius_blockchain.request.form = {
        "sender_address": transaction["sender_address"],
        "recipient_address": transaction["recipient_address"],
        "amount": transaction["amount_atomic"],
        "fee": transaction["fee_atomic"],
        "nonce": transaction["nonce"],
        "signature": signature,
        "transaction_id": transaction["transaction_id"],
    }
    try:
        with patch.object(denarius_blockchain, "blockchain", local), patch.object(local, "broadcast_transaction") as relay:
            _, first_status = denarius_blockchain.receive_transaction()
            _, duplicate_status = denarius_blockchain.receive_transaction()
        assert first_status == 201
        assert duplicate_status == 200
        relay.assert_called_once()
    finally:
        denarius_blockchain.request.headers = original_headers
        denarius_blockchain.request.form = original_form


def test_accept_block_from_peer_appends_and_removes_pending_transaction():
    local, sender_address, private_key = funded_blockchain()
    peer = Blockchain()
    peer.chain = [dict(block) for block in local.chain]
    peer.node_address, _ = wallet(peer)

    recipient_address, _ = wallet(peer)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    assert submit_signed(local, transaction)

    peer.transactions.append(OrderedDict(transaction))
    block = mine_block(peer)

    with patch.object(local, "save_everything"), patch.object(local, "broadcast_block") as broadcast_block:
        assert local.accept_block(block) is True

    assert local.chain[-1] == block
    assert local.transactions == []
    broadcast_block.assert_called_once_with(block)


def test_accept_block_rejects_invalid_peer_block():
    blockchain = Blockchain()
    block = {
        "version": blockchain.PROTOCOL_VERSION,
        "network": blockchain.NETWORK_ID,
        "block_number": 1,
        "timestamp": 1,
        "merkle_root": denarius_blockchain.calculate_merkle_root([]),
        "transactions": [],
        "nonce": 0,
        "previous_hash": "bad",
        "target": denarius_blockchain.target_to_hex(denarius_blockchain.INITIAL_TARGET),
    }

    assert blockchain.accept_block(block) is False


def test_valid_chain_rejects_oversized_blocks():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    mine_block(blockchain)
    blockchain.MAX_TRANSACTIONS_PER_BLOCK = 0

    assert blockchain.valid_chain(blockchain.chain) is False


def test_exchange_peer_table_adds_peers_from_registered_nodes():
    blockchain = Blockchain()
    blockchain.nodes.add("127.0.0.1:5001")

    def get_peer_data(url, timeout, headers):
        response = Mock(status_code=200)
        if url.endswith("/protocol"):
            response.json.return_value = blockchain.protocol_status()
        else:
            response.json.return_value = {
                "nodes": ["127.0.0.1:5002", "http://127.0.0.1:5003"]
            }
        return response

    with patch.object(denarius_blockchain.requests, "get", side_effect=get_peer_data) as get:
        blockchain.exchange_peer_table()

    assert "127.0.0.1:5002" in blockchain.nodes
    assert "127.0.0.1:5003" in blockchain.nodes
    assert get.call_count == 2


def test_resolve_conflicts_ignores_malformed_peer_header_response():
    blockchain = Blockchain()
    blockchain.nodes.add("127.0.0.1:5001")

    def get_malformed_headers(url, timeout, headers):
        response = Mock(status_code=200)
        response.json.return_value = (
            blockchain.protocol_status()
            if url.endswith("/protocol")
            else {"not_headers": []}
        )
        return response

    with patch.object(denarius_blockchain.requests, "get", side_effect=get_malformed_headers):
        assert blockchain.resolve_conflicts() is False


def test_resolve_conflicts_prefers_greater_chainwork():
    local = Blockchain()
    local.node_address, _ = wallet(local)
    mine_empty_block(local)

    peer = Blockchain()
    peer.node_address, _ = wallet(peer)
    mine_empty_block(peer)
    mine_empty_block(peer)

    local.nodes.add("peer")

    requested_paths = []

    def get_peer_chain(url, timeout, headers):
        parsed = urlparse(url)
        requested_paths.append(parsed.path)
        response = Mock(status_code=200)
        if parsed.path == "/protocol":
            response.json.return_value = peer.protocol_status()
        elif parsed.path == "/headers":
            query = parse_qs(parsed.query)
            start = int(query["start"][0])
            limit = int(query["limit"][0])
            peer_headers = peer.headers_for_chain()
            response.json.return_value = {
                "protocol": peer.protocol_status(),
                "length": len(peer_headers),
                "headers": peer_headers[start:start + limit],
            }
        elif parsed.path == "/blocks":
            query = parse_qs(parsed.query)
            start = int(query["start"][0])
            limit = int(query["limit"][0])
            response.json.return_value = {
                "protocol": peer.protocol_status(),
                "length": len(peer.chain),
                "blocks": peer.chain[start:start + limit],
            }
        else:
            raise AssertionError("unexpected peer request: " + url)
        return response

    with patch.object(denarius_blockchain.requests, "get", side_effect=get_peer_chain), patch.object(local, "save_everything"):
        assert local.resolve_conflicts() is True

    assert local.chain == peer.chain
    assert "/headers" in requested_paths
    assert "/blocks" in requested_paths
    assert "/chain" not in requested_paths


def test_equal_peer_sync_only_probes_the_local_tip_header():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    mine_empty_block(blockchain)
    local_headers = blockchain.headers_for_chain()
    peer = "127.0.0.1:5001"

    with patch.object(blockchain.network, "get_json") as get_json:
        get_json.return_value = {
            "protocol": blockchain.protocol_status(),
            "length": len(local_headers),
            "headers": [local_headers[-1]],
        }
        result = blockchain.fetch_peer_headers(peer, local_headers)

    assert result == (local_headers, len(local_headers) - 1)
    get_json.assert_called_once_with(
        peer,
        "/headers?start=" + str(len(local_headers) - 1) + "&limit=1",
    )


def test_header_validation_rejects_tampered_proof_metadata():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    mine_empty_block(blockchain)
    headers = blockchain.headers_for_chain()
    headers[1]["hash"] = "0" * 64

    assert blockchain.valid_header_chain(headers) is False


def test_peer_health_becomes_unreachable_after_repeated_failures():
    blockchain = Blockchain()
    peer_node = "127.0.0.1:5001"
    blockchain.nodes.add(peer_node)

    with patch.object(
        denarius_blockchain.requests,
        "get",
        side_effect=denarius_blockchain.requests.RequestException("offline"),
    ):
        for _ in range(3):
            assert blockchain.network.ensure_compatible(peer_node) is False

    health = blockchain.peer_health()[0]
    assert health["status"] == "unreachable"
    assert health["consecutive_failures"] == 3


def test_relay_is_asynchronous_and_bounded():
    entered = threading.Event()
    release = threading.Event()
    requests_module = Mock()
    requests_module.RequestException = Exception

    def slow_post(*args, **kwargs):
        entered.set()
        release.wait(2)
        return Mock(status_code=201)

    requests_module.post.side_effect = slow_post
    network = PeerNetwork(
        requests_module=requests_module,
        relay_workers=1,
        relay_queue_size=0,
    )
    peer = "127.0.0.1:5001"
    network.health.record_compatible(peer, denarius_blockchain.protocol_identity())
    transaction = {
        "sender_address": "sender",
        "recipient_address": "recipient",
        "amount_atomic": "1",
        "fee_atomic": str(denarius_protocol.MIN_TRANSACTION_FEE_ATOMIC),
        "nonce": 0,
        "signature": "signature",
        "transaction_id": "1" * 64,
    }
    try:
        first = network.relay_transaction([peer], transaction)
        assert len(first) == 1
        assert entered.wait(1)

        second = network.relay_transaction([peer], transaction)
        assert second == []
        assert network.peer_health([peer])[0]["relay_drops"] == 1
    finally:
        release.set()
        network.wait_for_relays(timeout=2)
        network.close()


def test_peer_misbehavior_bans_and_persists_across_restart():
    blockchain = Blockchain()
    peer = "127.0.0.1:5001"
    blockchain.nodes.add(peer)
    for _ in range(4):
        blockchain.network.health.record_misbehavior(peer, "invalid block", score=25)

    assert blockchain.network.health.is_banned(peer) is True

    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "denarius-testnet-v3.db"
        blockchain.STATE_PATH = database_path
        blockchain.save_everything()
        restored = Blockchain().load_everything(database_path)

    assert restored.network.health.is_banned(peer) is True
    assert restored.peer_health()[0]["score"] >= 100


def test_discovered_peers_are_bounded_and_network_diverse():
    blockchain = Blockchain()
    source = "127.0.0.1:5001"
    blockchain.nodes.add(source)

    for port in (5002, 5003, 5004):
        assert blockchain.register_node(
            "127.0.0.1:" + str(port),
            discovered_from=source,
        ) is True

    try:
        blockchain.register_node("127.0.0.1:5005", discovered_from=source)
    except ValueError as exc:
        assert "network group limit" in str(exc)
    else:
        raise AssertionError("discovered peer bypassed the network-group limit")

    try:
        blockchain.register_node("peer.example:5000", discovered_from=source)
    except ValueError as exc:
        assert "IP addresses" in str(exc)
    else:
        raise AssertionError("peer gossip accepted a hostname")


def test_background_synchronization_runs_and_can_be_woken():
    blockchain = Blockchain()
    synchronized = threading.Event()

    def mark_synchronized():
        synchronized.set()
        return False

    with patch.object(blockchain, "synchronize_network", side_effect=mark_synchronized):
        thread = blockchain.start_background_sync(interval=60)
        assert synchronized.wait(1)
        synchronized.clear()
        blockchain.trigger_background_sync()
        assert synchronized.wait(1)
        blockchain.stop_background_sync()
        thread.join(timeout=1)

    assert thread.is_alive() is False


def test_automining_runs_on_the_node_and_stops_cleanly():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    attempted = threading.Event()

    def fake_mine(relay=True, persist=True, stop_event=None):
        attempted.set()
        return {"block_number": 1}

    with patch.object(blockchain, "mine_pending_transactions", side_effect=fake_mine):
        started = blockchain.start_automining()
        assert started["running"] is True
        assert attempted.wait(1)
        for _ in range(100):
            if blockchain.automining_status()["blocks_mined"] == 1:
                break
            threading.Event().wait(0.01)
        stopped = blockchain.stop_automining()

    assert stopped["running"] is False
    assert stopped["blocks_mined"] == 1
    assert stopped["last_block"] == 1


def test_automining_requires_a_configured_miner_and_is_not_persisted():
    blockchain = Blockchain()
    assert blockchain.start_automining() is False

    blockchain.node_address, _ = wallet(blockchain)
    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "automining.db"
        blockchain.STATE_PATH = database_path
        blockchain.save_everything()
        restored = Blockchain().load_everything(database_path)

    assert restored.automining_status()["running"] is False
    assert restored.automining_status()["started_at"] is None


def test_proof_of_work_can_be_cancelled_by_autominer_shutdown():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    stop_event = threading.Event()
    stop_event.set()

    assert blockchain.proof_of_work(
        blockchain.create_candidate_block(),
        stop_event=stop_event,
    ) is None


def test_chainwork_uses_exact_target_formula():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    mine_empty_block(blockchain)

    target = denarius_blockchain.target_from_hex(blockchain.chain[1]["target"])
    assert blockchain.chainwork(blockchain.chain) == (1 << 256) // (target + 1)


def test_transaction_tables_format_atomic_values_as_denarii():
    console_source = (ROOT / "blockchain_client" / "static" / "js" / "console.js").read_text()
    activity_template = (ROOT / "blockchain_client" / "templates" / "activity.html").read_text()
    send_template = (ROOT / "blockchain_client" / "templates" / "send.html").read_text()

    assert "function formatDenarii" in console_source
    assert "DenariusConsole.formatDenarii(transaction.amount_atomic)" in activity_template

    assert "review_amount" in send_template
    assert "DenariusConsole.formatDenarii(transaction.amount_atomic)" in send_template
    assert "response.transaction_id" in send_template


def test_confirmed_denarii_monetary_policy():
    assert denarius_protocol.MAX_SUPPLY_DEN == 100_000_000
    assert denarius_protocol.TARGET_BLOCK_SECONDS == 120
    assert denarius_protocol.HALVING_INTERVAL == 1_051_200
    assert denarius_protocol.block_reward(1) == 4_756_468_797
    assert denarius_protocol.block_reward(denarius_protocol.HALVING_INTERVAL) == 4_756_468_797
    assert denarius_protocol.block_reward(denarius_protocol.HALVING_INTERVAL + 1) == 2_378_234_398

    scheduled_supply = 0
    height = 1
    while denarius_protocol.block_reward(height):
        scheduled_supply += (
            denarius_protocol.block_reward(height) * denarius_protocol.HALVING_INTERVAL
        )
        height += denarius_protocol.HALVING_INTERVAL

    assert scheduled_supply <= denarius_protocol.MAX_SUPPLY_ATOMIC


def test_coinbase_rewards_are_immature_for_ten_blocks():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    blockchain.node_address = miner_address

    mine_block(blockchain)
    reward = blockchain.block_reward(1)
    assert blockchain.get_atomic_balance(miner_address) == 0
    assert blockchain.get_immature_balance(miner_address) == reward

    for _ in range(denarius_protocol.COINBASE_MATURITY - 1):
        mine_block(blockchain)
    assert blockchain.get_atomic_balance(miner_address) == 0

    mine_block(blockchain)
    assert blockchain.get_atomic_balance(miner_address) == reward


def test_transaction_fees_are_signed_and_paid_to_the_miner():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    starting_balance = blockchain.get_atomic_balance(sender_address)
    transaction, _ = signed_transaction(
        sender_address,
        private_key,
        recipient_address,
        "2",
    )

    assert submit_signed(blockchain, transaction)
    block = mine_block(blockchain)

    fee = denarius_protocol.MIN_TRANSACTION_FEE_ATOMIC
    assert transaction["fee_atomic"] == str(fee)
    assert block["transactions"][0]["amount_atomic"] == str(
        blockchain.block_reward(block["block_number"]) + fee
    )
    assert blockchain.get_atomic_balance(recipient_address) == 2 * blockchain.ATOMIC_UNITS
    assert blockchain.get_atomic_balance(sender_address) == (
        starting_balance
        + blockchain.block_reward(2)
        - 2 * blockchain.ATOMIC_UNITS
        - fee
    )


def test_transactions_below_the_minimum_fee_are_rejected():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, _ = signed_atomic_transaction(
        sender_address,
        private_key,
        recipient_address,
        blockchain.ATOMIC_UNITS,
        fee_atomic=1,
    )

    assert blockchain.submit_transaction(
        transaction["sender_address"],
        transaction["recipient_address"],
        transaction["amount_atomic"],
        transaction["nonce"],
        transaction["signature"],
        transaction["transaction_id"],
        fee=transaction["fee_atomic"],
        relay=False,
    ) is False


def test_mempool_limits_each_sender_and_routine_mining_is_incremental():
    blockchain, sender_address, private_key = funded_blockchain()
    blockchain.MAX_PENDING_PER_SENDER = 1
    first_recipient, _ = wallet(blockchain)
    second_recipient, _ = wallet(blockchain)
    first, _ = signed_transaction(sender_address, private_key, first_recipient, "1")
    second, _ = signed_transaction(
        sender_address,
        private_key,
        second_recipient,
        "1",
        nonce=1,
    )

    assert submit_signed(blockchain, first)
    assert submit_signed(blockchain, second) is False

    with patch.object(blockchain, "valid_chain", side_effect=AssertionError("full replay")):
        block = mine_block(blockchain)

    assert blockchain.chain_state.tip_hash == blockchain.hash(block)
    assert first["transaction_id"] in blockchain.chain_state.confirmed_transactions


def test_sqlite_appends_one_block_and_persists_indexed_state_and_undo():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    blockchain.node_address = miner_address

    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "denarius-testnet-v3.db"
        blockchain.STATE_PATH = database_path
        blockchain.save_everything()

        with patch.object(
            denarius_storage.DenariusStorage,
            "save_state",
            side_effect=AssertionError("full snapshot rewrite"),
        ):
            block = blockchain.mine_pending_transactions(relay=False, persist=True)

        assert block is not False
        connection = sqlite3.connect(database_path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 2
            assert connection.execute("SELECT COUNT(*) FROM block_undo").fetchone()[0] == 1
            assert connection.execute(
                "SELECT value FROM chain_state WHERE key = 'tip_hash'"
            ).fetchone()[0] == json.dumps(blockchain.hash(block))
            reward = connection.execute(
                "SELECT amount_atomic FROM immature_rewards WHERE height = 1"
            ).fetchone()
            assert reward == (str(blockchain.block_reward(1)),)
        finally:
            connection.close()

        restored = Blockchain().load_everything(database_path)
        assert restored.chain_state.as_dict() == blockchain.chain_state.as_dict()
        assert restored.undo_records[1]["block_hash"] == blockchain.hash(block)


def test_routine_restart_uses_index_and_validates_only_the_tip_transition():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    mine_block(blockchain)
    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "indexed-restart.db"
        blockchain.STATE_PATH = database_path
        blockchain.save_everything()

        loader = Blockchain()
        with patch.object(
            loader,
            "replay_chain",
            side_effect=AssertionError("routine restart replayed the complete chain"),
        ):
            restored = loader.load_everything(database_path)

    assert restored.chain_state.as_dict() == blockchain.chain_state.as_dict()


def test_routine_restart_rejects_a_tampered_balance_index():
    blockchain, miner_address, _ = funded_blockchain()
    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "tampered-index.db"
        blockchain.STATE_PATH = database_path
        blockchain.save_everything()
        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                "UPDATE accounts SET balance_atomic = ? WHERE address = ?",
                ("999999999999", miner_address),
            )
            connection.commit()
        finally:
            connection.close()

        try:
            Blockchain().load_everything(database_path)
        except ValueError as exc:
            assert "checksum" in str(exc)
        else:
            raise AssertionError("tampered indexed balance was loaded")


def test_block_undo_restores_the_previous_indexed_state():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, _ = signed_transaction(
        sender_address,
        private_key,
        recipient_address,
        "1",
    )
    previous = blockchain.chain_state.clone()

    assert submit_signed(blockchain, transaction)
    block = mine_block(blockchain)
    restored = blockchain.restore_block_undo(
        blockchain.chain_state,
        blockchain.undo_records[block["block_number"]],
    )

    assert restored.as_dict() == previous.as_dict()


def test_phase_six_uses_an_explicit_testnet_identity_and_new_genesis():
    assert denarius_protocol.PROTOCOL_VERSION == 3
    assert denarius_protocol.NETWORK_ID == "denarius-testnet-v3"
    assert denarius_protocol.NETWORK_KIND == "testnet"
    assert "mainnet" not in denarius_protocol.NETWORK_ID
    assert denarius_protocol.GENESIS_BLOCK["timestamp"] == 1784505600
    assert denarius_protocol.GENESIS_BLOCK["previous_hash"] == denarius_protocol.sha256_hex(
        denarius_protocol.GENESIS_MESSAGE.encode("utf8")
    )
    assert denarius_protocol.GENESIS_HASH == denarius_protocol.block_hash(
        denarius_protocol.GENESIS_BLOCK
    )

    identity = denarius_blockchain.protocol_identity()
    assert identity["network"] == denarius_protocol.NETWORK_ID
    assert identity["network_kind"] == "testnet"
    assert identity["consensus"] == "sha256-proof-of-work"
    assert identity["genesis_hash"] == denarius_protocol.GENESIS_HASH


def test_consensus_upgrades_activate_only_at_deterministic_heights():
    active = denarius_protocol.active_consensus_upgrade(0)

    assert active == {
        "name": "testnet-v3",
        "activation_height": 0,
        "protocol_version": 3,
    }
    assert denarius_protocol.active_consensus_upgrade(1) == active

    for invalid_height in (-1, True, "1"):
        try:
            denarius_protocol.active_consensus_upgrade(invalid_height)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid consensus activation height was accepted")


def test_state_database_is_bound_to_network_and_genesis():
    blockchain = Blockchain()

    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "denarius-testnet-v3.db"
        blockchain.STATE_PATH = database_path
        blockchain.save_everything()

        connection = sqlite3.connect(database_path)
        with connection:
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = ?",
                (json.dumps("another-network"), "network_id"),
            )
        connection.close()

        try:
            denarius_storage.DenariusStorage(database_path).load_state()
        except ValueError as exc:
            assert "another Denarius network" in str(exc)
        else:
            raise AssertionError("database from another network was accepted")


def test_account_nonces_enforce_transaction_order():
    blockchain, sender_address, private_key = funded_blockchain()
    first_recipient, _ = wallet(blockchain)
    second_recipient, _ = wallet(blockchain)
    first, _ = signed_transaction(sender_address, private_key, first_recipient, "1", nonce=0)
    second, _ = signed_transaction(sender_address, private_key, second_recipient, "1", nonce=1)

    assert submit_signed(blockchain, second) is False
    assert submit_signed(blockchain, first)
    assert blockchain.get_next_nonce(sender_address) == 1
    assert submit_signed(blockchain, second)
    assert blockchain.get_next_nonce(sender_address) == 2

    mine_block(blockchain)
    assert blockchain.get_confirmed_nonce(sender_address) == 2
    assert blockchain.get_next_nonce(sender_address) == 2


def test_transaction_id_commits_to_the_signed_payload():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, _ = signed_transaction(sender_address, private_key, recipient_address, "1")
    altered = copy.deepcopy(transaction)
    altered["amount_atomic"] = str(2 * blockchain.ATOMIC_UNITS)

    assert blockchain.has_valid_transaction_id(transaction) is True
    assert blockchain.has_valid_transaction_id(altered) is False
    assert submit_signed(blockchain, altered) is False


def test_mined_block_uses_canonical_phase_one_fields():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    block = mine_block(blockchain)

    assert set(block) == set(denarius_protocol.BLOCK_FIELDS)
    assert block["version"] == denarius_protocol.PROTOCOL_VERSION
    assert block["network"] == denarius_protocol.NETWORK_ID
    assert block["transactions"][0]["sender_address"] == denarius_protocol.COINBASE_SENDER
    assert block["merkle_root"] == denarius_protocol.calculate_merkle_root(block["transactions"])
    assert block["target"] == denarius_protocol.target_to_hex(denarius_protocol.INITIAL_TARGET)


def test_retarget_is_deterministic_and_time_bounded():
    blockchain = Blockchain()
    chain = []
    block_spacing = denarius_protocol.TARGET_BLOCK_SECONDS // 2
    for height in range(denarius_protocol.RETARGET_INTERVAL):
        chain.append({
            "timestamp": denarius_protocol.GENESIS_BLOCK["timestamp"] + height * block_spacing,
            "target": denarius_protocol.target_to_hex(denarius_protocol.INITIAL_TARGET),
        })

    elapsed = chain[-1]["timestamp"] - chain[0]["timestamp"]
    expected = denarius_protocol.INITIAL_TARGET * elapsed // denarius_protocol.RETARGET_TIMESPAN

    assert blockchain.expected_target(chain, denarius_protocol.RETARGET_INTERVAL) == expected
    assert blockchain.expected_target(chain, denarius_protocol.RETARGET_INTERVAL - 1) == denarius_protocol.INITIAL_TARGET


def test_python_crypto_boundary_contains_no_wallet_private_key_operations():
    crypto_source = (ROOT / "denarius_crypto.py").read_text()

    assert "generate_encrypted_wallet" not in crypto_source
    assert "decrypt_wallet" not in crypto_source
    assert "Ed25519PrivateKey" not in crypto_source
    assert "private_key" not in crypto_source


def test_phase_one_json_state_can_migrate_to_sqlite():
    blockchain, _, _ = funded_blockchain()
    state = {
        "chain": blockchain.chain,
        "transactions": blockchain.transactions,
        "nodes": sorted(blockchain.nodes),
        "node_address": blockchain.node_address,
        "miner_name": blockchain.miner_name,
        "mining_target": denarius_protocol.target_to_hex(blockchain.MINING_TARGET),
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "phase-one.json"
        database_path = Path(tmpdir) / "denarius.db"
        json_path.write_text(json.dumps(state), encoding="utf8")
        migrate_json_state(json_path, database_path)
        restored = Blockchain().load_everything(database_path)

    assert restored.chain == blockchain.chain
    assert restored.node_address == blockchain.node_address


def test_json_migration_rejects_incomplete_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "incomplete.json"
        database_path = Path(tmpdir) / "denarius.db"
        json_path.write_text(json.dumps({"chain": []}), encoding="utf8")

        try:
            migrate_json_state(json_path, database_path)
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete JSON state was migrated")


def test_wallet_ui_never_requests_or_displays_raw_private_keys():
    create_template = (ROOT / "blockchain_client" / "templates" / "wallets.html").read_text()
    send_template = (ROOT / "blockchain_client" / "templates" / "send.html").read_text()
    base_template = (ROOT / "blockchain_client" / "templates" / "base.html").read_text()
    store_source = (ROOT / "blockchain_client" / "static" / "js" / "wallet_store.js").read_text()
    crypto_source = (ROOT / "blockchain_client" / "static" / "js" / "wallet_crypto.js").read_text()
    console_source = CLIENT_MODULE_PATH.read_text()

    assert "/api/wallets/new" not in create_template
    assert "/api/wallets/inspect" not in create_template
    assert "/api/transactions/sign" not in send_template
    assert "DenariusWalletCrypto.create" in create_template
    assert "DenariusWalletCrypto.signTransaction" in send_template
    assert 'id="sender_wallet"' in send_template
    assert 'name="wallet_file"' not in send_template
    assert "localStorage" in store_source
    assert "private_key" not in store_source
    assert "subtle.generateKey" in crypto_source
    assert "subtle.sign" in crypto_source
    assert "privateBytes.fill(0)" in crypto_source
    assert "decrypt_wallet" not in console_source
    assert "generate_encrypted_wallet" not in console_source
    assert "denarius-wallet-scope" in base_template
    assert "encryptedWallets.v2." in store_source

    operations_source = (ROOT / "denarius_operations.py").read_text()
    assert "script-src 'self' 'unsafe-inline'" not in operations_source
    for template_name in ("activity.html", "network.html", "overview.html", "send.html", "wallets.html"):
        template = (ROOT / "blockchain_client" / "templates" / template_name).read_text()
        assert '<script nonce="{{ csp_nonce }}">' in template


def test_console_exposes_protocol_but_no_wallet_custody_endpoints():
    console_source = CLIENT_MODULE_PATH.read_text()

    assert "@app.route('/api/protocol')" in console_source
    assert "@app.route('/api/wallets/new'" not in console_source
    assert "@app.route('/api/wallets/inspect'" not in console_source
    assert "@app.route('/api/transactions/sign'" not in console_source


def test_node_and_console_are_separate_process_boundaries():
    node_source = MODULE_PATH.read_text()
    console_source = CLIENT_MODULE_PATH.read_text()
    launcher_source = (ROOT / "run_denarius.py").read_text()

    assert "render_template" not in node_source
    assert "X-Denarius-Admin-Token" in node_source
    assert "X-Denarius-Admin-Token" in console_source
    assert "render_template" in console_source
    assert "node_dashboard/dashboard.py" not in launcher_source
    assert "blockchain.blockchain" in launcher_source
    assert "blockchain_client.blockchain_client" in launcher_source
    assert "--accounts-database" in launcher_source
    assert "--console-host" in launcher_source


def test_phase_three_uses_one_coherent_console_navigation():
    base_template = (ROOT / "blockchain_client" / "templates" / "base.html").read_text()
    launcher_source = (ROOT / "run_denarius.py").read_text()

    for destination in ("Overview", "Wallets", "Send", "Activity", "Network"):
        assert destination in base_template
    assert "{% if is_admin %}" in base_template
    assert "--console-port" in launcher_source
    assert "--dashboard-port" not in launcher_source
    overview_template = (ROOT / "blockchain_client" / "templates" / "overview.html").read_text()
    assert 'id="mine_block"' in overview_template
    assert 'id="toggle_automine"' in overview_template
    assert overview_template.index('id="mine_block"') < overview_template.index('id="toggle_automine"')


def test_peer_transport_supports_verified_https_urls():
    network = PeerNetwork(scheme="https")
    try:
        assert network.peer_url("203.0.113.4:5443", "/protocol") == (
            "https://203.0.113.4:5443/protocol"
        )
    finally:
        network.close()

    try:
        PeerNetwork(scheme="ftp")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported peer transport was accepted")


def test_pending_transaction_with_nonminimum_fee_survives_restart():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, _ = signed_transaction(
        sender_address,
        private_key,
        recipient_address,
        "1",
        fee="0.0002",
    )
    assert submit_signed(blockchain, transaction)

    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "pending-fee.db"
        blockchain.STATE_PATH = database_path
        blockchain.save_everything()
        restored = Blockchain().load_everything(database_path)

    assert restored.transactions == [transaction]


def test_malformed_transaction_fuzz_cases_never_crash_validation():
    blockchain = Blockchain()
    randomizer = random.Random(6006)
    values = [
        None,
        True,
        False,
        -1,
        0,
        1,
        1.5,
        "",
        "NaN",
        "not-an-address",
        [],
        {},
    ]
    for _ in range(500):
        fields = [randomizer.choice(values) for _ in range(7)]
        result = blockchain.submit_transaction(
            fields[0],
            fields[1],
            fields[2],
            fields[3],
            fields[4],
            fields[5],
            fee=fields[6],
            relay=False,
        )
        assert result is False


def test_rate_limiter_and_metrics_are_bounded_and_machine_readable():
    limiter = SlidingWindowRateLimiter(max_keys=2)
    assert limiter.check("first", 2, 60, now=100) == (True, 0)
    assert limiter.check("first", 2, 60, now=101) == (True, 0)
    allowed, retry_after = limiter.check("first", 2, 60, now=102)
    assert allowed is False
    assert retry_after > 0
    assert limiter.check("first", 2, 60, now=161) == (True, 0)

    limiter.check("second", 1, 60, now=200)
    limiter.check("third", 1, 60, now=200)
    assert len(limiter._events) <= 2

    metrics = OperationalMetrics("test-service")
    metrics.observe_request("protocol", "GET", 200, 0.125)
    output = metrics.render({"chain_height": 10})
    assert 'service="test-service"' in output
    assert 'endpoint="protocol"' in output
    assert "denarius_chain_height" in output


def test_public_chain_route_returns_a_bounded_recent_window():
    blockchain = Blockchain()
    blockchain.chain = [{"height": height} for height in range(300)]
    with patch.object(denarius_blockchain, "blockchain", blockchain), patch.object(
        denarius_blockchain.request,
        "args",
        {"limit": "10"},
    ):
        payload, status = denarius_blockchain.full_chain()

    assert status == 200
    assert payload["length"] == 300
    assert payload["start"] == 290
    assert payload["chain"] == [{"height": height} for height in range(290, 300)]


def test_trusted_proxy_count_is_explicitly_bounded():
    fake_app = types.SimpleNamespace(wsgi_app=object())
    with patch.dict(os.environ, {"DENARIUS_TRUSTED_PROXY_COUNT": "0"}):
        assert denarius_operations.configure_trusted_proxy(fake_app) == 0
    with patch.dict(os.environ, {"DENARIUS_TRUSTED_PROXY_COUNT": "9"}):
        try:
            denarius_operations.configure_trusted_proxy(fake_app)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe proxy trust depth was accepted")


def test_backup_verification_and_restore_round_trip():
    blockchain = Blockchain()
    accounts = None
    with tempfile.TemporaryDirectory() as tmpdir:
        directory = Path(tmpdir)
        chain_database = directory / "chain.db"
        accounts_database = directory / "accounts.db"
        blockchain.STATE_PATH = chain_database
        blockchain.save_everything()
        accounts = DenariusAccountStore(accounts_database)
        accounts.create_account("operator", "salt:hash")

        backup_directory = directory / "backup"
        create_backup(chain_database, accounts_database, backup_directory)
        assert verify_backup(backup_directory)["network"] == denarius_protocol.NETWORK_ID

        restored_chain = directory / "restored-chain.db"
        restored_accounts = directory / "restored-accounts.db"
        restore_backup(backup_directory, restored_chain, restored_accounts)

        assert Blockchain().load_everything(restored_chain).chain == blockchain.chain
        assert DenariusAccountStore(restored_accounts).has_admin() is True


def test_backup_verification_detects_file_tampering():
    blockchain = Blockchain()
    with tempfile.TemporaryDirectory() as tmpdir:
        directory = Path(tmpdir)
        chain_database = directory / "chain.db"
        accounts_database = directory / "accounts.db"
        blockchain.STATE_PATH = chain_database
        blockchain.save_everything()
        DenariusAccountStore(accounts_database).create_account("operator", "salt:hash")
        backup_directory = directory / "backup"
        create_backup(chain_database, accounts_database, backup_directory)
        with (backup_directory / "denarius-chain.sqlite3").open("ab") as backup_file:
            backup_file.write(b"tampered")

        try:
            verify_backup(backup_directory)
        except ValueError as exc:
            assert "hash mismatch" in str(exc)
        else:
            raise AssertionError("tampered backup passed verification")


def test_sqlite_block_append_rolls_back_after_simulated_crash():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    with tempfile.TemporaryDirectory() as tmpdir:
        database_path = Path(tmpdir) / "crash.db"
        blockchain.STATE_PATH = database_path
        blockchain.save_everything()

        with patch.object(
            denarius_storage.DenariusStorage,
            "_replace_chain_state_metadata",
            side_effect=RuntimeError("simulated process failure"),
        ):
            try:
                blockchain.mine_pending_transactions(relay=False, persist=True)
            except RuntimeError:
                pass
            else:
                raise AssertionError("simulated storage failure was not raised")

        connection = sqlite3.connect(database_path)
        try:
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 1
        finally:
            connection.close()
