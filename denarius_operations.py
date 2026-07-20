import json
import logging
import os
import re
import threading
from collections import defaultdict, deque
from time import monotonic, time
from uuid import uuid4


REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'timestamp': int(time()),
            'level': record.levelname.lower(),
            'logger': record.name,
            'message': record.getMessage(),
        }
        for field in ('service', 'request_id', 'method', 'path', 'status', 'duration_ms'):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(',', ':'), ensure_ascii=True)


def configure_json_logging(service, level='INFO'):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger = logging.getLogger(service)
    logger.info('service logging initialized', extra={'service': service})
    return logger


def configure_trusted_proxy(app):
    configured = os.environ.get('DENARIUS_TRUSTED_PROXY_COUNT', '0')
    try:
        count = int(configured)
    except ValueError as exc:
        raise ValueError('DENARIUS_TRUSTED_PROXY_COUNT must be an integer') from exc
    if count < 0 or count > 2:
        raise ValueError('DENARIUS_TRUSTED_PROXY_COUNT must be between 0 and 2')
    if count:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=count,
            x_proto=count,
            x_host=count,
            x_port=count,
        )
    return count


class SlidingWindowRateLimiter:
    def __init__(self, max_keys=10000):
        self.max_keys = max(1, int(max_keys))
        self._events = {}
        self._lock = threading.Lock()

    def check(self, key, limit, window_seconds, now=None):
        limit = max(1, int(limit))
        window_seconds = max(1, int(window_seconds))
        now = monotonic() if now is None else float(now)
        cutoff = now - window_seconds
        with self._lock:
            events = self._events.setdefault(str(key), deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(1, int(events[0] + window_seconds - now) + 1)
                return False, retry_after
            events.append(now)
            if len(self._events) > self.max_keys:
                self._prune(cutoff)
            return True, 0

    def _prune(self, cutoff):
        stale = [key for key, events in self._events.items() if not events or events[-1] <= cutoff]
        for key in stale:
            self._events.pop(key, None)
        while len(self._events) > self.max_keys:
            self._events.pop(next(iter(self._events)))


class OperationalMetrics:
    def __init__(self, service):
        self.service = service
        self._counters = defaultdict(int)
        self._durations = defaultdict(float)
        self._lock = threading.Lock()

    def observe_request(self, endpoint, method, status, duration_seconds):
        key = (endpoint or 'unmatched', method, str(status))
        with self._lock:
            self._counters[key] += 1
            self._durations[key] += max(0.0, float(duration_seconds))

    def record_rate_limit(self):
        with self._lock:
            self._counters[('rate_limited', 'ALL', '429')] += 1

    def render(self, gauges=None):
        gauges = gauges or {}
        lines = [
            '# HELP denarius_http_requests_total HTTP requests handled.',
            '# TYPE denarius_http_requests_total counter',
        ]
        with self._lock:
            counters = dict(self._counters)
            durations = dict(self._durations)
        for (endpoint, method, status), value in sorted(counters.items()):
            labels = self._labels(endpoint=endpoint, method=method, status=status)
            lines.append(f'denarius_http_requests_total{{{labels}}} {value}')
        lines.extend([
            '# HELP denarius_http_request_duration_seconds_total Total request time.',
            '# TYPE denarius_http_request_duration_seconds_total counter',
        ])
        for (endpoint, method, status), value in sorted(durations.items()):
            labels = self._labels(endpoint=endpoint, method=method, status=status)
            lines.append(f'denarius_http_request_duration_seconds_total{{{labels}}} {value:.6f}')
        for name, value in sorted(gauges.items()):
            metric_name = 'denarius_' + re.sub(r'[^a-zA-Z0-9_:]', '_', str(name))
            lines.append(f'# TYPE {metric_name} gauge')
            lines.append(f'{metric_name}{{service="{self.service}"}} {value}')
        return '\n'.join(lines) + '\n'

    def _labels(self, **labels):
        values = {'service': self.service, **labels}
        return ','.join(
            f'{key}="{str(value).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
            for key, value in sorted(values.items())
        )


def install_runtime_controls(
    app,
    service,
    policies,
    default_policy=(120, 60),
    secure_transport=False,
):
    from flask import g, jsonify, request

    limiter = SlidingWindowRateLimiter()
    metrics = OperationalMetrics(service)
    logger = logging.getLogger(service)

    @app.before_request
    def denarius_before_request():
        g.denarius_started = monotonic()
        submitted_id = request.headers.get('X-Request-ID', '')
        g.denarius_request_id = submitted_id if REQUEST_ID_PATTERN.fullmatch(submitted_id) else uuid4().hex
        g.denarius_csp_nonce = uuid4().hex
        policy = policies.get(request.endpoint, default_policy)
        if policy is None:
            return None
        client = request.remote_addr or 'unknown'
        allowed, retry_after = limiter.check(
            f'{client}:{request.endpoint or request.path}',
            policy[0],
            policy[1],
        )
        if allowed:
            return None
        metrics.record_rate_limit()
        response = jsonify({'message': 'Too many requests; retry later'})
        response.status_code = 429
        response.headers['Retry-After'] = str(retry_after)
        return response

    @app.after_request
    def denarius_after_request(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        csp_nonce = getattr(g, 'denarius_csp_nonce', uuid4().hex)
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; base-uri 'self'; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; "
            "script-src 'self' 'nonce-" + csp_nonce + "'; style-src 'self' 'unsafe-inline'"
        )
        if secure_transport:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        request_id = getattr(g, 'denarius_request_id', uuid4().hex)
        response.headers['X-Request-ID'] = request_id
        started = getattr(g, 'denarius_started', monotonic())
        duration = max(0.0, monotonic() - started)
        metrics.observe_request(request.endpoint, request.method, response.status_code, duration)
        logger.info(
            'http request',
            extra={
                'service': service,
                'request_id': request_id,
                'method': request.method,
                'path': request.path,
                'status': response.status_code,
                'duration_ms': round(duration * 1000, 2),
            },
        )
        return response

    return metrics
