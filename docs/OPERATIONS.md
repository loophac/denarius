# Production Operations

Denarius remains a testnet. This runbook describes hardened testnet operation
and the controls that must exist before a production launch is considered.

## Serving and TLS

The installed `denarius-node`, `denarius-console`, and `denarius` commands use
Waitress by default on Windows and Linux. `--development-server` is only for
local debugging. Run the services as an unprivileged operating-system account
behind a maintained reverse proxy that terminates TLS, limits request bodies,
and forwards only the intended ports.

For an HTTPS console, set:

```text
DENARIUS_SECRET_KEY=<at least 32 random bytes>
DENARIUS_ADMIN_TOKEN=<at least 32 random bytes>
DENARIUS_METRICS_TOKEN=<separate monitoring token>
DENARIUS_COOKIE_SECURE=1
DENARIUS_SECURE_TRANSPORT=1
```

Keep tokens in a service secret store, not shell history or source control.
Rotate the session secret to invalidate every console session. Browser wallet
keys are encrypted and signed on the user's device and are not part of node
backups.

When and only when direct access to Waitress is restricted to a trusted reverse
proxy, set `DENARIUS_TRUSTED_PROXY_COUNT=1` so client IP addresses and HTTPS
scheme are derived from one forwarded hop. Leave it at zero if clients can
reach Waitress directly; trusting attacker-supplied forwarding headers defeats
IP rate limits.

## Health and metrics

- `/healthz` reports process liveness.
- `/readyz` reports service readiness and, for the console, node reachability.
- `/metrics` emits Prometheus text and requires `X-Denarius-Metrics-Token`.
- JSON request logs include service, request ID, route, status, and duration;
  secrets and request bodies are not logged.

Alert on readiness failure, repeated process restarts, rising HTTP 429/5xx
rates, a stalled chain tip, growing mempool, loss of healthy peers, peer bans,
backup failure, disk pressure, and restore-verification failure.

Automining is an administrator-controlled runtime setting. It mines at most one
block every two seconds on the low-difficulty testnet and stops during node
shutdown; operators must start it again deliberately after a restart. Stop it
before maintenance, restoration, or consensus investigation.

## Backups

Create online SQLite backups in a new directory:

```bash
denarius-admin backup --output backups/2026-07-20T1200Z
denarius-admin verify --backup backups/2026-07-20T1200Z
```

The command uses SQLite's online backup API, runs integrity checks, and writes a
manifest containing the network, protocol, genesis hash, and SHA-256 hashes.
Copy completed backup directories to encrypted storage with retention and
access controls. Test a restore on another machine at least monthly.

To restore, stop both Denarius processes first:

```bash
denarius-admin restore --backup backups/2026-07-20T1200Z --confirm-services-stopped
```

Restart the node, check `/readyz`, compare the expected tip and chainwork, then
start the console. Wallet recovery is separate: each user must retain an
encrypted `.denwallet` export and its password.

Routine startup verifies the indexed-state checksum and the latest block/state
transition without replaying complete history. Run `denarius-node --reindex`
while the node is offline to replay every block and rewrite indexes after
suspected logical corruption or as a scheduled audit.

## Rate limits

Default per-client limits are 120 requests per minute. Login and registration
allow 10 attempts per five minutes; transaction submission allows 30 per
minute through the console and 20 per minute at the node; mining and explicit
synchronization allow 10 per minute. A reverse proxy should enforce an
additional outer limit and connection cap.
