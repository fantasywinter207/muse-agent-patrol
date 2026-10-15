# SQLite事务、schema和一致性备份

## Purpose
SQLite事务、schema和一致性备份。

## Responsibilities
物理持久化；公开表schema；原子页事务与失败回滚。

## Non-responsibilities
不执行网络、副作用或模型；不解释承诺正文。

## Public boundary
`index.py` 导出 Database/Busy/StaleLease/canonical/cursor/new_id。外部代码不得深导入私有实现。

## Allowed dependencies
Python标准库；业务组件通过此公开边界参加同一事务。

## Invariants
磁盘/忙/提交错误不能推进游标；默认DELETE，WAL要求已修复版本。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。
