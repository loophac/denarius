import argparse
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from denarius_accounts import DenariusAccountStore
from denarius_paths import state_path


PROJECT_ROOT = Path(__file__).resolve().parent


def command(module, port, *extra):
    return [sys.executable, '-m', module, '--port', str(port), *extra]


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the Denarius node and local console')
    parser.add_argument('--node-port', type=int, default=5000)
    parser.add_argument('--console-port', '--wallet-port', dest='console_port', type=int, default=8080)
    parser.add_argument('--console-host', default='127.0.0.1')
    parser.add_argument('--sync-interval', type=int, default=30)
    parser.add_argument('--node-host', default='127.0.0.1')
    parser.add_argument('--advertise-address', default=None)
    parser.add_argument(
        '--database',
        default=str(state_path('denarius.db')),
    )
    parser.add_argument(
        '--accounts-database',
        default=str(state_path('console-accounts.db')),
    )
    args = parser.parse_args(argv)

    environment = os.environ.copy()
    environment.setdefault('DENARIUS_ADMIN_TOKEN', secrets.token_hex(32))
    environment.setdefault('DENARIUS_SECRET_KEY', secrets.token_hex(32))
    environment['DENARIUS_NODE_URL'] = 'http://127.0.0.1:' + str(args.node_port)
    setup_token = None
    if not DenariusAccountStore(args.accounts_database).has_admin():
        setup_token = environment.setdefault('DENARIUS_SETUP_TOKEN', secrets.token_urlsafe(24))

    services = [
        command(
            'blockchain.blockchain',
            args.node_port,
            '--database', args.database,
            '--sync-interval', str(args.sync_interval),
            '--host', args.node_host,
            '--advertise-address', args.advertise_address or ('127.0.0.1:' + str(args.node_port)),
        ),
        command(
            'blockchain_client.blockchain_client',
            args.console_port,
            '--host', args.console_host,
            '--accounts-database', args.accounts_database,
        ),
    ]
    processes = [
        subprocess.Popen(service, cwd=str(PROJECT_ROOT), env=environment)
        for service in services
    ]
    print('Denarius node:      http://127.0.0.1:' + str(args.node_port))
    print('Denarius Console:   http://127.0.0.1:' + str(args.console_port))
    if setup_token:
        print('Administrator setup code: ' + setup_token)

    interrupted = False
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        failed = [process.returncode for process in processes if process.returncode not in (0, None)]
        if failed and not interrupted:
            raise SystemExit(failed[0])


if __name__ == '__main__':
    main()
