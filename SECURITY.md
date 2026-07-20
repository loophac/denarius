# Security Policy

Denarius is educational testnet software. It has not received an independent
security audit and must not be used to custody funds with real-world value.
Waitress serving and the documented hardening controls do not replace the
independent review and launch gates required for production.

## Supported versions

Security fixes are made on the latest release line only.

| Version | Supported |
| --- | --- |
| 0.5.x | Yes |
| Earlier versions | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's
[private vulnerability reporting form](https://github.com/loophac/denarius/security/advisories/new)
and include:

- The affected commit or release.
- Reproduction steps or a proof of concept.
- The expected and observed behavior.
- The likely impact, including whether keys, funds, consensus, or node control
  are affected.
- Any mitigation you have already identified.

Reports are handled on a best-effort basis. Please allow time to reproduce and
coordinate a fix before publishing details.

## Sensitive data

Never include private keys, wallet passwords, administration tokens, setup
codes, session secrets, production database files, or unredacted `.denwallet`
files in a report. Generate disposable test data when a reproduction requires
wallet or account material.
