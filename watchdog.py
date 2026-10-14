#!/usr/bin/env python3
"""CrewQuay watchdog: relaunch ws_watch.py if it died while mode==active.

Runs every 2 min via cron. Lightweight: check state + pidfile, relaunch at
most one watcher, else exit quietly. Never prints secrets."""
import os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_state, save_state, ws_running, WORKDIR, now, PID_PATH

def main():
    st = load_state()
    if st.get("mode") == "active" and not ws_running():
        au = st.get("activeUntil") or 0
        la = st.get("lastActivityAt") or 0
        t = now()
        if t < au or (t - la) < 30 * 60:
            log = os.path.join(WORKDIR, "ws_watch.log")
            with open(log, "a") as lf:
                lf.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] watchdog relaunch\n")
                subprocess.Popen(
                    [sys.executable, os.path.join(WORKDIR, "ws_watch.py")],
                    stdout=lf, stderr=subprocess.STDOUT,
                    start_new_session=True, cwd=WORKDIR)
            # give it a moment, then confirm
            time.sleep(8)
            print("relaunched" if ws_running() else "relaunch attempted, not yet up")
        else:
            st["mode"] = "idle"; st["activeUntil"] = None; save_state(st)
            print("active window long expired; set idle")
    else:
        print(f"ok mode={st.get('mode')} ws={ws_running()}")

if __name__ == "__main__":
    main()
