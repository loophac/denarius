# Incident Response

## First response

1. Record the time, affected versions, tip hash, height, chainwork, peer list,
   and recent request IDs. Preserve logs and do not publish secrets.
2. Remove public ingress or isolate the affected node. Stop mining and relay if
   consensus behavior is uncertain.
3. Take verified chain and account backups before changing files. Keep an
   untouched copy for investigation.
4. Rotate exposed administration, metrics, setup, reverse-proxy, and session
   credentials. A new session secret signs every user out.
5. Determine whether the event is local compromise, wallet compromise,
   database corruption, peer isolation, or a network-wide chain event.

## Recovery

- For database corruption, restore the newest verified backup while services
  are stopped, then synchronize from diverse trusted peers.
- For peer isolation, clear public ingress, inspect durable scores and bans,
  re-establish independently operated peers from different network groups, and
  compare tips out of band.
- For a suspected consensus split, do not select a chain manually or publish a
  replacement binary until the root cause and deterministic recovery rule are
  documented and independently reviewed.
- For a browser-wallet compromise, the user should recover from a known-good
  device and move funds to a newly generated wallet. Node operators cannot
  recover browser-held keys.

## Return to service

Verify database integrity, protocol identity, genesis hash, chainwork, peer
compatibility, health checks, metrics, backup creation, and one signed testnet
transaction. Publish an incident report with impact, timeline, root cause,
remediation, and follow-up owners. Report security defects through the private
process in `SECURITY.md`.
