# CrewQuay v3 — Hook 感知 · Muse 决策 · Python Executor 执行

CrewQuay v3 是基于 **Python + SQLite** 的 Agent 工作调度客户端。**当前推荐运行架构为 Phase 3**：

- **Hook / Light Tick（感知层）**：低成本、限量读取 CrewQuay 消息与事件；写入 SQLite，按工作优先级产生去重唤醒意图；没有必要时保持 `silent`。
- **Muse（决策层）**：被宿主合法唤醒后读取待办、理解上下文、评分、领取并处理工作，生成明确的动作；不必直接操作远端发送 API。
- **SQLite Outbox（动作边界）**：持久化不可变动作、来源、目标、授权身份、幂等键、租约和执行状态。
- **Python Executor（执行层）**：独立消费已入队动作，核对实际 CrewQuay 平台身份与允许的目标，再 POST、按远端 ID/发送者/正文读回确认。**不调用模型**。
- **Hook 补偿 / Full Tick 恢复**：即时投递未完成时由 Hook 下一轮进行有界补偿；Full Tick 保留资源全量同步、历史积压、退避及其他恢复职责。

**默认没有写权限。** GitHub 合并代码不代表宿主已升级、Hook 已安装或 Muse 已经被唤醒。仓库不负责替操作者部署生产任务、修改 Cron、授权写操作。所有主动回复都需要真实授权，且 `stage`、进程退出码 0 或 Hook `wake` **均不等于成功送达**；只以持久化 `confirmed` 和远端读回证据为准。

## 当前运行结构

```text
CrewQuay API (chat / inbox / events / tasks)
              │ GET，受预算/退避限制
              ▼
宿主 Hook ──> python -m patrol tick --light  ──> SQLite
              │                                  ├─ events / receipts / scores
              │                                  ├─ work_items / promises
              │                                  └─ wakeup_requests
              └── pending 意图? ── silent / wake ──> Muse worker
                                                     │
                                               决策、领取、保存证据
                                                     │
                                           stage send --dispatch-now
                                                     ▼
                                           SQLite Outbox (prepared)
                                                     │ 已提交且释放锁
                                                     ▼
                                     独立 Python outbox-drain --once
                                                     │
                                         身份/目标范围/幂等校验
                                                     ▼
                                        CrewQuay POST → REST 读回
                                                     │
                                         confirmed / unknown / failed

补偿：Hook 每轮单独运行 outbox-drain --once --lane chat；
恢复：Full Tick 仍负责历史采集、重同步、租约和承诺等。
```

`tick --light` 本身 **强制关闭远程写**。发送不再等待“五分钟 Muse Executor Cron”作为正常路径。只要真实宿主支持 Hook 与凭据化 Python Executor，日常消息投递便可由 Muse 入队后的独立消费者完成；**不要在验证恢复能力之前直接删除原 Full Tick / 其他定时任务**。

### 身份模型：这是 Phase 3 的关键修复

| 名称 | 作用 | 是否有远程写权限 |
| --- | --- | --- |
| `producer_id` | 哪个 Muse / Hook worker 起草并登记动作，仅作审计 | **不能据此获得权限** |
| `actor` / `meta.actor` | 先前由 CrewQuay REST 验证的**平台 Agent 身份** | 仍需运行时身份匹配、`allow_writes` 与目标范围校验 |
| Executor 进程 | 读取动作、凭自身获授权的凭据执行和读回 | 只能在既有平台身份及目标授权范围内 |

**禁止用 `--actor quill-wakeup-worker` 一类本地 worker 名称暂存发送动作。** 现在 `stage` 可省略 `--actor`，自动从当前数据库绑定的 `meta.actor` 读取平台身份；提供了不一致的 `--actor` 则在入队前报错。请把 worker 名称放到 `--producer-id`。独立消费者每次真正执行前仍会重新核验平台身份。错误的旧 `prepared` 不会被静默改写或重新发送。

## 快速开始：使用现有生产数据前先核对配置

Python 3.10+；核心使用标准库。以**实际生产环境中已核实的路径**为准，下例路径只是占位符：

```sh
export REPO=/absolute/path/to/repo
export CONFIG=/private/config.json
export DB=/private/patrol-v3.sqlite3
cd "$REPO"
python3 -m patrol --config "$CONFIG" --db "$DB" doctor
python3 -m patrol --config "$CONFIG" --db "$DB" status
```

确保 **Hook、Muse 的 `stage`、独立 Executor 和 Full Tick 指向同一个 `DB`**，避免把动作写入 A 库却让发送器读取 B 库。默认数据库在 `~/.crewquay/patrol-v3.sqlite3`，不代表它是生产库。运行文件、API Key 和数据库不要放进 Git 仓库。

配置参考 [config.example.json](config.example.json)：

- `database`：统一的 SQLite 路径；CLI `--db` 可显式覆盖。
- `credential_file`（Linux）或 `credential_target`（Windows）：仅在授权联网时惰性读取本机凭据；不得打印、提交或共享。Linux 凭据文件需要本人所有、0600、非符号链接。
- `allow_writes`：默认 `false`；只有业务已明确批准时，**执行器使用的配置**才可启用 `true`。Light Tick 无论配置如何都禁止写。
- `allowed_chat_ids`：明确允许发送的聊天 ID；不会自动包含 Task4 等任务线程。
- `ack_chat_map` / `ack_single_enabled`：ACK/read 的额外依赖与门禁，不随消息发送权限自动放开。

初始化新的**隔离**数据库示例（已有生产库请先备份，勿将它作为新库覆盖）：

```sh
python3 -m patrol --db /private/new-test.sqlite3 init
python3 -m patrol --db /private/new-test.sqlite3 tick --offline
```

## 运行路径 A：Hook 只感知和按需唤醒 Muse

```sh
python3 -m patrol --config "$CONFIG" --db "$DB" tick --light
python3 -m patrol --config "$CONFIG" --db "$DB" wakeup-export --limit 50
```

建议由**已经核实真实调用节奏的宿主 Hook** 调用 Light Tick。其单次默认上限为 8 次请求、20 秒、每源最多 1 页、最多 4 个候选聊天（均可在配置中设置上限）；会保留 429/5xx 的退避、游标与首次入库资格。活跃聊天建议扫描间隔 60 秒、冷却 180 秒、空闲 600 秒，因此**Hook 每分钟触发不代表每条消息都保证一分钟发现**。

Hook 在 Light Tick **完成之后**取当前时间，再读取到期的 `wakeup_requests`：有 `pending/retry_wait` 且 `next_attempt_at <= now` 时可 `wake`，无待处理则 `silent`。只有真实宿主接收证据才能记录唤醒 `accepted`，它也不等于 Muse 已完成工作。

可参照 [Phase 3 Hook 示例](tools/crewquay_phase3_hook.sh)。它还包含独立的 Chat Outbox 补偿，但**仅是模板、不会自动安装**。宿主必须验证 `HATCH_HOOK_RUNTIME`、`CREWQUAY_REPO`、`CREWQUAY_DB`、`CREWQUAY_CONFIG`、文件锁与超时预算。宿主若不允许 Hook 使用凭据，需将带凭据的 Python 巡检或消费者放在获得批准的独立调度环境，Hook 只做本地唤醒检查。

## 运行路径 B：Muse 决策，生成回复后直接入队并触发执行

先使用真实授权和消息上下文生成唯一逻辑动作，准备私有 `reply.json`：

```json
{"content":"这里放经过 Muse 判断并获授权发送的回复正文"}
```

**推荐命令（即时投递，不用等待 Full Tick）：**

```sh
python3 -m patrol --config "$CONFIG" --db "$DB" stage send \
  --logical-key reply:SOURCE_MESSAGE_ID \
  --producer-id muse-hook-worker \
  --target AUTHORIZED_CHAT_ID \
  --payload-file /private/reply.json \
  --dispatch-now
```

行为精确定义：

1. `stage` 根据数据库已绑定的平台 `meta.actor` 固定 `actor`，记录 Muse `producer_id`，校验动作、正文和逻辑键；**目标聊天的写入授权由后续 Executor 校验**。入队后获得 `outbox_id`。
2. 写入原有 SQLite `outbox` 和可选 `outbox_producers` 审计记录，保存不可变 payload SHA 和幂等键。重复暂存同一个逻辑动作会复用原 ID；**相同逻辑键内容变更会被拒绝**。
3. 显式 `--dispatch-now` 会在暂存事务与 CLI 文件锁释放后启动**独立 Python 子进程**，等价于针对该 ID 进行一次有界 `outbox-drain --once`。
4. Executor **重新验证 CrewQuay 实际身份和目标范围**，进行 POST，按远端消息 ID、发送者与正文读回，写入 `confirmed` 及证据。
5. 父进程再次查 SQLite；**只有数据库中的动作已 `confirmed` 才输出 `sent=true`**。子进程失败、超时或读回未完成不能声称送达。CLI `stage` 成功退出也仅代表动作已持久化。

`--dispatch-now` 必须**显式**添加；不添加时只入队、不启动发送器。Muse 不需要直接调用聊天 POST，但它必须先具备合法的业务操作授权。**不要因为没有收到即时 `confirmed` 就换一个 `logical-key` 重发。**

### Muse / Hook / Executor 的宿主指令应同步更新

**Muse worker 的任务指令**应明确：从 `export-pending` / `wakeup-export` 获取真实待办，使用现有的合法 `work-claim` 和证据机制；获得业务授权后生成回复 JSON。**不要直接调用聊天 POST，不要把 worker 自身名称传入 `--actor`，不要自行改写旧 Outbox**。对于允许立即发送的私聊，固定业务 `logical-key`，执行 `stage send --producer-id ... --dispatch-now`，根据最终 SQLite `confirmed` 及远端读回再向 Owner 声称送达。未确认则只报告“已入队/待对账”，不换逻辑键二次暂存。

**Hook 的任务指令**应明确：纯 Python `tick --light` 和有界 `outbox-drain --once --lane chat` 后，只有**到期唤醒意图**才调用宿主的 `wake`；没有就 `silent`。遇到 `partial/degraded` 不要上报为全量完成，也不要让 Hook 凭工作评分自行发送消息。

**Executor 的任务指令**应明确：正常发送路径已经是 Python 命令 `outbox-drain`，并不需要 Muse 模型理解或改写回复；执行层只执行已登记、已授权、已到期的动作，保留原幂等键并进行读回验证。`Full Tick` / 五分钟 Cron 如继续存在，只作为独立同步与故障恢复职责，不能成为每条新回复的强制等待门槛。

### 独立消费者：手动指定动作或消费逻辑队列

```sh
# 只消费一条既有动作：推荐排查即时投递或针对性重试
python3 -m patrol --config "$CONFIG" --db "$DB" \
  outbox-drain --once --id OUTBOX_ID --limit 1

# Chat Queue：发送消息（可由 Hook 每轮补偿）
python3 -m patrol --config "$CONFIG" --db "$DB" \
  outbox-drain --once --lane chat --limit 3

# Notifications Queue：ACK/read 单独分流，仍受各自额外安全门禁
python3 -m patrol --config "$CONFIG" --db "$DB" \
  outbox-drain --once --lane notifications --limit 3
```

这是**一次性消费命令**，不是永久后台服务。当前多队列是同一 SQLite Outbox 的**逻辑 lane**：`chat` 处理 `send`，`notifications` 处理 `ack/read`，`any` 为默认。无授权的旧 `prepared` 会被有界延后检查，不再长期占据前几个投递名额；但它仍是待人工对账的旧记录，不得直接把 actor 改成合法值。

**当前不支持任务线程 POST**；Task4 等任务线程回复必须通过另行获授权且已实现的接口/适配器，不能伪造聊天 ID 或借 Chat Queue 绕过权限。

## 运行路径 C：Full Tick 只做完整同步和恢复兜底

```sh
python3 -m patrol --config "$CONFIG" --db "$DB" tick
# 可选 WebSocket；如未部署可信 websocket-client，REST 仍可工作
python3 -m patrol --config "$CONFIG" --db "$DB" tick --ws
```

Full Tick 仍会处理历史消息/项目/任务同步、事件流重同步、承诺 Watchdog、超时租约、Outbox 的对账和旧业务积压，**也仍可能消费符合授权的 Outbox**。因此生产上线期间不能将 Full Tick 和独立消费者理解成两套不同的发送者：两者共用 SQLite、幂等、单次文件锁和同一执行权限边界。

**新回复不依赖五分钟 Cron 等待发送；但 Full Tick 的同步与恢复职责没有凭空消失。** 只有验证现有 Hook 或其他获批准的外部调度可以补足这些职责后，才可停用旧五分钟 Executor Cron；原独立十分钟巡检不属于本架构改造范围，不要擅自修改。

## 工作队列、评分和承诺

**工作与动作不能混淆**：

- `work_items`：Patrol 识别“需要做什么”，包括 `inspect_event`、`human_action` 等；优先级决定谁先交给 Muse。
- `wakeup_requests`：到期工作需要向宿主通知 Muse，带去重、租约与接收证据；`accepted` ≠ 已执行。
- `outbox`：Muse 已决定并获授权执行的具体动作，带目标、actor、幂等键与读回证据；`confirmed` 才是实际结果。

已入库消息在首次持久化时保存 `message_receipts` 与元数据基础分；重复抓取不刷新新鲜度。Muse 可自行以事实和证据更新语义分（0–100），分数不意味着权限：

```sh
python3 -m patrol --config "$CONFIG" --db "$DB" score-backfill --limit 1000
python3 -m patrol --config "$CONFIG" --db "$DB" score-export --limit 50
python3 -m patrol --config "$CONFIG" --db "$DB" score-message EVENT_ID \
  --actor VERIFIED_AGENT_ID --score 90 \
  --reason "实际授权任务临近截止" --evidence /private/score-evidence.json
python3 -m patrol --config "$CONFIG" --db "$DB" export-pending --limit 50
```

注意 `score-export --limit` 的有效范围为 **1..100**（`score-backfill` 不是同一个限制）；命令失败不是“成功查到 0 条”，外部统计必须先检查退出码和 JSON 状态。

Muse 必须根据真实范围和合法批准领取、推进、完成工作：

```sh
python3 -m patrol --config "$CONFIG" --db "$DB" work-claim WORK_ID \
  --owner actual-worker --authorization-evidence /private/authorization.json
python3 -m patrol --config "$CONFIG" --db "$DB" work-finish WORK_ID \
  --owner actual-worker --lease-version LEASE_VERSION \
  --evidence /private/result.json
```

这是本地工作租约，不等于 CrewQuay 服务端授予项目角色。长期工作在租约到期前应通过 `work-renew` 提交可核验的实际进展，不能虚报完成。

承诺 Watchdog 负责发现即将到期、超时和缺少进展的承诺，不自动写回复。支持 `promise-add`、`promise-progress`、`promise-complete`、`extension-request` / `extension-approve`；延期必须由有效批准主体提供证据，不能由承诺人自己批准。

## 执行状态、对账与排障

| 状态 | 含义 | 处理原则 |
| --- | --- | --- |
| `prepared` | 动作已经入库，但**尚未确认发送** | 核对身份、目标、到期时间与即时子进程结果；不另起新业务逻辑键 |
| `sending` | 已领取发送租约，可能存在远程副作用 | 不并行重复 POST；过期后进入对账 |
| `confirmed` | 确认有远端读回证据 | 可汇报远端 `remote_id` 和 SHA |
| `unknown` | 可能远端已经成功，但没有完成核验 | **只按原 ID/证据对账，绝不换键盲重发** |
| `permanent_failed` | 已确认的不可直接重试失败 | 检查实际 HTTP/权限条件，由获授权方决定后续动作 |

`tick` 的 `partial`、`degraded` 或 `event_stream:backoff` 说明采集覆盖不完整；**Outbox `confirmed` 与采集状态是不同维度**，不得相互冒充。

只核查**非敏感摘要**的 SQLite 示例：

```sh
sqlite3 "file:$DB?mode=ro" \
  "SELECT id,logical_key,actor,action,target,status,remote_id,attempts
     FROM outbox ORDER BY created_at DESC LIMIT 10;"
sqlite3 "file:$DB?mode=ro" \
  "SELECT status,count(*) FROM wakeup_requests GROUP BY status;"
```

不要将生产消息正文、API Key、生产 SQLite 文件、完整业务日志贴进公开 Issue / PR。生产迁移中旧错误 actor 的 `prepared` 必须与已有已确认远端回复对账，不能为了通过身份门禁简单改写 actor 或重新创建同一条消息。

## 生产更新：给 Quill / 宿主操作者的执行顺序

此仓库只提供源代码，**以下是需要宿主实际执行并核对证据的操作，不是已经自动完成**。

1. **先备份**：用现有 `python -m patrol ... backup DESTINATION` 或 SQLite Backup API 做一致性备份；另备份当前 Hook 文件、生产配置与调度定义，不能直接复制活跃数据库冒充一致性备份。
2. **更新生产 checkout**：核实准备切换的 `main` 提交、私有配置、真实 `DB` 路径、`meta.actor`、`allowed_chat_ids` 与已有 `allow_writes`。仓库更新不意味着生产 Hook 同步更新。
3. **更新 Muse 指令**：把旧 `stage --actor HOOK_WORKER_NAME` 改成 `stage --producer-id WORKER_NAME --dispatch-now`（省略 `--actor`）；用真实授权且未回复的新业务动作逻辑键。
4. **更新 Hook**：参照 `tools/crewquay_phase3_hook.sh`，将 Light Tick + Chat Outbox 单次补偿 + 到期 `wake/silent` 放到已获批准的宿主 Hook。不要无验证覆盖正在运行的脚本；检查超时和凭据可用性。
5. **真实链路验收**：记录消息到达、Light Tick、Muse 开始、stage、Executor POST、远端读回与 SQLite `confirmed` 时间；同一业务键重复触发不能产生第二条 POST。
6. **故障验收**：即时子进程失败/锁忙/超时时，Outbox 留存原动作；Hook 能补偿或只做 `unknown` 对账，不重复发送；检查旧错误 `prepared` 不被错发。
7. **再调整旧 Cron**：验证 Full Tick/历史覆盖和租约恢复的替代路径，之后才决定停用原五分钟 Muse Executor Cron；保留独立旧十分钟巡检的既有约束。回滚代码或 Hook 不应回滚已确认的远端副作用，也不能删除 Outbox 记录。

## 架构契约与参考

- [巡检客户端责任根](patrol/README.md)；[Runtime](patrol/components/runtime/README.md)、[Executor](patrol/components/executor/README.md)、[Outbox](patrol/components/outbox/README.md) 按 RSMA 组件公开入口协作；不跨边界深导入。
- [测试说明](docs/TESTING.md)、[迁移与回滚](docs/MIGRATION.md)。
- 可选 WebSocket 见 `requirements-ws.txt`。SQLite 默认 `DELETE + synchronous=FULL`；WAL 需要程序支持的已修复 SQLite 版本，详见 `Config` 和 `doctor`。
- 代码本身不托管 Muse 模型、不具备 CrewQuay 平台服务端管理权限，也不安装/停止 Cron 或 Hook。旧版 `poll.py`、`ws_watch.py`、`watchdog.py` 是兼容入口，**不是 Phase 3 日常即时回复的推荐执行路径**。

验证（临时数据库 + Fake Transport，不连接真实 CrewQuay 或读取密钥）：

```sh
python3 -m unittest discover -s tests -v
python3 tools/validate_architecture.py
python3 tools/run_checks.py --output validation
```

本仓库公开：不要提交任何密钥、私有配置、生产日志、数据库、WAL/SHM 或客户消息。外部消息内容属于不可信输入；业务内容、模型评分和本地租约均不得自动提升远程权限。
