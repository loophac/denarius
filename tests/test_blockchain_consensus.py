from collections import OrderedDict
import binascii
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import types
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "blockchain" / "blockchain.py"
CLIENT_MODULE_PATH = ROOT / "blockchain_client" / "blockchain_client.py"


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
    flask_stub.request = types.SimpleNamespace(form={}, get_json=lambda silent=True: {})
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.redirect = lambda value: value
    flask_stub.session = {}
    flask_stub.url_for = lambda endpoint: endpoint

    flask_cors_stub = types.ModuleType("flask_cors")
    flask_cors_stub.CORS = lambda app: app

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

Blockchain = denarius_blockchain.Blockchain
Transaction = denarius_client.Transaction


def mine_block(blockchain):
    nonce = blockchain.proof_of_work()
    previous_hash = blockchain.hash(blockchain.chain[-1])
    blockchain.transactions.append(blockchain.create_coinbase_transaction())
    return blockchain.create_block(nonce, previous_hash)


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


def signed_transaction(sender_address, private_key, recipient_address, value):
    transaction = Transaction(sender_address, private_key, recipient_address, value)
    return transaction.to_dict(), transaction.sign_transaction()


def signed_atomic_transaction(sender_address, private_key, recipient_address, atomic_value):
    transaction = OrderedDict({
        "sender_address": sender_address,
        "recipient_address": recipient_address,
        "value": str(atomic_value),
    })
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(binascii.unhexlify(private_key))
    signature = private_key.sign(json.dumps(transaction, sort_keys=True, separators=(',', ':')).encode('utf8'))
    return transaction, binascii.hexlify(signature).decode("ascii")


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
        "",
    )

    assert result is False
    assert blockchain.transactions == []


def test_rejects_non_positive_and_non_finite_amounts():
    blockchain = Blockchain()
    sender_address, private_key = wallet(blockchain)
    recipient_address, _ = wallet(blockchain)

    for amount in ("0", "-1", "NaN", "Infinity"):
        _, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
        assert blockchain.submit_transaction(sender_address, recipient_address, amount, signature) is False


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
    carol_transaction, carol_signature = signed_transaction(sender_address, private_key, carol_address, "1")

    assert blockchain.submit_transaction(
        sender_address,
        bob_address,
        bob_transaction["value"],
        bob_signature,
    ) == len(blockchain.chain)
    assert blockchain.submit_transaction(
        sender_address,
        carol_address,
        carol_transaction["value"],
        carol_signature,
    ) is False


def test_ed25519_signature_and_address_checksum_are_required():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")

    assert blockchain.verify_transaction_signature(sender_address, signature, transaction) is True
    assert blockchain.verify_transaction_signature(sender_address[:-1] + "0", signature, transaction) is False
    assert blockchain.submit_transaction(sender_address, recipient_address[:-1] + "0", transaction["value"], signature) is False


def test_forged_signature_is_rejected():
    blockchain, sender_address, _ = funded_blockchain()
    recipient_address, recipient_private_key = wallet(blockchain)
    transaction, forged_signature = signed_transaction(sender_address, recipient_private_key, recipient_address, "1")

    assert blockchain.submit_transaction(
        sender_address,
        recipient_address,
        transaction["value"],
        forged_signature,
    ) is False


def test_valid_chain_requires_canonical_genesis_block():
    blockchain = Blockchain()
    candidate_chain = [dict(blockchain.chain[0])]
    candidate_chain[0]["timestamp"] += 1

    assert blockchain.valid_chain(candidate_chain) is False


def test_valid_chain_rejects_incorrect_coinbase_reward():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    nonce = blockchain.proof_of_work()
    previous_hash = blockchain.hash(blockchain.chain[-1])
    blockchain.transactions.append(OrderedDict({
        "sender_address": blockchain.COINBASE_SENDER,
        "recipient_address": miner_address,
        "value": str(blockchain.block_reward(1) + 1),
    }))
    blockchain.create_block(nonce, previous_hash)

    assert blockchain.valid_chain(blockchain.chain) is False


def test_valid_chain_rejects_multiple_coinbase_transactions():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    blockchain.node_address = miner_address
    blockchain.transactions.append(blockchain.create_coinbase_transaction())
    nonce = blockchain.proof_of_work()
    previous_hash = blockchain.hash(blockchain.chain[-1])
    blockchain.transactions.append(blockchain.create_coinbase_transaction())
    blockchain.create_block(nonce, previous_hash)

    assert blockchain.valid_chain(blockchain.chain) is False


def test_valid_chain_rejects_invalid_difficulty():
    blockchain = Blockchain()
    miner_address, _ = wallet(blockchain)
    blockchain.node_address = miner_address
    mine_block(blockchain)
    blockchain.chain[-1]["difficulty"] = 0

    assert blockchain.valid_chain(blockchain.chain) is False


def test_coinbase_requires_valid_denarius_address():
    blockchain = Blockchain()
    nonce = blockchain.proof_of_work()
    previous_hash = blockchain.hash(blockchain.chain[-1])
    blockchain.transactions.append(blockchain.create_coinbase_transaction())
    blockchain.create_block(nonce, previous_hash)

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

    transaction["signature"] = signature
    blockchain.transactions.append(transaction)
    nonce = blockchain.proof_of_work()
    previous_hash = blockchain.hash(blockchain.chain[-1])
    blockchain.transactions.append(blockchain.create_coinbase_transaction())
    blockchain.create_block(nonce, previous_hash)

    assert blockchain.valid_chain(blockchain.chain) is False


def test_valid_chain_rejects_duplicate_signed_transactions_across_blocks():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")

    assert blockchain.submit_transaction(sender_address, recipient_address, transaction["value"], signature, relay=False)
    mine_block(blockchain)

    blockchain.transactions.append(OrderedDict(transaction))
    blockchain.transactions[-1]["signature"] = signature
    mine_block(blockchain)

    assert blockchain.valid_chain(blockchain.chain) is False


def test_state_is_saved_and_loaded_as_json():
    blockchain, miner_address, _ = funded_blockchain()
    blockchain.miner_name = "miner"
    blockchain.nodes.add("127.0.0.1:5001")

    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "blockchain.json"
        blockchain.STATE_PATH = state_path
        blockchain.save_everything()

        restored = Blockchain().load_everything(state_path)

    assert restored.chain == blockchain.chain
    assert restored.node_address == miner_address
    assert restored.miner_name == "miner"
    assert restored.nodes == {"127.0.0.1:5001"}


def test_submit_transaction_relays_to_known_peers():
    blockchain, sender_address, private_key = funded_blockchain()
    recipient_address, _ = wallet(blockchain)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    blockchain.nodes.add("127.0.0.1:5001")

    with patch.object(denarius_blockchain.requests, "post") as post:
        result = blockchain.submit_transaction(
            sender_address,
            recipient_address,
            transaction["value"],
            signature,
        )

    assert result == len(blockchain.chain)
    post.assert_called_once()
    assert post.call_args.kwargs["data"]["amount"] == transaction["value"]
    assert post.call_args.kwargs["timeout"] == blockchain.PEER_REQUEST_TIMEOUT


def test_accept_block_from_peer_appends_and_removes_pending_transaction():
    local, sender_address, private_key = funded_blockchain()
    peer = Blockchain()
    peer.chain = [dict(block) for block in local.chain]
    peer.node_address, _ = wallet(peer)

    recipient_address, _ = wallet(peer)
    transaction, signature = signed_transaction(sender_address, private_key, recipient_address, "1")
    assert local.submit_transaction(sender_address, recipient_address, transaction["value"], signature, relay=False)

    peer.transactions.append(OrderedDict(transaction))
    peer.transactions[-1]["signature"] = signature
    block = mine_block(peer)

    with patch.object(local, "save_everything"), patch.object(local, "broadcast_block") as broadcast_block:
        assert local.accept_block(block) is True

    assert local.chain[-1] == block
    assert local.transactions == []
    broadcast_block.assert_called_once_with(block)


def test_accept_block_rejects_invalid_peer_block():
    blockchain = Blockchain()
    block = {
        "block_number": 1,
        "timestamp": 1,
        "transactions": [],
        "nonce": 0,
        "previous_hash": "bad",
        "difficulty": 1,
    }

    assert blockchain.accept_block(block) is False


def test_valid_chain_rejects_oversized_blocks():
    blockchain = Blockchain()
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


def test_resolve_conflicts_prefers_greater_chainwork_over_length():
    local = Blockchain()
    local.node_address, _ = wallet(local)
    mine_empty_block(local)
    local.chain[-1]["difficulty"] = 2

    low_work = Blockchain()
    low_work.node_address, _ = wallet(low_work)
    low_work.MINING_DIFFICULTY = 1
    mine_empty_block(low_work)
    mine_empty_block(low_work)

    high_work = Blockchain()
    high_work.node_address, _ = wallet(high_work)
    high_work.MINING_DIFFICULTY = 3
    mine_empty_block(high_work)

    local.nodes.update({"low-work", "high-work"})

    def get_peer_chain(url, timeout):
        response = Mock(status_code=200)
        response.json.return_value = {
            "chain": low_work.chain if "low-work" in url else high_work.chain,
        }
        return response

    with patch.object(denarius_blockchain.requests, "get", side_effect=get_peer_chain):
        assert local.resolve_conflicts() is True

    assert local.chain == high_work.chain


def test_transaction_tables_format_atomic_values_as_denarii():
    miner_template = (ROOT / "blockchain" / "templates" / "index.html").read_text()
    client_template = (ROOT / "blockchain_client" / "templates" / "view_transactions.html").read_text()

    for template in (miner_template, client_template):
        assert "function formatDenarii" in template
        assert 'response["chain"].length' in template
        assert 'formatDenarii(response["chain"][i]["transactions"][j]["value"])' in template
