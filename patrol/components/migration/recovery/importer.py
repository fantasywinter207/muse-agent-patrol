"""Import without modifying old files. Unknown ledger formats remain visible blocked work."""
import hashlib
import json
import re
from pathlib import Path
from ...persistence.index import canonical, cursor
from ...queue.index import enqueue_tx


ALLOWED={"state.json","ws_pending.jsonl","promises.jsonl","patrol_runs.jsonl"}


def has_credentials(value):
    sensitive={"apikey","apitoken","authorization","credentials","credentialblob","password","passwd",
               "clientsecret","secretkey","privatekey","accesstoken","refreshtoken","bearer","cookie","setcookie"}
    if isinstance(value,dict):
        for key,item in value.items():
            normalized=re.sub(r"[^a-z0-9]","",str(key).lower())
            if normalized in sensitive or has_credentials(item):
                return True
    elif isinstance(value,list):
        return any(has_credentials(item) for item in value)
    elif isinstance(value,str):
        if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
            return True
        if re.search(r"(?i)(authorization\s*:\s*bearer|api[_ -]?key\s*[:=])\s*\S+",value):
            return True
    return False


def migrate_legacy(db, directory, now, stopped=False):
    if not stopped:
        raise ValueError("stop every legacy writer first; --confirm-stopped is required")
    directory=Path(directory).resolve()
    records=[]
    errors=[]
    for name in sorted(ALLOWED):
        path=directory/name
        if not path.exists():
            continue
        if path.is_symlink() or path.stat().st_size>16*1024*1024:
            raise ValueError("unsupported legacy file")
        raw=path.read_bytes()
        digest=hashlib.sha256(raw).hexdigest()
        entries=[]
        for number,line in enumerate(raw.decode("utf-8-sig").splitlines() if name.endswith(".jsonl") else [raw.decode("utf-8-sig")],1):
            if not line.strip():
                continue
            try:
                obj=json.loads(line)
                if not isinstance(obj,dict):
                    raise ValueError("object required")
                # Input source is retained untouched, including malformed lines.
                if has_credentials(obj):
                    raise ValueError("credential material is outside migration scope")
                entries.append((number,obj))
            except ValueError:
                errors.append({"source":name,"line":number,"reason":"invalid record; original retained"})
        records.append((name,digest,entries))
    if errors:
        return {"imported":False,"errors":errors,"note":"no database import occurred"}
    result=[]
    with db.transaction() as c:
        for name,digest,entries in records:
            if c.execute("SELECT 1 FROM migrations WHERE source_hash=? AND source_name=?",(digest,name)).fetchone():
                result.append({"source":name,"status":"already_imported"})
                continue
            for line,obj in entries:
                c.execute("INSERT INTO legacy_records VALUES(?,?,?) ON CONFLICT(source_hash,line_no) DO NOTHING",(digest,line,canonical(obj)))
                if name=="state.json":
                    saved=cursor(str(obj.get("lastProcessedCursor","0")))
                    stream=str(obj.get("streamId") or "")
                    old=c.execute("SELECT stream,received FROM sync_cursors WHERE source='events' AND scope='global'").fetchone()
                    if old:
                        raise ValueError("event sync already exists; migrate into a fresh database only")
                    c.execute("""INSERT INTO sync_cursors(source,scope,stream,received,handled) VALUES('events','global',?,?,?)""",(stream,saved,saved))
                    chats=obj.get("chat_cursors",{})
                    if not isinstance(chats,dict):
                        raise ValueError("chat_cursors must be an object")
                    for chat,value in chats.items():
                        value=cursor(str(value))
                        c.execute("INSERT INTO sync_cursors(source,scope,received,handled) VALUES('messages',?,?,?)",(chat,value,value))
                    if obj.get("updates_through") is not None:
                        value=cursor(str(obj["updates_through"]))
                        c.execute("INSERT INTO sync_cursors(source,scope,received,handled) VALUES('updates','global',?,?)",(value,value))
                    # Unverified ACKs and old activity are retained, never guessed as handled.
                    if obj.get("pending_inbox_ack"):
                        ident=enqueue_tx(c,"migration:ack:"+digest,"migration_review",{"legacy_hash":digest,"line":line,"reason":"verify message coverage and ACK side effects"},now)
                        c.execute("UPDATE work_items SET status='blocked',block_reason='legacy ACK requires reconciliation' WHERE id=?",(ident,))
                elif name=="ws_pending.jsonl":
                    external=str(obj.get("eventId") or digest+":"+str(line))
                    ec=cursor(str(obj["cursor"])) if obj.get("cursor") is not None else None
                    c.execute("""INSERT INTO events(source,scope,stream,external_key,cursor,payload,observed_at)
                        VALUES('legacy_pending','global','',?,?,?,?) ON CONFLICT(source,scope,stream,external_key) DO NOTHING""",(external,ec,canonical(obj),now))
                    row=c.execute("SELECT id FROM events WHERE source='legacy_pending' AND external_key=?",(external,)).fetchone()
                    enqueue_tx(c,"event:"+str(row[0]),"inspect_event",{"event_id":row[0]},now,event_id=row[0])
                elif name=="promises.jsonl":
                    # No published v2 record schema exists: preserve exact records and require explicit
                    # mapping, rather than silently declaring an unknown commitment done.
                    ident=enqueue_tx(c,"migration:promise:"+digest+":"+str(line),"migration_review",{"legacy_hash":digest,"line":line},now,priority=100)
                    c.execute("UPDATE work_items SET status='blocked',block_reason='map original promise identity/deadline/evidence using promise-add' WHERE id=?",(ident,))
            c.execute("INSERT INTO migrations VALUES(?,?,?,?)",(digest,name,now,len(entries)))
            result.append({"source":name,"sha256":digest,"records":len(entries),"status":"imported"})
    return {"imported":True,"sources":result,"manual_review":db.read("SELECT id,block_reason FROM work_items WHERE type='migration_review' AND status='blocked'")}
