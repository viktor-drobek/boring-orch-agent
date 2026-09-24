"""Durable lifecycle for native Coddy workflow sessions.

The legacy manager/worker path deliberately does not import this module.  A
lifecycle session is metadata and audit state around a native run; it is not a
live Coddy connection.  ``@session:<id>`` is only a read-only attachment
reference, and branches therefore receive independent session records.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import time
import uuid
from typing import Callable, Iterable, Mapping

from .model import (
    Conflict,
    Invalid,
    SESSION_DIGEST_MAX_BYTES,
    SessionConflict,
    SessionNotFound,
    StorageError,
    canonical,
    digest,
    session_id_from_mention,
    session_mention,
    validate_native_job,
)


SCHEMA_VERSION = 1
# Sessions default to asking; an agent that never asks would run with the
# operator's full rights, which docs/isolation.md refuses.
DEFAULT_PERMISSION_MODE = "ask"
DEFAULT_WARMUP_MODEL = "ndsub/qwen3.8-27b"
DEFAULT_WARMUP_CONTEXT_TOKENS = 262_144
MIN_WARMUP_CONTEXT_TOKENS = 100_000
WARMUP_STEPS = ("/compact", "/rpa-init")
# Executor error kinds that prove a warm-up command did not complete. Anything
# else, including ``unknown`` or an unclassified exception, becomes recovering.
CONFIRMED_FAILURE_KINDS = frozenset({"permanent", "transient", "validation", "cancelled"})

SESSION_STATES = frozenset({
    "new", "warming", "ready", "running", "completed", "failed",
    "recovering",
})
JOB_STATES = frozenset({
    "pending", "ready", "running", "succeeded", "failed", "blocked",
    "needs_operator", "cancelled",
})
RUN_STATES = frozenset({"running", "succeeded", "failed", "unknown", "cancelled"})
TRANSFER_STATES = frozenset({"in_progress", "delivered", "failed"})


LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle_meta(
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_sessions(
    id TEXT PRIMARY KEY,
    lineage_id TEXT NOT NULL,
    parent_session_id TEXT REFERENCES lifecycle_sessions(id),
    state TEXT NOT NULL,
    model TEXT NOT NULL,
    warmup_model TEXT NOT NULL,
    warmup_context_tokens INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    mode TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    scheduler_job_id TEXT,
    subagent_run TEXT,
    digest TEXT NOT NULL DEFAULT '',
    digest_sha256 TEXT,
    compact_status TEXT NOT NULL DEFAULT 'pending',
    rpa_init_status TEXT NOT NULL DEFAULT 'pending',
    warmup_attempts INTEGER NOT NULL DEFAULT 0,
    warmup_step TEXT,
    active_job_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    recovery_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_jobs(
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL,
    runtime TEXT NOT NULL,
    model TEXT NOT NULL,
    session_id TEXT REFERENCES lifecycle_sessions(id),
    dependencies TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    active_run_id TEXT,
    result TEXT,
    error_kind TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_runs(
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES lifecycle_jobs(id),
    session_id TEXT NOT NULL REFERENCES lifecycle_sessions(id),
    attempt INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    started_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    finished_at REAL,
    result TEXT,
    error_kind TEXT,
    error_message TEXT,
    recovery_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(job_id, attempt)
);
CREATE TABLE IF NOT EXISTS lifecycle_branches(
    child_session_id TEXT PRIMARY KEY REFERENCES lifecycle_sessions(id),
    parent_session_id TEXT NOT NULL REFERENCES lifecycle_sessions(id),
    child_job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(parent_session_id, child_job_id),
    UNIQUE(parent_session_id, ordinal)
);
CREATE TABLE IF NOT EXISTS lifecycle_transfers(
    id TEXT PRIMARY KEY,
    source_job_id TEXT NOT NULL,
    target_job_id TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at REAL NOT NULL,
    delivered_at REAL,
    UNIQUE(source_job_id, target_job_id)
);
CREATE TABLE IF NOT EXISTS lifecycle_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lifecycle_jobs_state ON lifecycle_jobs(state, created_at, id);
CREATE INDEX IF NOT EXISTS lifecycle_runs_session ON lifecycle_runs(session_id, state);
CREATE INDEX IF NOT EXISTS lifecycle_events_entity ON lifecycle_events(entity_type, entity_id, id);
"""


@dataclass(frozen=True)
class WarmupResult:
    session_id: str
    warmup_model: str
    steps: tuple[str, ...]


class SessionLifecycle:
    """Own native job/session state while leaving legacy task state untouched."""

    def __init__(self, store, model_contexts: Mapping[str, int | Mapping[str, int]] | None = None):
        self.store = store
        self.model_catalog_supplied = model_contexts is not None
        self.model_contexts = dict(model_contexts or {})
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.transaction() as db:
            # executescript() would COMMIT the caller's BEGIN IMMEDIATE first;
            # individual DDL statements stay inside the serialized transaction.
            for statement in LIFECYCLE_SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            row = db.execute("SELECT value FROM lifecycle_meta WHERE key='schema_version'").fetchone()
            if row is None:
                db.execute("INSERT INTO lifecycle_meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) > SCHEMA_VERSION:
                raise StorageError("Unsupported lifecycle database schema version")
            elif int(row["value"]) < SCHEMA_VERSION:
                # There are currently no destructive migrations.  Keeping this
                # marker makes the next migration explicit and restart-safe.
                db.execute("UPDATE lifecycle_meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))

    @staticmethod
    def _event(db, entity_type: str, entity_id: str, kind: str, **details) -> None:
        db.execute(
            "INSERT INTO lifecycle_events(entity_type,entity_id,kind,at,details) VALUES(?,?,?,?,?)",
            (entity_type, entity_id, kind, time.time(), canonical(details)),
        )

    @staticmethod
    def _json(value, fallback):
        return fallback if value is None else json.loads(value)

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def _session_locked(self, db, session_id: str):
        row = db.execute("SELECT * FROM lifecycle_sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFound(f"Unknown lifecycle session: {session_id}")
        return row

    def _job_locked(self, db, job_id: str):
        row = db.execute("SELECT * FROM lifecycle_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise SessionNotFound(f"Unknown lifecycle job: {job_id}")
        return row

    @staticmethod
    def _decode_session(row):
        value = dict(row)
        value["recovery"] = json.loads(value.pop("recovery_json"))
        value["mention"] = session_mention(value["id"])
        return value

    @staticmethod
    def _decode_job(row):
        value = dict(row)
        value["spec"] = json.loads(value.pop("spec"))
        value["dependencies"] = json.loads(value.pop("dependencies"))
        if value.get("result") is not None:
            value["result"] = json.loads(value["result"])
        return value

    @staticmethod
    def _decode_run(row):
        value = dict(row)
        if value.get("result") is not None:
            value["result"] = json.loads(value["result"])
        value["recovery"] = json.loads(value.pop("recovery_json"))
        return value

    def _context_tokens(self, model: str) -> int | None:
        value = self.model_contexts.get(model)
        if isinstance(value, Mapping):
            value = value.get("max_context_tokens")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def choose_warmup_model(self, requested_model: str) -> tuple[str, int]:
        requested_context = self._context_tokens(requested_model)
        if requested_context is not None and requested_context >= MIN_WARMUP_CONTEXT_TOKENS:
            return requested_model, requested_context
        if self.model_catalog_supplied:
            candidates = [(name, self._context_tokens(name)) for name in self.model_contexts]
            candidates = [(name, size) for name, size in candidates if size and size >= MIN_WARMUP_CONTEXT_TOKENS]
            if not candidates:
                raise Invalid("No configured warm-up model has a context window of at least 100000 tokens")
            return sorted(candidates, key=lambda item: (-item[1], item[0]))[0]
        return DEFAULT_WARMUP_MODEL, DEFAULT_WARMUP_CONTEXT_TOKENS

    @staticmethod
    def _validate_digest(value: str) -> str:
        if not isinstance(value, str):
            raise Invalid("session digest must be text")
        if len(value.encode("utf-8")) > SESSION_DIGEST_MAX_BYTES:
            raise Invalid("session digest exceeds Coddy's 24 KiB attachment limit")
        return value

    def _insert_session_locked(self, db, *, requested_model: str, cwd: str, mode: str,
                               permission_mode: str, scheduler_job_id: str | None = None,
                               subagent_run: str | None = None, parent_session_id: str | None = None,
                               session_id: str | None = None, lineage_id: str | None = None,
                               digest_text: str = "") -> str:
        if not isinstance(requested_model, str) or not requested_model.strip():
            raise Invalid("session model is required")
        if not isinstance(cwd, str) or not cwd:
            raise Invalid("session cwd is required")
        if not isinstance(mode, str) or not mode:
            raise Invalid("session mode is required")
        if not isinstance(permission_mode, str) or not permission_mode:
            raise Invalid("session permission_mode is required")
        if "bypass" in permission_mode.lower() or "skip" in permission_mode.lower():
            raise Invalid("a native session may not bypass its agent's permission system")
        digest_text = self._validate_digest(digest_text)
        if parent_session_id is not None:
            parent = self._session_locked(db, parent_session_id)
            lineage_id = lineage_id or parent["lineage_id"]
            if not digest_text:
                digest_text = parent["digest"]
        session_id = session_id or "sess_" + secrets.token_hex(12)
        lineage_id = lineage_id or session_id
        warmup_model, warmup_context = self.choose_warmup_model(requested_model)
        now = time.time()
        db.execute(
            """INSERT INTO lifecycle_sessions(
                id,lineage_id,parent_session_id,state,model,warmup_model,warmup_context_tokens,
                cwd,mode,permission_mode,scheduler_job_id,subagent_run,digest,digest_sha256,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, lineage_id, parent_session_id, "new", requested_model, warmup_model,
             warmup_context, cwd, mode, permission_mode, scheduler_job_id, subagent_run,
             digest_text, digest(digest_text) if digest_text else None, now, now),
        )
        self._event(db, "session", session_id, "session.created", parent_session_id=parent_session_id,
                    lineage_id=lineage_id, warmup_model=warmup_model)
        return session_id

    def create_session(self, *, model: str, cwd: str, mode: str = "agent",
                       permission_mode: str = DEFAULT_PERMISSION_MODE, scheduler_job_id: str | None = None,
                       subagent_run: str | None = None, session_id: str | None = None,
                       digest_text: str = "") -> dict:
        """Create one new session atomically; no Coddy process is started."""
        with self.store.transaction() as db:
            created = self._insert_session_locked(
                db, requested_model=model, cwd=cwd, mode=mode,
                permission_mode=permission_mode, scheduler_job_id=scheduler_job_id,
                subagent_run=subagent_run, session_id=session_id, digest_text=digest_text,
            )
            return self._decode_session(self._session_locked(db, created))

    def ensure_session(self, *, session_id: str, model: str, cwd: str, mode: str = "agent",
                       permission_mode: str = DEFAULT_PERMISSION_MODE,
                       scheduler_job_id: str | None = None,
                       inherited_permission: bool = False) -> dict:
        """Create transport metadata once or return the matching durable session."""
        if not inherited_permission and ("bypass" in permission_mode.lower() or
                                         "skip" in permission_mode.lower()):
            raise Invalid("a native session may not bypass its agent's permission system")
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM lifecycle_sessions WHERE id=?", (session_id,)).fetchone()
            if existing is not None:
                expected = {"model": model, "cwd": str(Path(cwd).resolve()), "mode": mode,
                            "permission_mode": permission_mode}
                for name, value in expected.items():
                    if existing[name] != value:
                        if name == "permission_mode":
                            if ("bypass" in value.lower() or "skip" in value.lower()) and not inherited_permission:
                                raise Invalid("a native session may not bypass its agent's permission system")
                            db.execute("UPDATE lifecycle_sessions SET permission_mode=?,revision=revision+1,updated_at=? WHERE id=?",
                                       (value, time.time(), session_id))
                            self._event(db, "session", session_id, "session.permission_inherited",
                                        permission_mode=value)
                            existing = self._session_locked(db, session_id)
                            continue
                        raise SessionConflict(
                            f"Lifecycle session {session_id} already has different {name}"
                        )
                return self._decode_session(existing)
            stored_permission = (DEFAULT_PERMISSION_MODE if inherited_permission and
                                 ("bypass" in permission_mode.lower() or "skip" in permission_mode.lower())
                                 else permission_mode)
            created = self._insert_session_locked(
                db, requested_model=model, cwd=str(Path(cwd).resolve()), mode=mode,
                permission_mode=stored_permission, scheduler_job_id=scheduler_job_id,
                session_id=session_id,
            )
            if stored_permission != permission_mode:
                db.execute("UPDATE lifecycle_sessions SET permission_mode=? WHERE id=?",
                           (permission_mode, session_id))
                self._event(db, "session", session_id, "session.permission_inherited",
                            permission_mode=permission_mode)
            return self._decode_session(self._session_locked(db, created))

    def session(self, session_id: str) -> dict:
        with self.store.reading() as db:
            return self._decode_session(self._session_locked(db, session_id))

    def sessions(self) -> list[dict]:
        with self.store.reading() as db:
            return [self._decode_session(row) for row in db.execute(
                "SELECT * FROM lifecycle_sessions ORDER BY created_at,id")]

    def attach_digest(self, session_id: str, digest_text: str) -> dict:
        digest_text = self._validate_digest(digest_text)
        with self.store.transaction() as db:
            row = self._session_locked(db, session_id)
            if row["state"] in {"running", "recovering"}:
                raise SessionConflict("A running or recovering session cannot replace its attachment")
            db.execute("UPDATE lifecycle_sessions SET digest=?,digest_sha256=?,revision=revision+1,updated_at=? WHERE id=?",
                       (digest_text, digest(digest_text) if digest_text else None, time.time(), session_id))
            self._event(db, "session", session_id, "session.digest_attached", bytes=len(digest_text.encode()))
            return self._decode_session(self._session_locked(db, session_id))

    def register_job(self, raw: dict, *, dependencies: Iterable[str] | None = None) -> dict:
        """Validate and atomically register one native job and its initial session."""
        spec = validate_native_job(raw)
        if dependencies is not None:
            supplied = list(dependencies)
            if sorted(supplied) != spec["dependencies"]:
                raise Invalid("dependencies argument differs from the immutable job document")
        with self.store.transaction() as db:
            return self._register_job_locked(db, spec)

    def _register_job_locked(self, db, spec: dict) -> dict:
        job_id = spec["id"]
        spec_hash = digest(spec)
        existing = db.execute("SELECT * FROM lifecycle_jobs WHERE id=?", (job_id,)).fetchone()
        if existing is not None:
            if existing["spec_sha256"] != spec_hash:
                raise Conflict(f"Native job already exists with different content: {job_id}")
            return self._decode_job(existing)
        dependencies = spec["dependencies"]
        for dependency in dependencies:
            if db.execute("SELECT 1 FROM lifecycle_jobs WHERE id=?", (dependency,)).fetchone() is None:
                raise Invalid(f"Unknown native job dependency: {dependency}")
        workspace = self._validated_workspace(spec.get("workspace"))
        session_id = session_id_from_mention(spec.get("session"))
        if session_id is not None:
            self._session_locked(db, session_id)
        elif not dependencies:
            session_id = self._insert_session_locked(
                db, requested_model=spec["model"], cwd=workspace,
                mode="agent", permission_mode=DEFAULT_PERMISSION_MODE, scheduler_job_id=job_id,
            )
        now = time.time()
        state = "ready" if not dependencies and session_id else "pending"
        db.execute(
            """INSERT INTO lifecycle_jobs(id,spec,spec_sha256,runtime,model,session_id,dependencies,state,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (job_id, canonical(spec), spec_hash, spec["runtime"], spec["model"], session_id,
             canonical(dependencies), state, now, now),
        )
        self._event(db, "job", job_id, "job.registered", session_id=session_id, dependencies=dependencies)
        return self._decode_job(self._job_locked(db, job_id))

    def _validated_workspace(self, workspace) -> str:
        """A native job runs in an existing absolute directory that is not the store."""
        if not isinstance(workspace, str) or not workspace:
            raise Invalid("native job workspace is required")
        path = Path(workspace)
        if not path.is_absolute() or not path.is_dir():
            raise Invalid("native job workspace must be an existing absolute directory")
        resolved, home = path.resolve(), self.store.home.resolve()
        if resolved == home or home in resolved.parents:
            raise Invalid("the store home cannot be a native job workspace")
        return str(resolved)

    def register_workflow(self, raw_jobs: Iterable[dict]) -> list[dict]:
        """Register a validated dependency graph in one transaction."""
        specs = [validate_native_job(raw) for raw in raw_jobs]
        if not specs:
            raise Invalid("workflow must contain at least one native job")
        ids = [spec["id"] for spec in specs]
        if len(set(ids)) != len(ids):
            raise Invalid("workflow job IDs must be unique")
        new_ids = set(ids)
        with self.store.transaction() as db:
            for spec in specs:
                for dependency in spec["dependencies"]:
                    if dependency not in new_ids and db.execute(
                            "SELECT 1 FROM lifecycle_jobs WHERE id=?", (dependency,)).fetchone() is None:
                        raise Invalid(f"Unknown native job dependency: {dependency}")
            graph = {spec["id"]: set(spec["dependencies"]) & new_ids for spec in specs}
            visiting, visited = set(), set()
            def visit(node):
                if node in visiting:
                    raise Invalid("native workflow dependency cycle")
                if node in visited:
                    return
                visiting.add(node)
                for dependency in graph[node]:
                    visit(dependency)
                visiting.remove(node)
                visited.add(node)
            for node in graph:
                visit(node)
            result = []
            remaining = list(specs)
            available = {row["id"] for row in db.execute("SELECT id FROM lifecycle_jobs")}
            while remaining:
                inserted = False
                for spec in list(remaining):
                    if all(dependency in available for dependency in spec["dependencies"]):
                        result.append(self._register_job_locked(db, spec))
                        available.add(spec["id"])
                        remaining.remove(spec)
                        inserted = True
                if not inserted:
                    raise Invalid("native workflow dependency cycle or unresolved dependency")
            by_id = {job["id"]: job for job in result}
            return [by_id[spec["id"]] for spec in specs]

    def job(self, job_id: str) -> dict:
        with self.store.reading() as db:
            return self._decode_job(self._job_locked(db, job_id))

    def jobs(self) -> list[dict]:
        with self.store.reading() as db:
            return [self._decode_job(row) for row in db.execute("SELECT * FROM lifecycle_jobs ORDER BY created_at,id")]

    def _consumer_count_locked(self, db, source_job_id: str) -> int:
        count = 0
        for row in db.execute("SELECT dependencies,state FROM lifecycle_jobs"):
            if source_job_id in json.loads(row["dependencies"]) and row["state"] not in {"cancelled", "failed"}:
                count += 1
        return count

    def _branch_locked(self, db, parent_session_id: str, child_job_id: str, ordinal: int | None = None) -> str:
        parent = self._session_locked(db, parent_session_id)
        existing = db.execute("SELECT child_session_id FROM lifecycle_branches WHERE parent_session_id=? AND child_job_id=?",
                              (parent_session_id, child_job_id)).fetchone()
        if existing:
            return existing["child_session_id"]
        if ordinal is None:
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM lifecycle_branches WHERE parent_session_id=?",
                                 (parent_session_id,)).fetchone()[0]
        child = self._insert_session_locked(
            db, requested_model=parent["model"], cwd=parent["cwd"], mode=parent["mode"],
            permission_mode=parent["permission_mode"], scheduler_job_id=child_job_id,
            parent_session_id=parent_session_id, digest_text=parent["digest"],
        )
        db.execute("INSERT INTO lifecycle_branches(child_session_id,parent_session_id,child_job_id,ordinal,created_at) VALUES(?,?,?,?,?)",
                   (child, parent_session_id, child_job_id, ordinal, time.time()))
        self._event(db, "session", child, "session.branched", parent_session_id=parent_session_id,
                    child_job_id=child_job_id, ordinal=ordinal)
        return child

    def branch_session(self, parent_session_id: str, child_job_id: str, ordinal: int | None = None) -> dict:
        with self.store.transaction() as db:
            child = self._branch_locked(db, parent_session_id, child_job_id, ordinal)
            return self._decode_session(self._session_locked(db, child))

    def branches(self, parent_session_id: str | None = None) -> list[dict]:
        """List deterministic branch lineage without exposing a live session."""
        with self.store.reading() as db:
            if parent_session_id is None:
                rows = db.execute("SELECT * FROM lifecycle_branches ORDER BY parent_session_id,ordinal")
            else:
                self._session_locked(db, parent_session_id)
                rows = db.execute("SELECT * FROM lifecycle_branches WHERE parent_session_id=? ORDER BY ordinal",
                                  (parent_session_id,))
            return [dict(row) for row in rows]

    def _record_transfer_locked(self, db, source_job, target_job, source_session_id: str,
                                target_session_id: str, payload: dict) -> None:
        key = f"transfer:{source_job['id']}:{target_job['id']}"
        encoded = canonical(payload)
        existing = db.execute("SELECT * FROM lifecycle_transfers WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            if existing["payload_sha256"] != digest(payload):
                raise Conflict("Transfer idempotency key was reused with different data")
            return
        now = time.time()
        db.execute(
            """INSERT INTO lifecycle_transfers(
                id,source_job_id,target_job_id,source_session_id,target_session_id,payload,payload_sha256,
                state,idempotency_key,attempts,created_at,delivered_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), source_job["id"], target_job["id"], source_session_id, target_session_id,
             encoded, digest(payload), "delivered", key, 1, now, now),
        )
        self._event(db, "transfer", key, "session.transfer.delivered", source_job_id=source_job["id"],
                    target_job_id=target_job["id"], payload_sha256=digest(payload))

    def _prepare_dependents_locked(self, db, source_job_id: str) -> None:
        source = self._job_locked(db, source_job_id)
        if source["state"] != "succeeded" or not source["session_id"]:
            return
        consumers = self._consumer_count_locked(db, source_job_id)
        for child in db.execute("SELECT * FROM lifecycle_jobs WHERE state='pending' ORDER BY created_at,id"):
            dependencies = json.loads(child["dependencies"])
            if source_job_id not in dependencies:
                continue
            dependency_rows = [self._job_locked(db, dep) for dep in dependencies]
            if any(row["state"] != "succeeded" or not row["session_id"] for row in dependency_rows):
                continue
            first = dependency_rows[0]
            if child["session_id"] is None:
                if len(dependencies) == 1 and consumers == 1:
                    target_session = first["session_id"]
                else:
                    target_session = self._branch_locked(db, first["session_id"], child["id"])
                branch = target_session != first["session_id"]
            else:
                # An explicitly mentioned session is kept. Readiness does not
                # grant concurrent use: start_job still refuses a session with
                # a running run, so reuse remains sequential.
                target_session = child["session_id"]
                branch = False
            db.execute("UPDATE lifecycle_jobs SET session_id=?,state='ready',updated_at=? WHERE id=?",
                       (target_session, time.time(), child["id"]))
            self._event(db, "job", child["id"], "job.ready", session_id=target_session, branch=branch)
            for dependency in dependency_rows:
                result = json.loads(dependency["result"]) if dependency["result"] else None
                payload = {"job_id": dependency["id"], "session": session_mention(dependency["session_id"]),
                           "result": result}
                self._record_transfer_locked(db, dependency, child, dependency["session_id"], target_session, payload)

    def ready_jobs(self) -> list[dict]:
        """Settle completed dependency transfers and return deterministic READY jobs."""
        with self.store.transaction() as db:
            for row in db.execute("SELECT id FROM lifecycle_jobs WHERE state='succeeded' ORDER BY id"):
                self._prepare_dependents_locked(db, row["id"])
            return [self._decode_job(row) for row in db.execute(
                "SELECT * FROM lifecycle_jobs WHERE state='ready' ORDER BY created_at,id")]

    def adopt_prepared_session(self, session_id: str, *, successful_steps: Iterable[str],
                               evidence_count: int) -> WarmupResult:
        """Record that an explicit remote session already contains prepared context."""
        steps = tuple(successful_steps)
        if steps != WARMUP_STEPS or isinstance(evidence_count, bool) or not isinstance(evidence_count, int):
            raise Invalid("a prepared session requires explicit successful /compact then /rpa-init evidence")
        with self.store.transaction() as db:
            row = self._session_locked(db, session_id)
            if (row["state"] == "ready" and row["compact_status"] == "succeeded" and
                    row["rpa_init_status"] == "succeeded"):
                return WarmupResult(session_id, row["warmup_model"], ())
            if row["state"] != "new":
                raise SessionConflict(f"Session cannot adopt remote preparation from state {row['state']}")
            db.execute("""UPDATE lifecycle_sessions SET state='ready',compact_status='succeeded',
                       rpa_init_status='succeeded',warmup_step=NULL,last_error=NULL,
                       recovery_json='{}',revision=revision+1,updated_at=? WHERE id=?""",
                       (time.time(), session_id))
            self._event(db, "session", session_id, "session.warmup.adopted",
                        source="coddy_session_snapshot", evidence_count=evidence_count,
                        successful_steps=list(steps))
            return WarmupResult(session_id, row["warmup_model"], ())

    def _warmup_claim(self, session_id: str) -> tuple[dict, list[tuple[str, str]]]:
        with self.store.transaction() as db:
            row = self._session_locked(db, session_id)
            if row["state"] == "ready" and row["compact_status"] == "succeeded" and row["rpa_init_status"] == "succeeded":
                return self._decode_session(row), []
            if row["state"] == "recovering":
                raise SessionConflict("Session warming has an unknown external outcome; operator confirmation is required")
            if row["state"] == "warming":
                raise SessionConflict("Session warming is already in progress")
            if row["state"] in {"running", "completed", "failed"}:
                raise SessionConflict(f"Session cannot be warmed from state {row['state']}")
            db.execute("""UPDATE lifecycle_sessions SET state='warming',warmup_attempts=warmup_attempts+1,
                       warmup_step=?,revision=revision+1,updated_at=? WHERE id=?""",
                       (WARMUP_STEPS[0], time.time(), session_id))
            self._event(db, "session", session_id, "session.warmup.started", warmup_model=row["warmup_model"])
            row = self._session_locked(db, session_id)
            steps = []
            if row["compact_status"] != "succeeded":
                steps.append(("/compact", f"warmup:{session_id}:/compact"))
            if row["rpa_init_status"] != "succeeded":
                steps.append(("/rpa-init", f"warmup:{session_id}:/rpa-init"))
            return self._decode_session(row), steps

    def warm_session(self, session_id: str, executor: Callable[..., object], *, deadline_at: float | None = None) -> WarmupResult:
        """Run exactly the missing warm-up steps with stable idempotency keys.

        ``executor`` receives ``(command, model, session_id, idempotency_key)``.
        A crash after the command starts leaves the step ``running`` and recovery
        refuses an automatic replay; an operator may retry with the same key.
        """
        session, steps = self._warmup_claim(session_id)
        completed = []
        for command, key in steps:
            if deadline_at is not None and time.time() >= deadline_at:
                self._warmup_failed(session_id, command, "warm-up deadline exceeded")
                raise SessionConflict("Warm-up exceeded the job deadline")
            with self.store.transaction() as db:
                row = self._session_locked(db, session_id)
                column = "compact_status" if command == "/compact" else "rpa_init_status"
                status = row[column]
                if status == "succeeded":
                    completed.append(command)
                    continue
                if status == "running":
                    db.execute("UPDATE lifecycle_sessions SET warmup_step=?,updated_at=? WHERE id=?",
                               (command, time.time(), session_id))
                else:
                    db.execute(f"UPDATE lifecycle_sessions SET {column}='running',warmup_step=?,updated_at=? WHERE id=?",
                               (command, time.time(), session_id))
            try:
                result = executor(command, session["warmup_model"], session_id, key)
            except Exception as exc:
                if self._confirmed_failure(exc):
                    self._warmup_failed(session_id, command, str(exc))
                    raise SessionConflict(f"Warm-up {command} failed: {exc}") from exc
                self._warmup_uncertain(session_id, command, key, str(exc))
                raise SessionConflict(
                    f"Warm-up {command} has an unknown outcome; operator confirmation is required: {exc}"
                ) from exc
            if result is False:
                # An explicit False is the executor's confirmed refusal.
                self._warmup_failed(session_id, command, "warm-up executor returned false")
                raise SessionConflict(f"Warm-up {command} failed: executor returned false")
            if deadline_at is not None and time.time() >= deadline_at:
                self._warmup_failed(session_id, command, "warm-up deadline exceeded")
                raise SessionConflict("Warm-up exceeded the job deadline")
            with self.store.transaction() as db:
                column = "compact_status" if command == "/compact" else "rpa_init_status"
                db.execute(f"UPDATE lifecycle_sessions SET {column}='succeeded',warmup_step=?,revision=revision+1,updated_at=? WHERE id=?",
                           (command, time.time(), session_id))
                self._event(db, "session", session_id, "session.warmup.step_succeeded",
                            command=command, idempotency_key=key)
            completed.append(command)
        with self.store.transaction() as db:
            row = self._session_locked(db, session_id)
            if row["compact_status"] != "succeeded" or row["rpa_init_status"] != "succeeded":
                raise SessionConflict("Warm-up ended without both required steps")
            db.execute("UPDATE lifecycle_sessions SET state='ready',warmup_step=NULL,revision=revision+1,updated_at=? WHERE id=?",
                       (time.time(), session_id))
            self._event(db, "session", session_id, "session.warmup.completed", steps=completed)
        return WarmupResult(session_id, session["warmup_model"], tuple(completed))

    @staticmethod
    def _confirmed_failure(exc: Exception) -> bool:
        """Only a classified, non-unknown executor error proves a step failed.

        Executors report outcomes with an exception ``kind`` (for example the
        provider's ``ExecutionError``). An ``unknown`` or unclassified error
        cannot disprove remote execution, so it is never treated as failed.
        """
        return getattr(exc, "kind", None) in CONFIRMED_FAILURE_KINDS

    def _warmup_uncertain(self, session_id: str, command: str, key: str, message: str) -> None:
        with self.store.transaction() as db:
            self._session_locked(db, session_id)
            db.execute("UPDATE lifecycle_sessions SET state='recovering',last_error=?,recovery_json=?,updated_at=? WHERE id=?",
                       (message, canonical({"action": "needs_operator", "phase": "warmup", "command": command,
                                            "retry_key": key}), time.time(), session_id))
            self._event(db, "session", session_id, "session.warmup.recovered_unknown", command=command,
                        idempotency_key=key, error=message)

    def _warmup_failed(self, session_id: str, command: str, message: str) -> None:
        with self.store.transaction() as db:
            self._session_locked(db, session_id)
            db.execute("UPDATE lifecycle_sessions SET state='failed',last_error=?,recovery_json=?,updated_at=? WHERE id=?",
                       (message, canonical({"phase": "warmup", "command": command, "retry_key": f"warmup:{session_id}:{command}"}),
                        time.time(), session_id))
            self._event(db, "session", session_id, "session.warmup.failed", command=command, error=message)

    def start_job(self, job_id: str, executor: Callable[..., object] | None = None) -> dict:
        """Warm (if necessary) and claim one job/run without sharing a live session."""
        job_started_at = time.time()
        with self.store.reading() as db:
            job = self._job_locked(db, job_id)
            if job["state"] != "ready":
                raise SessionConflict(f"Job {job_id} is not ready: {job['state']}")
            if not job["session_id"]:
                raise SessionConflict("Job has no transferred session")
            session = self._session_locked(db, job["session_id"])
        if session["state"] == "new":
            if executor is None:
                raise SessionConflict("A new session must be warmed before the first job")
            deadline = job_started_at + json.loads(job["spec"])["budget"]["deadline_seconds"]
            self.warm_session(session["id"], executor, deadline_at=deadline)
        with self.store.transaction() as db:
            job = self._job_locked(db, job_id)
            session = self._session_locked(db, job["session_id"])
            if job["state"] != "ready":
                raise SessionConflict(f"Job {job_id} changed state during warm-up: {job['state']}")
            if session["compact_status"] != "succeeded" or session["rpa_init_status"] != "succeeded":
                raise SessionConflict(
                    f"Session {session['id']} has not completed /compact then /rpa-init "
                    f"(state {session['state']}); retry its warm-up before starting a job"
                )
            if session["state"] in {"new", "warming", "recovering"}:
                raise SessionConflict(f"Session cannot start a job from state {session['state']}")
            active = db.execute("SELECT 1 FROM lifecycle_runs WHERE session_id=? AND state='running'", (session["id"],)).fetchone()
            if active:
                raise SessionConflict("A lifecycle session cannot be used concurrently")
            attempt = job["attempt_count"] + 1
            run_id = str(uuid.uuid4())
            key = f"run:{job_id}:{attempt}"
            spec = json.loads(job["spec"])
            now = time.time()
            deadline = job_started_at + spec["budget"]["deadline_seconds"]
            db.execute("INSERT INTO lifecycle_runs(id,job_id,session_id,attempt,idempotency_key,state,started_at,deadline_at) VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, job_id, session["id"], attempt, key, "running", job_started_at, deadline))
            db.execute("UPDATE lifecycle_jobs SET state='running',attempt_count=?,active_run_id=?,updated_at=? WHERE id=?",
                       (attempt, run_id, now, job_id))
            db.execute("UPDATE lifecycle_sessions SET state='running',active_job_id=?,revision=revision+1,updated_at=? WHERE id=?",
                       (job_id, now, session["id"]))
            self._event(db, "run", run_id, "run.claimed", job_id=job_id, session_id=session["id"], attempt=attempt)
            return self._decode_run(db.execute("SELECT * FROM lifecycle_runs WHERE id=?", (run_id,)).fetchone())

    def complete_run(self, run_id: str, state: str, *, result=None, error_kind: str | None = None,
                     error_message: str | None = None) -> dict:
        if state not in {"succeeded", "failed", "cancelled"}:
            raise Invalid("run completion must be succeeded, failed or cancelled")
        with self.store.transaction() as db:
            run = db.execute("SELECT * FROM lifecycle_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise SessionNotFound(f"Unknown lifecycle run: {run_id}")
            if run["state"] != "running":
                return self._decode_run(run)
            job = self._job_locked(db, run["job_id"])
            session = self._session_locked(db, run["session_id"])
            now = time.time()
            encoded = canonical(result) if result is not None else None
            db.execute("UPDATE lifecycle_runs SET state=?,finished_at=?,result=?,error_kind=?,error_message=? WHERE id=?",
                       (state, now, encoded, error_kind, error_message, run_id))
            job_state = "succeeded" if state == "succeeded" else "cancelled" if state == "cancelled" else "failed"
            db.execute("UPDATE lifecycle_jobs SET state=?,active_run_id=NULL,result=?,error_kind=?,error_message=?,updated_at=? WHERE id=?",
                       (job_state, encoded, error_kind, error_message, now, job["id"]))
            session_state = "completed" if state == "succeeded" else "failed"
            db.execute("UPDATE lifecycle_sessions SET state=?,active_job_id=NULL,last_error=?,revision=revision+1,updated_at=? WHERE id=?",
                       (session_state, error_message, now, session["id"]))
            self._event(db, "run", run_id, "run.completed", state=state, job_id=job["id"], session_id=session["id"])
            if state == "succeeded":
                self._prepare_dependents_locked(db, job["id"])
            return self._decode_run(db.execute("SELECT * FROM lifecycle_runs WHERE id=?", (run_id,)).fetchone())

    def retry_job(self, job_id: str) -> dict:
        """Requeue only a confirmed terminal failure, never an unknown run."""
        with self.store.transaction() as db:
            job = self._job_locked(db, job_id)
            if job["state"] != "failed":
                raise SessionConflict(f"Only failed jobs can be retried: {job['state']}")
            spec = json.loads(job["spec"])
            if not spec.get("metadata", {}).get("replay_safe", False):
                raise SessionConflict("Native job is not marked replay_safe")
            session = self._session_locked(db, job["session_id"])
            if session["state"] == "recovering":
                raise SessionConflict("Session recovery requires an operator decision")
            db.execute("UPDATE lifecycle_jobs SET state='ready',error_kind=NULL,error_message=NULL,updated_at=? WHERE id=?",
                       (time.time(), job_id))
            self._event(db, "job", job_id, "job.retry_scheduled", session_id=session["id"])
            return self._decode_job(self._job_locked(db, job_id))

    def run_history(self, job_id: str | None = None) -> list[dict]:
        with self.store.reading() as db:
            if job_id is None:
                rows = db.execute("SELECT * FROM lifecycle_runs ORDER BY started_at,id")
            else:
                rows = db.execute("SELECT * FROM lifecycle_runs WHERE job_id=? ORDER BY attempt", (job_id,))
            return [self._decode_run(row) for row in rows]

    def transfers(self, job_id: str | None = None) -> list[dict]:
        with self.store.reading() as db:
            sql = "SELECT * FROM lifecycle_transfers"
            params = ()
            if job_id is not None:
                sql += " WHERE source_job_id=? OR target_job_id=?"
                params = (job_id, job_id)
            sql += " ORDER BY created_at,id"
            return [dict(row) for row in db.execute(sql, params)]

    def events(self, entity_type: str | None = None, entity_id: str | None = None) -> list[dict]:
        with self.store.reading() as db:
            sql = "SELECT * FROM lifecycle_events"
            params = []
            clauses = []
            if entity_type is not None:
                clauses.append("entity_type=?")
                params.append(entity_type)
            if entity_id is not None:
                clauses.append("entity_id=?")
                params.append(entity_id)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id"
            return [{**dict(row), "details": json.loads(row["details"])} for row in db.execute(sql, params)]

    def recover(self) -> dict:
        """Recover durable evidence after a manager restart without replaying work."""
        recovered_runs, recovering_sessions, failed_transfers = [], [], []
        with self.store.transaction() as db:
            for run in db.execute("SELECT * FROM lifecycle_runs WHERE state='running'").fetchall():
                now = time.time()
                message = "Manager restarted while native outcome was unknown"
                db.execute("UPDATE lifecycle_runs SET state='unknown',finished_at=?,error_kind='unknown',error_message=?,recovery_json=? WHERE id=?",
                           (now, message, canonical({"action": "needs_operator", "run_id": run["id"]}), run["id"]))
                db.execute("UPDATE lifecycle_jobs SET state='needs_operator',active_run_id=NULL,error_kind='unknown',error_message=?,updated_at=? WHERE id=?",
                           (message, now, run["job_id"]))
                db.execute("UPDATE lifecycle_sessions SET state='recovering',active_job_id=NULL,last_error=?,recovery_json=?,updated_at=? WHERE id=?",
                           (message, canonical({"action": "needs_operator", "run_id": run["id"]}), now, run["session_id"]))
                self._event(db, "run", run["id"], "run.recovered_unknown", job_id=run["job_id"], session_id=run["session_id"])
                recovered_runs.append(run["id"])
            for session in db.execute("SELECT * FROM lifecycle_sessions WHERE state='warming'").fetchall():
                column = "compact_status" if session["warmup_step"] == "/compact" else "rpa_init_status"
                status = session[column]
                if status == "running":
                    message = "Manager restarted during warm-up; external command outcome is unknown"
                    db.execute("UPDATE lifecycle_sessions SET state='recovering',last_error=?,recovery_json=?,updated_at=? WHERE id=?",
                               (message, canonical({"action": "needs_operator", "command": session["warmup_step"],
                                                     "retry_key": f"warmup:{session['id']}:{session['warmup_step']}"}),
                                time.time(), session["id"]))
                    self._event(db, "session", session["id"], "session.warmup.recovered_unknown", command=session["warmup_step"])
                    recovering_sessions.append(session["id"])
            for transfer in db.execute("SELECT * FROM lifecycle_transfers WHERE state='in_progress'").fetchall():
                message = "Manager restarted during session transfer; delivery must be checked"
                db.execute("UPDATE lifecycle_transfers SET state='failed',error_message=?,attempts=attempts+1 WHERE id=?",
                           (message, transfer["id"]))
                self._event(db, "transfer", transfer["id"], "session.transfer.recovered_failed", error=message)
                failed_transfers.append(transfer["id"])
        return {"runs": recovered_runs, "sessions": recovering_sessions, "transfers": failed_transfers}

    def retry_warmup(self, session_id: str, executor: Callable[..., object], *, operator_confirmed: bool = False) -> WarmupResult:
        with self.store.transaction() as db:
            row = self._session_locked(db, session_id)
            if row["state"] == "recovering" and not operator_confirmed:
                raise SessionConflict("Retrying an unknown warm-up requires explicit operator confirmation")
            if row["state"] not in {"failed", "recovering"}:
                raise SessionConflict("Only a failed or recovered warm-up can be retried")
            if row["warmup_step"] not in WARMUP_STEPS:
                raise SessionConflict("Session has no recoverable warm-up step")
            column = "compact_status" if row["warmup_step"] == "/compact" else "rpa_init_status"
            db.execute(f"UPDATE lifecycle_sessions SET {column}='pending',state='new',last_error=NULL,recovery_json='{{}}',updated_at=? WHERE id=?",
                       (time.time(), session_id))
            self._event(db, "session", session_id, "session.warmup.retry_confirmed", command=row["warmup_step"])
        return self.warm_session(session_id, executor)