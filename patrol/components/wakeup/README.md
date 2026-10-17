# Wakeup intent ownership

## Purpose
Use the existing SQLite as the sole durable source of wakeup intent, with strict idempotency and lease fencing.

## Responsibilities
Stage high-priority human_action and due promise intents, export a read-only feed, atomically claim the next due host dispatch attempt (claim_next), and record confirmed delivery/failure/unknown results.

## Non-responsibilities
This package does not implement a Muse host Hook, a Cron installer, model execution, CrewQuay writes, permission grants or task completion. Acknowledging host acceptance is not proof that Muse actually worked.

## Public boundary
`index.py:Wakeups`. Other components import only `wakeup.index`.

## Allowed dependencies
`persistence.index` and published SQLite tables.

## Invariants
One wakeup logical key is staged once. Leased delivery has owner/version/expiry. An expired attempt whose business source still needs attention becomes `unknown`, never automatically retried without proof of non-delivery. An expired `leased` attempt whose work was already completed (or whose promise is closed/rekeyed) is atomically marked `obsolete`, not left indefinitely `leased`; active leases and acknowledged deliveries are not cancelled. Light and Full ticks both run this reconciliation. A blocked business work item may need attention but does not grant permission. Claiming intent never invokes an API.

## Testing boundary
Temporary databases, fake host operations only. No external credentials or live writes.
