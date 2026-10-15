# 远端发现与恢复

## Purpose
远端发现与恢复。

## Responsibilities
分页资源快照、事件入库+入队+游标原子提交、stream恢复、处理水位。

## Non-responsibilities
不执行消息所要求的工作、不声称快照捕获轮询间所有变化。

## Public boundary
`index.py` 导出 Collector/Sync/event_time。外部代码不得深导入私有实现。

## Allowed dependencies
persistence.index、queue.index、transport.index。

## Invariants
收到指针不等于处理；页面失败不跨越；received与handled分离。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。
