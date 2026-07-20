# Branch Protection

GitHub branch protection is repository configuration and cannot be enforced by
a committed file alone. Configure a branch ruleset after the first CI run on
`master` exposes the `Release quality` status check.

## Recommended `master` ruleset

In **Settings > Rules > Rulesets**, create an active branch ruleset targeting
the default branch with these settings:

- Require a pull request before merging.
- Require at least one approval.
- Dismiss stale approvals when new commits are pushed.
- Require conversation resolution before merging.
- Require status checks to pass and select `Release quality`.
- Require branches to be up to date before merging.
- Block force pushes and branch deletion.
- Do not allow bypass for repository administrators during normal development.

The stable `Release quality` job depends on every Windows/Linux test matrix job
and the package build. Requiring that single status keeps the ruleset stable if
the supported Python matrix changes.
