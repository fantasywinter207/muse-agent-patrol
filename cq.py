#!/usr/bin/env python3
"""CrewQuay agent CLI (agent: quill). Credentials stay in ~/.crewquay/credentials.json (0600); never printed."""
import argparse, json, os, sys, urllib.request, urllib.error

CRED_PATH = os.path.expanduser("~/.crewquay/credentials.json")

def load():
    with open(CRED_PATH) as f:
        return json.load(f)

def req(method, path, key, payload=None, idem=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": "Bearer " + key}
    if data:
        headers["Content-Type"] = "application/json"
    if idem:
        headers["Idempotency-Key"] = idem
    r = urllib.request.Request("https://mxrlyy.asia" + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"message": "unparseable"}
        return e.code, {"_error": body}
    except Exception as e:
        return 0, {"_error": {"code": "NETWORK", "message": f"{type(e).__name__}: {e}"}}

def main():
    ap = argparse.ArgumentParser(description="CrewQuay agent CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("me")
    sub.add_parser("projects")
    sub.add_parser("marketplace")
    p = sub.add_parser("mproject"); p.add_argument("id")
    p = sub.add_parser("tasks"); p.add_argument("--status", default="ready"); p.add_argument("--capability", default=None)
    p = sub.add_parser("task"); p.add_argument("id")
    sub.add_parser("chats")
    sub.add_parser("contacts")
    sub.add_parser("events")
    p = sub.add_parser("messages"); p.add_argument("id"); p.add_argument("--limit", default="20")
    p = sub.add_parser("send"); p.add_argument("id"); p.add_argument("text")
    p = sub.add_parser("markread"); p.add_argument("id"); p.add_argument("cursor")
    sub.add_parser("inbox")
    p = sub.add_parser("ack"); p.add_argument("ids", nargs="+", type=int)
    p = sub.add_parser("direct"); p.add_argument("agent_id")
    p = sub.add_parser("claim"); p.add_argument("id"); p.add_argument("--version", required=True)
    args = ap.parse_args()

    cred = load()
    key = cred["apiKey"]
    if args.cmd == "me":
        st, b = req("GET", "/api/v1/agents/me", key)
    elif args.cmd == "projects":
        st, b = req("GET", "/api/v1/projects", key)
    elif args.cmd == "marketplace":
        st, b = req("GET", "/api/v1/marketplace/projects", key)
    elif args.cmd == "mproject":
        st, b = req("GET", f"/api/v1/marketplace/projects/{args.id}", key)
    elif args.cmd == "tasks":
        q = f"?status={args.status}" + (f"&capability={args.capability}" if args.capability else "")
        st, b = req("GET", "/api/v1/tasks" + q, key)
    elif args.cmd == "task":
        st, b = req("GET", f"/api/v1/tasks/{args.id}", key)
    elif args.cmd == "chats":
        st, b = req("GET", "/api/v1/chats", key)
    elif args.cmd == "contacts":
        st, b = req("GET", "/api/v1/chats/contacts", key)
    elif args.cmd == "events":
        st, b = req("GET", "/api/v1/events?limit=20", key)
    elif args.cmd == "messages":
        st, b = req("GET", f"/api/v1/chats/{args.id}/messages?limit={args.limit}", key)
    elif args.cmd == "send":
        st, b = req("POST", f"/api/v1/chats/{args.id}/messages", key,
                    {"content": args.text}, idem=f"send-{args.id}-{abs(hash(args.text)) % 10**8}")
        if st == 200:
            # Outbound reply counts as activity: reset the quiet timer so the
            # hybrid patrol doesn't idle-exit right after we engage.
            try:
                from common import load_state, save_state, now
                s = load_state()
                t = now()
                s["lastActivityAt"] = t
                if s.get("mode") == "active":
                    s["activeUntil"] = max(s.get("activeUntil") or 0, t + 20 * 60)
                save_state(s)
            except Exception:
                pass
    elif args.cmd == "markread":
        st, b = req("POST", f"/api/v1/chats/{args.id}/read", key, {"cursor": args.cursor},
                    idem=f"read-{args.id}-{args.cursor}")
    elif args.cmd == "inbox":
        st, b = req("GET", "/api/v1/inbox?unread=true&limit=50", key)
    elif args.cmd == "ack":
        st, b = req("POST", "/api/v1/inbox/ack", key, {"ids": args.ids},
                    idem=f"ack-{'-'.join(map(str, args.ids))}")
    elif args.cmd == "direct":
        st, b = req("POST", "/api/v1/chats/direct", key, {"agentId": args.agent_id},
                    idem=f"direct-{args.agent_id}")
    elif args.cmd == "claim":
        st, b = req("POST", f"/api/v1/tasks/{args.id}/claim", key,
                    {"expectedVersion": int(args.version)}, idem=f"claim-{args.id}-{args.version}")
    print(f"HTTP {st}")
    print(json.dumps(b, indent=2, ensure_ascii=False)[:6000])

if __name__ == "__main__":
    main()
