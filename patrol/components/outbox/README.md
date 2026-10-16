# 远端动作持久化生命周期

## Purpose
远端动作持久化生命周期。

## Responsibilities
不可变逻辑动作/幂等键、发送前落库、未知结果隔离、证据对账。

## Non-responsibilities
不自动从聊天生成动作、不无条件重放未知POST。

## Public boundary
`index.py` 导出 Outbox。外部代码不得深导入私有实现。

## Allowed dependencies
persistence.index；显式注入can_send/send/verify窄能力。

## Invariants
同动作payload不可改变；崩溃恢复unknown；旧发送者token不能覆盖新结果。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。

## Phase 3: actor vs producer and logical lanes

`actor` is the authenticated CrewQuay platform principal; an executing worker or Hook never substitutes its own worker name. With an established DB `meta.actor` binding, Outbox refuses staging a different actor. The additive `outbox_producers` table persists the optional original `producer_id` for audit without rewriting any old request or its immutable payload/idempotency key. The CLI can derive actor from the verified DB binding and accepts `--producer-id hook-worker`; wrong `--actor` fails at staging instead of silently remaining prepared.

The existing Outbox is still the only action persistence store. Logical lanes are `chat` (send), `notifications` (ACK/read), and `any`; no task-thread API is invented. A bounded `outbox-drain --once` CLI uses the same published Executor capability as Full Tick. If a prepared action fails the current actor/scope gate it is not sent or rewritten; its next check is deferred, and later authorized work can continue. A successful POST is only confirmed after matching exact remote message ID, sender and text via REST. Timeout/ambiguous outcomes become unknown and are never blindly repeated.

When `stage --dispatch-now` is explicitly selected, the CLI first commits and releases the stable lock, then invokes a separate bounded Executor subprocess. The parent reads back the durable Outbox status before reporting `sent=true`, never trusts stdout or assumes a stage was delivery. External Hook can run a periodic `outbox-drain --once --lane chat` fallback without a periodic Muse model worker.
