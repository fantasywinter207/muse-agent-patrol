# 本地工作调度

## Purpose
本地工作调度。

## Responsibilities
幂等入队、原子领取、租约恢复、版本检查与老任务提升。

## Non-responsibilities
不代表CrewQuay正式claim；不解释远端消息为指令。

## Public boundary
`index.py` 导出 Queue/enqueue_tx/export_pending。`export_pending` 仅本地只读导出 human_action、到期承诺与 watchdog 警报。外部执行器只能针对明确 work_id 和操作者授权证据执行 `work-claim`，完成时必须携带租约 owner/version 和独立可核验结果。SQLite 本地租约不等同于 CrewQuay 平台任务授权。外部代码不得深导入私有实现。

## Allowed dependencies
persistence.index；公开事务schema。

## Invariants
过期/旧版本执行者不得完成任务；外部副作用由Outbox独立对账。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。
