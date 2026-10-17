# Social candidate admission

## Purpose
Validate a platform-provided, actor-bound read-only social opportunity page
and atomically map bounded candidates onto the existing SQLite Scheduler.

## Responsibilities
Strictly validate protocol, type, resource ID, action name and untrusted
metadata. Admit at most three per call, with rolling daily, per-target,
cooldown and active-activity checks inside the same SQLite transaction
as schedule registration. Persist IDs and action names only.

## Non-responsibilities
No network access, model, host wake, remote send, content storage or
permission grant. The registered activity remains subject to Scheduler
lease fencing and separate Outbox policy.

## Public boundary
`index.py:SocialOpportunities`; parent component re-exports this boundary.

## Dependencies
Only the published persistence index and the documented common SQLite
transaction tables. No private scheduler/queue/host imports.
