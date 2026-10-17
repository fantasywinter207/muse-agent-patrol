# Social discovery and admission

## Purpose

Use CrewQuay's optional `GET /api/v1/agent/social/opportunities`
without hardcoding domain REST actions, generating content, or creating a
second timer/worker. A **manual or explicitly scheduled external**
`social-sync` command is the only producer for this first iteration.
The regular Light Tick deliberately remains event-only; it does not scan
the social endpoint.

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

## Non-responsibilities

No LLM, automatic wake, remote POST, implicit agent-account credentials,
circles autojoin, security grant, social-message content generation or
claims that API catalog entries permit remote mutations. Existing
`wakeup_requests` is work/promise-only; social scheduling does not
by itself wake the host. The external Muse worker must explicitly take
`schedule-next` and use generation-fenced `stage platform_action`,
respecting CrewQuay's current authorization and the Executor's local
allow-list. The single successful social activity is then completed
with the ordinary `schedule-complete` evidence.

## Invariants and testing

No stale activity may be auto-replayed after termination. A running or
suspended activity is never reset by rediscovery. Local cooldown and
admission budgets are **selection bounds**, not a substitute for
future network-level anti-spam and outbox stage validation. Production
automatic social posting remains **disabled** pending an explicit
social-action send budget/confirmation contract and live host tests.

Tests use fake HTTP, temporary SQLite and real Scheduler dispatch,
without credentials or external writes.
