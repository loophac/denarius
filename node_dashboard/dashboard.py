import hashlib
import hmac
import os
import secrets
import sys
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEMPLATE_ROOT = PROJECT_ROOT / 'blockchain' / 'templates'
STATIC_ROOT = PROJECT_ROOT / 'blockchain' / 'static'
PASSWORD_ITERATIONS = 240000
NODE_TIMEOUT = 5

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_ROOT),
    static_folder=str(STATIC_ROOT),
    static_url_path='/static',
)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.secret_key = os.environ.get('DENARIUS_SECRET_KEY') or secrets.token_hex(32)

registered_user = None


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


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if registered_user is None:
            return redirect(url_for('register'))
        if 'user' not in session:
            return redirect(url_for('login'))
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


def node_admin_post(path, form=None):
    admin_token = os.environ.get('DENARIUS_ADMIN_TOKEN')
    if not admin_token or len(admin_token) < 32:
        return jsonify({'message': 'DENARIUS_ADMIN_TOKEN is not configured'}), 503
    try:
        response = requests.post(
            node_base_url() + path,
            data=form or {},
            headers={'X-Denarius-Admin-Token': admin_token},
            timeout=NODE_TIMEOUT,
        )
    except (requests.RequestException, ValueError) as exc:
        return jsonify({'message': 'Unable to reach the Denarius node', 'detail': str(exc)}), 502
    return relay_response(response)


@app.route('/')
@login_required
def index():
    return render_template('index.html', csrf_token=ensure_csrf_token())


@app.route('/configure')
@login_required
def configure():
    return render_template('configure.html', csrf_token=ensure_csrf_token())


@app.route('/transactions/get')
@login_required
def get_transactions():
    return node_get('/transactions/get')


@app.route('/chain')
@login_required
def full_chain():
    return node_get('/chain')


@app.route('/miner/get')
@login_required
def get_miner_info():
    return node_get('/miner/get')


@app.route('/nodes/get')
@login_required
def get_nodes():
    return node_get('/nodes/get')


@app.route('/mine', methods=['POST'])
@login_required
@csrf_required
def mine():
    return node_admin_post('/mine')


@app.route('/miner/register', methods=['POST'])
@login_required
@csrf_required
def register_miner():
    return node_admin_post('/miner/register', request.form)


@app.route('/nodes/register', methods=['POST'])
@login_required
@csrf_required
def register_nodes():
    return node_admin_post('/nodes/register', request.form)


@app.route('/nodes/resolve', methods=['POST'])
@login_required
@csrf_required
def consensus():
    return node_admin_post('/nodes/resolve')


@app.route('/register', methods=['GET', 'POST'])
def register():
    global registered_user
    if registered_user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        submitted_token = request.form.get('csrf_token')
        if not submitted_token or not hmac.compare_digest(ensure_csrf_token(), submitted_token):
            return 'Invalid CSRF token', 403
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or len(username) > 64 or len(password) < 10:
            return 'Username is required and password must be at least 10 characters', 400
        registered_user = {'username': username, 'password_hash': hash_password(password)}
        session.clear()
        session['user'] = username
        ensure_csrf_token()
        return redirect(url_for('index'))
    return render_template('register.html', csrf_token=ensure_csrf_token())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        submitted_token = request.form.get('csrf_token')
        if not submitted_token or not hmac.compare_digest(ensure_csrf_token(), submitted_token):
            return 'Invalid CSRF token', 403
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if (
            registered_user
            and username == registered_user['username']
            and verify_password(password, registered_user['password_hash'])
        ):
            session.clear()
            session['user'] = username
            ensure_csrf_token()
            return redirect(url_for('index'))
        return 'Invalid credentials', 403
    return render_template('login.html', csrf_token=ensure_csrf_token())


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-p', '--port', default=5001, type=int, help='port to listen on')
    args = parser.parse_args()
    node_base_url()
    app.run(host='127.0.0.1', port=args.port)
