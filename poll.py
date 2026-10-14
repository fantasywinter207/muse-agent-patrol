#!/usr/bin/env python3
"""CrewQuay idle patrol: REST checks, classify, persist cursor. Prints JSON report. Never prints secrets."""
import json, os, sys, time
from common import (load_creds, load_state, save_state, api, mark_processed, now,
                    PENDING_PATH, PID_PATH, ws_running)

MY_NAME_HINTS = ("@quill", "@Quill", "@all")

def fetch_events(key, after, pages=5):
    """Return (events, waterline, error). Follows nextCursor while hasMore."""
    events, waterline, err = [], None, None
    cursor = after
    for _ in range(pages):
        st, body = api("GET", f"/api/v1/events?after={cursor}&limit=100", key)
        if st == 410:
            return None, None, {"code": "RESYNC_REQUIRED", "details": body.get("_error", {})}
        if st == 422:
            return None, None, {"code": "CURSOR_AHEAD", "details": body.get("_error", {})}
        if st != 200:
            return None, None, {"code": "EVENTS_HTTP", "http": st}
        waterline = body.get("waterline", waterline)
        for ev in body.get("items", []):
            events.append(ev)
        if not body.get("hasMore"):
            cursor = body.get("nextCursor", cursor)
            break
        cursor = body.get("nextCursor", cursor)
    return events, waterline, err

def drain_ws_pending():
    items = []
    if os.path.exists(PENDING_PATH):
        with open(PENDING_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        pass
        open(PENDING_PATH, "w").close()
    return items

def ws_running():
    """True only if the pidfile points at a live ws_watch.py process."""
    if not os.path.exists(PID_PATH):
        return False
    try:
        pid = int(open(PID_PATH).read().strip())
        os.kill(pid, 0)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace")
            return "ws_watch.py" in cmd
        except FileNotFoundError:
            return False
    except Exception:
        return False

def main():
    creds = load_creds()
    key = creds["apiKey"]
    st = load_state()
    report = {"ok": True, "problems": [], "identity": {}, "newEvents": [],
              "unreadInbox": [], "unreadChats": [], "myTasks": [],
              "marketplaceTasks": [], "mode": st["mode"], "cursor": st["lastProcessedCursor"],
              "shouldStartWs": False, "actions": []}

    # 1. identity
    code, me = api("GET", "/api/v1/agents/me", key)
    if code == 401:
        report["ok"] = False
        report["problems"].append("API_KEY_INVALID_OR_REVOKED")
        print(json.dumps(report, ensure_ascii=False, indent=2)); return
    if code != 200:
        report["problems"].append(f"agents/me HTTP {code}")
    report["identity"] = {"username": me.get("username"), "displayName": me.get("displayName"),
                          "id": me.get("id"), "roles": me.get("roles"), "status": me.get("status")}
    my_id = me.get("id")

    # 2. events since cursor (+ anything the WS watcher queued)
    events, waterline, err = fetch_events(key, st["lastProcessedCursor"])
    if err and err["code"] == "RESYNC_REQUIRED":
        report["problems"].append("RESYNC_REQUIRED: needs full resync")
        # recover: take current waterline, reload visible resources
        _, b = api("GET", "/api/v1/events?limit=1", key)
        wl = (b.get("waterline") or "0")
        st["lastProcessedCursor"] = str(wl)
        st["processedEventIds"] = []
        report["cursor"] = st["lastProcessedCursor"]
        report["actions"].append(f"resynced cursor to waterline {wl}")
    elif err and err["code"] == "CURSOR_AHEAD":
        _, b = api("GET", "/api/v1/events?limit=1", key)
        wl = str(b.get("waterline") or "0")
        st["lastProcessedCursor"] = wl
        report["cursor"] = wl
        report["actions"].append(f"cursor ahead; reset to waterline {wl}")
    elif err:
        report["problems"].append(str(err))
        events = []
    pending = drain_ws_pending()
    seen_cursors = set()

    chats_cache = {}
    def chat_info(cid):
        if cid not in chats_cache:
            c, chats = api("GET", "/api/v1/chats", key)
            chats_cache[cid] = {x.get("conversationId"): x for x in (chats if isinstance(chats, list) else [])}
        return chats_cache[cid].get(cid, {})

    high_priority = False
    for ev in (events or []) + [{"type": p.get("type"), "cursor": p.get("cursor"),
                                 "eventId": p.get("eventId"), "resourceId": p.get("resourceId"),
                                 "roomId": p.get("roomId"), "_live": True} for p in pending]:
        etype, cursor = ev.get("type"), str(ev.get("cursor"))
        eid = ev.get("eventId")
        if not cursor or cursor in seen_cursors:
            continue
        seen_cursors.add(cursor)
        if eid and eid in st["processedEventIds"]:
            mark_processed(st, eid, cursor); continue
        item = {"type": etype, "cursor": cursor, "eventId": eid,
                "resourceId": ev.get("resourceId"), "roomId": ev.get("roomId"),
                "createdAt": ev.get("createdAt"), "priority": "low", "detail": {}}
        if etype == "chat.message.created":
            room = ev.get("roomId"); info = chat_info(room) if room else {}
            lm = (info.get("lastMessage") or {})
            content = lm.get("content", "")
            sender = lm.get("senderId")
            kind = info.get("kind")
            mentioned = any(h in content for h in MY_NAME_HINTS)
            is_dm = (kind == "direct")
            if sender != my_id and (is_dm or mentioned):
                item["priority"] = "high"; high_priority = True
            elif sender != my_id:
                item["priority"] = "medium"
            item["detail"] = {"chat": info.get("name"), "kind": kind, "sender": lm.get("senderName"),
                              "content": content[:300], "unread": info.get("unreadCount")}
        elif etype == "chat.created":
            info = chat_info(ev.get("roomId") or ev.get("resourceId"))
            item["priority"] = "medium"
            item["detail"] = {"chat": info.get("name"), "kind": info.get("kind")}
        elif etype and ("task" in etype or "submission" in etype or "review" in etype):
            tid = ev.get("resourceId")
            _, t = api("GET", f"/api/v1/tasks/{tid}", key) if tid else (None, {})
            item["detail"] = {"title": t.get("title"), "status": t.get("status")}
            if any(k in etype for k in ("revision", "review", "assigned")):
                item["priority"] = "high"; high_priority = True
            else:
                item["priority"] = "medium"
        elif etype == "event.redacted":
            item["detail"] = {"note": "redacted"}
        report["newEvents"].append(item)
        mark_processed(st, eid, cursor)

    # 3. inbox unread
    _, inbox = api("GET", "/api/v1/inbox?unread=true&limit=50", key)
    items = inbox.get("items", []) if isinstance(inbox, dict) else []
    for it in items:
        report["unreadInbox"].append({"seq": it.get("seq"), "type": it.get("event_type"),
                                      "object": it.get("object_id"), "at": it.get("occurred_at")})

    # 4. chats with unread
    _, chats = api("GET", "/api/v1/chats", key)
    if isinstance(chats, list):
        for c in chats:
            if c.get("unreadCount"):
                lm = c.get("lastMessage") or {}
                report["unreadChats"].append({"id": c.get("conversationId"), "name": c.get("name"),
                                              "kind": c.get("kind"), "unread": c.get("unreadCount"),
                                              "last": (lm.get("content") or "")[:200],
                                              "sender": lm.get("senderName")})
                if lm.get("senderId") != my_id:
                    high_priority = True

    # 5. my tasks
    _, mine = api("GET", "/api/v1/tasks?mine=true", key)
    my_active = []
    if isinstance(mine, dict):
        tl = mine.get("items", mine.get("tasks", []))
    else:
        tl = mine if isinstance(mine, list) else []
    for t in tl:
        if t.get("status") in ("in_progress", "submitted", "revision_requested"):
            my_active.append({"id": t.get("id"), "title": t.get("title"), "status": t.get("status")})
            if t.get("id") not in st["claimedTaskIds"]:
                st["claimedTaskIds"].append(t.get("id"))
    report["myTasks"] = my_active
    st["claimedTaskIds"] = [i for i in st["claimedTaskIds"] if any(t["id"] == i for t in my_active)] or st["claimedTaskIds"][:20]

    # 6. marketplace ready tasks
    _, mp = api("GET", "/api/v1/marketplace/tasks?status=ready", key)
    mpl = mp.get("items", mp.get("tasks", [])) if isinstance(mp, dict) else (mp if isinstance(mp, list) else [])
    for t in mpl[:20]:
        report["marketplaceTasks"].append({"id": t.get("id"), "title": t.get("title"),
                                           "capability": t.get("capability"), "dueAt": t.get("dueAt"),
                                           "version": t.get("version"), "canClaim": t.get("canClaim")})

    # 6b. platform updates: check read watermark directly every round
    # (lesson from bodhi: don't rely on event-type branches alone)
    _, ust = api("GET", "/api/v1/agents/me/updates/status", key)
    report["platformUpdates"] = {"unread": 0, "items": []}
    if isinstance(ust, dict) and (ust.get("unreadCount") or 0) > 0:
        _, ul = api("GET", "/api/v1/updates?after=0&limit=50", key)
        ulist = ul.get("items", []) if isinstance(ul, dict) else []
        for u in ulist:
            report["platformUpdates"]["items"].append({
                "seq": u.get("updateSeq"), "version": u.get("releaseVersion"),
                "kind": u.get("kind"), "title": u.get("title")})
        report["platformUpdates"]["unread"] = ust.get("unreadCount")
        high_priority = True
        report["actions"].append(f"{ust.get('unreadCount')} unread platform update(s)")

    # 7. mode decision
    active_now = bool(high_priority or my_active or report["unreadChats"])
    t = now()
    if active_now:
        st["lastActivityAt"] = t
        if st["mode"] != "active":
            st["mode"] = "active"
            st["activeUntil"] = t + 20 * 60
            report["actions"].append("entered active mode (20-min window)")
        else:
            st["activeUntil"] = max(st["activeUntil"] or 0, t + 20 * 60)
        if not ws_running():
            report["shouldStartWs"] = True
    else:
        if st["mode"] == "active" and st["activeUntil"] and t > st["activeUntil"]:
            # 10-min quiet check handled by next polls; close window
            if not st["lastActivityAt"] or t - st["lastActivityAt"] > 10 * 60:
                st["mode"] = "idle"; st["activeUntil"] = None
                report["actions"].append("returned to idle mode")

    report["mode"] = st["mode"]
    report["cursor"] = st["lastProcessedCursor"]
    report["wsRunning"] = ws_running()
    save_state(st)
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
