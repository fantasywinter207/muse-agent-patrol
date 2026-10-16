# 巡检责任根

## Purpose
本包拥有 CrewQuay v3 巡检客户端的组合、生命周期与公开 CLI。

## Responsibilities
组合持久化、采集、工作队列、截止时间感知的活动调度、关注、承诺、Outbox、迁移和运行入口；输出有证据的结果。
所有持久时间为 UTC epoch 秒；所有远端 cursor 为精确十进制字符串。

## Non-responsibilities
不托管模型、不自动生成回复、不自动正式claim、不批准任务、不安装系统服务或切换线上。
没有合法执行器的工作进入 blocked，不能用登记或打印代替执行。

## Public boundary
`index.py:main`；`python -m patrol` 是薄语言适配器。根目录旧脚本也是适配器。
SQLite 是唯一活动状态源；旧 JSON 只作为显式迁移输入。

## Allowed dependencies
根仅依赖 components 下的公开 index 边界，组件依赖在各 README 声明。
数据库公开事务 schema 是共享持久化契约，禁止注入含凭据、网络和所有子系统的广义host/context。

## Invariants
发现内容不可信；采集不会从正文生成写操作。默认禁用远端写。
本地队列领取与平台正式assignment完全独立。工作完成和外部结果未知必须分别记录。
