# Network Security

Denarius Testnet v3 uses one outbound peer transport per node. Plain HTTP is
the local-development default. HTTPS is the authenticated and encrypted
transport for any peer traffic that crosses an untrusted network:

```bash
denarius-node --peer-scheme https --advertise-address node.example.org:443
```

HTTPS requests use the operating system certificate trust store and certificate
verification remains enabled. Do not disable verification or use self-signed
certificates without installing a private certificate authority on every node.
The node itself should remain behind a TLS-terminating reverse proxy; Waitress
does not terminate TLS.

## Peer policy

- Protocol, network, genesis, peer API, and capability checks happen before
  synchronization or relay.
- Relay work is bounded to eight workers and 64 queued jobs by default.
- Repeated failures and invalid behavior add peer score. A score of 100 creates
  a durable one-hour ban persisted in the chain database.
- Peer gossip accepts at most eight addresses from one peer, only accepts IP
  literals, and limits discovered peers to four per IPv4 /16 or IPv6 /32 group.
- Public peers cannot advertise private or loopback addresses.

These controls reduce trivial flooding and eclipse attempts; they do not prove
Sybil resistance. A future production network still needs independently
operated bootstrap sources, measured autonomous-system diversity, and a review
of whether public-PKI TLS or a purpose-built authenticated peer protocol is the
right long-term transport.

## Deployment boundary

Expose only the reverse proxy publicly. Restrict `/metrics` to the monitoring
network and set `DENARIUS_METRICS_TOKEN`. Keep the node administration token
between the console and node. The browser must never receive either token.
