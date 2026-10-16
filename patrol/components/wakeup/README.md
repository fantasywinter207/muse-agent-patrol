# Wakeup intent ownership

## Purpose
Use the existing SQLite as the sole durable source of wakeup intent, with strict idempotency and lease fencing.

## Responsibilities
Stage high-priority human_action and due promise intents, export a read-only feed, safely claim host dispatch attempts and record confirmed delivery/failure/unknown results.

## Non-responsibilities
This package does not implement a Muse host Hook, a Cron installer, model execution, CrewQuay writes, permission grants or task completion. Acknowledging host acceptance is not proof that Muse actually worked.

## Public boundary
`index.py:Wakeups`. Other components import only `wakeup.index`.

## Allowed dependencies
`persistence.index` and published SQLite tables.

## Invariants
One wakeup logical key is staged once. Leased delivery has owner/version/expiry. An expired attempt becomes `unknown`, never automatically retried; recovery requires proof of non-delivery. A blocked business work item may need attention but does not grant permission. Claiming intent never invokes an API.

## Testing boundary
Temporary databases, fake host operations only. No external credentials or live writes.
