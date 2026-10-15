# 隔离验收与证据
使用 Python 3.10+；Linux目标应另外记录精确Python、SQLite、文件系统/锁语义与依赖版本。
`python -m unittest discover -s tests -v` 运行公共行为、持久化、迁移、Outbox和多进程故障用例。
`python tools/validate_architecture.py` 递归检查结构与可检测的跨组件深导入。
测试仅使用临时数据库和fake传输，不读取Credential Manager、不连接CrewQuay，不发送真实消息或ACK。
Windows结果不能替代Linux flock/目录持久性验证。Worker独立结果记录在后续证据中，不能借用实现者日志。
测试数量不作为正确性结论；每项故障用例应引用具体命令、源码SHA和结果。
`tools/run_checks.py --output <目录>` 生成源码摘要、真实运行环境、命令退出码与原始日志。日志按 UTF-8 字节写入，报告 SHA-256 直接计算已写入文件；Windows 换行转换不得改变证据字节。下载 CI 附件后应逐个核对日志摘要。
