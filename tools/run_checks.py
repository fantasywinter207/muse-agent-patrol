"""Credential-free checks with reproducible source hashes and raw command evidence."""
import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    out=Path(args.output).resolve()
    out.mkdir(parents=True,exist_ok=True)
    inputs={}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part in (".git","__pycache__",".venv","validation") for part in path.relative_to(root).parts) and path.suffix in (".py",".md",".toml",".json",".txt",".yml"):
            if path.is_relative_to(out):
                continue
            inputs[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    head=subprocess.run(["git","rev-parse","HEAD"],cwd=root,capture_output=True,text=True)
    dirty=subprocess.run(["git","status","--porcelain"],cwd=root,capture_output=True,text=True)
    report={"started_at_utc_epoch":time.time(),"python":platform.python_version(),"sqlite":sqlite3.sqlite_version,
        "platform":platform.platform(),"git_head":head.stdout.strip(),"dirty_worktree":bool(dirty.stdout),
        "source_sha256":inputs,"commands":[],"network":"fake transports only; no real credentials"}
    commands=[[sys.executable,"-m","unittest","discover","-s","tests","-v"],[sys.executable,"tools/validate_architecture.py"]]
    for index,cmd in enumerate(commands):
        started=time.time()
        r=subprocess.run(cmd,cwd=root,capture_output=True,text=True,timeout=180)
        name="command-"+str(index+1)+".txt"
        raw="$ "+" ".join(cmd)+"\n"+r.stdout+"\n"+r.stderr+"\nexit="+str(r.returncode)+"\n"
        (out/name).write_text(raw,encoding="utf-8")
        report["commands"].append({"command":cmd,"exit_code":r.returncode,"duration_seconds":time.time()-started,"raw_output":name,"sha256":hashlib.sha256(raw.encode()).hexdigest()})
        print(r.stdout,end="")
        print(r.stderr,end="",file=sys.stderr)
    report["ended_at_utc_epoch"]=time.time()
    report["passed"]=all(c["exit_code"]==0 for c in report["commands"])
    (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"passed":report["passed"],"report":str(out/"report.json")}))
    return 0 if report["passed"] else 1


if __name__=="__main__":
    raise SystemExit(main())
