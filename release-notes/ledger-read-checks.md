# Acceptance-harness ledger checks use read transactions

## Changes for users

The acceptance harness no longer requests a writer reservation for its five read-only ledger
checks. These checks can read committed state while another connection holds only a SQLite
RESERVED writer lock. Exclusive locks and storage failures can still cause refusal. State is still
revalidated by the existing write/admission and transport-entry paths. This adds no MCP tools or
configuration and does not resolve the separately tracked intermittent accounting-refusal
investigations.

## Upgrade

No action required.

## Security

No security fixes in this release.
