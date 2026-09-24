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

CURRENT_SCHEMA_VERSION = 2

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
 task_id TEXT NOT NULL, payload_hash TEXT NOT NULL, accepted_at REAL NOT NULL);
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
CREATE TABLE workflow_roots(
 id TEXT PRIMARY KEY, state TEXT NOT NULL, iteration INTEGER NOT NULL DEFAULT 0,
 plan_revision INTEGER NOT NULL DEFAULT 0, planner_task_id TEXT NOT NULL REFERENCES tasks(id),
 authority TEXT NOT NULL, max_children INTEGER NOT NULL, max_tokens INTEGER,
 max_attempts INTEGER NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
 attempts_used INTEGER NOT NULL DEFAULT 0, planner_context_threshold INTEGER,
 reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE workflow_plans(
 workflow_id TEXT NOT NULL REFERENCES workflow_roots(id), revision INTEGER NOT NULL,
 plan TEXT NOT NULL, plan_sha256 TEXT NOT NULL, state TEXT NOT NULL,
 reason TEXT, created_at REAL NOT NULL, PRIMARY KEY(workflow_id,revision));
CREATE TABLE workflow_children(
 internal_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflow_roots(id),
 revision INTEGER NOT NULL, child_index INTEGER NOT NULL, child_key TEXT NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(id), dependencies TEXT NOT NULL,
 delivery TEXT NOT NULL, carried_from_task_id TEXT, carried_output TEXT,
 measurements TEXT NOT NULL DEFAULT '{}', context_bytes INTEGER NOT NULL DEFAULT 0,
 UNIQUE(workflow_id,revision,child_index), UNIQUE(workflow_id,revision,child_key));
CREATE TABLE workflow_deliveries(
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflow_roots(id),
 source_task_id TEXT NOT NULL REFERENCES tasks(id), target_task_id TEXT NOT NULL REFERENCES tasks(id),
 payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL, bytes_count INTEGER NOT NULL,
 state TEXT NOT NULL, error_message TEXT, created_at REAL NOT NULL, delivered_at REAL,
 UNIQUE(workflow_id,source_task_id,target_task_id));
CREATE TABLE schema_migrations(
 version INTEGER PRIMARY KEY, state TEXT NOT NULL, intent_at REAL NOT NULL,
 completed_at REAL, outcome TEXT);
CREATE TABLE retention_intents(
 task_id TEXT PRIMARY KEY, phase TEXT NOT NULL, artifact_paths TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX pending_tasks ON tasks(status,next_run_at);
CREATE INDEX worker_reservations ON attempts(worker_id,reserved);
CREATE INDEX task_events ON events(task_id,id);
CREATE INDEX workflow_children_task ON workflow_children(task_id);
CREATE INDEX workflow_deliveries_target ON workflow_deliveries(target_task_id);
INSERT INTO schema_migrations(version,state,intent_at,completed_at,outcome)
 VALUES(2,'complete',strftime('%s','now'),strftime('%s','now'),'initialized');
PRAGMA user_version=2;
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
                            "observation_ttl": 5.0, "last_worker": "",
                            "retention_seconds": 86400.0,
                            "idempotency_horizon": 86400.0,
                            "discovery_output_bytes": 64 * 1024,
                            "discovery_timeout": 10.0}
                db.executemany("INSERT INTO settings VALUES(?,?)",
                               [(k, canonical(v)) for k, v in settings.items()])
                # Inventory is deliberately seeded during init without starting a
                # runtime or making a provider request.  Active tiers are explicit
                # Discovery operations, never an init side effect.
                from .discovery import seed_passive_inventory
                seed_passive_inventory(db, root)
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
        except sqlite3.Error as exc:
            raise StorageError(f"Cannot open store: {exc}") from exc
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            # initialize() opens an empty, exclusively-created file.  All other
            # callers get migration handling before either reads or writes.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                self._migrate(db)
            return db
        except BaseException as exc:
            # Never hand back, or leak, a half-opened connection.
            db.close()
            if isinstance(exc, sqlite3.Error):
                raise StorageError(f"Cannot open store: {exc}") from exc
            raise

    @staticmethod
    def _migration_tables(db):
        db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
                     version INTEGER PRIMARY KEY, state TEXT NOT NULL,
                     intent_at REAL NOT NULL, completed_at REAL, outcome TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS retention_intents(
                     task_id TEXT PRIMARY KEY, phase TEXT NOT NULL,
                     artifact_paths TEXT NOT NULL, created_at REAL NOT NULL,
                     updated_at REAL NOT NULL)""")

    def _migrate(self, db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > CURRENT_SCHEMA_VERSION or version < 1:
            raise StorageError("Unsupported database schema version")
        if version == CURRENT_SCHEMA_VERSION:
            return
        # The intent is committed independently.  If a process dies after this
        # commit, the next opener sees it and resumes the same version exactly
        # once under BEGIN IMMEDIATE.
        db.execute("BEGIN IMMEDIATE")
        try:
            self._migration_tables(db)
            now = time.time()
            db.execute("""INSERT INTO schema_migrations(version,state,intent_at,outcome)
                        VALUES(2,'started',?, 'migration_intent')
                        ON CONFLICT(version) DO UPDATE SET state='started', outcome='migration_intent'""", (now,))
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            # v1 had a foreign key from commands to tasks, which would make
            # tombstone retention impossible. Rebuild only that table.
            db.execute("""CREATE TABLE IF NOT EXISTS commands_new(
                        id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL, task_id TEXT NOT NULL,
                        payload_hash TEXT NOT NULL, accepted_at REAL NOT NULL)""")
            db.execute("""INSERT OR IGNORE INTO commands_new
                        SELECT id,idempotency_key,kind,task_id,payload_hash,accepted_at FROM commands""")
            db.execute("DROP TABLE commands")
            db.execute("ALTER TABLE commands_new RENAME TO commands")
            self._migration_tables(db)
            db.execute("INSERT OR IGNORE INTO settings VALUES('retention_seconds','86400.0')")
            db.execute("INSERT OR IGNORE INTO settings VALUES('idempotency_horizon','86400.0')")
            db.execute("PRAGMA user_version=2")
            db.execute("UPDATE schema_migrations SET state='complete', completed_at=?, outcome='migrated' WHERE version=2", (time.time(),))
            db.commit()
        except sqlite3.Error:
            db.rollback()
            raise

    @contextmanager
    def reading(self):
        """A consistent read snapshot. In WAL mode this never waits for, or blocks, a writer."""
        db = self.connect()
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
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
            if db.execute("PRAGMA user_version").fetchone()[0] != CURRENT_SCHEMA_VERSION:
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

    def expire_artifacts(self, now=None):
        """Expire old result bytes while retaining task and event history.

        A workflow dependency is a durable reference, so its artifact is never
        removed by this pass.  The task row intentionally remains queryable;
        reading the missing path raises the distinct ``gone`` error.
        """
        now = time.time() if now is None else now
        removed = []
        with self.transaction() as db:
            cfg = self.settings_from(db)
            cutoff = now - cfg.get("retention_seconds", 86400.0)
            rows = db.execute("""SELECT t.id,a.result_path FROM tasks t
                              JOIN attempts a ON a.id=t.current_attempt_id
                              WHERE t.status='Succeeded' AND t.finished_at<=?
                              AND a.result_path IS NOT NULL""", (cutoff,)).fetchall()
            for row in rows:
                if db.execute("SELECT 1 FROM workflow_children WHERE task_id=?", (row["id"],)).fetchone():
                    continue
                path = self.home / row["result_path"]
                path.unlink(missing_ok=True)
                removed.append(row["id"])
        return removed

    def retain(self, now=None, stop_after=None):
        """Apply resumable task retention and preserve live submit tombstones.

        Each deletion phase commits independently.  ``stop_after`` is a
        deterministic fault-injection hook used by acceptance tests; a crash
        between phases leaves ``retention_intents`` for the next manager.
        """
        now = time.time() if now is None else now
        completed = []
        with self.transaction() as db:
            cfg = self.settings_from(db)
            cutoff = now - cfg.get("retention_seconds", 86400.0)
            for task in db.execute("""SELECT id FROM tasks
                                  WHERE status IN ('Succeeded','Failed','Cancelled')
                                  AND finished_at IS NOT NULL AND finished_at<=?
                                  AND NOT EXISTS (SELECT 1 FROM workflow_children w WHERE w.task_id=tasks.id)
                                  AND NOT EXISTS (SELECT 1 FROM workflow_roots r WHERE r.planner_task_id=tasks.id)""", (cutoff,)):
                if not db.execute("SELECT 1 FROM retention_intents WHERE task_id=?", (task["id"],)).fetchone():
                    paths = [r[0] for r in db.execute("SELECT result_path FROM attempts WHERE task_id=? AND result_path IS NOT NULL", (task["id"],))]
                    db.execute("INSERT INTO retention_intents VALUES(?,?,?,?,?)",
                               (task["id"], "dependents", canonical(paths), now, now))
        # Resume one intent at a time, with every phase durable.
        while True:
            with self.transaction() as db:
                intent = db.execute("SELECT * FROM retention_intents ORDER BY created_at,task_id LIMIT 1").fetchone()
                if intent is None:
                    break
                task_id, done = intent["task_id"], intent["phase"]
                if done == "dependents":
                    self._delete_task_dependents(db, task_id)
                    db.execute("UPDATE retention_intents SET phase='task',updated_at=? WHERE task_id=?", (now, task_id))
                elif done == "task":
                    # A terminal task still accepts commands between phases (for example a
                    # cancel records task.cancel_ignored_terminal). Remove any dependent row
                    # written since the dependents phase in this same commit, so the foreign
                    # key can never block this or any later retention intent.
                    self._delete_task_dependents(db, task_id)
                    db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                    db.execute("UPDATE retention_intents SET phase='artifact',updated_at=? WHERE task_id=?", (now, task_id))
                elif done == "artifact":
                    for relative in json.loads(intent["artifact_paths"]):
                        (self.home / relative).unlink(missing_ok=True)
                    db.execute("DELETE FROM retention_intents WHERE task_id=?", (task_id,))
                    completed.append(task_id)
                else:
                    raise StorageError(f"Unknown retention phase for task {task_id}")
            # stop_after names the phase just committed ("dependents", "task" or "artifact").
            if stop_after is not None and stop_after == done:
                break
        # Tombstones are retained independently of task payloads until their
        # own horizon, so late duplicate submissions remain harmless.
        with self.transaction() as db:
            horizon = cfg.get("idempotency_horizon", 86400.0)
            db.execute("""DELETE FROM commands
                        WHERE accepted_at + ? < ? AND NOT EXISTS
                        (SELECT 1 FROM tasks WHERE tasks.id=commands.task_id)""", (horizon, now))
        return {"deleted": completed, "pending": [r[0] for r in self._retention_pending()]}

    @staticmethod
    def _delete_task_dependents(db, task_id):
        """Delete every row that references a task, in foreign-key order."""
        db.execute("DELETE FROM workflow_deliveries WHERE source_task_id=? OR target_task_id=?", (task_id, task_id))
        db.execute("DELETE FROM workflow_children WHERE task_id=?", (task_id,))
        db.execute("DELETE FROM outbox WHERE attempt_id IN (SELECT id FROM attempts WHERE task_id=?)", (task_id,))
        db.execute("DELETE FROM attempts WHERE task_id=?", (task_id,))
        db.execute("DELETE FROM events WHERE task_id=?", (task_id,))

    def _retention_pending(self):
        with self.reading() as db:
            return db.execute("SELECT task_id FROM retention_intents ORDER BY task_id").fetchall()

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

    # Workflow methods live in their own owning layer.  These small delegates keep
    # the public Store boundary consistent with submit/task operations without
    # importing the workflow module during legacy initialization.
    def create_workflow(self, raw, key):
        from .workflows import WorkflowStore
        return WorkflowStore(self).create(raw, key)

    def workflow(self, workflow_id):
        from .workflows import WorkflowStore
        return WorkflowStore(self).workflow(workflow_id)

    def workflows(self):
        from .workflows import WorkflowStore
        return WorkflowStore(self).workflows()

    def workflow_children(self, workflow_id, revision=None):
        from .workflows import WorkflowStore
        return WorkflowStore(self).children(workflow_id, revision)

    def settle_workflow_plan(self, workflow_id, plan, *, replan=False, key=None):
        from .workflows import WorkflowStore
        return WorkflowStore(self).settle_plan(workflow_id, plan, replan=replan, key=key)

    def deliver_workflow_dependencies(self, task_id):
        from .workflows import WorkflowStore
        return WorkflowStore(self).deliver_dependencies(task_id)

    def replan_workflow(self, workflow_id, plan, key=None):
        from .workflows import WorkflowStore
        return WorkflowStore(self).replan(workflow_id, plan, key=key)

    # Discovery is an independent, consent-aware boundary.  These delegates keep
    # callers from importing an implementation detail while preserving the same
    # Store transaction domain as tasks and workflows.
    def discovery(self):
        from .discovery import Discovery
        return Discovery(self)

    def discovery_inventory(self, routes=None):
        return self.discovery().inventory(routes)

    def discovery_approvals(self):
        return self.discovery().approvals()

    def discovery_evidence(self):
        return self.discovery().evidence()

    def discovery_audit(self):
        return self.discovery().audit()
