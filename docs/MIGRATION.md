# v3 历史数据迁移、备份和回滚

**这份文档仅服务于历史状态转换，不定义新的定时器。** 当前运行入口始终是原有 60 秒 HATCH Hook，调用本仓库的 `python -m patrol` CLI；旧 `poll.py`、`watchdog.py`、`ws_watch.py` 等根目录兼容脚本不再分发，但历史源码仍可从 Git 查询。新安装不要恢复旧版本作为并行运行入口。

## 历史导入前提

1. 由宿主操作者核实旧实例已全部停止，保存旧状态/源码 SHA/运行配置、活跃游标、Outbox 与未知送达记录的私有备份；**不要为了导入关闭已经正常工作的 HATCH Hook**。
2. 只在明确规划好的隔离状态库中导入历史 `state.json`、`ws_pending.jsonl`、`promises.jsonl`、`patrol_runs.jsonl`；不把密钥和生产 DB 放进仓库。
3. 新运行必须使用单一 SQLite；严禁旧 JSON writer 与新 SQLite writer 并行。

## 在已确认的迁移窗口操作

```bash
python3 -m patrol --config /private/config.json migrate /private/legacy-directory --confirm-stopped
python3 -m patrol --config /private/config.json status
python3 -m patrol --config /private/config.json tick --offline
python3 -m patrol --config /private/config.json backup /private/backup-v3.sqlite3
```

导入对每个来源保留 SHA、原始内容和故障行；重复导入幂等。旧未确认 ACK、无法验证的承诺必须维持待对账/blocked，不允许伪造完成。历史活跃期不能仅靠导入被自动延长。

## 数据备份和回退

- 使用 SQLite backup API 而不是热拷贝活动数据库文件；包括 WAL 中已提交事务，备份先做完整性检查。
- 回滚前先安全停止**目标 Agent** 的写执行器，按原 `requestId` 对账所有 sending/unknown Outbox；不换逻辑键重发，不重置游标。
- 若要切回旧源码，必须按实际数据库/schema 状态逐项审计；**默认不支持**用旧 JSON 覆盖 SQLite 或恢复旧 watcher/cron。
- 已验证的原 HATCH Hook 仍是唯一外部周期入口；宿主启动/停用由运维操作者在真实环境执行，不由 GitHub 文件隐式完成。

当前架构见 [README](../README.md)，宿主核验见 [HATCH 60 秒 Hook](HOST_60S_RECOVERY.md)。
