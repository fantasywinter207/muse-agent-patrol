# Phase 6.1 — Cooperative Activity Scheduler

## Purpose
Dispatch *local Muse activities* with deadline-aware weighted fairness and safe-point preemption. A schedule activity is NOT a CrewQuay platform task assignment, an external model invocation, or an Outbox action.

## Responsibilities
- Maintain durable activity states (ready, running, suspended, completed, cancelled), runtime-based weighted virtual fairness and bounded waiting-time aging.
- Derive candidate priority dynamically from task urgency, wait duration, service consumed, and context-switch cost; use a finite time slice and hysteresis for preemption decisions.
- Persist preemption requests without killing model processes. A live owner voluntarily yields at a checkpoint boundary; suspension and resumed leases increment a fencing generation.
- Import actionable local `human_action` work by stable work ID, without claiming, completing, or granting access to CrewQuay tasks.
- Expired local leases become resumable with explicit reconciliation warning. Neither scheduling nor suspension mutates a remote side effect.

## Non-responsibilities
No model wakeup, OS process cancellation, forced LLM interruption, platform API calls, dynamic permission elevation, automatic approval, deletion of existing Outbox, rollback of remote operations, or claim of token-level model-state recovery.

## Public boundary
`from patrol.components.scheduling.index import Scheduler`: `register`, `sync_work`, `assess`, `dispatch`, `checkpoint`, `complete`, `cancel`, `snapshot`. CLI `schedule-add`, `schedule-sync`, `schedule-assess`, `schedule-next`, `schedule-checkpoint`, `schedule-complete`, `schedule-cancel`, `schedule-status`. `stage --schedule-id --schedule-owner --schedule-generation` optionally fences a newly authored Outbox action atomically.

## Allowed dependencies
`persistence.index` shared SQLite transaction schema. Scheduling has no private dependency on Queue, Wakeups, Outbox, or Runtime. Runtime composes it through the public index.

## Invariants
At most one running activity per local DB (SQLite partial unique index). Only the current lease owner and generation can checkpoint/finish or start a fenced action. Preemption itself is a **request**, not an involuntary cancellation; a safe-point checkpoint fences the old generation. Fairness is based on actual wall-clock duration of the granted slice, not unverified user-reported model tokens. Restart after lease expiry preserves checkpoint and reports that already-staged external effects need their own Outbox readback. Project permissions and work-claim authorization are always separate.

## Testing boundary
Isolated temporary SQLite tests cover EDF urgency, weighted virtual runtime, aging, hysteresis, no competitor time slice, lease expiry, stale-generation rejection, exact Outbox stage fencing, atomic cooperative handoff, crash checkpoint recovery, idempotency and existing Phase 2–5 regressions. No credentials or live CrewQuay API.
