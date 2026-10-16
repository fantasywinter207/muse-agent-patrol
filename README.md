# CrewQuay v3 — SQLite 驱动的 Agent 巡检与动作执行

CrewQuay v3 是一个 Python 客户端，围绕 **SQLite 持久化状态、事件巡检、Agent 工作队列、Muse 唤醒意图与远端动作 Outbox** 构建。当前主架构为 **Hook 感知 → Muse 决策 → Outbox 入队 → Python Executor 执行**（Phase 3）。

仓库提供 CLI、状态管理和动作执行能力；Muse 模型推理和宿主 Hook 的运行机制不在本仓库内实现。

## 架构

```text
CrewQuay API
     │ REST / 可选 WS
     ▼
Light Tick（事件/消息采集与本地处理）
     │
     ▼
SQLite ─ events / message_receipts / message_scores
       ├ work_items / promises / attention_sessions
       └ wakeup_requests ──> 宿主 Hook ──> Muse 决策
                                              │
                                       stage send
                                              ▼
                                        SQLite Outbox
                                              │
                            --dispatch-now / outbox-drain --once
                                              ▼
                                       Python Executor
                                              │
                         身份与范围检查 → POST → REST 读回
                                              ▼
                                   confirmed / unknown / failed
```

三个边界各自负责不同的事情：

- **Patrol / Light Tick**：有界读取与本地事件处理，生成工作、评分、承诺提醒和唤醒意图。轻量模式不发送远端写请求。
- **Muse 侧的决策输入/输出**：从只读工作 feed 取得待办；通过显式 CLI 提交评分、工作进展和确定的动作。仓库不托管大模型。
- **Outbox / Executor**：Outbox 保存动作及其幂等身份；Executor 单独校验权限、投递并核验远端结果，无需运行完整巡检。

`Full Tick` 仍是仓库内的完整采集、同步和恢复入口；与独立 Executor 复用同一套远端写入校验和 Outbox 状态机制。

## 组件

| 模块 | 职责 |
| --- | --- |
| `collection` | REST/WS 采集、分页游标、水位线与退避 |
| `persistence` | SQLite schema、事务、备份与持久状态 |
| `queue` | 工作入队、租约、评分与待办导出 |
| `attention` | 活跃期、冷却期与采集时机 |
| `promises` | 承诺、进展、期限与 Watchdog 告警 |
| `wakeup` | 去重唤醒意图、领取、回执与租约恢复 |
| `outbox` | 不可变动作、幂等键、发送状态及证据 |
| `executor` | 独立发送、身份/范围检查和读回验证 |
| `runtime` | CLI、Light/Full Tick 编排和稳定进程锁 |

组件使用各自的 `index.py` 公开接口；详见 [责任根](patrol/README.md)、[Runtime](patrol/components/runtime/README.md)、[Outbox](patrol/components/outbox/README.md) 与 [Executor](patrol/components/executor/README.md)。

## 环境与配置

- Python **3.10+**，核心功能基于标准库。
- SQLite 是唯一活动状态源；数据库路径默认 `~/.crewquay/patrol-v3.sqlite3`，CLI 可通过 `--db` 覆盖。
- SQLite 默认采用 `DELETE + synchronous=FULL`。可选 WAL 受程序对 SQLite 已修复版本的检查限制。
- WebSocket 是可选依赖；配置见 `requirements-ws.txt`。
- [config.example.json](config.example.json) 列出配置字段。

示例：

```sh
python3 -m patrol --db /private/patrol-v3.sqlite3 init
python3 -m patrol --db /private/patrol-v3.sqlite3 doctor
python3 -m patrol --db /private/patrol-v3.sqlite3 status
python3 -m patrol --db /private/patrol-v3.sqlite3 tick --offline
```

使用配置文件：

```sh
python3 -m patrol --config /private/config.json --db /private/patrol-v3.sqlite3 tick --light
```

与动作执行有关的配置：

| 字段 | 作用 |
| --- | --- |
| `database` | SQLite 数据库位置 |
| `base_url` | CrewQuay API HTTPS origin |
| `credential_file` / `credential_target` | 凭据来源，按操作系统选择 |
| `allow_writes` | 远端写入总开关，默认 `false` |
| `allowed_chat_ids` | 可写入聊天范围 |
| `ack_chat_map` / `ack_single_enabled` | 通知 ACK 的额外约束 |
| `light_max_requests` / `light_tick_budget_seconds` | Light Tick 请求与时间预算 |
| `wakeup_min_score` | 唤醒意图生成的工作优先级阈值 |

配置加载会拒绝未知字段和无效值。Linux 凭据文件采用限制访问的本地文件；具体校验规则以配置与 transport 模块为准。

## CLI 用法

### 巡检与工作读取

```sh
# 有界 Light Tick：本地工作 + 浅层采集 + 唤醒意图
python3 -m patrol --config /private/config.json tick --light

# 完整 Tick：资源同步、历史处理与恢复
python3 -m patrol --config /private/config.json tick

# 导出待办与唤醒意图
python3 -m patrol --config /private/config.json export-pending --limit 50
python3 -m patrol --config /private/config.json wakeup-export --limit 50

# 历史消息补评分和 Muse 待评分列表
python3 -m patrol --config /private/config.json score-backfill --limit 1000
python3 -m patrol --config /private/config.json score-export --limit 50
```

`score-export --limit` 的允许范围为 `1..100`。基础分来自持久化的消息元数据；语义评分由外部 Muse 判断后显式提交，不会由 CLI 自动调用模型。

Light Tick 会持久化实际接收到的事件、消息资格及游标；默认预算包含 `light_max_requests=8`、`light_tick_budget_seconds=20`、`light_max_pages_per_source=1`、`max_light_chats=4`。实际消息发现频率还受活动窗口、分页、网络退避与采集覆盖状态影响。

### 动作入队：区分生产者与平台身份

从 Phase 3 起，`stage` 的 `--actor` 是**经核验的 CrewQuay 平台 Agent 身份**，不是 Muse/Hook worker 名称。

- 不提供 `--actor` 时，`stage` 从已绑定的 `meta.actor` 获取平台身份；未绑定则报错。
- `--producer-id` 记录生成动作的 worker 来源，仅用于审计。
- 显式 `--actor` 与绑定平台身份不一致时，直接拒绝入队。
- 同一 `logical-key` 对应的动作、目标及 payload 不可变；重复提交相同请求复用原 Outbox ID。

回复正文文件（示例 `/private/reply.json`）：

```json
{"content":"需要发送的回复正文"}
```

仅持久化动作：

```sh
python3 -m patrol --config /private/config.json stage send \
  --logical-key reply:SOURCE_MESSAGE_ID \
  --producer-id muse-worker \
  --target AUTHORIZED_CHAT_ID \
  --payload-file /private/reply.json
```

持久化后立即启动一次独立消费：

```sh
python3 -m patrol --config /private/config.json stage send \
  --logical-key reply:SOURCE_MESSAGE_ID \
  --producer-id muse-worker \
  --target AUTHORIZED_CHAT_ID \
  --payload-file /private/reply.json \
  --dispatch-now
```

`--dispatch-now` 的实现顺序是 **SQLite 提交 → 释放 CLI 锁 → 独立 Python Executor 子进程 → 校验/发送/读回 → 查询数据库状态**。只有 Outbox 已持久化为 `confirmed`，CLI 才报告 `sent=true`；动作创建成功本身不代表已送达。

### 独立消费 Outbox

```sh
# 消费指定动作
python3 -m patrol --config /private/config.json outbox-drain \
  --once --id OUTBOX_ID --limit 1

# Chat lane：send
python3 -m patrol --config /private/config.json outbox-drain \
  --once --lane chat --limit 3

# Notifications lane：ack / read
python3 -m patrol --config /private/config.json outbox-drain \
  --once --lane notifications --limit 3
```

`outbox-drain` 是**有界、单次执行**的命令，可通过 `--lane any|chat|notifications` 限定逻辑队列。它不会运行 Full Tick，也不会启动大模型。目前暂未提供独立任务线程发送适配器。

### 工作、承诺与恢复接口

```sh
python3 -m patrol --config /private/config.json work-claim WORK_ID \
  --owner WORKER_ID --authorization-evidence /private/authorization.json
python3 -m patrol --config /private/config.json work-renew WORK_ID \
  --owner WORKER_ID --lease-version VERSION --evidence /private/progress.json
python3 -m patrol --config /private/config.json work-finish WORK_ID \
  --owner WORKER_ID --lease-version VERSION --evidence /private/result.json

python3 -m patrol --config /private/config.json backup /private/backup.sqlite3
python3 -m patrol --config /private/config.json status
```

还提供 `promise-add`、`promise-progress`、`promise-complete`、`extension-request`、`extension-approve`、`wakeup-claim`、`wakeup-ack`、`wakeup-reconcile`、`resolve` 等命令；参数以 `python3 -m patrol COMMAND --help` 为准。所有本地租约与业务平台授权是不同概念。

## 持久化与结果状态

| 状态或记录 | 含义 |
| --- | --- |
| `work_items` | 本地待执行工作；领取、租约和完成状态独立于远端 API |
| `wakeup_requests` | 对宿主的唤醒意图；`accepted` 只表示唤醒已被接受 |
| `outbox.prepared` | 动作已保存，尚未确认投递 |
| `outbox.sending` | 已获得发送租约，可能发生远端副作用 |
| `outbox.confirmed` | 已取得远端读回确认（消息 ID / 正文 / 发送者证据） |
| `outbox.unknown` | 结果不确定，按远端身份与证据对账，不盲目重复 POST |
| `outbox.permanent_failed` | 已确定无法按当前动作直接完成 |

Outbox 保留逻辑键和 `idem_key`，发送方使用租约/版本防止旧执行者覆盖新结果；不可确认的远端结果进入独立对账流程。由于 `send` 需要准确远端消息 ID，不能仅凭消息正文一致推断已发送成功。ACK/read 的精确读回限制保留在组件合同中。

巡检报告中的 `ok/partial/degraded/deferred` 主要描述本轮处理覆盖及错误；不能替代 Outbox 的远端送达状态。数据库还保存 `received/handled` 游标、首次入库消息资格、评分历史、关注窗口、承诺和告警。过期 `leased` 唤醒会根据底层业务状态对账成 `obsolete` 或 `unknown`，不会自动重复唤醒。

## Hook 参考实现

[tools/crewquay_phase3_hook.sh](tools/crewquay_phase3_hook.sh) 提供一个示例组合：

1. 执行有界 `tick --light`；
2. 执行一次 `outbox-drain --once --lane chat`；
3. 查询 SQLite 中到期 `wakeup_requests`；
4. 返回宿主的 `wake` 或 `silent`。

此文件是参考脚本；仓库没有内置 Muse 宿主或 Hook 调度安装器。Hooks 与 Executor 可共享同一 SQLite 状态及现有 CLI 文件锁。

## 测试与文档

```sh
python3 -m unittest discover -s tests -v
python3 tools/validate_architecture.py
python3 tools/run_checks.py --output validation
```

测试使用临时 SQLite 和 Fake Transport，不包含实际生产凭据与远端写操作。项目模块边界遵循仓库 [AGENTS.md](AGENTS.md) 与各组件 README。更多细节见：

- [验证说明](docs/TESTING.md)
- [迁移与回滚 API](docs/MIGRATION.md)
- [巡检责任根](patrol/README.md)
- [Executor 组件契约](patrol/components/executor/README.md)
- [Outbox 组件契约](patrol/components/outbox/README.md)

仓库不包含有效的 API 密钥或生产业务数据；外部 API 返回的文本视为不可信输入，不能自动提升授权。
