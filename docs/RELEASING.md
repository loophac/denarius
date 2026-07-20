# Releasing Denarius

## Prepare the release

1. Start from a clean `master` branch with all GitHub checks passing.
2. Update `project.version` in `pyproject.toml` and add the dated release entry
   to `CHANGELOG.md`.
3. Confirm whether consensus, network, peer API, database, or wallet formats
   changed and document any migration or reset requirement.
4. Confirm the release still identifies itself as a testnet. Creating or naming
   a mainnet requires every launch gate in `docs/THREAT_MODEL.md` and an
   independently reviewed production genesis procedure.
5. Install the pinned development environment with
   `python -m pip install -r requirements-dev.txt`.
6. Run `python -m pytest`, `python benchmarks/long_chain.py --blocks 1000`,
   and `python -m build`.
7. Install the wheel from `dist` in a clean virtual environment and run
   `denarius --help`, `denarius-node --help`, `denarius-console --help`, and
   `denarius-admin --help`.

## Publish the release

1. Merge the prepared pull request after the `Release quality` check succeeds.
2. Create an annotated tag matching the package version, such as `v0.5.0`.
3. Create a GitHub release from that tag and attach the wheel and source archive
   produced by the `Build package` CI job.
4. Use the matching changelog entry as the release notes and clearly repeat the
   educational-software warning.
5. Start the released package with fresh temporary databases and complete one
   administrator setup, wallet, mining, standard-user, restart, and peer-sync
   smoke test before announcing the release.

Publishing to a public Python package index is intentionally not automated.
Add trusted publishing and a separate protected release environment before
introducing that distribution channel.
