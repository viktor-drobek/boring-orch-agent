"""One SQLite transaction domain for commands, tasks, reservations and dispatch."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .model import (Conflict, Invalid, NotFound, StorageError, TERMINAL,
                    canonical, digest, validate_spec)

SCHEMA = """
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tasks(
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending',
 desired_action TEXT NOT NULL DEFAULT 'Run', current_attempt_id TEXT,
 version INTEGER NOT NULL DEFAULT 1, submitted_at REAL NOT NULL, deadline REAL NOT NULL,
 next_run_at REAL NOT NULL, finished_at REAL, reason TEXT, result_path TEXT, result_sha256 TEXT,
 tokens_used INTEGER NOT NULL DEFAULT 0, usage_unknown INTEGER NOT NULL DEFAULT 0,
 observation_condition TEXT NOT NULL DEFAULT 'Fresh', observed_at REAL);
CREATE TABLE commands(
 id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(id), payload_hash TEXT NOT NULL, accepted_at REAL NOT NULL);
CREATE TABLE workers(
 id TEXT PRIMARY KEY, runtimes TEXT NOT NULL, slots INTEGER NOT NULL CHECK(slots>0),
 allow_write INTEGER NOT NULL, last_seen REAL NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), number INTEGER NOT NULL,
 worker_id TEXT NOT NULL REFERENCES workers(id), state TEXT NOT NULL DEFAULT 'Queued',
 reserved INTEGER NOT NULL DEFAULT 1, settled INTEGER NOT NULL DEFAULT 0,
 sequence INTEGER NOT NULL DEFAULT 0, merged_sequence INTEGER NOT NULL DEFAULT -1,
 created_at REAL NOT NULL, started_at REAL, finished_at REAL, heartbeat REAL NOT NULL,
 runner_pid INTEGER, runner_start TEXT, runtime_pid INTEGER,
 result_path TEXT, result_sha256 TEXT, error_kind TEXT, error_message TEXT,
 tokens INTEGER, known_tokens INTEGER NOT NULL DEFAULT 0, UNIQUE(task_id,number));
CREATE TABLE outbox(
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), worker_id TEXT NOT NULL,
 delivered_at REAL);
CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT REFERENCES tasks(id),
 attempt_id TEXT, kind TEXT NOT NULL, at REAL NOT NULL, details TEXT NOT NULL);
CREATE INDEX pending_tasks ON tasks(status,next_run_at);
CREATE INDEX worker_reservations ON attempts(worker_id,reserved);
CREATE INDEX task_events ON events(task_id,id);
PRAGMA user_version=1;
"""


def event(db, kind, task_id=None, attempt_id=None, **details):
    db.execute("INSERT INTO events(task_id,attempt_id,kind,at,details) VALUES(?,?,?,?,?)",
               (task_id, attempt_id, kind, time.time(), canonical(details)))


def executions(db, task_id, upto_number=None):
    """Attempts that reached a runner. A confirmed non-start (error_kind 'not_started') is
    recorded for evidence but is neither an execution nor a consumed retry."""
    sql = "SELECT count(*) FROM attempts WHERE task_id=? AND (error_kind IS NULL OR error_kind!='not_started')"
    params = [task_id]
    if upto_number is not None:
        sql += " AND number<=?"
        params.append(upto_number)
    return db.execute(sql, params).fetchone()[0]


class Store:
    def __init__(self, home: str | Path):
        self.home = Path(home).resolve()
        self.path = self.home / "agent.db"

    def initialize(self, workspace_root: str | Path, max_active=2, allow_write=False):
        if isinstance(max_active, bool) or not isinstance(max_active, int) or not 1 <= max_active <= 128:
            raise Invalid("max_active must be an integer from 1 to 128")
        root = Path(workspace_root).resolve()
        if not root.is_dir():
            raise Invalid("workspace_root must be a directory")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        (self.home / "artifacts").mkdir(exist_ok=True, mode=0o700)
        (self.home / "locks").mkdir(exist_ok=True, mode=0o700)
        (self.home / "logs").mkdir(exist_ok=True, mode=0o700)
        # Exclusive file creation prevents a second init from replacing a live database.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError as exc:
            raise Conflict(f"Already initialized: {self.path}") from exc
        try:
            with closing(self.connect()) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(SCHEMA)
                settings = {"workspace_root": str(root), "max_active": max_active,
                            "allow_write": bool(allow_write), "worker_ttl": 5.0,
                            "observation_ttl": 5.0, "last_worker": ""}
                db.executemany("INSERT INTO settings VALUES(?,?)",
                               [(k, canonical(v)) for k, v in settings.items()])
        except sqlite3.Error as exc:
            # A half-written database must not make the next init report "Already initialized".
            for suffix in ("", "-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            raise StorageError(f"Cannot initialize store: {exc}") from exc

    def connect(self):
        if not self.path.is_file():
            raise NotFound(f"Store not initialized: {self.home}. Run boa init first.")
        try:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.Error as exc:
            raise StorageError(f"Cannot open store: {exc}") from exc

    @contextmanager
    def reading(self):
        """A consistent read snapshot. In WAL mode this never waits for, or blocks, a writer."""
        db = self.connect()
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise StorageError("Unsupported database schema version")
            db.execute("BEGIN")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"Store read failed: {exc}") from exc
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        """Serialized read-modify-write. Use reading() for anything that only observes."""
        db = self.connect()
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise StorageError("Unsupported database schema version")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise Conflict(f"Store constraint: {exc}") from exc
        except sqlite3.Error as exc:
            db.rollback()
            raise StorageError(f"Store operation failed: {exc}") from exc
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def settings_from(db):
        return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}

    def settings(self):
        with self.reading() as db:
            return self.settings_from(db)

    @staticmethod
    def require_task(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(f"Unknown task: {task_id}")
        return row

    @staticmethod
    def receipt(row, duplicate=False):
        return {"command_id": row["id"], "task_id": row["task_id"],
                "accepted_at": row["accepted_at"], "duplicate": duplicate}

    @staticmethod
    def key_check(key):
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise Invalid("idempotency_key must contain 1–200 characters")

    def submit(self, raw, key):
        self.key_check(key)
        # Hash the caller's request, not time-dependent or expanded default fields.
        try:
            payload_hash = digest({"kind": "submit", "spec": raw})
        except (ValueError, TypeError, RecursionError) as exc:
            raise Invalid(f"Invalid task document: {exc}") from exc
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM commands WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise Conflict("Idempotency key already used with a different payload")
                return self.receipt(existing, True)
            cfg = self.settings_from(db)
            spec = validate_spec(raw, Path(cfg["workspace_root"]), cfg["allow_write"])
            now = time.time()
            task_id, command_id = str(uuid.uuid4()), str(uuid.uuid4())
            db.execute("INSERT INTO tasks(id,spec,submitted_at,deadline,next_run_at) VALUES(?,?,?,?,?)",
                       (task_id, canonical(spec), now, now + spec["budget"]["deadline_seconds"], now))
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)",
                       (command_id, key, "submit", task_id, payload_hash, now))
            event(db, "task.accepted", task_id, command_id=command_id)
            return {"command_id": command_id, "task_id": task_id, "accepted_at": now, "duplicate": False}

    def cancel(self, task_id, key):
        self.key_check(key)
        payload_hash = digest({"kind": "cancel", "task_id": task_id})
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM commands WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise Conflict("Idempotency key already used with a different payload")
                return self.receipt(existing, True)
            task = self.require_task(db, task_id)
            now, command_id = time.time(), str(uuid.uuid4())
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)",
                       (command_id, key, "cancel", task_id, payload_hash, now))
            if task["status"] not in TERMINAL:
                db.execute("UPDATE tasks SET desired_action='Cancel', reason='user_cancelled', version=version+1 WHERE id=?",
                           (task_id,))
                event(db, "task.cancel_requested", task_id, command_id=command_id)
                if task["status"] == "Pending":
                    # Nothing is reserved or running, so the same commit can finish the task.
                    db.execute("UPDATE tasks SET status='Cancelled',finished_at=?,observation_condition='Fresh' WHERE id=?",
                               (now, task_id))
                    event(db, "task.finished", task_id, status="Cancelled", reason="user_cancelled")
            else:
                event(db, "task.cancel_ignored_terminal", task_id, status=task["status"])
            return {"command_id": command_id, "task_id": task_id, "accepted_at": now, "duplicate": False}

    def task_from(self, db, task_id, cfg=None):
        row = dict(self.require_task(db, task_id))
        row["spec"] = json.loads(row["spec"])
        row["attempts"] = [dict(r) for r in db.execute(
            "SELECT * FROM attempts WHERE task_id=? ORDER BY number", (task_id,))]
        if row["status"] not in TERMINAL and row["current_attempt_id"]:
            current = row["attempts"][-1]
            if current["state"] == "Unknown":
                row["observation_condition"] = "Unknown"
            elif current["state"] in ("Launching", "Running"):
                # Only a runner produces observations that can go stale. A Queued attempt is
                # waiting for its worker; that is capacity, not missing evidence.
                ttl = (cfg or self.settings_from(db))["observation_ttl"]
                if time.time() - current["heartbeat"] > ttl:
                    row["observation_condition"] = "Stale"
        return row

    def task(self, task_id):
        with self.reading() as db:
            return self.task_from(db, task_id)

    def tasks(self):
        with self.reading() as db:
            cfg = self.settings_from(db)
            ids = [r[0] for r in db.execute("SELECT id FROM tasks ORDER BY submitted_at,id")]
            return [self.task_from(db, task_id, cfg) for task_id in ids]

    def events(self, task_id):
        with self.reading() as db:
            self.require_task(db, task_id)
            return [{**dict(r), "details": json.loads(r["details"])} for r in db.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))]

    def register_worker(self, worker_id, runtimes, slots, allow_write=False):
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 100:
            raise Invalid("worker ID must contain 1–100 characters")
        if not runtimes or any(x not in ("demo", "llm") for x in runtimes):
            raise Invalid("worker runtimes must be demo and/or llm")
        if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 128:
            raise Invalid("worker slots must be an integer from 1 to 128")
        with self.transaction() as db:
            db.execute("""INSERT INTO workers VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                runtimes=excluded.runtimes, slots=excluded.slots, allow_write=excluded.allow_write,
                last_seen=excluded.last_seen,pid=excluded.pid""",
                       (worker_id, canonical(sorted(set(runtimes))), slots, int(allow_write), time.time(), os.getpid()))

    def heartbeat_worker(self, worker_id):
        with self.transaction() as db:
            db.execute("UPDATE workers SET last_seen=? WHERE id=?", (time.time(), worker_id))

    def capacity(self):
        with self.reading() as db:
            cfg = self.settings_from(db)
            workers = []
            for row in db.execute("SELECT * FROM workers ORDER BY id"):
                row = dict(row)
                row["runtimes"] = json.loads(row["runtimes"])
                row["used"] = db.execute("SELECT count(*) FROM attempts WHERE worker_id=? AND reserved=1",
                                         (row["id"],)).fetchone()[0]
                row["available"] = max(0, row["slots"] - row["used"])
                row["valid"] = time.time() - row["last_seen"] <= cfg["worker_ttl"]
                workers.append(row)
            used = db.execute("SELECT count(*) FROM attempts WHERE reserved=1").fetchone()[0]
            return {"max_active": cfg["max_active"], "used": used,
                    "available": max(0, cfg["max_active"] - used), "workers": workers}

    def observe(self, attempt_id, worker_id, sequence, state, **data):
        """Authenticated by the local worker boundary; apply a monotonic observation."""
        if state not in {"Launching", "Running", "Succeeded", "Failed", "Cancelled", "Unknown"}:
            raise Invalid(f"Invalid attempt state: {state}")
        allowed = {"runner_pid", "runner_start", "runtime_pid", "result_path", "result_sha256",
                   "error_kind", "error_message", "tokens", "known_tokens"}
        if set(data) - allowed:
            raise Invalid("Unknown observation fields")
        if type(sequence) is not int or sequence < 1:
            raise Invalid("Observation sequence must be a positive integer")
        if data.get("tokens") is not None and (type(data["tokens"]) is not int or data["tokens"] < 0):
            raise Invalid("Observed tokens must be a nonnegative integer or null")
        if "known_tokens" in data and (type(data["known_tokens"]) is not int or data["known_tokens"] < 0):
            raise Invalid("Known tokens must be a nonnegative integer")
        with self.transaction() as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise NotFound(f"Unknown attempt: {attempt_id}")
            task = self.require_task(db, attempt["task_id"])
            if (attempt["worker_id"] != worker_id or sequence <= attempt["sequence"] or
                attempt["state"] in TERMINAL or task["current_attempt_id"] != attempt_id or
                    task["status"] in TERMINAL):
                return False
            transitions = {
                "Queued": {"Launching", "Cancelled", "Failed"},
                "Launching": {"Running", "Cancelled", "Failed", "Unknown"},
                "Running": {"Running", "Succeeded", "Failed", "Cancelled", "Unknown"},
                "Unknown": {"Unknown"},
            }
            if state not in transitions.get(attempt["state"], set()):
                raise Conflict(f"Invalid attempt transition: {attempt['state']} -> {state}")
            if "tokens" in data or "known_tokens" in data:
                # Usage is cumulative within an attempt. A newer sequence must not erase
                # known consumption or make a retry appear affordable again (R7/R8).
                floor = max(attempt["known_tokens"], data.get("known_tokens", 0), data.get("tokens") or 0)
                total = data.get("tokens", attempt["tokens"])
                data["known_tokens"] = floor
                if total is not None and total < floor:
                    data["tokens"] = None
                    event(db, "attempt.usage_inconsistent", task["id"], attempt_id,
                          reported_total=total, known_tokens=floor)
            now = time.time()
            updates = {"state": state, "sequence": sequence, "heartbeat": now, **data}
            if state == "Running" and attempt["started_at"] is None:
                updates["started_at"] = now
            if state in TERMINAL:
                updates["finished_at"] = now
            sql = ",".join(f"{key}=?" for key in updates)
            db.execute(f"UPDATE attempts SET {sql} WHERE id=?", (*updates.values(), attempt_id))
            if state != attempt["state"]:
                event(db, "attempt.observed", task["id"], attempt_id, state=state, sequence=sequence)
            return True

    def resolve(self, attempt_id, note, confirmed_stopped=False):
        if not confirmed_stopped or not isinstance(note, str) or not note.strip():
            raise Invalid("Resolution requires --confirm-stopped and a nonempty --note")
        with self.transaction() as db:
            a = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if a is None:
                raise NotFound(f"Unknown attempt: {attempt_id}")
            if a["state"] != "Unknown":
                raise Conflict("Only Unknown attempts require manual resolution")
            db.execute("""UPDATE attempts SET state='Failed',error_kind='permanent',error_message=?,
                       sequence=sequence+1,heartbeat=?,finished_at=? WHERE id=?""",
                       ("operator_confirmed_stopped: " + note, time.time(), time.time(), attempt_id))
            event(db, "attempt.resolved", a["task_id"], attempt_id, note=note)
        return {"attempt_id": attempt_id, "resolution": "confirmed_stopped"}
