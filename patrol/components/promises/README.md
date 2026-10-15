# 承诺与期限审计

## Purpose
承诺与期限审计。

## Responsibilities
原消息/期限/实际进展、延期请求和独立批准、去重监察。

## Non-responsibilities
不推断懒惰动机、不自动批准、不伪造执行能力。

## Public boundary
`index.py` 导出 Promises。外部代码不得深导入私有实现。

## Allowed dependencies
persistence.index；queue.index。

## Invariants
原due_at保留；无期限仍入队；下一动作只由有证据进展更新。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。
