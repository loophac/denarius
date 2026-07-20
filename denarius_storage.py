import json
import hashlib
import sqlite3
from pathlib import Path

from denarius_protocol import (
    GENESIS_HASH,
    NETWORK_ID,
    PROTOCOL_VERSION,
    block_hash,
    canonical_json_bytes,
)


SCHEMA_VERSION = 3


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
                node TEXT PRIMARY KEY,
                score INTEGER NOT NULL DEFAULT 0,
                banned_until INTEGER,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS accounts (
                address TEXT PRIMARY KEY,
                balance_atomic TEXT NOT NULL,
                nonce INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS confirmed_transactions (
                transaction_id TEXT PRIMARY KEY,
                height INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS immature_rewards (
                height INTEGER PRIMARY KEY,
                matures_at INTEGER NOT NULL,
                address TEXT NOT NULL,
                amount_atomic TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chain_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS block_undo (
                height INTEGER PRIMARY KEY,
                block_hash TEXT NOT NULL,
                undo_json TEXT NOT NULL
            );
            '''
        )

    def _peer_rows(self, state):
        peer_states = state.get('peer_states') or {}
        for node in state['nodes']:
            peer = peer_states.get(node, {})
            yield (
                node,
                max(0, int(peer.get('score', 0))),
                int(peer['banned_until']) if peer.get('banned_until') else None,
                max(0, int(peer.get('consecutive_failures', 0))),
                str(peer.get('last_error'))[:240] if peer.get('last_error') else None,
            )

    def _write_chain_state(self, connection, chain_state):
        if not isinstance(chain_state, dict):
            return
        connection.execute('DELETE FROM accounts')
        addresses = set(chain_state.get('balances', {})) | set(chain_state.get('nonces', {}))
        connection.executemany(
            'INSERT INTO accounts(address, balance_atomic, nonce) VALUES (?, ?, ?)',
            (
                (
                    address,
                    str(chain_state.get('balances', {}).get(address, 0)),
                    int(chain_state.get('nonces', {}).get(address, 0)),
                )
                for address in sorted(addresses)
            ),
        )
        connection.execute('DELETE FROM confirmed_transactions')
        transaction_heights = chain_state.get('transaction_heights', {})
        connection.executemany(
            '''
            INSERT INTO confirmed_transactions(transaction_id, height)
            VALUES (?, ?)
            ''',
            (
                (transaction_id, int(transaction_heights.get(transaction_id, 0)))
                for transaction_id in chain_state.get('confirmed_transactions', [])
            ),
        )
        self._replace_immature_rewards(connection, chain_state.get('immature_rewards', []))
        self._replace_chain_state_metadata(connection, chain_state)

    def _replace_immature_rewards(self, connection, rewards):
        connection.execute('DELETE FROM immature_rewards')
        connection.executemany(
            '''
            INSERT INTO immature_rewards(height, matures_at, address, amount_atomic)
            VALUES (?, ?, ?, ?)
            ''',
            (
                (
                    int(reward['height']),
                    int(reward['matures_at']),
                    reward['address'],
                    str(reward['amount_atomic']),
                )
                for reward in rewards
            ),
        )

    def _replace_chain_state_metadata(self, connection, chain_state):
        values = {
            'issued_atomic': str(chain_state.get('issued_atomic', 0)),
            'chainwork': str(chain_state.get('chainwork', 0)),
            'tip_height': int(chain_state.get('tip_height', 0)),
            'tip_hash': chain_state.get('tip_hash', GENESIS_HASH),
            'state_hash': hashlib.sha256(canonical_json_bytes(chain_state)).hexdigest(),
        }
        connection.execute('DELETE FROM chain_state')
        connection.executemany(
            'INSERT INTO chain_state(key, value) VALUES (?, ?)',
            ((key, json.dumps(value, separators=(',', ':'))) for key, value in values.items()),
        )

    def _write_undo_records(self, connection, undo_records):
        connection.execute('DELETE FROM block_undo')
        connection.executemany(
            'INSERT INTO block_undo(height, block_hash, undo_json) VALUES (?, ?, ?)',
            (
                (
                    int(height),
                    undo['block_hash'],
                    canonical_json_bytes(undo).decode('utf8'),
                )
                for height, undo in sorted((undo_records or {}).items())
            ),
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
                connection.execute('DELETE FROM accounts')
                connection.execute('DELETE FROM confirmed_transactions')
                connection.execute('DELETE FROM immature_rewards')
                connection.execute('DELETE FROM chain_state')
                connection.execute('DELETE FROM block_undo')

                metadata = {
                    'schema_version': SCHEMA_VERSION,
                    'protocol_version': PROTOCOL_VERSION,
                    'network_id': NETWORK_ID,
                    'genesis_hash': GENESIS_HASH,
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
                    '''
                    INSERT INTO peers(
                        node, score, banned_until, consecutive_failures, last_error
                    ) VALUES (?, ?, ?, ?, ?)
                    ''',
                    self._peer_rows(state),
                )
                self._write_chain_state(connection, state.get('chain_state'))
                self._write_undo_records(connection, state.get('undo_records'))
        finally:
            connection.close()

    def append_block(self, state, block, undo):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            raise ValueError('State database must be initialized before appending a block')
        connection = self._connect()
        try:
            with connection:
                self.initialize(connection)
                connection.execute('BEGIN IMMEDIATE')
                height = int(block['block_number'])
                tip = connection.execute(
                    'SELECT height, block_hash FROM blocks ORDER BY height DESC LIMIT 1'
                ).fetchone()
                if not tip or tip[0] != height - 1 or tip[1] != block['previous_hash']:
                    raise ValueError('State database tip does not match the appended block')

                encoded_block = canonical_json_bytes(block).decode('utf8')
                current_hash = block_hash(block)
                connection.execute(
                    'INSERT INTO blocks(height, block_hash, block_json) VALUES (?, ?, ?)',
                    (height, current_hash, encoded_block),
                )

                chain_state = state['chain_state']
                for address in undo.get('balances_before', {}):
                    balance = chain_state['balances'].get(address, 0)
                    nonce = chain_state['nonces'].get(address, 0)
                    if balance == 0 and nonce == 0:
                        connection.execute('DELETE FROM accounts WHERE address = ?', (address,))
                    else:
                        connection.execute(
                            '''
                            INSERT INTO accounts(address, balance_atomic, nonce)
                            VALUES (?, ?, ?)
                            ON CONFLICT(address) DO UPDATE SET
                                balance_atomic = excluded.balance_atomic,
                                nonce = excluded.nonce
                            ''',
                            (address, str(balance), int(nonce)),
                        )
                for transaction in block.get('transactions', [])[1:]:
                    connection.execute(
                        '''
                        INSERT INTO confirmed_transactions(transaction_id, height)
                        VALUES (?, ?)
                        ''',
                        (transaction['transaction_id'], height),
                    )
                self._replace_immature_rewards(
                    connection,
                    chain_state.get('immature_rewards', []),
                )
                self._replace_chain_state_metadata(connection, chain_state)

                connection.execute('DELETE FROM pending_transactions')
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
                connection.execute(
                    'INSERT INTO block_undo(height, block_hash, undo_json) VALUES (?, ?, ?)',
                    (height, current_hash, canonical_json_bytes(undo).decode('utf8')),
                )
                metadata = {
                    'schema_version': SCHEMA_VERSION,
                    'protocol_version': PROTOCOL_VERSION,
                    'network_id': NETWORK_ID,
                    'genesis_hash': GENESIS_HASH,
                    'node_address': state['node_address'],
                    'miner_name': state['miner_name'],
                    'mining_target': state['mining_target'],
                }
                connection.executemany(
                    '''
                    INSERT INTO metadata(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    ''',
                    (
                        (key, json.dumps(value, separators=(',', ':')))
                        for key, value in metadata.items()
                    ),
                )
        finally:
            connection.close()

    def update_peers(self, nodes, peer_states):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            with connection:
                self.initialize(connection)
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('DELETE FROM peers')
                connection.executemany(
                    '''
                    INSERT INTO peers(
                        node, score, banned_until, consecutive_failures, last_error
                    ) VALUES (?, ?, ?, ?, ?)
                    ''',
                    self._peer_rows({
                        'nodes': sorted(nodes),
                        'peer_states': peer_states,
                    }),
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
                if metadata.get('network_id') != NETWORK_ID:
                    raise ValueError('State database belongs to another Denarius network')
                if metadata.get('genesis_hash') != GENESIS_HASH:
                    raise ValueError('State database belongs to another Denarius genesis block')
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
                peer_rows = list(connection.execute(
                    '''
                    SELECT node, score, banned_until, consecutive_failures, last_error
                    FROM peers ORDER BY node
                    '''
                ))
                nodes = [row[0] for row in peer_rows]
                peer_states = {
                    node: {
                        'score': score,
                        'banned_until': banned_until,
                        'consecutive_failures': consecutive_failures,
                        'last_error': last_error,
                    }
                    for node, score, banned_until, consecutive_failures, last_error in peer_rows
                }
                accounts = list(connection.execute(
                    'SELECT address, balance_atomic, nonce FROM accounts ORDER BY address'
                ))
                chain_state_values = {
                    key: json.loads(value)
                    for key, value in connection.execute('SELECT key, value FROM chain_state')
                }
                immature_rewards = [
                    {
                        'height': height,
                        'matures_at': matures_at,
                        'address': address,
                        'amount_atomic': int(amount_atomic),
                    }
                    for height, matures_at, address, amount_atomic in connection.execute(
                        '''
                        SELECT height, matures_at, address, amount_atomic
                        FROM immature_rewards ORDER BY height
                        '''
                    )
                ]
                confirmed = [
                    (transaction_id, height)
                    for transaction_id, height in connection.execute(
                        'SELECT transaction_id, height FROM confirmed_transactions'
                    )
                ]
                undo_records = {
                    height: json.loads(undo_json)
                    for height, undo_json in connection.execute(
                        'SELECT height, undo_json FROM block_undo ORDER BY height'
                    )
                }
        except json.JSONDecodeError as exc:
            raise ValueError('State database is invalid') from exc
        except ValueError:
            raise
        except (sqlite3.DatabaseError, KeyError, TypeError) as exc:
            raise ValueError('State database is invalid') from exc
        finally:
            if connection is not None:
                connection.close()

        persisted_chain_state = None
        if chain_state_values:
            persisted_chain_state = {
                'balances': {address: int(balance) for address, balance, _ in accounts},
                'nonces': {address: nonce for address, _, nonce in accounts if nonce},
                'confirmed_transactions': sorted(transaction_id for transaction_id, _ in confirmed),
                'transaction_heights': dict(confirmed),
                'immature_rewards': immature_rewards,
                'issued_atomic': int(chain_state_values.get('issued_atomic', 0)),
                'chainwork': int(chain_state_values.get('chainwork', 0)),
                'tip_height': int(chain_state_values.get('tip_height', 0)),
                'tip_hash': chain_state_values.get('tip_hash', GENESIS_HASH),
            }
            state_hash = chain_state_values.get('state_hash')
            expected_state_hash = hashlib.sha256(
                canonical_json_bytes(persisted_chain_state)
            ).hexdigest()
            if state_hash != expected_state_hash:
                raise ValueError('State database chain index checksum does not match')

        return {
            'chain': chain,
            'chain_state': persisted_chain_state,
            'undo_records': undo_records,
            'transactions': transactions,
            'nodes': nodes,
            'peer_states': peer_states,
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
