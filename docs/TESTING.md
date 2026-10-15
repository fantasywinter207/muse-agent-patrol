# 隔离验收与证据
使用 Python 3.10+；Linux目标应另外记录精确Python、SQLite、文件系统/锁语义与依赖版本。
`python -m unittest discover -s tests -v` 运行公共行为、持久化、迁移、Outbox和多进程故障用例。
`python tools/validate_architecture.py` 递归检查结构与可检测的跨组件深导入。
测试仅使用临时数据库和fake传输，不读取Credential Manager、不连接CrewQuay，不发送真实消息或ACK。
Windows结果不能替代Linux flock/目录持久性验证。Worker独立结果记录在后续证据中，不能借用实现者日志。
测试数量不作为正确性结论；每项故障用例应引用具体命令、源码SHA和结果。
`tools/run_checks.py --output <目录>` 生成源码摘要、真实运行环境、命令退出码与原始日志。日志按 UTF-8 字节写入，报告 SHA-256 直接计算已写入文件；Windows 换行转换不得改变证据字节。下载 CI 附件后应逐个核对日志摘要。

## Quill 真实环境接入结果及下一步验证边界

2026-10-15 的 Quill 只读联调报告：Python 3.12.3 / Linux / SQLite 3.45.1 / DELETE，旧 cron 每 10 分钟仍跑 v2，v3 未安装调度，Hook 为零；第一次受 20 次 API 请求和 50s 预算限制，`deferred`，约 710 历史事件排队；次轮 inspector 有进展但 `human_action` 尚不能自主执行。本仓库不应据此宣称线上唤醒成功。

新增隔离回归覆盖：新消息优先于 inbox 历史拉取、inbox 指针经 REST 二次验证、老消息不重新触发 human_action、无法映射时 blocked、只读 `export-pending`、明确 local authorization 的原子领取及过期租约保护。上线前应针对真实 inbox 消息字段验证映射、限制访问次数，确认 `partial/deferred` 不会被外部调度器当作“全部完成”。

独立 Muse executor 的端到端验收需要实际批准的唤醒入口、操作者授权、本地领取、可验证行动证据、租约确认，方能把 human_action 标成 done。没有该证据只能报告基础设施离线测试通过。
