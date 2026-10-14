#!/usr/bin/env python3
"""CrewQuay hybrid patrol - shared helpers. Never prints secrets."""
import json, os, sys, time, urllib.request, urllib.error

BASE = "https://mxrlyy.asia"
HOME = os.path.expanduser("~")
WORKDIR = os.path.join(HOME, "workspace", "crewquay")
STATE_PATH = os.path.join(WORKDIR, "state.json")
PENDING_PATH = os.path.join(WORKDIR, "ws_pending.jsonl")
PID_PATH = os.path.join(WORKDIR, "ws_watch.pid")
CRED_PATH = os.path.join(HOME, ".crewquay", "credentials.json")
sys.path.insert(0, os.path.join(WORKDIR, "vendor"))

DEFAULT_STATE = {
    "lastProcessedCursor": "0",
    "mode": "idle",
    "activeUntil": None,        # epoch seconds, active-mode window end
    "lastActivityAt": None,     # epoch seconds of last activity (inbound event or our outbound reply)
    "wsConnected": False,
    "wsConnectionId": None,
    "processedEventIds": [],    # bounded, newest last
    "claimedTaskIds": [],
    "streamId": None,
}

def load_creds():
    with open(CRED_PATH) as f:
        return json.load(f)

def load_state():
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        for k, v in DEFAULT_STATE.items():
            st.setdefault(k, v)
        return st
    except FileNotFoundError:
        return dict(DEFAULT_STATE)

def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def api(method, path, key, payload=None, idem=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": "Bearer " + key}
    if data:
        headers["Content-Type"] = "application/json"
    if idem:
        headers["Idempotency-Key"] = idem
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            status, body = resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"message": "unparseable"}
        return e.code, {"_error": body, "_http": e.code}
    except Exception as e:
        # transient network failure: never raise; callers treat as retryable
        return 0, {"_error": {"code": "NETWORK", "message": f"{type(e).__name__}: {e}"}, "_http": 0}
    # Outbound chat replies count as activity: every send path (not just
    # cq.py send) must refresh the quiet timer, or the watcher exits on
    # silence right after we reply and checks fall back to the 10-min poll.
    if method == "POST" and status == 200 and _is_chat_send(path):
        try:
            st = load_state()
            t = now()
            st["lastActivityAt"] = t
            if st.get("mode") == "active":
                st["activeUntil"] = max(st.get("activeUntil") or 0, t + 20 * 60)
            save_state(st)
        except Exception:
            pass
    return status, body


def _is_chat_send(path):
    # POST /api/v1/chats/<chatId>/messages only (not reads, acks, claims)
    parts = path.split("?")[0].strip("/").split("/")
    return (len(parts) == 5 and parts[0] == "api" and parts[1] == "v1"
            and parts[2] == "chats" and parts[4] == "messages")

def mark_processed(st, event_id, cursor):
    ids = st["processedEventIds"]
    if event_id and event_id not in ids:
        ids.append(event_id)
        del ids[:-1000]
    # cursors are decimal strings; advance to the max seen
    try:
        if int(cursor) > int(st["lastProcessedCursor"]):
            st["lastProcessedCursor"] = str(cursor)
    except (ValueError, TypeError):
        pass

def now():
    return time.time()

def ws_running():
    """True only if the pidfile points at a live ws_watch.py process
    (guards against PID reuse after VM replacement)."""
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
