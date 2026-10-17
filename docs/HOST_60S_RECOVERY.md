# 60 秒 HATCH Hook：现有宿主核验与去重部署

## 结论先行

**HATCH 的原有 60 秒 Hook 是唯一核心入口，已在 Phase 3 生产测试中证明 Owner 私聊 3/3 可完成 `confirmed`。** v3 SQLite / Phase 5 / Phase 6.1 是该入口后面的可更新组件，**不是**另一个定时器。

仓库不掌握现有宿主的任务定义，也不自动安装/修改/删除任务。此文是运维核验清单，不是要求创建名为 `crewquay-patrol-v3` 的任务，更不是要求把重复任务改为 60 秒。

## 先只读检查，后变更

由拥有 HATCH 实际管理权限的操作者完成，敏感信息脱敏保存：

1. 列出现有 HATCH Hook / timers / cron 的名称、运行命令、实际执行间隔与最近 5 次开始时间。**先定位已经稳定运行的原 60 秒 Hook**，不要凭任务名称推测真实触发链路。
2. 记录其调用的仓库路径及 SHA、`CQ_CONFIG` / `CQ_DB` 实际路径、SQLite `status`、`agent_events` 游标、最近的 `patrol_runs`。确保同一 Agent 同一个身份与状态库。
3. 查明 `crewquay-patrol-v3`（若存在）实际是否另外执行 `tick`、`outbox-drain` 或 Muse wake；与原 Hook 是否冲突。**确认是重复定时入口后才停用多余任务**，保持旧状态/定义供回滚核查，避免误删正在使用的唯一入口。
4. 检查 Hook 是否包含有界的 `tick --light`、`outbox-drain --once --lane any --limit 3`、`social-sync --if-idle-due`。如果缺步骤，**在原 Hook 上更新执行体**；不另建一个 Cron 来运行这些步骤。
5. 仅当真实 HATCH 提供 `wake()`、可信接受回执、`wakeup-ack` 持久对账后，才启用 Phase 6 Host Handoff。否则维持 `poll_only` 并明确标出真正自动 Muse 唤醒未通过。
6. 在获授权的隔离范围观察 Owner 消息、社交机会、Scheduler 抢占的完整证据，不用人工写 `accepted`/`confirmed` 伪造结果。

## 单一 Hook 的职责和频率

```text
每 60 秒：原 HATCH Hook
  ├─ 有界 tick --light             从统一 Agent Event Feed 采集事件
  ├─ 一次 outbox-drain             补偿已入队、有权限的写动作
  ├─ social-sync --if-idle-due     SQLite 至少 30 分钟节流
  └─ 满足真实宿主桥前提时：
       host-wakeup-prepare → HATCH wake() → 真实回执 → wakeup-ack
       然后真正的 Muse 读取活动并使用 Phase 6.1 Scheduler
```

- `tick_seconds=60` 是客户端配置，**不安装** Hook。
- `normal_scan_seconds=600` 是常规 Full Tick 内部资源扫描参数，**不等于** 600 秒 Light Tick。
- 项目中的 `tools/crewquay_phase3_hook.sh` 是一次性**参考执行体**，历史文件名不要求使用 Phase 3 老系统，也不应作为新定时任务安装。
- 有重入锁、有界请求预算；发现阶段不调用模型、不自动发布社交内容。
- `host-wakeup-prepare` 的 leased 不是 delivered；过期/unknown 不能无证据直接重唤醒。
- ***必须*** 使用同一 SQLite 的稳定幂等逻辑键保护 Outbox。`confirmed` 前不宣布实际发送成功。

## 验收与失败处理

- 记录 3–5 次实际运行开始时间、退出码、游标和不完整来源。`degraded`、`partial`、`deferred` 不能报全量成功。
- 使用单页 `limit=5` 对照当前生产事件端点，避免曾观察到的较大页长断连问题。
- 以 Muse 真正执行证据、平台消息 ID、远端动作 `confirmed` 检验闭环；对抢占检查 Checkpoint 与旧 generation fencing。
- 若出现并行入口或重复回复，优先在**已确认的多余入口**停止新触发并保存证据；不删除生产 SQLite、不重放未确认 Outbox、不恢复旧 watcher/独立 Executor。

更多操作细节见根 [README](../README.md) 与 [Host Handoff 契约](../patrol/components/host/README.md)。
