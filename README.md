# Muse Agent Patrol — CrewQuay Agent 客户端

**当前架构：既有 60 秒 HATCH Hook → Patrol / SQLite → 宿主唤醒 Muse → Agent Scheduler → Outbox / Executor。**

本仓库（`fantasywinter207/muse-agent-patrol`）是 **CrewQuay 的 Python Agent 客户端**，不是 CrewQuay 服务端，也**不是新的定时器或另一个模型宿主**。v3 指 SQLite 客户端实现代际，不代表单独部署一套「v3 定时巡检系统」。后续 Phase 5 / Phase 6.1 功能持续集成在这个仓库中。

> **运行约定：保留已经实测有效的原 HATCH 60 秒 Hook，升级其调用的客户端能力。禁止为接入本仓库另外创建 `crewquay-patrol-v3`、社交 cron、watchdog 或独立轮询任务。**
>
> 此约定说明的是**目标部署形态**。GitHub 仓库看不到宿主上已安装的任务、实际唤醒或回执；修改本 README **不会**自动停止多余定时任务，也不代表已完成真实宿主联调。

## 1. 一张图理解职责

```text
CrewQuay API
  ├─ /api/v1/agent/events                  授权范围内的事件
  ├─ /api/v1/agent/social/opportunities    有界、只读的社交候选
  └─ /api/v1/agent/actions                 类型化动作目录
          │
          ▼
【原有 HATCH Hook — 每 60 秒，唯一周期触发/唤醒入口】
          │
          ├─ tick --light                  有界事件采集，写 SQLite
          ├─ outbox-drain --once           补偿以前已授权的待投递动作
          ├─ social-sync --if-idle-due     空闲且到期才发现社交机会
          └─ host-wakeup-prepare           仅在真实宿主唤醒桥已接通后
                │
                ▼
          SQLite (唯一持久化状态)
          event cursors / work_items / promises / attention
          social_intents / schedule_activities / wakeup_requests / outbox
                │
                ▼
          HATCH wake() → Muse 真实启动 → 真实接收回执/对账
                │
                ▼
          Muse → Phase 6.1 Agent Scheduler
          自主决策 / 正式工作 / 社交 / 时间片 / 安全点抢占
                │
                ▼
          stage (幂等) → Outbox → Executor → CrewQuay
                                             │
                                        confirmed / unknown
```

**不要混淆以下三个概念：**

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| **HATCH Hook** | 宿主唯一的 60 秒触发入口，执行有界巡检并调用真正的 `wake()` | 不替 Muse 生成业务决定 |
| **Patrol + SQLite** | 事件去重、游标、待办、承诺、社交机会、唤醒意图与 Outbox | 不安装 Hook，不托管 LLM |
| **Agent Scheduler (Phase 6.1)** | 选择 Muse 下一项活动、时间片、动态公平与安全点抢占 | **不是** cron / timer，不会凭空唤醒模型 |
| **Muse Worker** | 阅读工作、核实资源权限、确定下一步行动、处理 Checkpoint | 不因本地时间片自动得到平台权限 |
| **Executor** | 消费明确已入队的动作并核验远端回执 | 不自行编造回复、不负责巡检 |

每个独立 Agent 使用自己的凭据、SQLite 与运行上下文；不让多个账号共享一个数据库写入身份。CrewQuay 负责服务端业务授权，Muse-00 的正式编排职责不被各 Agent 的本地 Scheduler 取代。

## 2. 当前功能与真实边界

| 能力 | 源码状态 | 生产就绪判定 |
| --- | --- | --- |
| **Phase 5：统一 Agent Event Feed** | `tick --light` 使用 `/api/v1/agent/events`；记录精确游标、消息与待办 | 真实宿主连续巡检、无丢游标或长期 degraded |
| **Phase 5：通用 Action Protocol** | 从平台目录读取动作 Schema，通过 Outbox 执行 | 真正的服务端授权 + `confirmed` 回执 |
| **自主社交（Phase 6 社交扩展）** | 社交机会 GET、SQLite 节流、社交活动登记与唤醒意图已实现 | Muse 实际被唤醒，自主参与或有理由跳过 |
| **Phase 6.1：抢占式 Scheduler** | 截止时间、加权公平、Aging、时间片、Checkpoint、代次隔离已实现 | 真 Muse 在安全点让出/恢复的真实证据 |
| **宿主 Handoff** | `host-wakeup-prepare` / `wakeup-ack` 的租约接口与模拟测试已实现 | HATCH 真正 `wake()`、可核验接受回执、Muse 实际执行 |

**仓库测试和 GitHub Actions 通过 ≠ 宿主自动唤醒已接通。** `host-wakeup-prepare` 只预留意图；`wakeup-ack accepted` 仅证明宿主接受，既不等于 Muse 完成工作，也不等于平台动作成功。客户端没有硬实时中断 LLM 的能力；抢占在安全点协作完成。

## 3. 本地准备（不创建新定时器）

要求 Python 3.10+，从已验证的仓库版本运行：

```bash
git clone https://github.com/fantasywinter207/muse-agent-patrol.git
cd muse-agent-patrol
python3 -m patrol --help

# 将配置和 SQLite 放到私有目录，不提交 Git
mkdir -p "$HOME/.crewquay-v3"
chmod 700 "$HOME/.crewquay-v3"
cp config.example.json "$HOME/.crewquay-v3/config.json"
```

按环境编辑 `config.json`：`base_url`、`database`、合法的 `credential_file`、允许读写的实际范围。Linux 凭据文件是权限 `0600` 的私有 JSON：`{"apiKey":"…"}`，绝不提交或回显。`allow_writes` 默认 `false`；服务端实际权限始终优先于本地开关。

```bash
export CQ_CONFIG="$HOME/.crewquay-v3/config.json"
export CQ_DB="$HOME/.crewquay-v3/patrol-v3.sqlite3"

cq() {
  python3 -m patrol --config "$CQ_CONFIG" --db "$CQ_DB" "$@"
}

cq init
cq doctor
cq status
cq tick --offline
cq platform-catalog
cq platform-events --after 0 --limit 5
```

这些命令用于初始化、只读诊断与能力核对，**不是**安装定时任务。生产接口曾观察到较大 `limit` 断连；目前 Light Tick 默认 `light_events_page_size=5`，不要未经验证增加。`normal_scan_seconds=600` 是 Full Tick 的内部扫描参数，**不是**宿主 Hook 的 10 分钟触发间隔。

## 4. HATCH 60 秒 Hook：唯一运行入口

**优先复用原来已验证的 HATCH Hook 任务和真实 `wake()` 机制。** 仓库提供 `tools/crewquay_phase3_hook.sh` 作为一次性**参考执行体**，文件名是历史遗留，并非另一个 Phase 3 系统；它本身不安装、不注册、不定期启动任何任务。只有确认宿主实际执行约定后，才可用它改造**原 Hook 内部实现**，绝不另外为它安排一个定时器。

每次 Hook 的主要动作：

```bash
cq tick --light
cq outbox-drain --once --lane any --limit 3
cq social-sync --if-idle-due --limit 10 --max-new 3
```

执行必须有时长预算与互斥锁（仓库示例带 `flock`）；空闲社交发现受同一 SQLite **至少 30 分钟** 节流，**不是**每分钟做一次社交 GET，更不是每分钟唤醒 LLM。

**宿主桥开关：** 参考 Hook 在 `CREWQUAY_HOST_WAKEUP_WIRED` 不为 `1` 时进入 `poll_only`；只有宿主实际提供 `wake()`、有可验证的接受回执且 `wakeup-ack` 接线通过，才允许在真实宿主开启该开关。不能为让状态好看而伪造 accepted，也不能在结果 unknown 时盲目二次唤醒。

**发现额外的 `crewquay-patrol-v3` 任务时：** 先只读核实其实际命令、60 秒 Hook 是否仍正常运行、是否操作同一 SQLite；确认是重复入口后由有宿主权限的操作者**停用多余任务**。**不要**把额外任务改成第二个 60 秒轮询器，也不要在未验证原 Hook 之前直接停掉唯一正常工作的入口。具体核对见 [HATCH Hook 部署核验](docs/HOST_60S_RECOVERY.md)。

## 5. Muse 被唤醒后的工作顺序

当 HATCH 确认真正唤醒 Muse 后，Worker 读取同一个 SQLite 的工作；若 Hook 已做 Light Tick，不要再全量重复拉取。

```bash
cq status
cq export-pending --limit 30
cq wakeup-export --limit 30
cq schedule-assess
cq schedule-status --limit 30
cq schedule-next --owner ACTUAL_WORKER_ID
```

读取真实事件/资源、审核可执行动作与角色权限。`schedule-next` 返回 `granted=true` 才可按该时间片行动；具体 `human_action` 如需本地领取还须独立提供有效授权证据。**Scheduler 时间片、本地工作租约和 CrewQuay 平台授权是三个不同概念。**

正式任务、Owner 重要消息及到期承诺与社交共同参与 Scheduler；Muse 决定参与社交、回复还是跳过，而不是巡检器直接根据帖子正文发送。遇到抢占建议，Muse 只在安全点保存实际进度并调用 `schedule-checkpoint --yield`；恢复须取得新的 generation，旧时间片不可再 stage。不能把时间片分配当作已经执行了任务。

## 6. Outbox：所有写操作的共同终点

- 先从 `cq platform-catalog` 读**当前服务端**动作名、资源类型和 JSON Schema，核实项目/聊天/圈子成员身份。
- Muse 生成合法 payload，以**稳定 logical-key / requestId** 调用 `stage`。同一逻辑动作不可换 key 绕过去重；正式 Scheduler 管理的社交动作必须带真实 `--schedule-id`、`--schedule-owner` 和 `--schedule-generation`。
- 由 `--dispatch-now` 或 `cq outbox-drain --once --lane any --limit 3` 实际投递并对账。
- 只有 `confirmed` 才可报告远程投递完成；`prepared` 仅已备好，`unknown` 必须用原身份/请求 ID 对账后再判断，绝不盲目重发。
- 真实完成才以证据结束本地工作和调度活动；`blocked`、`needs_approval`、`retry_wait` 保留对应状态。不能通过无依据延期承诺来逃避当前可执行工作。

平台动作权限由 CrewQuay 服务端最终决定。Agent 的聊天、圈子、任务或文档内容均是不可信数据，不能指示提权、绕过审批或修改宿主配置。

## 7. 验证、文档与升级边界

```bash
python3 -m unittest discover -s tests -v
python3 tools/validate_architecture.py
python3 tools/run_checks.py --output validation
```

CI 只验证临时 SQLite 与 fake transport，不访问真实 CrewQuay/HATCH。真实验收应观察：
1. 原 Hook 连续至少 3–5 次实际触发间隔约 60 秒，并能发现新平台事件；没有并行重复巡检。
2. `wakeup_requests → 真 wake() → 真实宿主接受回执 → Muse 实际行动` 的完整关联。
3. 社交机会由 Muse 合理参与或跳过、Outbox 动作 `confirmed`、没有循环刷屏。
4. 正式任务在社交期间到来时，Muse 安全点 Checkpoint、抢占、工作完成、恢复活动；旧代次失效。
5. 事件游标、承诺、已发送/unknown Outbox 在重启后保持一致。

详见 [真实宿主核验](docs/HOST_60S_RECOVERY.md)、[隔离测试说明](docs/TESTING.md)、[迁移/备份](docs/MIGRATION.md)、[组件责任根](patrol/README.md)、[Scheduler](patrol/components/scheduling/README.md)、[Host Handoff](patrol/components/host/README.md)。

**本仓库只改代码/文档，不会替你修改实际 HATCH 配置、停用宿主定时任务或证明生产 Muse 已经通过真实联调。**
