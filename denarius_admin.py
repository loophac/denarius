import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from denarius_accounts import ACCOUNT_SCHEMA_VERSION
from denarius_paths import state_path
from denarius_protocol import GENESIS_HASH, NETWORK_ID, PROTOCOL_VERSION
from denarius_storage import DenariusStorage


BACKUP_FORMAT_VERSION = 1
CHAIN_BACKUP_NAME = 'denarius-chain.sqlite3'
ACCOUNTS_BACKUP_NAME = 'denarius-accounts.sqlite3'
MANIFEST_NAME = 'manifest.json'


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source_path, destination_path):
    source_path = Path(source_path).resolve()
    destination_path = Path(destination_path).resolve()
    if not source_path.is_file():
        raise ValueError('Database does not exist: ' + str(source_path))
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(destination_path.name + '.tmp')
    temporary_path.unlink(missing_ok=True)
    source = sqlite3.connect(str(source_path), timeout=10)
    destination = sqlite3.connect(str(temporary_path), timeout=10)
    try:
        source.backup(destination)
        result = destination.execute('PRAGMA integrity_check').fetchone()
        if not result or result[0] != 'ok':
            raise ValueError('SQLite backup failed its integrity check')
    finally:
        destination.close()
        source.close()
    try:
        os.replace(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination_path


def verify_accounts_database(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Account database does not exist')
    connection = sqlite3.connect(str(path), timeout=10)
    try:
        result = connection.execute('PRAGMA quick_check').fetchone()
        if not result or result[0] != 'ok':
            raise ValueError('Account database failed its integrity check')
        version = connection.execute(
            'SELECT value FROM account_metadata WHERE key = ?',
            ('schema_version',),
        ).fetchone()
        if version is None or version[0] != str(ACCOUNT_SCHEMA_VERSION):
            raise ValueError('Unsupported account database schema')
    except sqlite3.DatabaseError as exc:
        raise ValueError('Account database is invalid') from exc
    finally:
        connection.close()


def create_backup(chain_database, accounts_database, output_directory):
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in (CHAIN_BACKUP_NAME, ACCOUNTS_BACKUP_NAME, MANIFEST_NAME)):
        raise ValueError('Backup directory already contains Denarius backup files')

    chain_copy = sqlite_backup(chain_database, output / CHAIN_BACKUP_NAME)
    accounts_copy = sqlite_backup(accounts_database, output / ACCOUNTS_BACKUP_NAME)
    try:
        DenariusStorage(chain_copy).load_state()
        verify_accounts_database(accounts_copy)
        manifest = {
            'backup_format': BACKUP_FORMAT_VERSION,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'network': NETWORK_ID,
            'protocol_version': PROTOCOL_VERSION,
            'genesis_hash': GENESIS_HASH,
            'files': {
                CHAIN_BACKUP_NAME: file_sha256(chain_copy),
                ACCOUNTS_BACKUP_NAME: file_sha256(accounts_copy),
            },
        }
        temporary_manifest = output / (MANIFEST_NAME + '.tmp')
        temporary_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + '\n',
            encoding='utf8',
        )
        os.replace(temporary_manifest, output / MANIFEST_NAME)
    except Exception:
        chain_copy.unlink(missing_ok=True)
        accounts_copy.unlink(missing_ok=True)
        raise
    return manifest


def verify_backup(backup_directory):
    directory = Path(backup_directory).resolve()
    manifest_path = directory / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    except FileNotFoundError as exc:
        raise ValueError('Backup manifest does not exist') from exc
    except json.JSONDecodeError as exc:
        raise ValueError('Backup manifest is invalid') from exc
    expected = {
        'backup_format': BACKUP_FORMAT_VERSION,
        'network': NETWORK_ID,
        'protocol_version': PROTOCOL_VERSION,
        'genesis_hash': GENESIS_HASH,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise ValueError('Backup has an incompatible ' + field)
    files = manifest.get('files')
    if not isinstance(files, dict):
        raise ValueError('Backup file manifest is missing')
    for filename in (CHAIN_BACKUP_NAME, ACCOUNTS_BACKUP_NAME):
        path = directory / filename
        expected_hash = files.get(filename)
        if not expected_hash or file_sha256(path) != expected_hash:
            raise ValueError('Backup hash mismatch: ' + filename)
    DenariusStorage(directory / CHAIN_BACKUP_NAME).load_state()
    verify_accounts_database(directory / ACCOUNTS_BACKUP_NAME)
    return manifest


def restore_backup(backup_directory, chain_database, accounts_database):
    verify_backup(backup_directory)
    directory = Path(backup_directory).resolve()
    chain_database = Path(chain_database).resolve()
    accounts_database = Path(accounts_database).resolve()
    staged_chain = chain_database.with_name(chain_database.name + '.restore')
    staged_accounts = accounts_database.with_name(accounts_database.name + '.restore')
    previous_chain = chain_database.with_name(chain_database.name + '.pre-restore')
    previous_accounts = accounts_database.with_name(accounts_database.name + '.pre-restore')
    staged_chain.unlink(missing_ok=True)
    staged_accounts.unlink(missing_ok=True)
    previous_chain.unlink(missing_ok=True)
    previous_accounts.unlink(missing_ok=True)
    try:
        sqlite_backup(directory / CHAIN_BACKUP_NAME, staged_chain)
        sqlite_backup(directory / ACCOUNTS_BACKUP_NAME, staged_accounts)
        DenariusStorage(staged_chain).load_state()
        verify_accounts_database(staged_accounts)
        chain_moved = False
        accounts_moved = False
        try:
            if chain_database.exists():
                os.replace(chain_database, previous_chain)
                chain_moved = True
            if accounts_database.exists():
                os.replace(accounts_database, previous_accounts)
                accounts_moved = True
            os.replace(staged_chain, chain_database)
            os.replace(staged_accounts, accounts_database)
        except Exception:
            if chain_moved:
                chain_database.unlink(missing_ok=True)
                os.replace(previous_chain, chain_database)
            if accounts_moved:
                accounts_database.unlink(missing_ok=True)
                os.replace(previous_accounts, accounts_database)
            raise
        for database in (chain_database, accounts_database):
            database.with_name(database.name + '-wal').unlink(missing_ok=True)
            database.with_name(database.name + '-shm').unlink(missing_ok=True)
        previous_chain.unlink(missing_ok=True)
        previous_accounts.unlink(missing_ok=True)
    finally:
        staged_chain.unlink(missing_ok=True)
        staged_accounts.unlink(missing_ok=True)
    DenariusStorage(chain_database).load_state()
    verify_accounts_database(accounts_database)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Denarius backup and recovery tools')
    commands = parser.add_subparsers(dest='command', required=True)

    backup = commands.add_parser('backup', help='create and verify an online backup')
    backup.add_argument('--database', default=str(state_path('denarius-testnet-v3.db')))
    backup.add_argument('--accounts-database', default=str(state_path('console-accounts.db')))
    backup.add_argument('--output', required=True)

    verify = commands.add_parser('verify', help='verify a Denarius backup directory')
    verify.add_argument('--backup', required=True)

    restore = commands.add_parser('restore', help='restore a verified backup')
    restore.add_argument('--backup', required=True)
    restore.add_argument('--database', default=str(state_path('denarius-testnet-v3.db')))
    restore.add_argument('--accounts-database', default=str(state_path('console-accounts.db')))
    restore.add_argument(
        '--confirm-services-stopped',
        action='store_true',
        help='confirm that node and console processes are stopped',
    )

    args = parser.parse_args(argv)
    try:
        if args.command == 'backup':
            manifest = create_backup(args.database, args.accounts_database, args.output)
            print('Backup created and verified: ' + str(Path(args.output).resolve()))
            print('Created: ' + manifest['created_at'])
        elif args.command == 'verify':
            manifest = verify_backup(args.backup)
            print('Backup verified for ' + manifest['network'])
        elif args.command == 'restore':
            if not args.confirm_services_stopped:
                parser.error('restore requires --confirm-services-stopped')
            restore_backup(args.backup, args.database, args.accounts_database)
            print('Backup restored and verified')
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
