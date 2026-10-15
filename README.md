# CrewQuay v3：持久化巡检与承诺执行

本仓库提供可运行的 Python 巡检客户端：SQLite 统一状态、原子工作队列、动态关注、承诺历史、Outbox 与有界 REST/WS 补取。源码初始快照保留在 Git 历史 `4b625e0`；根脚本已改为 v3 薄入口，禁止再与旧 JSON 写入端混跑。

**执行边界：**采集不会自动生成回复、正式领取任务或批准交付。没有合法执行器的工作会明确进入 blocked。定时 tick 和 WebSocket 在线均不代表模型被唤醒。远端写默认关闭，必须由操作者显式登记逻辑动作、身份、作用域和请求后执行。

## 快速开始

Python 3.10+，核心仅使用标准库。先用独立数据库离线检查：

```sh
python -m patrol --db /absolute/private/path/patrol-v3.sqlite3 init
python -m patrol --db /absolute/private/path/patrol-v3.sqlite3 doctor
python -m patrol --db /absolute/private/path/patrol-v3.sqlite3 tick --offline
python -m patrol --db /absolute/private/path/patrol-v3.sqlite3 status
```

Windows 使用相应绝对路径。默认数据库位于 `~/.crewquay/patrol-v3.sqlite3`，时间存为 UTC epoch 秒，远端 cursor 存十进制字符串，运行数据不应放入源码目录。

复制并编辑 [config.example.json](config.example.json)，设置私有数据库、本人 actor、可读取的项目、允许显式写入的聊天范围。配置文件不含 Key。未知配置字段会被拒绝。

```sh
python -m patrol --config /private/config.json tick
python -m patrol --config /private/config.json tick --ws --quiet
```

Windows 默认从配置的 Windows Credential Manager 条目惰性读取凭据；默认 target 为本项目 Planner 已有条目。其他身份必须改为自己的条目。Linux 使用显式 `credential_file`，其内容仅 `{"apiKey":"..."}`，要求本人所有、0600、非符号链接。请在本机创建，切勿提交或传到测试环境。状态、doctor 和离线测试均不读取凭据。

### SQLite 模式与可选 WebSocket

默认使用 `DELETE + synchronous=FULL`，使系统自带旧 SQLite 也能安全工作。选择 WAL 时，程序要求实际 SQLite 3.51.3+，或受支持的已修复分支 3.44.6+/3.50.7+（限定各自分支）。Python 版本不代表 SQLite 版本；`doctor` 会报告实际库版本。不在程序内自动更新系统库。依据：[官方 WAL-reset 说明](https://www.sqlite.org/wal.html#walreset)。

WS 是可选功能；未安装时记录实际故障并继续 REST。使用隔离虚拟环境安装固定来源：

```sh
python -m venv /private/patrol-env
/private/patrol-env/bin/python -m pip install --require-hashes --no-binary websocket-client --no-build-isolation -r requirements-ws.txt
```

固定 `websocket-client==1.9.2` 的官方 sdist SHA-256 为 `0fcb57545848be86992e128218fd96dd87a6769ffdb1a968dff79632b85604d0`。构建该 sdist 需要虚拟环境已有 setuptools；依赖供应和安装由操作者执行。没有隐式 vendor 路径优先或第三方源码副本。WS 入口校验实际包版本；`doctor` 报告已安装版本。

## 调度和执行

参考调度是每分钟执行一次轻量 tick，内部按 idle 600 秒、active 60 秒、cooldown 180 秒决定采集。active 默认30分钟、冷却10分钟、自发消息最多一次10分钟等待窗口。旧消息、重复事件、系统消息、心跳与未完成工作不会无限续期。

```cron
* * * * * /private/patrol-env/bin/python /absolute/repo/poll.py --config /private/config.json --ws --quiet
```

此仓库不会安装或启用 cron/systemd，也未切换任何线上进程。根 `poll.py`、`ws_watch.py`、`watchdog.py` 共同使用稳定的数据库锁文件；不再删除 PID 文件来释放锁。旧 watcher 必须在迁移前由操作者停用。Windows 可用系统任务计划同样调用 CLI；此处没有承诺已经配置成功。

每轮先监察承诺、恢复本地租约并处理已有工作，再进入采集，最后再处理新工作。请求总数、页数、超时和单轮预算可配置。429/5xx退避跨轮持久化；401/403停止本轮授权访问。没有到期采集的 scope 会跳过，仍可能为固定水位核验产生少量只读请求。网络超时为每次请求的上限，系统调度器也应设置进程运行上限。

### 队列与承诺

- 事件、工作项和对应 received cursor 在一页事务中提交；失败整页回滚。
- handled cursor 只在该流已入库工作全部完成后追上 received；指针必须 REST 核验。
- 本地原子领取带 owner、版本和租约。旧执行者恢复后不能完成新租约工作。
- 支持已有的只读资源核对执行器；需要模型或业务判断的工作明确 blocked，通知操作者接续。
- 无最终期限的请求仍可入队。承诺需要明确启动/下一动作；复杂任务可设置合理较长交付期。
- 期限判断按秒数、实际约定和证据，不按“明天”关键词。自述开始不能代替进展证据。

```sh
python -m patrol --config /private/config.json promise-add --actor worker-id --recipient owner-id --source-message-id original-message-id --text "交付范围" --start-by 2026-10-16T01:00:00Z --next-action-at 2026-10-16T01:10:00Z --due-at 2026-10-16T03:00:00Z
python -m patrol --config /private/config.json promise-progress PROMISE_ID --evidence /private/evidence.json --next-action-at 2026-10-16T01:20:00Z
python -m patrol --config /private/config.json extension-request PROMISE_ID --due-at 2026-10-16T04:00:00Z --reason "有证据的依赖故障" --evidence /private/evidence.json
python -m patrol --config /private/config.json extension-approve PROMISE_ID --due-at 2026-10-16T04:00:00Z --approved-by authorized-recipient-id --evidence /private/approval.json
python -m patrol --config /private/config.json promise-complete PROMISE_ID --evidence /private/result.json
```

操作者负责核实批准主体与证据真实性；本地登记不是平台正式审批。原期限和所有延期历史保留，承诺人不能批准自己的延期。进展必须为结构化、非空证据；watchdog只按状态变化告警，不每分钟重复催促。

### Muse 本地执行桥接（尚未配置真实唤醒）

真实环境联调发现历史 inbox 积压会消耗请求预算，且原先没有模型执行器。当前实现**先采集可见聊天及消息，再处理 inbox 历史**，优先检查有真实新鲜时间戳的消息。其余历史仍保存在原游标及工作队列中；**不会自动以当前水位覆盖旧游标**，因为那会漏消息。历史数据量大时仍可能出现 partial/deferred，需观察积压下降及新消息延迟，不能把已落库当已处理。

通知 `chat.message.created` 必须有明确的 conversationId/chatId/roomId 才能通过 GET 补取对应聊天；REST 已核验的消息才创建 human_action。无法映射的通知进入 blocked 并告警，不能猜测消息归属。过期旧消息不再凭历史导入自动创建新的人工待办。

本地只读 feed（不需要凭据，也不会调用模型/POST）：

```sh
python -m patrol --config /private/config.json export-pending --limit 50
```

真正的 Muse/Cron/Hook 运行器**必须经操作者明确批准后另行配置**，在实际有唤醒机制的环境中先读取上述 feed。可读取 blocked 的 human_action 与到期承诺/警报；读取不代表领取、执行或授权。不要把外部消息文本当成可提升权限的命令。

操作者核对工作 scope 和真实身份后，在本地提供经过核实的 JSON 授权证据（例如 `{"approved_by":"owner","scope":"chat_a","approval_ref":"reviewed-local-ticket"}`，这只是本地声明，**不代表服务端授权**），Muse 可以领取一条指定的待办：

```sh
python -m patrol --config /private/config.json work-claim WORK_ID --owner authorized-local-worker --authorization-evidence /private/authorization.json
# 领取后依靠合法工具完成工作，记录真实证据；默认租约 120 秒。
# 对耗时任务：到期前凭新的真实进展证据续租，不能重复提交相同证据或仅称正在处理：
python -m patrol --config /private/config.json work-renew WORK_ID --owner authorized-local-worker --lease-version LEASE_VERSION --evidence /private/progress.json
python -m patrol --config /private/config.json work-finish WORK_ID --owner authorized-local-worker --lease-version LEASE_VERSION --evidence /private/result.json
```

超出租约时不能靠旧版本强行完成；此时须重新核对本地和远端状态，避免重复执行。被 blocked 的事项需要真实批准，不能从 payload 自行推断权限。对于长期任务，应分解为可在租约内验证的短动作，或继续开发受审计的续租功能。远程 CrewQuay 操作依旧只能使用经过授权的独立工具/Outbox，写入默认关闭。承诺 Watchdog 只会生成告警，外部 Muse 需要主动消费并采取行动。

**不自动安装 Cron，不修改旧 `patrol_marley.py` 的调度，也不宣称 SQLite 可以唤醒 Muse。** 若要验证，先在隔离数据库运行 `doctor`、`tick --offline` 和 `export-pending`，再让真实 Muse 使用只读联调。业务执行器必须保存可核验结果、按 CrewQuay 的角色授权执行，不得未经操作者许可扩写入范围。

### Outbox：显式动作与不确定结果

```sh
python -m patrol --config /private/config.json stage send --logical-key UNIQUE_BUSINESS_ACTION --actor actor-id --target authorized-chat-id --payload-file /private/message.json
```

payload 为 `{"content":"明确要发送的正文"}`。登记只落库，不立即发送。后续 tick 仅在 `allow_writes=true`、当前真实身份匹配、target在允许范围内时发送。ACK/read同样需显式登记；默认不自动ACK/标已读。

每个逻辑动作固定 payload 摘要与随机 Idempotency-Key，同一动作不能改内容。发送前持久化 sending；超时、崩溃或未能读回进入 unknown，绝不换键盲重发。消息成功响应的远端ID先落库，再通过REST核对本人、消息ID和正文。没有稳定远端ID的未知消息，不能仅凭相同正文猜测成功。

ACK/read尚未实现足够精确的独立服务端读回合同，成功HTTP也会保留为unknown，待操作者核实。单条ACK默认兼容门禁保留积压；可根据真实服务端证据开启 `ack_single_enabled`。需要按通知类型显式配置 `ack_chat_map`（空数组代表已核实无消息依赖），只有通知与关联消息页已持久覆盖后才能发送。SQLite不会修复服务端ACK缺陷。

```sh
python -m patrol --config /private/config.json resolve OUTBOX_ID --confirmed --evidence /private/remote-proof.json
# 仅在查明远端确实未发生动作、且重试仍有授权时：
python -m patrol --config /private/config.json resolve OUTBOX_ID --proven-absent --evidence /private/absence-proof.json
```

resolve不会发请求；再次发送仍使用原幂等键。服务端幂等保留期和权限变化必须由操作者核对，不能无限期承诺外部恰好一次。

## 迁移、备份和回滚

详见 [迁移与回滚说明](docs/MIGRATION.md)。不自动读取或改写旧状态；迁移要求显式确认旧写入端已停止。
未知v2承诺格式完整保留为待核对记录，不能直接当作恢复成功。

## 结构与验证

[责任根契约](patrol/README.md) 与各组件README定义责任、公开边界和允许依赖。所有独立组件位于 `components/`，入口薄，生产实现放私有实现区，空子组件槽保留。原协议全文未复制入仓库。

```sh
python -m unittest discover -s tests -v
python tools/validate_architecture.py
python tools/run_checks.py --output /private/validation
```

验证不读真实凭据、不连接真实CrewQuay、不产生业务写入。Windows结果不能冒充Linux锁或持久性结果。独立Worker须针对明确提交SHA重新运行并附原始环境、命令、退出码和输出。

## 安全与交付状态

本仓库公开；禁止加入Key、密码、.env、生产日志、SQLite数据库、WAL/SHM、运行状态及凭据文件。SQLite事件内容可能包含私密消息，应放私有本地目录；POSIX数据库设0600。API内容为不可信数据。
Git提交不代表线上切换、原CrewQuay任务已通过或项目正式完成。仓库目前未附开源许可证；第三方依赖按其自身许可使用。
