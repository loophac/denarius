# Contributing to Denarius

Thank you for helping improve Denarius. The project is an educational network,
but changes should still preserve deterministic consensus behavior, private-key
safety, and a clear user experience.

## Development setup

Use Python 3.10, 3.11, or 3.12.

```bash
python -m venv .venv
```

Activate the environment, then install the application and development tools:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Windows, activate it with `.\.venv\Scripts\Activate.ps1`; on Linux and macOS,
use `source .venv/bin/activate`. You can also invoke the environment's Python
directly at `.venv\Scripts\python.exe` or `.venv/bin/python`.

Run the complete suite before opening a pull request:

```bash
python -m pytest
python -m build
```

The integration tests start real node and console processes on temporary local
ports. They must pass on both Windows and Linux and must leave no processes or
state files behind.

## Change guidelines

- Keep Denarius as the network name, Denarii as the currency name, and `DEN` as
  the currency notation.
- Use integer atomic units for consensus accounting. Never introduce floating
  point values into signed transactions, block headers, balances, or rewards.
- Treat canonical serialization, block validation, proof of work, monetary
  policy, and transaction validation as consensus-critical code.
- Bump the consensus protocol and network identifiers when a change makes old
  blocks or transactions incompatible. Add rejection tests for the old or
  malformed form.
- Bump the peer API version or capabilities when networking behavior changes
  without changing consensus.
- Keep private keys encrypted outside the short-lived signing operation. Never
  log passwords, wallet documents, setup codes, session secrets, or admin
  tokens.
- Preserve account role checks on the server; hiding a control in the browser
  is not authorization.
- Add focused regression tests for every behavior change and process-level
  coverage when a change crosses service or persistence boundaries.

## Pull requests

Keep each pull request focused. Explain user-visible behavior, protocol or
storage compatibility, test evidence, and any migration or chain-reset impact.
All `Release quality` checks must pass before merge, review conversations must
be resolved, and security reports must follow [SECURITY.md](SECURITY.md) rather
than a public pull request.

The release process is documented in [docs/RELEASING.md](docs/RELEASING.md).
