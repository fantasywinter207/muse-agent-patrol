# 目标关注窗口

## Purpose
目标关注窗口。

## Responsibilities
唯一互动续期、有限自身回复、冷却与重启恢复。

## Non-responsibilities
不调用模型；未完成工作不无限续期。

## Public boundary
`index.py` 导出 Attention。外部代码不得深导入私有实现。

## Allowed dependencies
persistence.index；config公开配置。

## Invariants
旧事件和未来漂移不续期；长停机直接idle；重复事件消费一次。可激活事件年龄必须覆盖正常轮询间隔及调度余量，避免 10 分钟轮询遇到 5 分钟新鲜度窗口的漏激活。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。
