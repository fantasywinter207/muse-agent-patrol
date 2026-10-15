import argparse
import importlib.metadata
import importlib.util
import hashlib
import json
import platform
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from ...config.index import load_config
from ...migration.index import migrate_legacy
from ...outbox.index import Outbox
from ...persistence.index import Database, Busy
from ...promises.index import Promises
from ...queue.index import Queue, export_pending
from ...transport.index import Client, credential_provider
from ..process.lock import instance_lock
from .scheduler import Tick


def timestamp(value):
    if value is None:
        return None
    try:
        number=float(value)
        if number>=0 and number<float("inf"):
            return number
    except ValueError:
        pass
    date=datetime.fromisoformat(value.replace("Z","+00:00"))
    if date.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return date.timestamp()


def evidence(path):
    with open(path,encoding="utf-8") as f:
        value=json.load(f)
    if not isinstance(value,dict) or not value:
        raise ValueError("nonempty evidence object required")
    return value


def parser():
    p=argparse.ArgumentParser(description="CrewQuay v3: durable patrol, no model wake-up")
    p.add_argument("--config")
    p.add_argument("--db")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("init")
    sub.add_parser("doctor")
    sub.add_parser("status")
    t=sub.add_parser("tick")
    t.add_argument("--offline",action="store_true")
    t.add_argument("--ws",action="store_true")
    t.add_argument("--quiet",action="store_true")
    m=sub.add_parser("migrate")
    m.add_argument("directory")
    m.add_argument("--confirm-stopped",action="store_true")
    b=sub.add_parser("backup")
    b.add_argument("destination")
    a=sub.add_parser("stage")
    a.add_argument("action",choices=("send","ack","read"))
    a.add_argument("--logical-key",required=True)
    a.add_argument("--actor",required=True)
    a.add_argument("--target",default="")
    a.add_argument("--payload-file",required=True)
    r=sub.add_parser("resolve")
    r.add_argument("id")
    r.add_argument("--evidence",required=True)
    r.add_argument("--confirmed",action="store_true")
    r.add_argument("--proven-absent",action="store_true")
    a=sub.add_parser("promise-add")
    for name in ("actor","recipient","source-message-id","text","start-by","next-action-at"):
        a.add_argument("--"+name,required=True)
    a.add_argument("--due-at")
    a.add_argument("--kind",default="human_action")
    a.add_argument("--work-id")
    a.add_argument("--reason")
    a=sub.add_parser("promise-progress")
    a.add_argument("id")
    a.add_argument("--evidence",required=True)
    a.add_argument("--next-action-at",required=True)
    a=sub.add_parser("promise-complete")
    a.add_argument("id")
    a.add_argument("--evidence",required=True)
    a=sub.add_parser("extension-request")
    a.add_argument("id")
    a.add_argument("--due-at",required=True)
    a.add_argument("--reason",required=True)
    a.add_argument("--evidence",required=True)
    a=sub.add_parser("extension-approve")
    a.add_argument("id")
    a.add_argument("--due-at",required=True)
    a.add_argument("--approved-by",required=True)
    a.add_argument("--evidence",required=True)
    a=sub.add_parser("resume")
    a.add_argument("id")
    a.add_argument("--evidence",required=True)
    a=sub.add_parser("export-pending")
    a.add_argument("--limit",type=int,default=50)
    a=sub.add_parser("work-claim")
    a.add_argument("id")
    a.add_argument("--owner",required=True)
    a=sub.add_parser("work-finish")
    a.add_argument("id")
    a.add_argument("--owner",required=True)
    a.add_argument("--lease-version",type=int,required=True)
    a.add_argument("--status",choices=("done","blocked","needs_approval","retry_wait"),default="done")
    a.add_argument("--evidence",required=True)
    a.add_argument("--reason")
    a.add_argument("--retry-at")
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        cfg=load_config(args.config,database=args.db)
        # File lock is stable across process exits and migrations. Never unlink it.
        with instance_lock(cfg.database+".tick.lock"):
            db=Database(cfg.database,cfg.journal)
            now=time.time()
            if args.command=="init":
                result={"initialized":True,"schema_version":1,"journal":cfg.journal}
            elif args.command=="doctor":
                try:
                    ws_version=importlib.metadata.version("websocket-client")
                except importlib.metadata.PackageNotFoundError:
                    ws_version=None
                spec=importlib.util.find_spec("websocket") if ws_version else None
                origin=spec.origin if spec else None
                ws_hash=hashlib.sha256(Path(origin).read_bytes()).hexdigest() if origin and Path(origin).is_file() else None
                result={"python":platform.python_version(),"platform":platform.system(),"sqlite":sqlite3.sqlite_version,
                    "journal":cfg.journal,"integrity":db.integrity(),"websocket_client":ws_version,
                    "websocket_origin":origin,"websocket_entry_sha256":ws_hash,
                    "writes_enabled":cfg.allow_writes,"model_executor":False,
                    "scheduler":"external timer required; this command installs nothing"}
            elif args.command=="status":
                result={"queue":db.read("SELECT status,count(*) AS count FROM work_items GROUP BY status"),
                    "outbox":db.read("SELECT status,count(*) AS count FROM outbox GROUP BY status"),
                    "promises":db.read("SELECT id,status,start_by,original_due_at,due_at,next_action_at,first_action_at,last_progress_at FROM promises"),
                    "cursors":db.read("SELECT * FROM sync_cursors"),"attention":db.read("SELECT * FROM attention_sessions"),
                    "alert_count":db.read("SELECT count(*) AS count FROM alerts")[0]["count"],
                    "runs":db.read("SELECT id,started_at,ended_at,status FROM patrol_runs ORDER BY started_at DESC LIMIT 5")}
            elif args.command=="tick":
                client=None if args.offline else Client(cfg,credential_provider(cfg))
                result=Tick(db,cfg,client).run(websocket=args.ws)
                if args.quiet and result.get("status")=="ok" and not result.get("alerts") and not result.get("completed_work") and not result.get("outbox_attempts"):
                    return 0
            elif args.command=="migrate":
                result=migrate_legacy(db,args.directory,now,args.confirm_stopped)
            elif args.command=="backup":
                result={"backup":db.backup(args.destination),"integrity":"ok"}
            elif args.command=="stage":
                with open(args.payload_file,encoding="utf-8") as f:
                    payload=json.load(f)
                ident=Outbox(db).stage(args.logical_key,args.actor,args.action,args.target,payload,now)
                result={"outbox_id":ident,"status":"prepared","sent":False}
            elif args.command=="resolve":
                Outbox(db).resolve(args.id,evidence(args.evidence),now,args.confirmed,args.proven_absent)
                result={"resolved":args.id,"remote_request_issued":False}
            elif args.command=="promise-add":
                ident=Promises(db).create(args.actor,args.recipient,args.source_message_id,args.text,now,
                    timestamp(args.start_by),timestamp(args.next_action_at),timestamp(args.due_at),
                    work_id=args.work_id,kind=args.kind,objective_reason=args.reason)
                result={"promise_id":ident}
            elif args.command=="promise-progress":
                Promises(db).progress(args.id,now,evidence(args.evidence),timestamp(args.next_action_at))
                result={"progress_recorded":args.id}
            elif args.command=="promise-complete":
                Promises(db).complete(args.id,now,evidence(args.evidence))
                result={"completed":args.id}
            elif args.command=="extension-request":
                Promises(db).request_extension(args.id,timestamp(args.due_at),args.reason,evidence(args.evidence),now)
                result={"extension_requested":args.id,"deadline_changed":False}
            elif args.command=="extension-approve":
                Promises(db).approve_extension(args.id,timestamp(args.due_at),args.approved_by,evidence(args.evidence),now)
                result={"extension_approved":args.id}
            elif args.command=="resume":
                Queue(db,cfg.lease_seconds).resume(args.id,now,evidence(args.evidence))
                result={"resumed":args.id}
            elif args.command=="export-pending":
                result=export_pending(db,now,args.limit)
            elif args.command=="work-claim":
                item=Queue(db,cfg.lease_seconds).claim_specific(args.id,args.owner,now)
                result={"work_id":item["id"],"lease_owner":item["lease_owner"],
                        "lease_version":item["lease_version"],"lease_until":item["lease_until"],
                        "payload":item["payload"],"warning":"local claim is not CrewQuay role authorization"}
            elif args.command=="work-finish":
                if args.status=="retry_wait" and not args.retry_at:
                    raise ValueError("retry_wait requires explicit retry timestamp")
                retry_at=timestamp(args.retry_at) if args.retry_at else None
                if retry_at is not None and retry_at<=now:
                    raise ValueError("retry_at must be in the future")
                Queue(db,cfg.lease_seconds).finish(
                    {"id":args.id,"lease_owner":args.owner,"lease_version":args.lease_version},
                    now,evidence=evidence(args.evidence),status=args.status,
                    reason=args.reason,retry_at=retry_at)
                result={"finished_work":args.id,"status":args.status,"remote_action_issued":False}
            print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
            return 0 if result.get("status") not in ("authorization_failed","failed","deferred","degraded","partial") and result.get("imported") is not False else 1
    except Busy:
        print(json.dumps({"status":"busy","action":"deferred; no concurrent writer"}))
        return 75
    except Exception as e:
        # Exception bodies can contain server/credential data. Only the type is public.
        print(json.dumps({"status":"failed","error":type(e).__name__,"hint":"check configuration, schema and local evidence; no secrets printed"}))
        return 1
