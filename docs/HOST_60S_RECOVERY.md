# 恢复单一 60 秒 Light Tick：宿主接线与验收

## 目的与适用范围

Phase 3 的真实 60 秒 Hook 已经验证：在 5 分钟 Full Tick 暂停后，Owner 私聊测试 3/3 条取得 confirmed，且有 Outbox 去重及补偿证据。升级 v3 不应把发现周期回退到旧巡检的 600 秒。**这是一项宿主任务调度修复，不是数据库迁移，也不是增加第二套定时器。**

**v3 仓库是唯一支持的运行代码来源。** 本仓库提供一次性 Patrol CLI 和 `tools/crewquay_phase3_hook.sh` 参考入口；不会自动修改宿主的实际任务定义。请由拥有真实宿主任务管理权限的操作者在**现有** `crewquay-patrol-v3` 上执行下列变更，并保留任务配置与实际运行证据。无需、也不允许并行启动旧 v2 Patrol、旧 watchdog 或旧 5 分钟 Executor。

## 唯一任务的期望状态

| 项目 | 期望 |
| --- | --- |
| 唯一启用的 v3 周期任务 | `crewquay-patrol-v3` |
| 触发间隔 | **60 秒**（不是 600 秒） |
| 运行内容 | `tick --light` → `outbox-drain --once --lane any --limit 3` → `social-sync --if-idle-due` |
| 同步锁 | 一套固定的 Hook `flock` + CLI `.tick.lock`；重入直接跳过 |
| 社交远端 GET | 受 SQLite 至少 1800 秒节流，单轮最多 3 候选 |
| 事件流 | `GET /api/v1/agent/events` 单页默认 `limit=5`，游标持久化，不回退多端点轮询 |
| 旧 `crewquay-patrol` / `crewquay-watchdog` | 继续暂停，不恢复旧 10 分钟 Full Tick |
| 5 分钟独立 Executor | 继续暂停；用同一 Hook 做到期 Outbox 补偿 |
| 宿主 Phase 6 唤醒桥 | 未实际接通并证明接收回执时保持不启用，不占用 `wakeup_requests` 租约 |

宿主侧应**修改现有任务的 interval（600 → 60 秒）**，不要新建第二个 `cron`、systemd timer 或 HATCH 任务。即使以后采用进程守护或配置即代码的部署管理，运行中的任务触发权仍位于宿主环境，GitHub 文件不会自动替代宿主任务定义。先读取其**真实命令**：如当前仅运行 `tick --light`，则不应因此谎称包含 Outbox 补偿和社交发现；需在原入口中保持/恢复 Phase 3 已验证的单 Hook 顺序。完整命令运行前必须确认工作目录、配置、凭据文件、SQLite 和 Hook runtime 属于同一授权 Agent。

仓库参考脚本按单次调用运行。若直接采用，操作者需要提供 `HATCH_HOOK_RUNTIME`、`CREWQUAY_REPO`、`CREWQUAY_CONFIG`、`CREWQUAY_DB`；状态文件与凭据不放进仓库。脚本逐段有界执行：Light Tick ≤21 秒、Outbox 补偿 ≤20 秒、社交发现 ≤6 秒、可选宿主交接 ≤8 秒；锁保证上一轮未结束时不叠加执行。不可将这些阶段拆成多个每分钟定时任务。

## 新旧宿主唤醒机制的边界

- 60 秒巡检、已授权 Outbox 补偿、只读社交发现**不依赖**新的 `host-wakeup-prepare`。
- 参考 Hook 的 `CREWQUAY_HOST_WAKEUP_WIRED` 默认不为 `1`，因此不会为了未部署的 Phase 6 桥领取唤醒租约。**此时只能证明轮询/补偿/社交候选发现，不能证明无人值守消息回复。** 停用旧 v2/旧独立 worker 后，真实 Muse 的唤醒必须通过已接线的 v3 宿主桥完成，不以恢复旧进程作为回退方案。
- **仅当已验证 `wake()` 确实接到宿主并有可靠的接受回执**，才能在该 Hook 的受控环境中设置 `CREWQUAY_HOST_WAKEUP_WIRED=1`，使用 `host-wakeup-prepare` 和同一 `wakeup-ack` 持久对账。开启前先在隔离测试圈完成 `reserved → actual wake → accepted receipt → Muse executed` 的测试。
- `CREWQUAY_HOST_WAKEUP_WIRED` 表示新宿主桥是否**真实就绪**，不是社交是否开启。社交发现保持默认开启、30 分钟节流；未就绪仅禁止虚假的宿主唤醒领取。
- `allow_writes`、三项 `allowed_chat_ids`、项目与社区动作权限保持原始实际授权范围；这次频率修改不扩大写权限，不执行任何新的平台任务 claim/submit/review。

## 只读验收

1. 在真实宿主确认新 `crewquay-patrol-v3` 为**唯一激活**的 v3 入口，旧 10 分钟巡检与 5 分钟独立 Executor 均保持暂停。
2. 连续观察 3–5 次任务开始时间：理想间距约 60 秒；如上轮超时、占锁或失败，必须记录原因，不能因任务配置显示 60 秒就宣称已达标。
3. 执行 `python3 -m patrol --config <配置> --db <数据库> status`，核实 `patrol_runs` 近期记录与 `agent_events` 游标、`errors`。Light Tick 的 `ok` 不代表 Agent 业务完成。
4. 用**只读、最小页长**执行 `platform-events --after 0 --limit 5`（诊断端点）；确认 Light Tick 后续 GET 同样使用 `limit=5`。若仍发生 `degraded`，保存错误码、退避和原游标，不清理 SQLite、不跳过未处理事件。
5. 对已有已授权 Outbox 待办观察 `outbox-drain --lane any --limit 3` 的补偿记录。仅 `confirmed` 可作为送达证据；`unknown` 先按原 request ID 对账，绝不改逻辑键重发。
6. 检查闲时社交冷却（同一 SQLite 的 30 分钟间隔）以及没有正式任务时的行为。新宿主桥未接线的任务应只读发现，不生成新的 `leased → unknown` 宿主请求。
7. 真正的 Owner/Planner/Quill 端到端验收需另在已授权范围进行，不把离线单测、定时触发或社交候选登记当作已唤醒模型。

## 失败回退

若一分钟触发引发服务端压力或持续 `degraded`，优先核查分页大小、错误与预算，再将**同一任务**暂时暂停并保留诊断证据；不要启动旧 v2 任务、扩大页长或增加并行扫描器当作回退。保留原 SQLite 与 Outbox，防止游标倒退、重复回复与不明投递重试。
