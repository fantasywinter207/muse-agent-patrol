# Social discovery and admission

## Purpose

Use CrewQuay's authenticated `GET /api/v1/agent/social/opportunities`
without hardcoding domain REST actions, generating content, or creating a
second timer/worker. The already installed Hook calls bounded
`social-sync --if-idle-due` by default after Light Tick, with a verified
actor and durable 30-minute cooldown. Light Tick itself remains strictly
event-only.

## Responsibilities

- Validate server protocol/version, locally verified actor identity and
  typed `post/circle` resources and advertised action names; reject a
  malformed page without writing partial rows.
- Admit at most **3** social activities per command, **6** per rolling
  24 hours globally, **2** per target per rolling 24 hours, and a
  **15-minute** per-target cooldown.
- Never admit another active activity for the same target (regardless of
  proposed action). Atomically reserve budget and insert into the
  existing `schedule_activities` table inside **one SQLite write txn**.
  All existing Scheduler precedence/preemption/lease fencing applies.
- Return resource IDs and action names; do not persist titles, previews,
  user-authored content, self-declared interests or private profiles.
  The public `social-detail ACTIVITY_ID` command maps an existing
  activity to an immutable resource/action pointer.

## Public boundary

`from patrol.components.social.index import SocialOpportunities`.
Other components may import this index only.

## Allowed dependencies

Only published persistence and nested discovery public indexes; no private
runtime, scheduler or executor imports. Scheduler activity state is shared
through the documented SQLite transaction schema, not a second queue.

## Non-responsibilities

No LLM, automatic wake, remote POST, implicit agent-account credentials,
circles autojoin, security grant, social-message content generation or
claims that API catalog entries permit remote mutations. The same
`wakeup_requests` now also includes admitted social activities. The
external Muse worker must still take `schedule-next` and use generation-
fenced `stage platform_action` for the registered resource/action,
respecting CrewQuay's authoritative permissions and the existing global
write gate. A discovered social activity can be completed only after all its staged
Outbox effects have durable `confirmed` receipts. With no staged remote
action, Muse may explicitly skip the opportunity using
`{ "decision": "skipped", "reason": "..." }` evidence; an empty or
unverified `done` claim is rejected.

## Invariants and testing

No stale activity may be auto-replayed after termination. A running or
suspended activity is never reset by rediscovery. Local cooldown and
admission budgets are **selection bounds**, not a substitute for
future network-level anti-spam and outbox stage validation. Default-on
**discovery and wake** do not themselves send messages; the host must
actually run a Muse worker, outbound writes need the existing global
write permission plus platform authority, and live host/anti-loop behavior
must still be verified before broad production rollout.

Tests use fake HTTP, temporary SQLite and real Scheduler dispatch,
without credentials or external writes.
