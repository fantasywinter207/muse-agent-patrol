#!/usr/bin/env python3
"""CrewQuay active-mode WebSocket watcher.

Spec: connect, record ready waterline W, backfill events (savedCursor -> W),
listen live; queue pointers to ws_pending.jsonl; exit per active-mode rules:
- 20-min initial window (state.activeUntil); after it, exit when 10 min quiet
- single connection max 60 min -> reconnect + backfill
- REST reconcile every 10 min; 410 -> resync; 422 -> cursor reset
Never prints secrets.
"""
import json, os, sys, time
import common
from common import (load_creds, load_state, save_state, api, mark_processed, now,
                    PENDING_PATH, PID_PATH, WORKDIR, ws_running)
sys.path.insert(0, os.path.join(WORKDIR, "vendor"))
import websocket

ACTIONABLE = ("chat.message.created", "chat.created", "task.", "submission.", "review.",
              "room.member", "assignment.")

def queue_event(ev):
    with open(PENDING_PATH, "a") as f:
        f.write(json.dumps({"type": ev.get("type"), "cursor": str(ev.get("cursor")),
                            "eventId": ev.get("eventId"), "resourceId": ev.get("resourceId"),
                            "roomId": ev.get("roomId"), "projectId": ev.get("projectId"),
                            "createdAt": ev.get("createdAt")}) + "\n")

def backfill(key, st, to_waterline):
    """REST backfill savedCursor -> W. Returns (new_count, error)."""
    after = st["lastProcessedCursor"]
    new_count = 0
    for _ in range(10):
        code, body = api("GET", f"/api/v1/events?after={after}&to={to_waterline}&limit=100", key)
        if code == 410:
            return new_count, "RESYNC_REQUIRED"
        if code == 422:
            return new_count, "CURSOR_AHEAD"
        if code != 200:
            return new_count, f"HTTP_{code}"
        for ev in body.get("items", []):
            eid, cursor = ev.get("eventId"), str(ev.get("cursor"))
            if eid and eid in st["processedEventIds"]:
                mark_processed(st, eid, cursor); continue
            queue_event(ev)
            if any(a in (ev.get("type") or "") for a in ACTIONABLE):
                st["lastActivityAt"] = now()
            mark_processed(st, eid, cursor)
            new_count += 1
        if not body.get("hasMore"):
            break
        after = body.get("nextCursor", after)
    save_state(st)
    return new_count, None

def resync(key, st):
    """410 recovery: fresh waterline, reload visible resources, cursor=W."""
    code, body = api("GET", "/api/v1/events?limit=1", key)
    wl = str(body.get("waterline", "0")) if code == 200 else st["lastProcessedCursor"]
    for path in ("/api/v1/chats", "/api/v1/tasks?mine=true", "/api/v1/inbox?unread=true&limit=50"):
        api("GET", path, key)  # refresh view; poll.py does the detailed work
    st["lastProcessedCursor"] = wl
    st["processedEventIds"] = []
    st["streamId"] = None
    save_state(st)
    return wl

def connect(key):
    ws = websocket.create_connection(
        "wss://mxrlyy.asia/api/v1/events/ws",
        header={"Authorization": f"Bearer {key}"}, timeout=20)
    ws.settimeout(10)  # must be < server ping interval (15s), else pings reset the timeout and the loop never iterates
    ready = json.loads(ws.recv())
    assert ready.get("type") == "ready", f"unexpected first frame: {str(ready)[:120]}"
    return ws, ready

def main():
    creds = load_creds(); key = creds["apiKey"]
    st = load_state()
    if ws_running():
        print("already running"); return
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))
    conn_start = now()
    last_reconcile = now()
    backoff = 5
    try:
        while True:
            # quiet-exit check also applies before (re)connecting
            t = now()
            au = st.get("activeUntil")
            la = st.get("lastActivityAt") or 0
            if au and t > au and t - la > 10 * 60:
                print("quiet 10 min past window; exiting active mode", flush=True)
                st["mode"] = "idle"; st["activeUntil"] = None; st["wsConnected"] = False
                save_state(st); return
            try:
                ws, ready = connect(key)
            except Exception as e:
                msg = str(e)
                if "401" in msg or "4001" in msg:
                    print(f"credential invalid ({msg[:80]}); not retrying", flush=True)
                    st["wsConnected"] = False; save_state(st); return
                print(f"connect failed ({msg[:100]}); retry in {backoff}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                st = load_state()
                continue
            backoff = 5
            W = str(ready["waterline"])
            st["wsConnected"] = True; st["wsConnectionId"] = ready.get("connectionId")
            st["streamId"] = ready.get("streamId")
            n, err = backfill(key, st, W)
            print(f"connected waterline={W} backfilled={n} err={err}", flush=True)
            if err == "RESYNC_REQUIRED":
                W = resync(key, st); ws.close()
                continue
            if err == "CURSOR_AHEAD":
                st["lastProcessedCursor"] = W; save_state(st); ws.close()
                continue
            if err:
                # transient (network) error during backfill: reconnect with backoff
                print(f"backfill transient error {err}; reconnecting", flush=True)
                try:
                    ws.close()
                except Exception:
                    pass
                time.sleep(min(backoff, 30)); backoff = min(backoff * 2, 60)
                st = load_state()
                continue
            while True:
                t = now()
                # exit rules
                if t - conn_start > 60 * 60:
                    print("60-min connection limit; reconnecting", flush=True); break
                # if the patrol moved us back to idle, exit promptly
                st = load_state()
                if st.get("mode") != "active":
                    print("mode is idle; exiting watcher", flush=True)
                    st["wsConnected"] = False; save_state(st)
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return
                au = st.get("activeUntil")
                la = st.get("lastActivityAt") or 0
                if au and t > au and t - la > 10 * 60:
                    print("quiet 10 min past window; exiting active mode", flush=True)
                    st["mode"] = "idle"; st["activeUntil"] = None; st["wsConnected"] = False
                    save_state(st); ws.close(); return
                if t - last_reconcile > 10 * 60:
                    code, b = api("GET", "/api/v1/events?limit=1", key)
                    wl = str(b.get("waterline", W)) if code == 200 else W
                    n2, e2 = backfill(key, st, wl)
                    print(f"reconcile backfilled={n2} err={e2}", flush=True)
                    if e2 == "RESYNC_REQUIRED":
                        resync(key, st)
                    last_reconcile = t
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException as e:
                    print(f"ws closed: {e}", flush=True)
                    code = getattr(e, "status_code", None)
                    if code == 4001:
                        print("credential invalid; not reconnecting", flush=True); return
                    break  # reconnect loop (covers 4009 resync via backfill)
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                ftype = frame.get("type")
                if ftype in ("ready", "events.replay", "pong"):
                    continue
                if ftype == "error":
                    print(f"ws error frame: {str(frame)[:200]}", flush=True); continue
                eid, cursor = frame.get("eventId"), str(frame.get("cursor", ""))
                if eid and eid in st["processedEventIds"]:
                    continue
                queue_event(frame)
                if any(a in (ftype or "") for a in ACTIONABLE):
                    st["lastActivityAt"] = now()
                # advance cursor for live frames past waterline
                try:
                    if cursor and int(cursor) > int(st["lastProcessedCursor"]):
                        st["lastProcessedCursor"] = cursor
                except (ValueError, TypeError):
                    pass
                if eid:
                    ids = st["processedEventIds"]; ids.append(eid); del ids[:-1000]
                save_state(st)
            try:
                ws.close()
            except Exception:
                pass
            conn_start = now()  # new connection window
    finally:
        try:
            st = load_state(); st["wsConnected"] = False; save_state(st)
        except Exception:
            pass
        if os.path.exists(PID_PATH):
            os.remove(PID_PATH)

if __name__ == "__main__":
    main()
