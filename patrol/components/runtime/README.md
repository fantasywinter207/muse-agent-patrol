# CLI与有界tick组合

## Purpose
CLI与有界tick组合。

## Responsibilities
稳定文件锁、预算次序、窄回调连接、能力不足blocked、状态/doctor。

## Non-responsibilities
不安装服务、不部署、不托管LLM、不替成员claim/approve。

## Public boundary
`index.py` 导出 main/Tick/instance_lock。外部代码不得深导入私有实现。

## Allowed dependencies
仅兄弟组件index；runtime内部composition/process私有实现。

## Invariants
先执行到期工作再批量采集；默认只读；所有旧入口转此边界。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。

