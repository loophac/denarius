import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from time import perf_counter, time

import requests

from denarius_protocol import (
    CONSENSUS_ALGORITHM,
    GENESIS_HASH,
    NETWORK_KIND,
    NETWORK_ID,
    PEER_API_VERSION,
    PEER_CAPABILITIES,
    PROTOCOL_VERSION,
)


PROTOCOL_CACHE_SECONDS = 60
PEER_BAN_SCORE = 100
PEER_BAN_SECONDS = 60 * 60
RELAY_WORKERS = 8
RELAY_QUEUE_SIZE = 64


def protocol_identity():
    return {
        'protocol_version': PROTOCOL_VERSION,
        'network': NETWORK_ID,
        'network_kind': NETWORK_KIND,
        'consensus': CONSENSUS_ALGORITHM,
        'genesis_hash': GENESIS_HASH,
        'peer_api_version': PEER_API_VERSION,
        'capabilities': list(PEER_CAPABILITIES),
    }


def protocol_error(metadata):
    if not isinstance(metadata, dict):
        return 'Peer returned invalid protocol metadata'
    if metadata.get('protocol_version') != PROTOCOL_VERSION:
        return 'Peer uses a different consensus protocol version'
    if metadata.get('network') != NETWORK_ID:
        return 'Peer belongs to a different Denarius network'
    if metadata.get('genesis_hash') != GENESIS_HASH:
        return 'Peer has a different genesis block'
    if metadata.get('peer_api_version') != PEER_API_VERSION:
        return 'Peer uses an incompatible networking API'
    capabilities = metadata.get('capabilities')
    if not isinstance(capabilities, list) or not set(PEER_CAPABILITIES).issubset(capabilities):
        return 'Peer does not support the required networking capabilities'
    return None


class RelayCache:
    def __init__(self, max_items=10000):
        self.max_items = max_items
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def add(self, item_id):
        if not isinstance(item_id, str):
            return
        with self._lock:
            self._items.pop(item_id, None)
            self._items[item_id] = None
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def contains(self, item_id):
        with self._lock:
            return item_id in self._items


class PeerHealthTracker:
    def __init__(self):
        self._peers = {}
        self._lock = threading.RLock()

    def _entry(self, peer):
        return self._peers.setdefault(peer, {
            'status': 'unknown',
            'compatible': None,
            'last_seen': None,
            'last_checked': None,
            'latency_ms': None,
            'consecutive_failures': 0,
            'last_error': None,
            'score': 0,
            'banned_until': None,
            'relay_drops': 0,
            'protocol_version': None,
            'network': None,
            'height': None,
            'chainwork': None,
        })

    def record_success(self, peer, latency_ms=None):
        with self._lock:
            entry = self._entry(peer)
            entry['last_seen'] = int(time())
            entry['latency_ms'] = round(latency_ms, 1) if latency_ms is not None else None
            entry['consecutive_failures'] = 0
            entry['last_error'] = None
            entry['score'] = max(0, entry['score'] - 1)
            if entry['banned_until'] and entry['banned_until'] <= int(time()):
                entry['banned_until'] = None
            if entry['compatible'] is not False:
                entry['status'] = 'healthy'

    def record_compatible(self, peer, metadata, latency_ms=None):
        with self._lock:
            entry = self._entry(peer)
            entry['compatible'] = True
            entry['last_checked'] = int(time())
            entry['protocol_version'] = metadata.get('protocol_version')
            entry['network'] = metadata.get('network')
            entry['height'] = metadata.get('height')
            entry['chainwork'] = metadata.get('chainwork')
        self.record_success(peer, latency_ms)

    def record_incompatible(self, peer, message, metadata=None):
        with self._lock:
            entry = self._entry(peer)
            entry['status'] = 'incompatible'
            entry['compatible'] = False
            entry['last_checked'] = int(time())
            entry['last_error'] = message
            if isinstance(metadata, dict):
                entry['protocol_version'] = metadata.get('protocol_version')
                entry['network'] = metadata.get('network')
                entry['height'] = metadata.get('height')
                entry['chainwork'] = metadata.get('chainwork')

    def record_failure(self, peer, message):
        with self._lock:
            entry = self._entry(peer)
            entry['consecutive_failures'] += 1
            entry['last_error'] = str(message)[:240]
            entry['score'] += 10
            if entry['score'] >= PEER_BAN_SCORE:
                entry['banned_until'] = int(time()) + PEER_BAN_SECONDS
                entry['status'] = 'banned'
                return
            entry['status'] = 'unreachable' if entry['consecutive_failures'] >= 3 else 'degraded'

    def record_misbehavior(self, peer, message, score=25):
        with self._lock:
            entry = self._entry(peer)
            entry['score'] += max(1, int(score))
            entry['last_error'] = str(message)[:240]
            if entry['score'] >= PEER_BAN_SCORE:
                entry['banned_until'] = int(time()) + PEER_BAN_SECONDS
                entry['status'] = 'banned'
            else:
                entry['status'] = 'degraded'

    def record_relay_drop(self, peer):
        with self._lock:
            entry = self._entry(peer)
            entry['relay_drops'] += 1

    def is_banned(self, peer):
        with self._lock:
            entry = self._entry(peer)
            banned_until = entry.get('banned_until')
            if banned_until and banned_until > int(time()):
                entry['status'] = 'banned'
                return True
            if banned_until:
                entry['banned_until'] = None
                entry['score'] = PEER_BAN_SCORE // 2
                entry['status'] = 'degraded'
            return False

    def export_state(self, peers):
        with self._lock:
            return {
                peer: {
                    'score': self._entry(peer)['score'],
                    'banned_until': self._entry(peer)['banned_until'],
                    'consecutive_failures': self._entry(peer)['consecutive_failures'],
                    'last_error': self._entry(peer)['last_error'],
                }
                for peer in peers
            }

    def import_state(self, peer_states):
        if not isinstance(peer_states, dict):
            return
        with self._lock:
            for peer, saved in peer_states.items():
                if not isinstance(saved, dict):
                    continue
                entry = self._entry(peer)
                entry['score'] = max(0, int(saved.get('score', 0)))
                banned_until = saved.get('banned_until')
                entry['banned_until'] = int(banned_until) if banned_until else None
                entry['consecutive_failures'] = max(
                    0,
                    int(saved.get('consecutive_failures', 0)),
                )
                entry['last_error'] = saved.get('last_error')
                if entry['banned_until'] and entry['banned_until'] > int(time()):
                    entry['status'] = 'banned'

    def update_tip(self, peer, height, chainwork):
        with self._lock:
            entry = self._entry(peer)
            entry['height'] = height
            entry['chainwork'] = str(chainwork)

    def cached_compatibility(self, peer, max_age=PROTOCOL_CACHE_SECONDS):
        with self._lock:
            entry = self._peers.get(peer)
            if not entry or entry['last_checked'] is None:
                return None
            if entry.get('banned_until') and entry['banned_until'] > int(time()):
                return False
            if int(time()) - entry['last_checked'] > max_age:
                return None
            return entry['compatible']

    def snapshot(self, peers):
        with self._lock:
            result = []
            for peer in sorted(peers):
                entry = dict(self._entry(peer))
                entry.pop('last_checked', None)
                entry['node'] = peer
                result.append(entry)
            return result


class PeerNetwork:
    def __init__(
        self,
        timeout=3,
        requests_module=requests,
        relay_workers=RELAY_WORKERS,
        relay_queue_size=RELAY_QUEUE_SIZE,
        scheme='http',
    ):
        if scheme not in ('http', 'https'):
            raise ValueError('Peer transport must be http or https')
        self.timeout = timeout
        self.requests = requests_module
        self.scheme = scheme
        self.health = PeerHealthTracker()
        self.seen_transactions = RelayCache()
        self.seen_blocks = RelayCache()
        self._relay_executor = ThreadPoolExecutor(
            max_workers=relay_workers,
            thread_name_prefix='denarius-relay',
        )
        self._relay_capacity = threading.BoundedSemaphore(relay_workers + relay_queue_size)
        self._relay_futures = set()
        self._relay_lock = threading.Lock()

    def peer_url(self, peer, path):
        return self.scheme + '://' + peer + path

    def request_headers(self):
        identity = protocol_identity()
        return {
            'X-Denarius-Protocol-Version': str(identity['protocol_version']),
            'X-Denarius-Network': identity['network'],
            'X-Denarius-Peer-API-Version': str(identity['peer_api_version']),
        }

    def _request(self, peer, method, path, **kwargs):
        started = perf_counter()
        try:
            if method == 'GET':
                response = self.requests.get(
                    self.peer_url(peer, path),
                    timeout=self.timeout,
                    **kwargs,
                )
            else:
                response = self.requests.post(
                    self.peer_url(peer, path),
                    timeout=self.timeout,
                    **kwargs,
                )
        except self.requests.RequestException as exc:
            self.health.record_failure(peer, exc)
            raise

        latency_ms = (perf_counter() - started) * 1000
        if response.status_code >= 500:
            self.health.record_failure(peer, 'Peer returned HTTP ' + str(response.status_code))
        else:
            self.health.record_success(peer, latency_ms)
        return response, latency_ms

    def ensure_compatible(self, peer, force=False):
        if self.health.is_banned(peer):
            return False
        if not force:
            cached = self.health.cached_compatibility(peer)
            if cached is not None:
                return cached

        try:
            response, latency_ms = self._request(
                peer,
                'GET',
                '/protocol',
                headers=self.request_headers(),
            )
        except self.requests.RequestException:
            return False

        if response.status_code != 200:
            message = 'Protocol check returned HTTP ' + str(response.status_code)
            if response.status_code < 500:
                self.health.record_incompatible(peer, message)
            else:
                self.health.record_failure(peer, message)
            return False
        try:
            metadata = response.json()
        except (TypeError, ValueError):
            self.health.record_incompatible(peer, 'Peer returned invalid protocol metadata')
            return False

        error = protocol_error(metadata)
        if error:
            self.health.record_incompatible(peer, error, metadata)
            return False
        self.health.record_compatible(peer, metadata, latency_ms)
        return True

    def get_json(self, peer, path):
        if not self.ensure_compatible(peer):
            return None
        try:
            response, _ = self._request(
                peer,
                'GET',
                path,
                headers=self.request_headers(),
            )
        except self.requests.RequestException:
            return None
        if response.status_code != 200:
            self.health.record_failure(peer, 'Peer returned HTTP ' + str(response.status_code))
            return None
        try:
            payload = response.json()
        except (TypeError, ValueError):
            self.health.record_failure(peer, 'Peer returned invalid JSON')
            return None
        if not isinstance(payload, dict):
            self.health.record_failure(peer, 'Peer returned an invalid response')
            return None
        metadata = payload.get('protocol')
        if metadata is not None:
            error = protocol_error(metadata)
            if error:
                self.health.record_incompatible(peer, error, metadata)
                return None
        return payload

    def post(self, peer, path, **kwargs):
        if not self.ensure_compatible(peer):
            return None
        headers = dict(kwargs.pop('headers', {}))
        headers.update(self.request_headers())
        try:
            response, _ = self._request(peer, 'POST', path, headers=headers, **kwargs)
        except self.requests.RequestException:
            return None
        return response

    def relay_transaction(self, peers, transaction):
        return [
            future
            for peer in list(peers)
            for future in [self._submit_relay(
                peer,
                '/transactions/receive',
                data={
                    'sender_address': transaction['sender_address'],
                    'recipient_address': transaction['recipient_address'],
                    'amount': transaction['amount_atomic'],
                    'fee': transaction['fee_atomic'],
                    'nonce': transaction['nonce'],
                    'signature': transaction['signature'],
                    'transaction_id': transaction['transaction_id'],
                },
            )]
            if future is not None
        ]

    def relay_block(self, peers, block):
        return [
            future
            for peer in list(peers)
            for future in [self._submit_relay(
                peer,
                '/blocks/receive',
                json={'block': block},
            )]
            if future is not None
        ]

    def _submit_relay(self, peer, path, **kwargs):
        if self.health.is_banned(peer):
            return None
        if not self._relay_capacity.acquire(blocking=False):
            self.health.record_relay_drop(peer)
            return None
        future = self._relay_executor.submit(self.post, peer, path, **kwargs)
        with self._relay_lock:
            self._relay_futures.add(future)

        def finished(completed):
            with self._relay_lock:
                self._relay_futures.discard(completed)
            self._relay_capacity.release()
            try:
                completed.result()
            except Exception as exc:
                self.health.record_failure(peer, exc)

        future.add_done_callback(finished)
        return future

    def wait_for_relays(self, timeout=None):
        with self._relay_lock:
            futures = list(self._relay_futures)
        if futures:
            wait(futures, timeout=timeout)

    def close(self):
        self._relay_executor.shutdown(wait=False, cancel_futures=True)

    def peer_health(self, peers):
        return self.health.snapshot(peers)
