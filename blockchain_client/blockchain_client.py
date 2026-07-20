import hashlib
import hmac
import os
import re
import secrets
import sys
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from denarius_accounts import DenariusAccountStore
from denarius_operations import (
    configure_json_logging,
    configure_trusted_proxy,
    install_runtime_controls,
)
from denarius_paths import state_path

PASSWORD_ITERATIONS = 240000
DUMMY_PASSWORD_HASH = ('00' * 16) + ':' + ('00' * 32)
NODE_TIMEOUT = 5
ADMIN_TOKEN_ENV = 'DENARIUS_ADMIN_TOKEN'
SETUP_TOKEN_ENV = 'DENARIUS_SETUP_TOKEN'
DEFAULT_ACCOUNT_DATABASE = state_path('console-accounts.db')
USERNAME_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$')


app = Flask(__name__)
configure_trusted_proxy(app)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get(
    'DENARIUS_COOKIE_SECURE',
    '',
).lower() in ('1', 'true', 'yes')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['SESSION_COOKIE_NAME'] = 'denarius_session'
app.secret_key = os.environ.get('DENARIUS_SECRET_KEY') or secrets.token_hex(32)
metrics = install_runtime_controls(
    app,
    'denarius-console',
    policies={
        'healthz': None,
        'readyz': None,
        'login': (10, 300),
        'register': (10, 300),
        'api_submit_transaction': (30, 60),
        'api_mine': (10, 60),
        'api_automine': (30, 60),
        'api_register_miner': (20, 60),
        'api_register_nodes': (20, 60),
        'api_resolve': (10, 60),
    },
    secure_transport=app.config['SESSION_COOKIE_SECURE'],
)
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
    session.permanent = True
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
        if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            return func(*args, **kwargs)
        submitted_token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        session_token = session.get('csrf_token')
        if not session_token or not submitted_token or not hmac.compare_digest(session_token, submitted_token):
            return jsonify({'message': 'Invalid CSRF token'}), 403
        return func(*args, **kwargs)
    return wrapper


def metrics_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        expected_token = os.environ.get('DENARIUS_METRICS_TOKEN') or os.environ.get(ADMIN_TOKEN_ENV)
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
    try:
        account_store.has_admin()
        response = requests.get(node_base_url() + '/healthz', timeout=NODE_TIMEOUT)
        ready = response.status_code == 200
    except (requests.RequestException, ValueError):
        ready = False
    return jsonify({'status': 'ready' if ready else 'not-ready'}), 200 if ready else 503


@app.route('/metrics', methods=['GET'])
@metrics_required
def prometheus_metrics():
    return Response(
        metrics.render({'administrator_configured': int(account_store.has_admin())}),
        content_type='text/plain; version=0.0.4',
    )


def relay_response(response):
    try:
        payload = response.json()
    except ValueError:
        payload = {'message': response.text or 'Node returned an unreadable response'}
    return jsonify(payload), response.status_code


def node_get(path, admin=False):
    headers = {}
    if admin:
        admin_token = os.environ.get(ADMIN_TOKEN_ENV)
        if not admin_token or len(admin_token) < 32:
            return jsonify({'message': 'DENARIUS_ADMIN_TOKEN is not configured'}), 503
        headers['X-Denarius-Admin-Token'] = admin_token
    try:
        response = requests.get(
            node_base_url() + path,
            headers=headers,
            timeout=NODE_TIMEOUT,
        )
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
        csp_nonce=getattr(g, 'denarius_csp_nonce', ''),
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


@app.route('/api/protocol')
@login_required
def api_protocol():
    return node_get('/protocol')


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


@app.route('/api/automine', methods=['GET', 'POST'])
@admin_required
@csrf_required
def api_automine():
    if request.method == 'GET':
        return node_get('/mining/auto', admin=True)
    action = request.form.get('action')
    if action not in ('start', 'stop'):
        return jsonify({'message': 'Invalid automining action'}), 400
    return node_post('/mining/auto/' + action, admin=True)


@app.route('/api/resolve', methods=['POST'])
@admin_required
@csrf_required
def api_resolve():
    return node_post('/nodes/resolve', admin=True)


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


def main(argv=None):
    global account_store

    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-p', '--port', default=8080, type=int, help='console port to listen on')
    parser.add_argument('--host', default='127.0.0.1', help='interface to listen on')
    parser.add_argument(
        '--accounts-database',
        default=str(DEFAULT_ACCOUNT_DATABASE),
        help='SQLite database for console accounts',
    )
    parser.add_argument(
        '--development-server',
        action='store_true',
        help='use Flask development serving for local debugging only',
    )
    parser.add_argument('--threads', type=int, default=8, help='Waitress worker threads')
    args = parser.parse_args(argv)
    port = args.port
    account_store = DenariusAccountStore(args.accounts_database)

    node_base_url()
    if args.host not in ('127.0.0.1', '::1', 'localhost') and not os.environ.get('DENARIUS_SECRET_KEY'):
        parser.error('DENARIUS_SECRET_KEY is required when the console is not loopback-only')
    configure_json_logging('denarius-console', os.environ.get('DENARIUS_LOG_LEVEL', 'INFO'))
    if args.development_server:
        app.run(host=args.host, port=port)
    else:
        from waitress import serve
        serve(app, host=args.host, port=port, threads=max(4, args.threads))


if __name__ == '__main__':
    main()


