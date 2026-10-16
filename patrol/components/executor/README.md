# Executor — authorization and remote delivery boundary

## Purpose
Deliver previously staged, explicitly authorized actions quickly without running Full Tick or waking a Muse model.

## Responsibilities
Verify the actual CrewQuay actor identity; enforce allowed chat scopes and existing ACK/read gates; claim a due Outbox record, send with an immutable idempotency key, confirm by exact remote ID/readback, and persist confirmed/unknown/failed outcomes.

## Non-responsibilities
Do not decide reply contents, raise permissions, infer platform actor from producer worker names, resend ambiguous POSTs, modify task threads without a separately approved adapter, schedule permanent services, or use a prompt to authorize a tool operation.

## Public boundary
`index.py:ActionSender`. `drain_once(limit,action_id)` is the bounded external executor entry point. Light Tick does not call it. Full Tick delegates the same send/check/verify capability to this component.

## Allowed dependencies
Published persistence, transport, collection, attention and Outbox index boundaries only.

## Invariants
The producer of an Outbox action is distinct from the verified CrewQuay actor; the executor must authenticate and match the bound actor before POST. Scope, idempotency, readback evidence, in-flight fencing, and unknown-result reconciliation must be preserved. Independent action IDs are never rewritten simply to evade an authorization block.

## Testing
Fake transport plus temporary SQLite. CI never calls CrewQuay endpoints or loads real credentials.

## Phase 4: Platform routes

`ActionSender` supports bounded `collaboration` and `tasks` lanes. All platform routes use explicit action-to-path mappings, typed payload validation and an authenticated actor, not arbitrary URL execution. Project writing requires a separate `allowed_project_action_ids` scope; claim/progress/submission/review/decision/artifact mutations also require `allowed_task_actions`.

Confirmed project/task/Room messages require an exact message ID/actor/text REST readback. Claim/progress check task version and assignee; submissions/reviews/decisions use server IDs and exact matching fields; artifacts compare binary SHA-256 to authorized server metadata. Failure to prove identity remains `unknown` without blind POST replay.
