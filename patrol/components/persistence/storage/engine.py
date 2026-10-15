"""SQLite connection/transaction ownership. No network calls inside transactions."""
import contextlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path


class Busy(RuntimeError):
    pass


class StaleLease(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def new_id():
    return uuid.uuid4().hex


def cursor(value):
    # Keep arbitrary-precision cursors as strings at every public boundary.
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValueError("cursor must be a canonical decimal string")
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS patrol_runs(
 id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
 status TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS sync_cursors(
 source TEXT NOT NULL, scope TEXT NOT NULL, stream TEXT NOT NULL DEFAULT '',
 received TEXT NOT NULL DEFAULT '0', handled TEXT NOT NULL DEFAULT '0',
 target TEXT, next_at REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 covered_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(source,scope));
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, scope TEXT NOT NULL, stream TEXT NOT NULL,
 external_key TEXT NOT NULL, cursor TEXT, payload TEXT NOT NULL, observed_at REAL NOT NULL,
 UNIQUE(source,scope,stream,external_key));
CREATE TABLE IF NOT EXISTS work_items(
 id TEXT PRIMARY KEY, event_id INTEGER, logical_key TEXT NOT NULL UNIQUE,
 type TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
 priority INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 start_by REAL, next_action_at REAL NOT NULL, due_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_version INTEGER NOT NULL DEFAULT 0, lease_until REAL,
 first_action_at REAL, last_progress_at REAL, evidence TEXT, block_reason TEXT);
CREATE INDEX IF NOT EXISTS work_due ON work_items(status,next_action_at);
CREATE TABLE IF NOT EXISTS promises(
 id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL REFERENCES work_items(id),
 actor TEXT NOT NULL, recipient TEXT NOT NULL, source_message_id TEXT NOT NULL UNIQUE,
 text TEXT NOT NULL, created_at REAL NOT NULL, start_by REAL NOT NULL,
 original_due_at REAL, due_at REAL, next_action_at REAL NOT NULL,
 first_action_at REAL, last_progress_at REAL, progress_evidence TEXT,
 status TEXT NOT NULL DEFAULT 'open', completed_evidence TEXT);
CREATE TABLE IF NOT EXISTS promise_history(
 id INTEGER PRIMARY KEY, promise_id TEXT NOT NULL REFERENCES promises(id),
 action TEXT NOT NULL, at REAL NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attention_sessions(
 target TEXT PRIMARY KEY, state TEXT NOT NULL, active_until REAL NOT NULL,
 cooldown_until REAL NOT NULL, next_check_at REAL NOT NULL,
 last_activity_at REAL NOT NULL, own_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS attention_consumed(
 target TEXT NOT NULL, event_key TEXT NOT NULL, PRIMARY KEY(target,event_key));
CREATE TABLE IF NOT EXISTS outbox(
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
 logical_key TEXT NOT NULL UNIQUE, idem_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
 payload_hash TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
 next_attempt_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 remote_id TEXT, result TEXT, error TEXT, dispatch_owner TEXT, dispatch_at REAL,
 verify_cursor TEXT NOT NULL DEFAULT '0');
CREATE TABLE IF NOT EXISTS alerts(
 logical_key TEXT PRIMARY KEY, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS migrations(
 source_hash TEXT NOT NULL, source_name TEXT NOT NULL, imported_at REAL NOT NULL,
 record_count INTEGER NOT NULL, PRIMARY KEY(source_name,source_hash));
CREATE TABLE IF NOT EXISTS legacy_records(
 source_hash TEXT NOT NULL, line_no INTEGER NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(source_hash,line_no));
"""


class Database:
    """Public persistence capability. Tables form a documented shared transaction schema."""
    def __init__(self, path, journal="DELETE", busy_ms=1000):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.journal = journal.upper()
        if self.journal not in ("WAL", "DELETE"):
            raise ValueError("journal must be WAL or DELETE")
        version = sqlite3.sqlite_version_info
        patched = version >= (3, 51, 3) or ((3, 44, 6) <= version < (3, 45, 0)) or ((3, 50, 7) <= version < (3, 51, 0))
        if self.journal == "WAL" and not patched:
            raise ValueError("WAL requires SQLite 3.51.3+ or a verified supported fixed branch; use DELETE otherwise")
        self.busy_ms = busy_ms
        with contextlib.closing(self.connect()) as c:
            mode = c.execute("PRAGMA journal_mode=" + self.journal).fetchone()[0]
            if mode.upper() != self.journal:
                raise RuntimeError("requested journal mode unavailable")
            c.executescript(SCHEMA)
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row and row[0] != "1":
                raise RuntimeError("unsupported database schema")
            c.execute("INSERT INTO meta(key,value) VALUES('schema_version','1') ON CONFLICT(key) DO NOTHING")
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def connect(self):
        c = sqlite3.connect(str(self.path), timeout=self.busy_ms / 1000, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA busy_timeout=" + str(int(self.busy_ms)))
        return c

    @contextlib.contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except sqlite3.OperationalError as e:
            c.rollback()
            if "locked" in str(e) or "busy" in str(e):
                raise Busy("database busy; no cursor or work was committed") from None
            raise
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def read(self, sql, args=()):
        with contextlib.closing(self.connect()) as c:
            return [dict(r) for r in c.execute(sql, args)]

    def alert(self, key, kind, detail, now):
        with self.transaction() as c:
            c.execute("INSERT INTO alerts VALUES(?,?,?,?) ON CONFLICT(logical_key) DO NOTHING",
                      (key, kind, canonical(detail), now))

    def backup(self, destination):
        dest = Path(destination).resolve()
        if dest == self.path or dest.exists():
            raise ValueError("backup destination must be a new file")
        with contextlib.closing(self.connect()) as src, contextlib.closing(sqlite3.connect(str(dest))) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup integrity failed")
        if os.name == "posix":
            os.chmod(dest, 0o600)
        return str(dest)

    def integrity(self):
        return self.read("PRAGMA integrity_check")
