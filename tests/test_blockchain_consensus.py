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
import types
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "blockchain" / "blockchain.py"
CLIENT_MODULE_PATH = ROOT / "blockchain_client" / "blockchain_client.py"
DASHBOARD_MODULE_PATH = ROOT / "node_dashboard" / "dashboard.py"


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
        form={},
        files={},
        headers={},
        method="GET",
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

Blockchain = denarius_blockchain.Blockchain
Transaction = denarius_client.Transaction

import denarius_protocol
import denarius_crypto
from denarius_storage import migrate_json_state


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


def test_submit_transaction_relays_to_known_peers():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    blockchain.nodes.add("127.0.0.1:5001")

    with patch.object(denarius_blockchain.requests, "post") as post:
        result = submit_signed(blockchain, transaction, relay=True)

    assert result == len(blockchain.chain)
    post.assert_called_once()
    assert post.call_args.kwargs["data"]["amount"] == transaction["amount_atomic"]
    assert post.call_args.kwargs["data"]["nonce"] == transaction["nonce"]
    assert post.call_args.kwargs["data"]["transaction_id"] == transaction["transaction_id"]
    assert post.call_args.kwargs["timeout"] == blockchain.PEER_REQUEST_TIMEOUT


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
    response = Mock(status_code=200)
    response.json.return_value = {"nodes": ["127.0.0.1:5002", "http://127.0.0.1:5003"]}

    with patch.object(denarius_blockchain.requests, "get", return_value=response) as get:
        blockchain.exchange_peer_table()

    assert "127.0.0.1:5002" in blockchain.nodes
    assert "127.0.0.1:5003" in blockchain.nodes
    get.assert_called_once()


def test_resolve_conflicts_ignores_malformed_peer_chain_response():
    blockchain = Blockchain()
    blockchain.nodes.add("127.0.0.1:5001")
    response = Mock(status_code=200)
    response.json.return_value = {"not_chain": []}

    with patch.object(denarius_blockchain.requests, "get", return_value=response):
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

    def get_peer_chain(url, timeout):
        response = Mock(status_code=200)
        response.json.return_value = {"chain": peer.chain}
        return response

    with patch.object(denarius_blockchain.requests, "get", side_effect=get_peer_chain):
        assert local.resolve_conflicts() is True

    assert local.chain == peer.chain


def test_chainwork_uses_exact_target_formula():
    blockchain = Blockchain()
    blockchain.node_address, _ = wallet(blockchain)
    mine_empty_block(blockchain)

    target = denarius_blockchain.target_from_hex(blockchain.chain[1]["target"])
    assert blockchain.chainwork(blockchain.chain) == (1 << 256) // (target + 1)


def test_transaction_tables_format_atomic_values_as_denarii():
    miner_template = (ROOT / "blockchain" / "templates" / "index.html").read_text()
    client_template = (ROOT / "blockchain_client" / "templates" / "view_transactions.html").read_text()
    send_template = (ROOT / "blockchain_client" / "templates" / "make_transaction.html").read_text()

    for template in (miner_template, client_template):
        assert "function formatDenarii" in template
        assert 'response["chain"].length' in template
        assert 'formatDenarii(response["chain"][i]["transactions"][j]["amount_atomic"])' in template

    assert "confirmation_amount_display" in send_template
    assert 'formatDenarii(response["transaction"]["amount_atomic"])' in send_template
    assert 'response["transaction_id"]' in send_template


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
    create_template = (ROOT / "blockchain_client" / "templates" / "index.html").read_text()
    send_template = (ROOT / "blockchain_client" / "templates" / "make_transaction.html").read_text()
    store_source = (ROOT / "blockchain_client" / "static" / "js" / "wallet_store.js").read_text()

    assert "private_key" not in create_template
    assert "sender_private_key" not in send_template
    assert 'id="sender_wallet"' in send_template
    assert 'name="wallet_file"' not in send_template
    assert "wallet_document" in send_template
    assert "localStorage" in store_source
    assert "private_key" not in store_source


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


def test_node_and_dashboard_are_separate_process_boundaries():
    node_source = MODULE_PATH.read_text()
    dashboard_source = DASHBOARD_MODULE_PATH.read_text()

    assert "render_template" not in node_source
    assert "X-Denarius-Admin-Token" in node_source
    assert "X-Denarius-Admin-Token" in dashboard_source
    assert "render_template" in dashboard_source
