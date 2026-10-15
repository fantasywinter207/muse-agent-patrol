# 旧状态迁移

## Purpose
旧状态迁移。

## Responsibilities
显式停写确认、输入哈希、逐文件幂等导入、未知承诺保留与待核对。

## Non-responsibilities
不停止线上进程、不恢复陈旧JSON、不丢弃坏行。

## Public boundary
`index.py` 导出 migrate_legacy。外部代码不得深导入私有实现。

## Allowed dependencies
persistence.index、queue.index。

## Invariants
原文件不改动；坏行先报告且整批不导入；既有活动游标拒绝覆盖。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。
