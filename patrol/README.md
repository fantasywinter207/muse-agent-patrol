# Patrol 责任根

## Purpose
CrewQuay Agent 的 Python 巡检/状态/调度/投递客户端。由**原 HATCH 60 秒 Hook** 调用；不拥有第二套宿主任务或定时器。

## Responsibilities
组合事件采集、SQLite 持久化、队列、承诺、关注期、Phase 6.1 Activity Scheduler、自主社交候选、Host Handoff、Outbox 与可审计 CLI。远端游标保存为精确字符串，持久时间使用 UTC epoch 秒。

## Non-responsibilities
不安装/运行 Hook 或 cron，不托管模型，不自行给 Agent 授权，不代替 Muse 阅读意图，不保证真实宿主 `wake()` 已接通。`tick --light` 不是模型执行，也不会直接发远端消息。

## Public boundary
`index.py:main`；标准 CLI 为 `python -m patrol`。仓库根目录的旧巡检/兼容适配脚本不再是支持的运行入口，历史版本保留在 Git。SQLite 是唯一运行时状态源，JSON 仅供显式迁移。

## Allowed dependencies
仅依赖各组件公开 index，按组件 README 的责任边界集成；不能跨组件私有导入。宿主 HATCH runtime 不属于本仓库。

## Invariants
原 60 秒 HATCH Hook 为唯一宿主周期唤醒入口。Scheduler 管理本地时间片与安全点抢占，不管理宿主定时器。所有写动作需真实平台授权、不可变 Outbox logical key 和可靠回执；`confirmed` 才算远端完成。保护历史 SQLite、游标、承诺和未知动作对账。
