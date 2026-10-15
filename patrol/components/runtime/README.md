# CLI与有界tick组合

## Purpose
CLI与有界tick组合。

## Responsibilities
稳定文件锁、预算次序、窄回调连接、能力不足blocked、状态/doctor。

## Non-responsibilities
不安装服务、不部署、不托管LLM、不替成员正式claim/approve。`export-pending` 不唤醒模型；`work-claim`/`work-finish` 只是携带本地租约版本的显式操作，不代替服务端审批。

## Public boundary
`index.py` 导出 main/Tick/instance_lock。外部代码不得深导入私有实现。

## Allowed dependencies
仅兄弟组件index；runtime内部composition/process私有实现。

## Invariants
缓存有已核实 actor 身份时，必须先用本地预算执行到期任务，再进行任何远端身份校验/分页，避免网络 BudgetExceeded 抢走本地 inspect_event 的执行机会；没有核实身份不能默默将消息标记为 done。默认只读；所有旧入口转此边界。全局游标未到期时，独立到期的消息或项目游标也必须唤醒；分页或预算未覆盖全部资源时返回 partial，不得虚报 ok。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。

## Phase 2 light tick
`Tick.run(light=True)` 是现有 runtime 的模式，不增加第二个调度中心。以浅分页和请求时间预算优先检查已缓存的聊天，跳过历史全量任务/项目同步与远程 Outbox 发送。全量 `tick` 保持既有恢复职责；Light 与 Full 共享 SQLite、游标和文件锁。当前只实现尽力而为，空闲聊天的 600 秒扫描间隔以及初次历史数据积压仍可能影响发现时延。
