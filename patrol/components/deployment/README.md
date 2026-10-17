# Platform-native deployment handshake

## Purpose
Validate the native CrewQuay SYN → platform SYN-ACK → ACK control protocol while keeping host Hook acceptance a separate externally evidenced fact.

## Responsibilities
- Store actor-bound, bounded deployment runs, challenge receipts, expired states and one deduplicated Bug report.
- Admit human work into the existing Queue and Scheduler; reconcile already committed Agent Event Feed pointers in read-only 60s Light Ticks.
- Stage strict native SYN/ACK/Bug actions through existing Outbox, generation-fenced by work claims.
- Expose sanitized status without a challenge or secret.

## Non-responsibilities
No model execution, host installation, arbitrary HTTP dispatch, another timer, proof that manually invoked Tick was a natural HATCH tick, or automatic elevation of deployment status based solely on server protocol_verified.

## Public boundary
`from patrol.components.deployment.index import Deployment`. CLI `deployment-start`, `deployment-status`, `deployment-stage-syn`, `deployment-stage-ack`, `deployment-stage-bug`, `deployment-reconcile`.

## Allowed dependencies
Only public `persistence.index`, `queue.index` and `outbox.index` interfaces. Runtime and Collection invoke this component's public boundary; it owns no host scheduler.
