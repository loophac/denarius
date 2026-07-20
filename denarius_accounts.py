import sqlite3
import threading
from pathlib import Path
from secrets import token_hex
from time import time


ACCOUNT_SCHEMA_VERSION = 1


class DenariusAccountStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA synchronous = FULL')
        return connection

    def initialize(self, connection):
        connection.execute('PRAGMA journal_mode = WAL')
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS account_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS console_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                username_key TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                wallet_scope TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            );
            '''
        )
        connection.execute(
            'INSERT OR IGNORE INTO account_metadata(key, value) VALUES (?, ?)',
            ('schema_version', str(ACCOUNT_SCHEMA_VERSION)),
        )
        version = connection.execute(
            'SELECT value FROM account_metadata WHERE key = ?',
            ('schema_version',),
        ).fetchone()
        if version is None or version['value'] != str(ACCOUNT_SCHEMA_VERSION):
            raise ValueError('Unsupported account database schema')
        connection.commit()

    def ensure_initialized(self, connection):
        if self._initialized:
            return
        with self._initialize_lock:
            if not self._initialized:
                self.initialize(connection)
                self._initialized = True

    def has_admin(self):
        connection = self._connect()
        try:
            self.ensure_initialized(connection)
            return connection.execute(
                "SELECT 1 FROM console_accounts WHERE role = 'admin' LIMIT 1"
            ).fetchone() is not None
        finally:
            connection.close()

    def create_account(self, username, password_hash):
        username = username.strip()
        username_key = username.casefold()
        if not username or not username_key or not isinstance(password_hash, str):
            raise ValueError('Invalid account details')

        connection = self._connect()
        try:
            self.ensure_initialized(connection)
            connection.execute('BEGIN IMMEDIATE')
            has_admin = connection.execute(
                "SELECT 1 FROM console_accounts WHERE role = 'admin' LIMIT 1"
            ).fetchone() is not None
            role = 'user' if has_admin else 'admin'
            wallet_scope = token_hex(16)
            created_at = int(time())
            try:
                cursor = connection.execute(
                    '''
                    INSERT INTO console_accounts(
                        username, username_key, password_hash, role, wallet_scope, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ''',
                    (username, username_key, password_hash, role, wallet_scope, created_at),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValueError('That username is already registered') from exc
            connection.commit()
            return {
                'id': cursor.lastrowid,
                'username': username,
                'password_hash': password_hash,
                'role': role,
                'wallet_scope': wallet_scope,
                'created_at': created_at,
            }
        finally:
            connection.close()

    def find_by_id(self, account_id):
        if not isinstance(account_id, int) or isinstance(account_id, bool):
            return None
        connection = self._connect()
        try:
            self.ensure_initialized(connection)
            row = connection.execute(
                '''
                SELECT id, username, password_hash, role, wallet_scope, created_at
                FROM console_accounts WHERE id = ?
                ''',
                (account_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def find_by_username(self, username):
        if not isinstance(username, str) or not username.strip():
            return None
        connection = self._connect()
        try:
            self.ensure_initialized(connection)
            row = connection.execute(
                '''
                SELECT id, username, password_hash, role, wallet_scope, created_at
                FROM console_accounts WHERE username_key = ?
                ''',
                (username.strip().casefold(),),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()
