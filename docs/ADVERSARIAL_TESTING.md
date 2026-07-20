# Adversarial Testing

Normal CI runs regression and process integration tests on Windows and Linux.
The separate adversarial job also runs generated Hypothesis properties, a
200-block benchmark gate, and the synchronization soak.

Run the same checks locally with:

```bash
python -m pytest
python benchmarks/long_chain.py --blocks 1000
DENARIUS_RUN_SOAK=1 python -m pytest tests/test_network_soak.py
```

Current coverage includes malformed transaction fuzz cases, amount and target
round trips, canonical serialization, indexed-state serialization, simulated
SQLite failure rollback, backup tampering, restart persistence, coinbase
maturity, deep candidate-chain validation, and network worker wakeup pressure.

Before mainnet, extend this into sustained multi-process relay and reorg soak
tests, mutation testing, coverage-guided native fuzzing of parsers, power-loss
testing on real filesystems, and independently operated testnet exercises.
