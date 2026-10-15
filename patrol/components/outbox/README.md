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

