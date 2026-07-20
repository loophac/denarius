import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from denarius_protocol import (
    ATOMIC_UNITS,
    canonical_json_bytes,
    signed_transaction_id,
    transaction_signing_payload,
)
from denarius_crypto import (
    address_from_public_key,
    decrypt_wallet,
    generate_encrypted_wallet,
    wallet_public_metadata,
)
from denarius_accounts import DenariusAccountStore

MAX_WALLET_DOCUMENT_BYTES = 64 * 1024
PASSWORD_ITERATIONS = 240000
DUMMY_PASSWORD_HASH = ('00' * 16) + ':' + ('00' * 32)
NODE_TIMEOUT = 5
ADMIN_TOKEN_ENV = 'DENARIUS_ADMIN_TOKEN'
SETUP_TOKEN_ENV = 'DENARIUS_SETUP_TOKEN'
DEFAULT_ACCOUNT_DATABASE = PROJECT_ROOT / 'states' / 'console-accounts.db'
USERNAME_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$')


class Transaction:
    ATOMIC_UNITS = ATOMIC_UNITS

    def __init__(self, sender_address, sender_private_key, recipient_address, value, nonce):
        self.sender_address = sender_address
        self.sender_private_key = sender_private_key
        self.recipient_address = recipient_address
        self.value = self.parse_amount(value)
        try:
            self.nonce = int(str(nonce))
        except (TypeError, ValueError) as exc:
            raise ValueError('Invalid nonce') from exc
        if self.nonce < 0:
            raise ValueError('Invalid nonce')

        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            binascii.unhexlify(self.sender_private_key)
        )
        public_key_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if address_from_public_key(public_key_bytes) != self.sender_address:
            raise ValueError('Private key does not match sender address')

    def parse_amount(self, value):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError('Invalid amount')

        if not amount.is_finite() or amount <= 0:
            raise ValueError('Invalid amount')

        atomic_amount = amount * self.ATOMIC_UNITS
        if atomic_amount != atomic_amount.to_integral_value():
            raise ValueError('Invalid amount')

        return str(int(atomic_amount))

    def to_dict(self):
        return transaction_signing_payload(
            self.sender_address,
            self.recipient_address,
            self.value,
            self.nonce,
        )

    def canonical_transaction_bytes(self):
        return canonical_json_bytes(self.to_dict())

    def sign_transaction(self):
        """
        Sign transaction with private key
        """
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(binascii.unhexlify(self.sender_private_key))
        return binascii.hexlify(private_key.sign(self.canonical_transaction_bytes())).decode('ascii')

    def signed_data(self):
        signature = self.sign_transaction()
        return signature, signed_transaction_id(self.to_dict(), signature)


app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.secret_key = os.environ.get('DENARIUS_SECRET_KEY') or secrets.token_hex(32)
account_store = DenariusAccountStore(
    os.environ.get('DENARIUS_ACCOUNT_DATABASE', DEFAULT_ACCOUNT_DATABASE)
)


def node_base_url():
    configured = os.environ.get('DENARIUS_NODE_URL', 'http://127.0.0.1:5000').rstrip('/')
    parsed = urlparse(configured)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('DENARIUS_NODE_URL must be an HTTP or HTTPS node URL')
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('DENARIUS_NODE_URL must not contain credentials, paths, or query data')
    return configured


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


def current_account():
    return account_store.find_by_id(session.get('account_id'))


def begin_account_session(account):
    session.clear()
    session['account_id'] = account['id']
    ensure_csrf_token()


def is_loopback_request():
    return getattr(request, 'remote_addr', None) in ('127.0.0.1', '::1')


def account_login_redirect():
    session.clear()
    endpoint = 'login' if account_store.has_admin() else 'register'
    return redirect(url_for(endpoint))


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if current_account() is None:
            return account_login_redirect()
        return func(*args, **kwargs)
    return wrapper


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        account = current_account()
        if account is None:
            return account_login_redirect()
        if account['role'] != 'admin':
            if getattr(request, 'path', '').startswith('/api/'):
                return jsonify({'message': 'Administrator access is required'}), 403
            return render_template(
                'forbidden.html',
                csrf_token=ensure_csrf_token(),
                username=account['username'],
            ), 403
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


def relay_response(response):
    try:
        payload = response.json()
    except ValueError:
        payload = {'message': response.text or 'Node returned an unreadable response'}
    return jsonify(payload), response.status_code


def node_get(path):
    try:
        response = requests.get(node_base_url() + path, timeout=NODE_TIMEOUT)
    except (requests.RequestException, ValueError) as exc:
        return jsonify({'message': 'Unable to reach the Denarius node', 'detail': str(exc)}), 502
    return relay_response(response)


def node_post(path, form=None, admin=False):
    headers = {}
    if admin:
        admin_token = os.environ.get(ADMIN_TOKEN_ENV)
        if not admin_token or len(admin_token) < 32:
            return jsonify({'message': 'DENARIUS_ADMIN_TOKEN is not configured'}), 503
        headers['X-Denarius-Admin-Token'] = admin_token
    try:
        response = requests.post(
            node_base_url() + path,
            data=form or {},
            headers=headers,
            timeout=NODE_TIMEOUT,
        )
    except (requests.RequestException, ValueError) as exc:
        return jsonify({'message': 'Unable to reach the Denarius node', 'detail': str(exc)}), 502
    return relay_response(response)


def render_console(template_name, active_page):
    account = current_account()
    return render_template(
        template_name,
        active_page=active_page,
        csrf_token=ensure_csrf_token(),
        username=account['username'],
        is_admin=account['role'] == 'admin',
        wallet_scope=account['wallet_scope'],
    )

@app.route('/')
@login_required
def index():
    if current_account()['role'] != 'admin':
        return redirect(url_for('wallets'))
    return render_console('overview.html', 'overview')


@app.route('/wallets')
@login_required
def wallets():
    return render_console('wallets.html', 'wallets')


@app.route('/send')
@login_required
def send():
    return render_console('send.html', 'send')


@app.route('/activity')
@login_required
def activity():
    return render_console('activity.html', 'activity')


@app.route('/network')
@admin_required
def network():
    return render_console('network.html', 'network')


@app.route('/make/transaction')
@login_required
def legacy_make_transaction():
    return redirect(url_for('send'))


@app.route('/view/transactions')
@login_required
def legacy_view_transactions():
    return redirect(url_for('activity'))


@app.route('/configure')
@admin_required
def legacy_configure():
    return redirect(url_for('network'))


@app.route('/api/chain')
@login_required
def api_chain():
    return node_get('/chain')


@app.route('/api/accounts/<address>')
@login_required
def api_account(address):
    return node_get('/accounts/' + quote(address, safe=''))


@app.route('/api/transactions', methods=['GET'])
@login_required
def api_pending_transactions():
    return node_get('/transactions/get')


@app.route('/api/transactions', methods=['POST'])
@login_required
@csrf_required
def api_submit_transaction():
    return node_post('/transactions/new', request.form)


@app.route('/api/miner', methods=['GET'])
@admin_required
def api_miner():
    return node_get('/miner/get')


@app.route('/api/miner', methods=['POST'])
@admin_required
@csrf_required
def api_register_miner():
    return node_post('/miner/register', request.form, admin=True)


@app.route('/api/nodes', methods=['GET'])
@admin_required
def api_nodes():
    return node_get('/nodes/get')


@app.route('/api/nodes', methods=['POST'])
@admin_required
@csrf_required
def api_register_nodes():
    return node_post('/nodes/register', request.form, admin=True)


@app.route('/api/mine', methods=['POST'])
@admin_required
@csrf_required
def api_mine():
    return node_post('/mine', admin=True)


@app.route('/api/resolve', methods=['POST'])
@admin_required
@csrf_required
def api_resolve():
    return node_post('/nodes/resolve', admin=True)


def submitted_wallet_document():
    wallet_json = request.form.get('wallet_document')
    if wallet_json is not None:
        wallet_bytes = wallet_json.encode('utf8')
    else:
        wallet_file = request.files.get('wallet_file')
        if wallet_file is None:
            raise ValueError('Encrypted wallet data is required')
        wallet_bytes = wallet_file.read(MAX_WALLET_DOCUMENT_BYTES + 1)
    if len(wallet_bytes) > MAX_WALLET_DOCUMENT_BYTES:
        raise ValueError('Encrypted wallet data is too large')
    try:
        document = json.loads(wallet_bytes.decode('utf8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('Encrypted wallet data is invalid') from exc
    if not isinstance(document, dict):
        raise ValueError('Encrypted wallet data is invalid')
    return document


@app.route('/api/wallets/new', methods=['POST'])
@app.route('/wallet/new', methods=['POST'])
@login_required
@csrf_required
def new_wallet():
    try:
        wallet_document = generate_encrypted_wallet(request.form.get('password', ''))
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    response = {
        'address': wallet_document['address'],
        'public_key': wallet_document['public_key'],
        'wallet': wallet_document,
        'filename': 'denarius-' + wallet_document['address'][:14] + '.denwallet',
    }
    return jsonify(response), 200


@app.route('/api/wallets/inspect', methods=['POST'])
@app.route('/wallet/inspect', methods=['POST'])
@login_required
@csrf_required
def inspect_wallet():
    try:
        metadata = wallet_public_metadata(submitted_wallet_document())
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    return jsonify(metadata), 200


@app.route('/api/transactions/sign', methods=['POST'])
@app.route('/generate/transaction', methods=['POST'])
@login_required
@csrf_required
def generate_transaction():
    try:
        wallet_data = decrypt_wallet(
            submitted_wallet_document(),
            request.form.get('password', ''),
        )
        sender_address = wallet_data['address']
        sender_private_key = wallet_data['private_key']
        recipient_address = request.form['recipient_address']
        value = request.form['amount']
        nonce = request.form['nonce']

        transaction = Transaction(
            sender_address,
            sender_private_key,
            recipient_address,
            value,
            nonce,
        )
        signature, transaction_id = transaction.signed_data()
    except KeyError:
        return jsonify({'message': 'Transaction details are incomplete'}), 400
    except (binascii.Error, ValueError) as exc:
        return jsonify({'message': str(exc) or 'Invalid transaction'}), 400

    response = {
        'transaction': transaction.to_dict(),
        'signature': signature,
        'transaction_id': transaction_id,
    }

    return jsonify(response), 200


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_account() is not None:
        return redirect(url_for('index'))
    setup_mode = not account_store.has_admin()
    setup_token = os.environ.get(SETUP_TOKEN_ENV, '') if setup_mode else ''
    setup_available = not setup_mode or bool(setup_token) or is_loopback_request()
    if not setup_available:
        return render_template(
            'register.html',
            csrf_token=ensure_csrf_token(),
            error=None,
            setup_mode=True,
            setup_token_required=False,
            setup_unavailable=True,
        ), 403
    error = None
    if request.method == 'POST':
        submitted_token = request.form.get('csrf_token')
        if not submitted_token or not hmac.compare_digest(ensure_csrf_token(), submitted_token):
            error = 'Your session expired. Please try again.'
        elif setup_mode and setup_token and not hmac.compare_digest(
            setup_token,
            request.form.get('setup_token', ''),
        ):
            error = 'The administrator setup code is incorrect.'
        else:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            password_confirm = request.form.get('password_confirm', '')
            if not USERNAME_PATTERN.fullmatch(username) or not 10 <= len(password) <= 256:
                error = 'Use 3-64 letters, numbers, dots, dashes, or underscores and a password of 10-256 characters.'
            elif password != password_confirm:
                error = 'The passwords do not match.'
            else:
                try:
                    account = account_store.create_account(username, hash_password(password))
                except ValueError as exc:
                    error = str(exc)
                else:
                    begin_account_session(account)
                    return redirect(url_for('index'))
    return render_template(
        'register.html',
        csrf_token=ensure_csrf_token(),
        error=error,
        setup_mode=setup_mode,
        setup_token_required=bool(setup_token),
        setup_unavailable=False,
    ), 400 if error else 200


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not account_store.has_admin():
        return redirect(url_for('register'))
    if current_account() is not None:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        submitted_token = request.form.get('csrf_token')
        if not submitted_token or not hmac.compare_digest(ensure_csrf_token(), submitted_token):
            error = 'Your session expired. Please try again.'
        else:
            username = request.form.get('username', '')
            password = request.form.get('password', '')
            account = account_store.find_by_username(username) if USERNAME_PATTERN.fullmatch(username) else None
            encoded_password = account['password_hash'] if account else DUMMY_PASSWORD_HASH
            password_matches = len(password) <= 256 and verify_password(password, encoded_password)
            if account and password_matches:
                begin_account_session(account)
                return redirect(url_for('index'))
            error = 'The username or password is incorrect.'
    return render_template('login.html', csrf_token=ensure_csrf_token(), error=error), 403 if error else 200


@app.route('/logout', methods=['POST'])
@login_required
@csrf_required
def logout():
    session.clear()
    return redirect(url_for('login'))


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-p', '--port', default=8080, type=int, help='console port to listen on')
    parser.add_argument('--host', default='127.0.0.1', help='interface to listen on')
    parser.add_argument(
        '--accounts-database',
        default=str(DEFAULT_ACCOUNT_DATABASE),
        help='SQLite database for console accounts',
    )
    args = parser.parse_args()
    port = args.port
    account_store = DenariusAccountStore(args.accounts_database)

    node_base_url()
    app.run(host=args.host, port=port)


