# v3 迁移、备份与回滚

## 迁移前
1. 保存当前源码完整SHA、配置和调度说明。由操作者停止所有旧poll、watcher、cron巡检及watchdog；不要只停一个writer。
2. 对原state.json/ws_pending.jsonl/promises.jsonl/patrol_runs.jsonl做私有备份，保留损坏行；禁止带入credentials.json或.env。
3. 使用独立、全新v3数据库，默认关闭远端写。切勿在旧writer活动时混用JSON与SQLite。

## 导入与影子核对
```sh
python -m patrol --config /private/config.json migrate /private/legacy-directory --confirm-stopped
python -m patrol --config /private/config.json status
python -m patrol --config /private/config.json tick --offline
python -m patrol --config /private/config.json backup /private/backup-v3.sqlite3
```
每个文件保存SHA和逐条原始JSON记录；重复导入幂等。坏JSONL行会报告文件/行号并阻止整批导入，不悄悄跳过。
已存在活动同步游标时拒绝用不同输入覆盖。state.json的流/聊天/更新游标恢复；ws_pending作为待核验指针入队，不抢先推进handled。
pending ACK以及无已发布格式契约的旧promises完整保留并生成blocked迁移核对项。用原消息、期限与证据通过promise-add重建经确认的承诺，再显式resume核对项；不能把不理解的历史承诺自动标done。旧active窗口只保留输入，不盲续期；通过新的可靠互动重建关注。

## 备份
backup使用SQLite backup API并检查integrity_check，不覆盖已有目标。WAL中的提交也是持久数据，不可只拷贝打开的主db文件。原输入只读保留；不要把运行数据提交Git。

## 切换
影子运行只读采集及离线/受控验收，确认全部状态、承诺、积压和未知Outbox。只有操作者明确批准才安装/更改线上调度、停止/启用服务。当前交付不执行这些操作。
一次只允许一个真实写入执行器。根旧入口在新代码中是同一个v3 tick，common.py不再提供整份旧JSON写入函数。

## 回滚
1. 操作者停v3调度并确认无活动请求/进程；用backup创建新的完整证据快照。
2. 核对所有sending/unknown Outbox，保留原key和remote ID；结果未明前不得恢复旧发送路径。
3. 对照切换后的received/handled cursor、消息、承诺和ACK逐项对账。不可把切换前JSON直接覆盖回来，避免重复业务动作。
4. 若必须恢复旧源码，使用保留的Git提交和人工确认的状态映射；不能宣称自动无损降级。恢复期间保持远端写关闭。
此实现提供一致备份、可审计导入和显式恢复边界，没有“把SQLite自动反写成旧JSON并继续发送”的危险按钮。
