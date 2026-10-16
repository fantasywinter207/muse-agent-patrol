# Wakeup implementation

## Purpose
Provide atomic SQLite-backed intent lifecycle.

## Responsibilities
Staging, read-only export, leased claims, host acceptance receipts, unknown reconciliation.

## Non-responsibilities
No external calls, no model invocations, no authorization promotion.

## Public boundary
`index.py` exports Wakeups; other owners use `wakeup.index`.

## Allowed dependencies
`persistence.index`.

## Invariants
No repeat dispatch for a possibly-delivered request; no false success claims.
