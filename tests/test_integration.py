import importlib.metadata
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
ADMIN_TOKEN = "integration-admin-token-00000000000000000000000000000000"
SETUP_TOKEN = "integration-setup-token"
SECRET_KEY = "integration-session-secret-000000000000000000000000000000"
CSRF_PATTERNS = (
    re.compile(r'name="csrf-token" content="([^"]+)"'),
    re.compile(r'name="csrf_token" value="([^"]+)"'),
)
ALLOCATED_PORTS = set()

try:
    for distribution in ("Flask", "Flask-Cors", "cryptography", "requests"):
        importlib.metadata.version(distribution)
except importlib.metadata.PackageNotFoundError:
    RUNTIME_DEPENDENCIES_AVAILABLE = False
else:
    RUNTIME_DEPENDENCIES_AVAILABLE = True

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RUNTIME_DEPENDENCIES_AVAILABLE,
        reason="Denarius runtime dependencies are not installed",
    ),
]


def unused_port():
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port not in ALLOCATED_PORTS:
            ALLOCATED_PORTS.add(port)
            return port


def csrf_token(response):
    for pattern in CSRF_PATTERNS:
        match = pattern.search(response.text)
        if match:
            return match.group(1)
    raise AssertionError("The console response did not contain a CSRF token")


def wait_for_response(url, predicate=None, timeout=20):
    predicate = predicate or (lambda response: response.status_code == 200)
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=1)
            if predicate(response):
                return response
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {url}: {last_error}")


def service_environment(node_port):
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT), existing_pythonpath) if part
    )
    environment["PYTHONUNBUFFERED"] = "1"
    environment["DENARIUS_ADMIN_TOKEN"] = ADMIN_TOKEN
    environment["DENARIUS_SETUP_TOKEN"] = SETUP_TOKEN
    environment["DENARIUS_SECRET_KEY"] = SECRET_KEY
    environment["DENARIUS_NODE_URL"] = f"http://127.0.0.1:{node_port}"
    return environment


class RunningService:
    def __init__(self, module, arguments, environment, log_path):
        self.log_path = log_path
        self.log_file = log_path.open("w+", encoding="utf8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [sys.executable, "-m", module, *arguments],
            cwd=str(ROOT),
            env=environment,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log_file.flush()

    def output(self):
        self.log_file.flush()
        self.log_file.seek(0)
        return self.log_file.read()

    def close(self):
        self.stop()
        self.log_file.close()


@pytest.fixture
def services(tmp_path):
    running = []

    def start(name, module, arguments, environment):
        service = RunningService(
            module,
            arguments,
            environment,
            tmp_path / f"{name}.log",
        )
        running.append(service)
        return service

    yield start

    for service in reversed(running):
        service.close()


def start_node(services, name, port, database, environment):
    service = services(
        name,
        "blockchain.blockchain",
        [
            "--port", str(port),
            "--database", str(database),
            "--sync-interval", "1",
            "--advertise-address", f"127.0.0.1:{port}",
        ],
        environment,
    )
    try:
        wait_for_response(f"http://127.0.0.1:{port}/protocol")
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{name} output:\n{service.output()}") from exc
    return service


def start_console(services, name, port, accounts_database, environment):
    service = services(
        name,
        "blockchain_client.blockchain_client",
        [
            "--port", str(port),
            "--accounts-database", str(accounts_database),
        ],
        environment,
    )
    try:
        wait_for_response(
            f"http://127.0.0.1:{port}/register",
            predicate=lambda response: response.status_code in (200, 302),
        )
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{name} output:\n{service.output()}") from exc
    return service


def test_console_workflow_and_restart_persist_real_state(services, tmp_path):
    node_port = unused_port()
    console_port = unused_port()
    node_url = f"http://127.0.0.1:{node_port}"
    console_url = f"http://127.0.0.1:{console_port}"
    chain_database = tmp_path / "denarius.db"
    accounts_database = tmp_path / "console-accounts.db"
    environment = service_environment(node_port)

    node = start_node(services, "node-first", node_port, chain_database, environment)
    console = start_console(
        services,
        "console-first",
        console_port,
        accounts_database,
        environment,
    )

    administrator = requests.Session()
    registration = administrator.get(f"{console_url}/register", timeout=3)
    response = administrator.post(
        f"{console_url}/register",
        data={
            "csrf_token": csrf_token(registration),
            "setup_token": SETUP_TOKEN,
            "username": "nodeadmin",
            "password": "administrator password",
            "password_confirm": "administrator password",
        },
        allow_redirects=False,
        timeout=5,
    )
    assert response.status_code == 302

    network_page = administrator.get(f"{console_url}/network", timeout=3)
    assert network_page.status_code == 200
    admin_csrf = csrf_token(network_page)

    wallet_response = administrator.post(
        f"{console_url}/api/wallets/new",
        data={"password": "wallet password"},
        headers={"X-CSRF-Token": admin_csrf},
        timeout=10,
    )
    assert wallet_response.status_code == 200
    wallet = wallet_response.json()
    assert wallet["wallet"]["address"] == wallet["address"]
    miner_address = wallet["address"]

    miner_response = administrator.post(
        f"{console_url}/api/miner",
        data={"name": "Integration Miner", "address": miner_address},
        headers={"X-CSRF-Token": admin_csrf},
        timeout=5,
    )
    assert miner_response.status_code == 201

    mining_response = administrator.post(
        f"{console_url}/api/mine",
        headers={"X-CSRF-Token": admin_csrf},
        timeout=20,
    )
    assert mining_response.status_code == 200
    assert mining_response.json()["block_number"] == 1

    account_response = administrator.get(
        f"{console_url}/api/accounts/{miner_address}",
        timeout=5,
    )
    assert account_response.status_code == 200
    assert int(account_response.json()["balance_atomic"]) > 0
    assert requests.get(f"{node_url}/chain", timeout=3).json()["length"] == 2

    console.stop()
    node.stop()
    assert chain_database.exists()
    assert accounts_database.exists()

    node_port = unused_port()
    console_port = unused_port()
    node_url = f"http://127.0.0.1:{node_port}"
    console_url = f"http://127.0.0.1:{console_port}"
    environment = service_environment(node_port)
    start_node(services, "node-restarted", node_port, chain_database, environment)
    start_console(
        services,
        "console-restarted",
        console_port,
        accounts_database,
        environment,
    )

    assert requests.get(f"{node_url}/chain", timeout=3).json()["length"] == 2
    assert administrator.get(f"{console_url}/network", timeout=3).status_code == 200

    member = requests.Session()
    member_registration = member.get(f"{console_url}/register", timeout=3)
    response = member.post(
        f"{console_url}/register",
        data={
            "csrf_token": csrf_token(member_registration),
            "username": "alice",
            "password": "standard user password",
            "password_confirm": "standard user password",
        },
        allow_redirects=False,
        timeout=5,
    )
    assert response.status_code == 302

    wallets_page = member.get(f"{console_url}/wallets", timeout=3)
    assert wallets_page.status_code == 200
    assert 'href="/network"' not in wallets_page.text
    assert member.get(f"{console_url}/network", timeout=3).status_code == 403
    assert member.get(f"{console_url}/api/nodes", timeout=3).status_code == 403


def test_two_real_nodes_synchronize_over_peer_protocol(services, tmp_path):
    first_port = unused_port()
    second_port = unused_port()
    first_url = f"http://127.0.0.1:{first_port}"
    second_url = f"http://127.0.0.1:{second_port}"
    first_environment = service_environment(first_port)
    second_environment = service_environment(second_port)

    start_node(
        services,
        "peer-first",
        first_port,
        tmp_path / "first.db",
        first_environment,
    )
    start_node(
        services,
        "peer-second",
        second_port,
        tmp_path / "second.db",
        second_environment,
    )

    from denarius_crypto import generate_encrypted_wallet

    miner_address = generate_encrypted_wallet("network test password")["address"]
    admin_headers = {"X-Denarius-Admin-Token": ADMIN_TOKEN}
    miner_response = requests.post(
        f"{first_url}/miner/register",
        data={"name": "Peer Miner", "address": miner_address},
        headers=admin_headers,
        timeout=5,
    )
    assert miner_response.status_code == 201
    assert requests.post(
        f"{first_url}/mine",
        headers=admin_headers,
        timeout=20,
    ).status_code == 200

    peer_response = requests.post(
        f"{second_url}/nodes/register",
        data={"nodes": f"127.0.0.1:{first_port}"},
        headers=admin_headers,
        timeout=5,
    )
    assert peer_response.status_code == 201
    requests.post(
        f"{second_url}/nodes/resolve",
        headers=admin_headers,
        timeout=20,
    )

    synchronized = wait_for_response(
        f"{second_url}/chain",
        predicate=lambda response: (
            response.status_code == 200 and response.json().get("length") == 2
        ),
    ).json()
    authoritative = requests.get(f"{first_url}/chain", timeout=3).json()

    assert synchronized["chain"][-1] == authoritative["chain"][-1]
    peers = requests.get(f"{second_url}/nodes/get", timeout=3).json()
    assert f"127.0.0.1:{first_port}" in peers["nodes"]
