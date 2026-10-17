# Host handoff boundary

## Purpose

Allow the existing HATCH Hook to reserve a **durable** pending Wakeup
intent before issuing `wake()`, and report Scheduler advice without
calling or leasing the Muse model. This is an incremental host integration
of the existing SQLite Wakeup and Phase 6.1 Scheduler facilities, not a
replacement scheduler, additional timer, or second wakeup queue.

## Responsibilities

- `HostHandoff.prepare(owner,now)` reconciles local scheduled work and
  atomically reserves a due, still-relevant Wakeup intent. Concurrent Hooks
  cannot claim the same intent.
- Return only opaque IDs, lease generation/expiry and activity IDs. Never
  serialize message content, checkpoint text, access tokens or API secrets.
- `host-wakeup-prepare --owner HOST_WORKER` produces a JSON instruction.
  The **external Hook** alone calls the approved host's `wake()` function.
- Muse / host must acknowledge **verified delivery** using the existing
  `wakeup-ack --result accepted --evidence RECEIPT_JSON` after an actual
  host acceptance receipt is available. If no receipt is available, the
  leased attempt expires as `unknown` and must be reconciled with proof
  of non-delivery. It is not silently retried or marked accepted.
- Once awake, the real Muse worker uses existing `schedule-next`,
  `work-claim` (where appropriate), `schedule-assess`,
  `schedule-checkpoint`, `schedule-complete` and generation-fenced
  `stage`. A Hook reservation is **not** an activity time slice.


## Social handoff

`HostHandoff.prepare(owner,now,allow_social=True)` includes low-priority
admitted social activity by default **only** if no Scheduler activity is
running and the highest-priority eligible candidate is social. The same
durable `wakeup_requests` lease and `wakeup-ack` proof protocol apply.
No Scheduler time slice is claimed by this call; it neither starts nor
implicitly authorizes a Muse model. Identity-bound discovery uses the
existing transactional 30-minute throttle. Legacy callers can still pass
`allow_social=False` for an explicit work-only operation.

## Non-responsibilities

No model invocation, runtime installation, implicit work authorization,
host-acceptance fabrication, automated social message generation, or
assumption that a running model can be forcefully interrupted. Only the
host can confirm actual wake acceptance and run a model.

## Public boundary

`from patrol.components.host.index import HostHandoff`.

## Allowed dependencies

Only published `scheduling.index` and `wakeup.index` plus the supplied
SQLite capability; no private component imports or network clients.

## Invariants

A reserved wakeup intent is leased exactly once per generation and not
retransmitted after a crash without explicit absence evidence.
Scheduler activity lease owner/generation remain distinct from host wakeup
lease owner/version and from CrewQuay roles/assignments. Existing schema and
all older wakeup/Outbox receipts are retained. Social intents can already be
admitted to the shared durable wakeup queue, but the repository **does not**
install or activate an external Host `wake()` bridge. The reference Hook
skips `host-wakeup-prepare` unless its operator has explicitly verified
host wiring and sets `CREWQUAY_HOST_WAKEUP_WIRED=1`. An unwired bridge must
not consume leases or mark any activity accepted.

## Tests

Temporary SQLite tests exercise reservation arbitration, stale-source
retirement, deferred work, no-duplicate delivery and local-only scheduler
hints; no HATCH host or CrewQuay network access.
