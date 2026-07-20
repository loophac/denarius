import json
import sqlite3
from pathlib import Path

from denarius_protocol import PROTOCOL_VERSION, block_hash, canonical_json_bytes


SCHEMA_VERSION = 1


class DenariusStorage:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA synchronous = FULL')
        return connection

    def initialize(self, connection):
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY,
                block_hash TEXT NOT NULL UNIQUE,
                block_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_transactions (
                position INTEGER PRIMARY KEY,
                transaction_id TEXT NOT NULL UNIQUE,
                transaction_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS peers (
                node TEXT PRIMARY KEY
            );
            '''
        )

    def save_state(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            with connection:
                self.initialize(connection)
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('DELETE FROM metadata')
                connection.execute('DELETE FROM blocks')
                connection.execute('DELETE FROM pending_transactions')
                connection.execute('DELETE FROM peers')

                metadata = {
                    'schema_version': SCHEMA_VERSION,
                    'protocol_version': PROTOCOL_VERSION,
                    'node_address': state['node_address'],
                    'miner_name': state['miner_name'],
                    'mining_target': state['mining_target'],
                }
                connection.executemany(
                    'INSERT INTO metadata(key, value) VALUES (?, ?)',
                    ((key, json.dumps(value, separators=(',', ':'))) for key, value in metadata.items()),
                )
                connection.executemany(
                    'INSERT INTO blocks(height, block_hash, block_json) VALUES (?, ?, ?)',
                    (
                        (
                            height,
                            block_hash(block),
                            canonical_json_bytes(block).decode('utf8'),
                        )
                        for height, block in enumerate(state['chain'])
                    ),
                )
                connection.executemany(
                    '''
                    INSERT INTO pending_transactions(position, transaction_id, transaction_json)
                    VALUES (?, ?, ?)
                    ''',
                    (
                        (
                            position,
                            transaction['transaction_id'],
                            canonical_json_bytes(transaction).decode('utf8'),
                        )
                        for position, transaction in enumerate(state['transactions'])
                    ),
                )
                connection.executemany(
                    'INSERT INTO peers(node) VALUES (?)',
                    ((node,) for node in state['nodes']),
                )
        finally:
            connection.close()

    def load_state(self):
        if not self.path.exists():
            raise ValueError('State database does not exist')
        connection = None
        try:
            connection = self._connect()
            with connection:
                result = connection.execute('PRAGMA quick_check').fetchone()
                if not result or result[0] != 'ok':
                    raise ValueError('State database failed its integrity check')
                metadata = {
                    key: json.loads(value)
                    for key, value in connection.execute('SELECT key, value FROM metadata')
                }
                if metadata.get('schema_version') != SCHEMA_VERSION:
                    raise ValueError('Unsupported state database schema')
                if metadata.get('protocol_version') != PROTOCOL_VERSION:
                    raise ValueError('State database belongs to another protocol version')
                chain = []
                for expected_height, stored_hash, block_json in connection.execute(
                    'SELECT height, block_hash, block_json FROM blocks ORDER BY height'
                ):
                    if expected_height != len(chain):
                        raise ValueError('State database contains a block-height gap')
                    block = json.loads(block_json)
                    if block_hash(block) != stored_hash:
                        raise ValueError('State database contains a mismatched block hash')
                    chain.append(block)
                transactions = [
                    json.loads(transaction_json)
                    for _, transaction_json in connection.execute(
                        'SELECT position, transaction_json FROM pending_transactions ORDER BY position'
                    )
                ]
                nodes = [node for node, in connection.execute('SELECT node FROM peers ORDER BY node')]
        except json.JSONDecodeError as exc:
            raise ValueError('State database is invalid') from exc
        except ValueError:
            raise
        except (sqlite3.DatabaseError, KeyError, TypeError) as exc:
            raise ValueError('State database is invalid') from exc
        finally:
            if connection is not None:
                connection.close()

        return {
            'chain': chain,
            'transactions': transactions,
            'nodes': nodes,
            'node_address': metadata.get('node_address'),
            'miner_name': metadata.get('miner_name'),
            'mining_target': metadata.get('mining_target'),
        }


def migrate_json_state(json_path, database_path):
    json_path = Path(json_path).resolve()
    try:
        with json_path.open('r', encoding='utf8') as state_file:
            state = json.load(state_file)
    except FileNotFoundError as exc:
        raise ValueError('JSON state file does not exist') from exc
    except json.JSONDecodeError as exc:
        raise ValueError('JSON state file is invalid') from exc
    if not isinstance(state, dict):
        raise ValueError('JSON state file must contain an object')
    try:
        DenariusStorage(database_path).save_state(state)
    except (sqlite3.DatabaseError, KeyError, TypeError) as exc:
        raise ValueError('JSON state file does not contain valid Denarius state') from exc
