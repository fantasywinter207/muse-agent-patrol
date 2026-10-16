# 远端发现与恢复

## Purpose
远端发现与恢复。

## Responsibilities
分页资源快照、事件入库+入队+游标原子提交、stream恢复、处理水位。

## Non-responsibilities
不执行消息所要求的工作、不声称快照捕获轮询间所有变化。

## Public boundary
`index.py` 导出 Collector/Sync/event_time。外部代码不得深导入私有实现。

## Allowed dependencies
persistence.index、queue.index、transport.index。

## Invariants
收到指针不等于处理；页面失败不跨越；received与handled分离。优先读取可见聊天与消息，再处理大规模 inbox 历史积压；每条已存储消息在提交事件、work 与 cursor 的事务内同时获得 metadata-only 评分；真实 Muse 评分可覆盖，但不得伪造模型调用。新消息的新鲜度在首次事件入库时（或旧库首次检查时使用首次 observed_at）写入不可被重复抓取延长的 message_receipts，不能等 inspect_event 执行才根据当前时间决定是否仍需待办；历史积压公平清理，不跳过旧 cursor 或直接 ACK。

## Testing boundary
通过公开边界使用临时数据库、fake HTTP/WS和真实多进程故障场景核验；不读取真实凭据、不访问业务API。

## Invalid pagination responses
When an upstream REST page returns a malformed, regressive, or non-advancing cursor while `hasMore` is true, `Collector.pages` treats the page as a failed source, preserves the previous received cursor, records `INVALID_PAGE_OR_CURSOR` and applies persisted retry backoff. A regressive empty page must **not** be coerced into a successful terminal page. The collector continues other independent sources. The equivalent event-stream guard records `INVALID_EVENT_PAGE_OR_CURSOR`. This is defense against bad upstream responses; the real endpoint exhibiting this behavior has not been verified.

## Phase 5 Agent Event Feed

`Collector.agent_events(now)` only calls `GET /api/v1/agent/events` with the server-owned recipient cursor, stable `streamId`, bounded page size and a pinned `to` until the batch is terminal. It uses independent `sync_cursors(source='agent_events',scope='global')`; the legacy Full Tick `events` cursor is untouched. `Sync.page` persists each authorized normalized message, immutable first-receipt freshness, baseline score, local inspector, event pointer and feed cursor inside one SQLite transaction. Failed, malformed, redacted, expired or inconsistent events must not be inferred from polling chat history. No event stream reset, watermark jump or secret-bearing log is permitted after 410/cursor regression/context failure.
