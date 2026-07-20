from collections import OrderedDict
import binascii
import copy
import importlib.util
import json
import os
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

        def run(self, *args, **kwargs):
            return None

    flask_stub.Flask = Flask
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
    flask_stub.session = {}
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
Transaction = denarius_client.Transaction

import denarius_protocol
import denarius_crypto
from denarius_storage import migrate_json_state
from denarius_accounts import DenariusAccountStore


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


def signed_transaction(sender_address, private_key, recipient_address, value, nonce=0):
    transaction = Transaction(sender_address, private_key, recipient_address, value, nonce)
    signature, transaction_id = transaction.signed_data()
    signed = OrderedDict(transaction.to_dict())
    signed["signature"] = signature
    signed["transaction_id"] = transaction_id
    return signed, signature


def signed_atomic_transaction(sender_address, private_key, recipient_address, atomic_value, nonce=0):
    transaction = denarius_blockchain.transaction_signing_payload(
        sender_address, recipient_address, atomic_value, nonce
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
        relay=relay,
    )


def funded_blockchain():
    blockchain = Blockchain()
    address, private_key = wallet(blockchain)
    blockchain.node_address = address
    mine_block(blockchain)
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

    reward = blockchain.block_reward(1)
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
    reward = blockchain.block_reward(1)
    transaction, signature = signed_atomic_transaction(
        sender_address,
        private_key,
        recipient_address,
        reward + 1,
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


def test_wallet_file_encrypts_private_key_and_rejects_wrong_password():
    password = "a strong wallet password"
    wallet_document = denarius_crypto.generate_encrypted_wallet(password)

    assert set(wallet_document) == set(denarius_crypto.WALLET_FIELDS)
    assert "private_key" not in wallet_document
    wallet_data = denarius_crypto.decrypt_wallet(wallet_document, password)
    assert wallet_data["address"] == wallet_document["address"]
    assert len(wallet_data["private_key"]) == 64

    try:
        denarius_crypto.decrypt_wallet(wallet_document, "the wrong password")
    except ValueError:
        pass
    else:
        raise AssertionError("encrypted wallet accepted the wrong password")


def test_wallet_ciphertext_detects_tampering():
    wallet_document = denarius_crypto.generate_encrypted_wallet("another strong password")
    tampered = copy.deepcopy(wallet_document)
    tampered["ciphertext"] = (
        "00" if tampered["ciphertext"][:2] != "00" else "11"
    ) + tampered["ciphertext"][2:]

    try:
        denarius_crypto.decrypt_wallet(tampered, "another strong password")
    except ValueError:
        pass
    else:
        raise AssertionError("tampered wallet ciphertext was decrypted")


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

    assert "private_key" not in create_template
    assert "sender_private_key" not in send_template
    assert 'id="sender_wallet"' in send_template
    assert 'name="wallet_file"' not in send_template
    assert "wallet_document" in send_template
    assert "localStorage" in store_source
    assert "private_key" not in store_source
    assert "denarius-wallet-scope" in base_template
    assert "encryptedWallets.v2." in store_source


def test_wallet_service_accepts_a_browser_stored_wallet_document():
    wallet_document = denarius_crypto.generate_encrypted_wallet("browser wallet password")
    original_form = denarius_client.request.form
    original_files = denarius_client.request.files
    try:
        denarius_client.request.form = {"wallet_document": json.dumps(wallet_document)}
        denarius_client.request.files = {}
        assert denarius_client.submitted_wallet_document() == wallet_document
    finally:
        denarius_client.request.form = original_form
        denarius_client.request.files = original_files


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
