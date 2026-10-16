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

## 全消息评分验证边界

运行 `python -m unittest discover -s tests -v`，应包括测试每条消息持久化基础分、注入文本不抬分、Muse 修改评分后重排、重复/越权拒绝、旧 500 优先级重置、历史份额、公平 backfill、独立审计以及 CLI 只读导出。SQLite 新增的 `message_scores` 和 `message_score_history` 属于可重复执行的 v1 additive schema extension，不修改旧 `received/handled`。

注意：只有已下载进 SQLite 的消息能评分；`base_score` 表示启发式新鲜度，不是模型判定。由合法唤醒后的真实 Muse 提交评分及证据，不得假称模型已自动评分。初次迁移与旧队列仍需隔离 DB 检查积压，不能以单元测试代替实际新消息到处理时延的测量。

## Event 202 回归：首次入库资格与预留执行预算

通过 `tests/test_receipt_priority.py` 验证：当消息首次入库时在 15 分钟内，排队超过 15 分钟后仍然产生 human_action、但不会重开活跃关注窗口；首次入库时已经旧的消息即使被评分 99 也不伪装为新消息；重复 REST 抓取不刷新原始资格；已经存在于旧 v3 SQLite 的消息按第一次 observed_at 修复资格。另验证在 DB actor 已绑定时，即便远端 identity(GET) 立即 BudgetExceeded，仍能先完成本地高分消息 inspector、产生人类待办并返回 deferred 状态，且没有发送远端写请求。没有身份时 inspector 必须 retry_wait。

测试不证明 Muse 自动执行或真实新消息的端到端延迟。上线后请对 Event 202 的 event、receipt、inspect_event 和 human_action 实测记录分别截取真实时间和状态。

## Phase 2 轻量巡检与按需唤醒隔离验收

`tests/test_phase2_light_wakeup.py` 使用无凭据的 SQLite / Fake HTTP 模拟：新消息浅抓取、一次性唤醒去重、blocked 工作需要 Muse 决策但仍未授权写入、租约版本防覆盖、crash unknown 不盲重发、带证据的失败/重试、承诺临近下次动作、无任务时静默、`tick --light --offline` 以及 BudgetExceeded 后保存本地工作和唤醒意图。

注意：`wakeup_requests.status=accepted` 不是远程发消息，也不是业务完成。真实宿主 Hook 能力、定时器部署、消息 60 秒到达目标、长时间积压的恢复速度，都必须经 Owner 授权后在隔离环境另行验收。不可在 CI 中接入正式 CrewQuay 凭据。

## Phase 2 BUG-1: invalid/non-progressing pagination
`tests/test_phase2_invalid_cursor.py` includes the exact reported regression: initial `messages.received=1`, then `GET messages?after=1` returns `items=[]`, `nextCursor="0"`, `hasMore=false`. Both Light and Full tick must preserve received=1, record an invalid-page error/backoff, continue polling another source (Inbox) and record a truthful `degraded` run. Further tests cover regressive Event Stream cursors and unexpected `ValueError` resulting in persisted `failed` (not `partial`) for both run modes. Isolated Fake REST only; no proof a real service emits such a page.

## Production regression: stale `leased` wakeup after business completion
`tests/test_wakeup_expired_lease_recovery.py` reproduces production `work:2aa9…` behavior with a newly constructed isolated work/wakeup record. A delivery claim becomes `leased`, the underlying `human_action` becomes `done`, and the lease expires; Light or Full Tick must persist `obsolete`, clear the local lease and retain original attempts. A direct later `wakeup-claim` must likewise persist `obsolete` rather than only report it in JSON. For work still unfinished, expired lease becomes `unknown` (no blind re-dispatch). Active leases and accepted terminal records remain untouched. A closed promise is handled analogously. Validation runs without connecting to production or sending Task4 thread replies.

## Phase 3: production-origin actor mismatch and immediate delivery

`tests/test_phase3_action_executor.py` tests a worker `producer_id` distinct from the verified CrewQuay `actor`, fast-failing wrong actors before staging, a one-shot Executor that independently authenticates and reads back an actual matching remote message, no duplicate POST on repeated triggers, a scoped chat-only lane, notification separation, skipped/temporarily deferred unauthorized queue heads, no write permission, no task-thread route, and crash-ambiguous unknown preservation. The `stage --dispatch-now` subprocess is invoked only after the old file lock is released; the producer does not claim delivery without a **durable confirmed** row. All test transports/SQLite are fake or temporary. No production credentials or message sends occur in CI.

A live test is still necessary for actual throughput and host permissions: (1) keep the existing Hook Light Tick, (2) stage a specifically authorized reply with a genuine logical key and `--producer-id`, (3) verify immediate Outbox confirmation and remote ID/SHA from CrewQuay readback, (4) repeat same action without a second POST, (5) simulate failed immediate dispatch and see Hook `outbox-drain` compensation, (6) verify legacy wrong-actor prepared rows are not silently resent, (7) only then consider disabling the 5-minute Muse Executor Cron. The task-thread route needs a separate authorized endpoint/adapter; it remains forbidden.

## Phase 4: project collaboration and task action adapters

`tests/test_phase4_collaboration.py` and `tests/test_phase4_task_actions.py` exercise exact project discussion, task-thread, Room and task/decision/artifact REST routes with ephemeral SQLite and Fake HTTP. Assertions cover default-deny project write scope, wrong task resource mapping, per-action mutation opt-in, stable outbox idempotency/remote ID, one POST only, exact readback, and unknown-outcome isolation. These tests do not issue any production API call or prove a deployed platform has matching routes; check the server's live OpenAPI and actual role grants separately.

## Phase 5 generic Agent protocol

`test_phase5_platform_protocol.py` uses fake authenticated platform catalog/resource/receipt APIs and temporary SQLite. It tests new dynamic action names without new routing code, deny-by-default project and action permissions, duplicate trigger suppression, exact request digest, and crash-ambiguous POST recovery using `requestId` without a second POST. No production API calls occur in CI.

## Phase 5 Light Tick event-only collection

`tests/test_phase5_light_event_only.py` uses temporary SQLite + a fake authenticated event feed to cover chat/Room/project/Task discussion message normalization, first-observation receipts, baseline scoring, generic task-event wakeup, duplicate replay, typed scopes, redacted/old events, multi-page target pinning, stream mismatch, malformed context/cursors and 410 resync without data loss. `tests/test_phase2_light_wakeup.py` and `tests/test_phase2_invalid_cursor.py` cover the migrated Light Tick entrypoint; Full Tick historical restoration retains its separate tests. No live API or credentials are used. The server adds `agent-event-context.test.ts` for actual recipient visibility and authorized message context.

## Phase 6.1 preemptive activity scheduling

`tests/test_phase6_preemptive_scheduler.py` covers EDF urgency, waited activity aging, weighted virtual-runtime accounting, finite quantum rotation, hysteresis, safe-point fencing, resumed checkpoint generation, expired lease recovery, no unstaged remote actions, optional atomic Outbox stage guards and deduped `human_action` import during Light Tick. Tests create temporary SQLite databases; they never invoke a model or live API. Earlier Phase 2–5 tests must continue passing on Linux and Windows.
