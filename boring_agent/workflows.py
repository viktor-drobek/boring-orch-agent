"""Durable, opt-in workflow planning for the legacy task store.

A workflow is deliberately not a second task lifecycle.  The workflow root,
plan revisions and delivery records are metadata; planner and execution jobs
remain ordinary rows in ``tasks`` and are settled by the existing manager.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import time
import uuid

from jsonschema import Draft202012Validator, SchemaError

from .artifacts import artifact_checksum
from .model import Conflict, Invalid, NotFound, canonical, digest, fields, validate_spec
from .store import event


MAX_PLAN_BYTES = 256_000
PLAN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["children"],
    "additionalProperties": False,
    "properties": {
        "children": {
            "type": "array", "minItems": 1, "maxItems": 1000,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "order": {"type": "integer", "minimum": 0},
                    "task": {"type": "object"},
                    "objective": {"type": "string", "minLength": 1},
                    "runtime": {"enum": ["demo", "llm"]},
                    "workspace": {"type": "string"},
                    "model": {"type": ["string", "null"]},
                    "sandbox": {"enum": ["read-only", "workspace-write"]},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "output_schema": {"type": "object"},
                    "budget": {"type": "object"},
                    "retry": {"type": "object"},
                    "demo": {"type": "object"},
                    "expect_files": {"type": "array", "items": {"type": "string"}},
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                    "deliver": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "result": {"type": "boolean"},
                            "files": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "metadata": {"type": "object"},
    },
}
Draft202012Validator.check_schema(PLAN_SCHEMA)


WORKFLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_roots(
 id TEXT PRIMARY KEY, state TEXT NOT NULL, iteration INTEGER NOT NULL DEFAULT 0,
 plan_revision INTEGER NOT NULL DEFAULT 0, planner_task_id TEXT NOT NULL REFERENCES tasks(id),
 authority TEXT NOT NULL, max_children INTEGER NOT NULL, max_tokens INTEGER,
 max_attempts INTEGER NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
 attempts_used INTEGER NOT NULL DEFAULT 0, planner_context_threshold INTEGER,
 reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_plans(
 workflow_id TEXT NOT NULL REFERENCES workflow_roots(id), revision INTEGER NOT NULL,
 plan TEXT NOT NULL, plan_sha256 TEXT NOT NULL, state TEXT NOT NULL,
 reason TEXT, created_at REAL NOT NULL, PRIMARY KEY(workflow_id,revision));
CREATE TABLE IF NOT EXISTS workflow_children(
 internal_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflow_roots(id),
 revision INTEGER NOT NULL, child_index INTEGER NOT NULL, child_key TEXT NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(id), dependencies TEXT NOT NULL,
 delivery TEXT NOT NULL, carried_from_task_id TEXT, carried_output TEXT,
 measurements TEXT NOT NULL DEFAULT '{}', context_bytes INTEGER NOT NULL DEFAULT 0,
 UNIQUE(workflow_id,revision,child_index), UNIQUE(workflow_id,revision,child_key));
CREATE TABLE IF NOT EXISTS workflow_deliveries(
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflow_roots(id),
 source_task_id TEXT NOT NULL REFERENCES tasks(id), target_task_id TEXT NOT NULL REFERENCES tasks(id),
 payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL, bytes_count INTEGER NOT NULL,
 state TEXT NOT NULL, error_message TEXT, created_at REAL NOT NULL, delivered_at REAL,
 UNIQUE(workflow_id,source_task_id,target_task_id));
CREATE TABLE IF NOT EXISTS workflow_plan_commands(
 command_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflow_roots(id),
 receipt TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS workflow_children_task ON workflow_children(task_id);
CREATE INDEX IF NOT EXISTS workflow_deliveries_target ON workflow_deliveries(target_task_id);
"""

# Task fields an untrusted plan may set on a child.  Everything else, including
# ``workflow`` and ``schema_version``, is inherited from the root or refused.
CHILD_TASK_FIELDS = frozenset({"objective", "runtime", "workspace", "model", "sandbox", "tools",
                               "output_schema", "budget", "retry", "demo", "expect_files", "coddy"})
PERMISSION_RANK = {"ask": 0, "accept_edits": 1, "bypass": 2}
# Coddy mention fields a child may change.  Every other mention field, such as
# the subagent name, its model or its timeouts, must equal the root's value.
NARROWABLE_MENTION_FIELDS = frozenset({"prompt", "description", "permission_mode"})
TERMINAL_STATUSES = ("Succeeded", "Failed", "Cancelled")


class WorkflowStore:
    """Own workflow metadata while delegating task execution to ``Store``."""

    def __init__(self, store, *, ensure=True):
        self.store = store
        if ensure:
            self._ensure_schema()

    def _ensure_schema(self):
        with self.store.transaction() as db:
            self._ensure_schema_locked(db)

    @staticmethod
    def _ensure_schema_locked(db):
        # executescript() would COMMIT the caller's BEGIN IMMEDIATE transaction
        # first, dropping its write lock and making earlier writes durable even
        # if the transaction later fails.  Run each DDL statement in place.
        for statement in WORKFLOW_SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)

    @staticmethod
    def _json(value, default):
        return default if value is None else json.loads(value)

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        for column, default in (("authority", {}),):
            value[column] = json.loads(value[column]) if value.get(column) is not None else default
        return value

    def _root_locked(self, db, workflow_id):
        row = db.execute("SELECT * FROM workflow_roots WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise NotFound(f"Unknown workflow: {workflow_id}")
        return row

    @staticmethod
    def _workflow_event(db, root, kind, **details):
        event(db, kind, root["planner_task_id"], workflow_id=root["id"], **details)

    def _root_public_locked(self, db, workflow_id):
        row = self._root_locked(db, workflow_id)
        value = dict(row)
        value["authority"] = json.loads(value.pop("authority"))
        plans = []
        for plan in db.execute("SELECT * FROM workflow_plans WHERE workflow_id=? ORDER BY revision", (workflow_id,)):
            item = dict(plan)
            item["plan"] = json.loads(item["plan"])
            plans.append(item)
        value["plans"] = plans
        value["children"] = self._children_locked(db, workflow_id)
        value["remaining_tokens"] = None if value["max_tokens"] is None else max(0, value["max_tokens"] - value["tokens_used"])
        value["remaining_attempts"] = max(0, value["max_attempts"] - value["attempts_used"])
        return value

    def _children_locked(self, db, workflow_id, revision=None):
        sql = "SELECT * FROM workflow_children WHERE workflow_id=?"
        args = [workflow_id]
        if revision is not None:
            sql += " AND revision=?"
            args.append(revision)
        sql += " ORDER BY revision,child_index"
        result = []
        for row in db.execute(sql, args):
            item = dict(row)
            item["dependencies"] = json.loads(item["dependencies"])
            item["delivery"] = json.loads(item["delivery"])
            item["measurements"] = json.loads(item["measurements"])
            if item["carried_output"] is not None:
                item["carried_output"] = json.loads(item["carried_output"])
            result.append(item)
        return result

    @staticmethod
    def _validate_plan_shape(raw, max_children):
        if not isinstance(raw, dict):
            raise Invalid("workflow plan must be an object")
        if len(canonical(raw).encode("utf-8")) > MAX_PLAN_BYTES:
            raise Invalid("workflow plan exceeds 256000 bytes")
        errors = sorted(Draft202012Validator(PLAN_SCHEMA).iter_errors(raw), key=lambda e: list(e.path))
        if errors:
            raise Invalid("Invalid workflow plan: " + errors[0].message)
        children = raw["children"]
        if not children:
            raise Invalid("workflow plan must contain at least one child")
        if len(children) > max_children:
            raise Invalid(f"workflow plan contains more than {max_children} children")
        ids = [child["id"] for child in children]
        if len(set(ids)) != len(ids):
            raise Invalid("workflow child IDs must be unique")
        orders = [child.get("order", index) for index, child in enumerate(children)]
        if len(set(orders)) != len(orders):
            raise Invalid("workflow child order must be unique")
        if sorted(orders) != list(range(len(orders))):
            raise Invalid("workflow child order must be contiguous")
        known = set(ids)
        graph = {}
        normalized = []
        for index, child in enumerate(children):
            dependencies = child.get("dependencies", [])
            if len(set(dependencies)) != len(dependencies):
                raise Invalid(f"duplicate dependencies for child {child['id']}")
            unknown = set(dependencies) - known
            if unknown:
                raise Invalid(f"unknown dependency for child {child['id']}: {sorted(unknown)[0]}")
            graph[child["id"]] = set(dependencies)
            task = deepcopy(child.get("task", {}))
            for key in ("objective", "runtime", "workspace", "model", "sandbox", "tools",
                        "output_schema", "budget", "retry", "demo", "expect_files"):
                if key in child and key in task:
                    raise Invalid(f"child {child['id']} defines {key} twice")
                if key in child:
                    task[key] = deepcopy(child[key])
            delivery = deepcopy(child.get("deliver", {}))
            files = delivery.get("files", [])
            if len(set(files)) != len(files):
                raise Invalid(f"duplicate delivered file for child {child['id']}")
            normalized.append({"id": child["id"], "order": child.get("order", index),
                              "task": task, "dependencies": dependencies, "delivery": delivery})
        visiting, visited = set(), set()
        def visit(node):
            if node in visiting:
                raise Invalid("workflow plan contains a dependency cycle")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)
        normalized.sort(key=lambda item: item["order"])
        return normalized

    @staticmethod
    def _authority(spec):
        return {key: deepcopy(value) for key, value in spec.items() if key != "workflow"}

    @staticmethod
    def _narrow_coddy(root_coddy, child_task, child_id):
        """Merge a child's ``coddy`` block onto the root's, refusing any widening.

        A plan may keep or drop the root session, lower the permission mode and
        adjust the prompt text of an existing mention.  It can never resume a new
        session, raise a permission mode, or add or redirect a subagent mention.
        """
        if "coddy" not in child_task:
            return deepcopy(root_coddy)
        requested = child_task["coddy"]
        if requested is None:
            return None
        if not isinstance(requested, dict):
            raise Invalid(f"child {child_id} coddy must be an object")
        root = root_coddy or {"session": None, "permission_mode": None, "stream": True, "mention": None}
        merged = {**deepcopy(root), **deepcopy(requested)}
        if merged.get("session") is not None and merged.get("session") != root["session"]:
            raise Invalid(f"child {child_id} requests a coddy session the root did not resume")
        # An unset root mode inherits from the session; a plan may only lower
        # that to ``ask`` because the inherited level is not known here.
        limit = PERMISSION_RANK[root["permission_mode"]] if root["permission_mode"] else 0
        mode = merged.get("permission_mode")
        if mode is not None and (mode not in PERMISSION_RANK or PERMISSION_RANK[mode] > limit):
            raise Invalid(f"child {child_id} broadens the coddy permission mode")
        mention = merged.get("mention")
        root_mention = root["mention"]
        if "mention" in requested and mention is not None:
            if root_mention is None:
                raise Invalid(f"child {child_id} introduces a coddy subagent mention")
            if not isinstance(mention, dict):
                raise Invalid(f"child {child_id} coddy.mention must be an object")
            mention = {**deepcopy(root_mention), **mention}
            for name in (set(mention) | set(root_mention)) - NARROWABLE_MENTION_FIELDS:
                if mention.get(name) != root_mention.get(name):
                    raise Invalid(f"child {child_id} changes coddy.mention.{name}")
            mention_limit = root_mention.get("permission_mode") or root["permission_mode"]
            mention_rank = PERMISSION_RANK[mention_limit] if mention_limit else 0
            child_mode = mention.get("permission_mode")
            if child_mode is not None and \
                    (child_mode not in PERMISSION_RANK or PERMISSION_RANK[child_mode] > mention_rank):
                raise Invalid(f"child {child_id} broadens the coddy mention permission mode")
            merged["mention"] = mention
        return merged

    @classmethod
    def _child_spec(cls, root_spec, child, workspace_root, allow_write, token_share, workflow_bounded):
        """Build one child's task from root authority and the plan's narrowing.

        ``token_share`` is the ceiling assigned to a child that sets no
        ``max_tokens`` under a bounded workflow; the caller checks the sum.
        """
        child_task = child["task"]
        unknown = set(child_task) - CHILD_TASK_FIELDS
        if unknown:
            raise Invalid(f"child {child['id']} sets task fields a plan cannot set: {', '.join(sorted(unknown))}")
        raw = deepcopy(root_spec)
        raw.pop("workflow", None)
        if raw.get("runtime") != "demo":
            raw.pop("demo", None)  # the root's normalized demo defaults are not authority
        coddy = cls._narrow_coddy(root_spec.get("coddy"), child_task, child["id"])
        raw.update(deepcopy({key: value for key, value in child_task.items() if key != "coddy"}))
        raw.pop("coddy", None)
        if coddy is not None:
            raw["coddy"] = coddy
        # A planner's output is untrusted: it may only remove authority.
        if raw.get("runtime") != root_spec["runtime"]:
            raise Invalid(f"child {child['id']} requests a runtime outside the root authority")
        if raw.get("workspace", root_spec["workspace"]) != root_spec["workspace"]:
            raise Invalid(f"child {child['id']} requests a different workspace")
        if root_spec["sandbox"] == "read-only" and raw.get("sandbox", "read-only") != "read-only":
            raise Invalid(f"child {child['id']} broadens read-only authority")
        if raw.get("sandbox", root_spec["sandbox"]) == "workspace-write" and "write_file" not in root_spec["tools"]:
            raise Invalid(f"child {child['id']} requests write access not granted by the root")
        child_tools = raw.get("tools", root_spec["tools"])
        if not isinstance(child_tools, list) or not set(child_tools).issubset(root_spec["tools"]):
            raise Invalid(f"child {child['id']} requests tools outside the root authority")
        # A null root model is not a licence to pick one: the child must keep it.
        if raw.get("model") != root_spec["model"]:
            raise Invalid(f"child {child['id']} requests a model outside the root policy")
        for key in ("retry", "budget"):
            if key in child_task and not isinstance(child_task[key], dict):
                raise Invalid(f"child {child['id']} {key} must be an object")
        # A child inherits every retry and budget field it does not set. It may
        # tighten a field, never loosen it, and it cannot drop a ceiling by
        # writing null where the root has a number.
        retry = {**root_spec["retry"], **child_task.get("retry", {})}
        if retry.get("max_attempts", 1) > root_spec["retry"]["max_attempts"] or \
                (retry.get("replay_safe", False) and not root_spec["retry"]["replay_safe"]):
            raise Invalid(f"child {child['id']} broadens retry authority")
        root_budget = root_spec["budget"]
        child_budget = child_task.get("budget", {})
        budget = {**root_budget, **child_budget}
        if root_budget["max_tokens"] is not None and budget.get("max_tokens") is None:
            raise Invalid(f"child {child['id']} cannot remove the root token ceiling")
        if workflow_bounded and "max_tokens" in child_budget and child_budget["max_tokens"] is None:
            raise Invalid(f"child {child['id']} cannot remove the workflow token ceiling")
        for key in ("deadline_seconds", "attempt_seconds", "max_steps", "max_output_bytes",
                    "request_seconds", "output_tokens", "max_tokens"):
            value, ceiling = budget.get(key), root_budget.get(key)
            if value is not None and ceiling is not None and \
                    isinstance(value, (int, float)) and not isinstance(value, bool) and value > ceiling:
                raise Invalid(f"child {child['id']} broadens budget {key} beyond the root")
        if "max_tokens" not in child_budget and token_share is not None:
            inherited = budget.get("max_tokens")
            budget["max_tokens"] = token_share if inherited is None else min(inherited, token_share)
        raw["retry"] = retry
        raw["budget"] = budget
        raw["tools"] = child_tools
        raw["sandbox"] = raw.get("sandbox", root_spec["sandbox"])
        raw["workspace"] = root_spec["workspace"]
        return validate_spec(raw, workspace_root, allow_write)

    @staticmethod
    def _insert_task_locked(db, spec, task_id):
        now = time.time()
        db.execute("""INSERT INTO tasks(id,spec,status,desired_action,submitted_at,deadline,next_run_at,
                   observation_condition,version,tokens_used,usage_unknown) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (task_id, canonical(spec), "Pending", "Run", now,
                    now + spec["budget"]["deadline_seconds"], now, "Fresh", 0, 0, 0))
        event(db, "task.accepted", task_id, spec=spec, internal=True)

    def create(self, raw, key):
        """Accept a workflow root and its ordinary planner task atomically."""
        self.store.key_check(key)
        if not isinstance(raw, dict):
            raise Invalid("workflow root must be an object")
        settings = self.store.settings()
        workspace_root = Path(settings["workspace_root"])
        root_spec = validate_spec(raw, workspace_root, settings["allow_write"])
        config = root_spec.get("workflow")
        if not config or not config.get("enabled", True):
            raise Invalid("workflow planning is opt-in; set workflow.enabled=true")
        planner_raw = deepcopy(raw)
        planner = config.get("planner", {})
        planner_raw.pop("workflow", None)
        planner_raw.update(planner)
        planner_raw["sandbox"] = "read-only"
        planner_raw["tools"] = sorted(set(root_spec["tools"]) & {"list_files", "read_file"})
        planner_raw["output_schema"] = PLAN_SCHEMA
        planner_raw["objective"] = planner.get("objective", "Plan workflow: " + root_spec["objective"])
        planner_spec = validate_spec(planner_raw, workspace_root, False)
        workflow_id, planner_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        authority = self._authority(root_spec)
        # Use the same durable command boundary as Store.submit, but keep the
        # workflow and planner insert in one transaction.
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM commands WHERE idempotency_key=?", (key,)).fetchone()
            command_id = str(uuid.uuid4())
            command_spec_hash = digest({"kind": "submit", "spec": raw})
            if existing is not None:
                if existing["payload_hash"] != command_spec_hash:
                    raise Conflict("Idempotency key was already used with different content")
                root_row = db.execute("SELECT id FROM workflow_roots WHERE planner_task_id=?",
                                      (existing["task_id"],)).fetchone()
                if root_row is None:
                    raise Conflict("Idempotency key was already used by a plain task submission, not a workflow")
                return {"command_id": existing["id"], "task_id": existing["task_id"],
                        "workflow_id": root_row["id"], "duplicate": True}
            self._insert_task_locked(db, planner_spec, planner_id)
            db.execute("INSERT INTO commands(id,idempotency_key,kind,task_id,payload_hash,accepted_at) VALUES(?,?,?,?,?,?)",
                       (command_id, key, "submit", planner_id, command_spec_hash, now))
            db.execute("""INSERT INTO workflow_roots(
                id,state,planner_task_id,authority,max_children,max_tokens,max_attempts,
                planner_context_threshold,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (workflow_id, "planning", planner_id, canonical(authority), config["max_children"],
                        config["max_tokens"], config["max_attempts"], config["planner_context_threshold"], now, now))
            self._workflow_event(db, {"id": workflow_id, "planner_task_id": planner_id},
                                 "workflow.created", planner_task_id=planner_id)
            return {"command_id": command_id, "task_id": planner_id, "workflow_id": workflow_id, "duplicate": False}

    def workflow(self, workflow_id):
        with self.store.reading() as db:
            return self._root_public_locked(db, workflow_id)

    def workflows(self):
        with self.store.reading() as db:
            return [self._root_public_locked(db, row["id"]) for row in
                    db.execute("SELECT id FROM workflow_roots ORDER BY created_at,id")]

    def children(self, workflow_id, revision=None):
        with self.store.reading() as db:
            self._root_locked(db, workflow_id)
            return self._children_locked(db, workflow_id, revision)

    def _source_child_locked(self, db, workflow_id, child_key, before_revision):
        return db.execute("""SELECT * FROM workflow_children WHERE workflow_id=? AND child_key=?
                            AND revision<=? ORDER BY revision DESC LIMIT 1""",
                          (workflow_id, child_key, before_revision)).fetchone()

    def _record_rejected_locked(self, db, workflow_id, revision, plan, reason):
        root = self._root_locked(db, workflow_id)
        db.execute("INSERT INTO workflow_plans(workflow_id,revision,plan,plan_sha256,state,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                   (workflow_id, revision, canonical(plan), digest(plan), "rejected", reason, time.time()))
        if root["plan_revision"] == 0:
            # Without an accepted plan the workflow cannot proceed.  A rejected
            # replacement for an accepted plan leaves the current revision, and
            # its executing children, exactly as they were.
            db.execute("UPDATE workflow_roots SET state='failed',reason=?,updated_at=? WHERE id=?",
                       (reason, time.time(), workflow_id))
        self._workflow_event(db, root, "workflow.plan_rejected", revision=revision, reason=reason)

    def _carry_source_locked(self, db, workflow_id, child_key, before_revision):
        """The latest succeeded task for ``child_key``; only verified work is carried."""
        source = self._source_child_locked(db, workflow_id, child_key, before_revision)
        if source is None:
            return None
        status_row = db.execute("SELECT status FROM tasks WHERE id=?", (source["task_id"],)).fetchone()
        return source if status_row is not None and status_row[0] == "Succeeded" else None

    @staticmethod
    def _open_children_locked(db, workflow_id):
        """Non-terminal child tasks of any revision, with their current attempt state.

        ``not_started`` is true only when nothing can have run: the task has no
        current attempt or its attempt is still ``Queued``.  The runner commits
        ``Launching`` before any effect in a transaction that requires ``Queued``,
        so this serialized transaction proves a queued attempt has not started.
        """
        rows = db.execute("""SELECT DISTINCT t.id AS task_id,t.status,t.spec,t.tokens_used,
                                    t.current_attempt_id,a.state AS attempt_state
                             FROM workflow_children c JOIN tasks t ON t.id=c.task_id
                             LEFT JOIN attempts a ON a.id=t.current_attempt_id
                             WHERE c.workflow_id=? AND t.status NOT IN ('Succeeded','Failed','Cancelled')""",
                          (workflow_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["not_started"] = item["current_attempt_id"] is None or item["attempt_state"] == "Queued"
            result.append(item)
        return result

    def _settle_plan_locked(self, db, workflow_id, plan, *, replan=False):
        root = self._root_locked(db, workflow_id)
        if not replan and root["plan_revision"] > 0:
            # A plan is settled once; changing it is an explicit replan.  The
            # manager reaches this path when a planner finishes after a plan was
            # already settled through the API, so record the fact without failing.
            self._workflow_event(db, root, "workflow.plan_ignored", revision=root["plan_revision"],
                                 reason="workflow plan is already settled")
            return {"workflow_id": workflow_id, "revision": root["plan_revision"], "state": "ignored",
                    "children": [], "reason": "workflow plan is already settled"}
        root_spec = json.loads(root["authority"])
        settings = self.store.settings_from(db)
        latest = db.execute("SELECT COALESCE(MAX(revision),0) FROM workflow_plans WHERE workflow_id=?",
                            (workflow_id,)).fetchone()[0]
        revision = max(root["plan_revision"], latest) + 1
        open_children = self._open_children_locked(db, workflow_id) if replan else []
        try:
            children = self._validate_plan_shape(plan, root["max_children"])
            remaining_tokens = None if root["max_tokens"] is None else root["max_tokens"] - root["tokens_used"]
            remaining_attempts = root["max_attempts"] - root["attempts_used"]
            if remaining_tokens is not None and remaining_tokens <= 0:
                raise Invalid("workflow token budget is exhausted")
            if remaining_attempts < 1:
                raise Invalid("workflow attempt budget is exhausted")
            available = remaining_tokens
            if available is not None:
                # Launched children of earlier revisions keep their allocation
                # until they settle; only provably unstarted work is released.
                for old in open_children:
                    if not old["not_started"]:
                        ceiling = json.loads(old["spec"])["budget"].get("max_tokens") or 0
                        available -= max(0, ceiling - old["tokens_used"])
                if available <= 0:
                    raise Invalid("workflow token budget is fully allocated to children still running")
            carried = {child["id"]: self._carry_source_locked(db, workflow_id, child["id"], root["plan_revision"])
                       for child in children}
            fresh = [child for child in children if carried[child["id"]] is None]
            share = None
            if available is not None:
                explicit = [child["task"]["budget"]["max_tokens"] for child in fresh
                            if isinstance(child["task"].get("budget"), dict) and "max_tokens" in child["task"]["budget"]]
                implicit = len(fresh) - len(explicit)
                if implicit:
                    allocated = sum(value for value in explicit
                                    if isinstance(value, int) and not isinstance(value, bool))
                    share = (available - allocated) // implicit
                    if share < 1:
                        raise Invalid("no workflow token budget is left for children without a token ceiling")
            prepared = []
            for child in children:
                is_fresh = carried[child["id"]] is None
                spec = self._child_spec(root_spec, child, Path(settings["workspace_root"]), settings["allow_write"],
                                        share if is_fresh else None, available is not None)
                prepared.append((child, spec, carried[child["id"]]))
            if available is not None:
                requested = sum(spec["budget"]["max_tokens"] for _, spec, source in prepared if source is None)
                if requested > available:
                    raise Invalid(f"children request {requested} tokens but the workflow has {available} left")
        except (Invalid, SchemaError, RecursionError) as exc:
            self._record_rejected_locked(db, workflow_id, revision, plan, str(exc))
            return {"workflow_id": workflow_id, "revision": revision, "state": "rejected",
                    "children": [], "reason": str(exc)}

        now = time.time()
        for old in open_children:
            # Cancellation precedes replacement insertion.  Only provably unstarted
            # work is cancelled here.  A launched or Unknown child keeps its
            # reservation and goes through the normal cancellation path; it is
            # reported Cancelled only after the runner confirms the stop.
            db.execute("UPDATE tasks SET desired_action='Cancel',reason='obsolete_by_replan',version=version+1 WHERE id=?",
                       (old["task_id"],))
            if old["not_started"]:
                if old["current_attempt_id"] is not None:
                    db.execute("""UPDATE attempts SET reserved=0,state='Cancelled',settled=1,sequence=sequence+1,
                                finished_at=?,heartbeat=?,tokens=0,error_kind='cancelled',
                                error_message='Cancelled before launch' WHERE id=? AND state='Queued'""",
                               (now, now, old["current_attempt_id"]))
                db.execute("UPDATE tasks SET status='Cancelled',finished_at=?,observation_condition='Fresh' WHERE id=?",
                           (now, old["task_id"]))
                event(db, "workflow.child_cancelled", old["task_id"], reason="obsolete_by_replan")
            else:
                event(db, "workflow.child_cancel_requested", old["task_id"], reason="obsolete_by_replan",
                      attempt_state=old["attempt_state"])

        db.execute("INSERT INTO workflow_plans(workflow_id,revision,plan,plan_sha256,state,created_at) VALUES(?,?,?,?,?,?)",
                   (workflow_id, revision, canonical(plan), digest(plan), "accepted", time.time()))
        inserted = []
        for index, (child, spec, carried_source) in enumerate(prepared):
            internal = f"workflow:{workflow_id}:revision:{revision}:child:{index}"
            # Only verified, completed work is carried over. A child that was
            # pending, running or cancelled (including one cancelled just above as
            # obsolete) gets a fresh task under the new revision.
            if carried_source is not None:
                task_id = carried_source["task_id"]
                carried_output = carried_source["carried_output"] or self._task_result_json(db, task_id)
                db.execute("""INSERT INTO workflow_children(
                    internal_id,workflow_id,revision,child_index,child_key,task_id,dependencies,delivery,
                    carried_from_task_id,carried_output,measurements) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (internal, workflow_id, revision, index, child["id"], task_id,
                            canonical(child["dependencies"]), canonical(child["delivery"]), carried_source["task_id"],
                            carried_output, carried_source["measurements"]))
            else:
                task_id = str(uuid.uuid4())
                self._insert_task_locked(db, spec, task_id)
                db.execute("""INSERT INTO workflow_children(
                    internal_id,workflow_id,revision,child_index,child_key,task_id,dependencies,delivery)
                    VALUES(?,?,?,?,?,?,?,?)""",
                           (internal, workflow_id, revision, index, child["id"], task_id,
                            canonical(child["dependencies"]), canonical(child["delivery"])))
            inserted.append(task_id)
            event(db, "workflow.child_created", task_id, workflow_id=workflow_id, internal_id=internal,
                  child_key=child["id"], revision=revision)
        db.execute("UPDATE workflow_roots SET state='executing',plan_revision=?,iteration=iteration+?,reason=NULL,updated_at=? WHERE id=?",
                   (revision, 1 if replan else 0, time.time(), workflow_id))
        self._workflow_event(db, root, "workflow.plan_accepted", revision=revision, child_count=len(inserted))
        return {"workflow_id": workflow_id, "revision": revision, "state": "accepted", "children": inserted}

    def settle_plan(self, workflow_id, plan, *, replan=False, key=None):
        """Settle a plan (or replan) as one command.

        With an idempotency ``key`` the command is recorded: the same key and
        payload return the original receipt marked ``duplicate``; the same key
        with another payload is a conflict.  A non-replan settle is refused once
        the workflow has an accepted plan.
        """
        kind = "workflow_replan" if replan else "workflow_plan"
        payload_hash = None
        if key is not None:
            self.store.key_check(key)
            try:
                payload_hash = digest({"kind": kind, "workflow_id": workflow_id, "plan": plan})
            except (ValueError, TypeError, RecursionError) as exc:
                raise Invalid(f"Invalid workflow plan: {exc}") from exc
        with self.store.transaction() as db:
            if key is not None:
                existing = db.execute("SELECT * FROM commands WHERE idempotency_key=?", (key,)).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise Conflict("Idempotency key already used with a different payload")
                    stored = db.execute("SELECT receipt FROM workflow_plan_commands WHERE command_id=?",
                                        (existing["id"],)).fetchone()
                    if stored is None:
                        raise Conflict("Idempotency key was already used by a different command")
                    return {**json.loads(stored["receipt"]), "duplicate": True}
            root = self._root_locked(db, workflow_id)
            if not replan and root["plan_revision"] > 0:
                raise Conflict("workflow already has an accepted plan; use replan to change it")
            result = self._settle_plan_locked(db, workflow_id, plan, replan=replan)
            if key is not None:
                now, command_id = time.time(), str(uuid.uuid4())
                result = {**result, "command_id": command_id, "duplicate": False}
                db.execute("INSERT INTO commands(id,idempotency_key,kind,task_id,payload_hash,accepted_at) VALUES(?,?,?,?,?,?)",
                           (command_id, key, kind, root["planner_task_id"], payload_hash, now))
                db.execute("INSERT INTO workflow_plan_commands(command_id,workflow_id,receipt,created_at) VALUES(?,?,?,?)",
                           (command_id, workflow_id, canonical(result), now))
            return result

    def _task_result_json(self, db, task_id):
        row = db.execute("SELECT result_path FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or not row["result_path"]:
            return None
        # The accepted task's artifact has already passed output-schema and
        # checksum validation.  Keep carry-over bounded and JSON-only.
        artifact = self.store.home / row["result_path"]
        try:
            value = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return canonical(value)

    def _payload_for_source_locked(self, db, workflow_id, source, target, delivery):
        payload = {"source_task_id": source["task_id"]}
        if delivery.get("result", False):
            row = db.execute("SELECT result_path,result_sha256 FROM tasks WHERE id=?", (source["task_id"],)).fetchone()
            if row is None or row["result_path"] is None:
                raise Invalid("dependency has no accepted result")
            task = self.store.task(source["task_id"])
            attempt = task["attempts"][-1]
            artifact = self.store.home / row["result_path"]
            try:
                data = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise Invalid(f"dependency result cannot be delivered: {exc}") from exc
            if artifact_checksum(self.store, attempt, task["spec"]) != row["result_sha256"]:
                raise Invalid("dependency result checksum is invalid")
            payload["result"] = data
        files = []
        workspace = Path(json.loads(db.execute("SELECT spec FROM tasks WHERE id=?", (source["task_id"],)).fetchone()[0])["workspace"])
        for relative in delivery.get("files", []):
            path = Path(relative)
            if path.is_absolute() or not relative or any(part.startswith(".") for part in path.parts):
                raise Invalid(f"invalid dependency file path: {relative}")
            target_path = (workspace / path).resolve()
            if not target_path.is_file() or not target_path.is_relative_to(workspace):
                raise Invalid(f"dependency file is missing: {relative}")
            data = target_path.read_bytes()
            if len(data) > 1_048_576:
                raise Invalid("dependency file exceeds 1 MiB delivery limit")
            files.append({"path": relative, "content": data.decode("utf-8")})
        if files:
            payload["files"] = files
        return payload

    def _deliver_dependencies_locked(self, db, task_id):
        row = db.execute("SELECT * FROM workflow_children WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
        if row is None:
            return {"state": "not_workflow"}
        root = self._root_locked(db, row["workflow_id"])
        dependencies = json.loads(row["dependencies"])
        if not dependencies:
            return {"state": "ready", "bytes": 0, "deliveries": []}
        delivered, total = [], 0
        for key in dependencies:
            source = self._source_child_locked(db, row["workflow_id"], key, row["revision"])
            if source is None:
                return self._dependency_failed_locked(db, root, row, f"unknown dependency: {key}", row["task_id"])
            source_status = db.execute("SELECT status FROM tasks WHERE id=?", (source["task_id"],)).fetchone()[0]
            if source_status in ("Pending", "Scheduled", "Running"):
                return {"state": "waiting", "bytes": 0, "deliveries": []}
            if source_status != "Succeeded":
                return self._dependency_failed_locked(db, root, row, f"dependency {key} did not succeed", source["task_id"])
            # The producer declares what it delivers; the consumer only names its sources.
            declaration = json.loads(source["delivery"])
            try:
                payload = self._payload_for_source_locked(db, row["workflow_id"], source, row, declaration)
            except Invalid as exc:
                return self._dependency_failed_locked(db, root, row, str(exc), source["task_id"])
            encoded = canonical(payload)
            existing = db.execute("SELECT * FROM workflow_deliveries WHERE workflow_id=? AND source_task_id=? AND target_task_id=?",
                                  (row["workflow_id"], source["task_id"], task_id)).fetchone()
            if existing is None:
                now = time.time()
                db.execute("""INSERT INTO workflow_deliveries(
                    id,workflow_id,source_task_id,target_task_id,payload,payload_sha256,bytes_count,state,created_at,delivered_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (str(uuid.uuid4()), row["workflow_id"], source["task_id"], task_id, encoded,
                            digest(payload), len(encoded.encode()), "delivered", now, now))
            total += len(encoded.encode())
            delivered.append(payload)
        db.execute("UPDATE workflow_children SET context_bytes=? WHERE internal_id=?", (total, row["internal_id"]))
        event(db, "workflow.dependencies_delivered", task_id, bytes=total, count=len(delivered))
        return {"state": "ready", "bytes": total, "deliveries": delivered}

    @staticmethod
    def _dependency_failed_locked(db, root, child, reason, source_task_id):
        now = time.time()
        db.execute("""INSERT OR IGNORE INTO workflow_deliveries(
            id,workflow_id,source_task_id,target_task_id,payload,payload_sha256,bytes_count,state,error_message,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (str(uuid.uuid4()), child["workflow_id"], source_task_id, child["task_id"], "{}", digest({}), 0,
                    "failed", reason, now))
        db.execute("UPDATE tasks SET status='Failed',reason=?,finished_at=?,version=version+1 WHERE id=? AND status='Pending'",
                   ("dependency_transfer_failed: " + reason, now, child["task_id"]))
        db.execute("UPDATE workflow_roots SET reason=?,updated_at=? WHERE id=?", (reason, now, root["id"]))
        event(db, "workflow.dependency_failed", child["task_id"], reason=reason)
        return {"state": "failed", "bytes": 0, "deliveries": [], "reason": reason}

    def deliver_dependencies(self, task_id):
        with self.store.transaction() as db:
            return self._deliver_dependencies_locked(db, task_id)

    def claim_attempt_locked(self, db, task_id):
        row = db.execute("SELECT * FROM workflow_children WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
        if row is None:
            return True
        root = self._root_locked(db, row["workflow_id"])
        reason = None
        if root["max_tokens"] is not None and root["tokens_used"] >= root["max_tokens"]:
            reason = "workflow_token_budget_exhausted"
        elif root["state"] == "failed":
            reason = root["reason"] or "workflow_failed"
        elif root["attempts_used"] >= root["max_attempts"]:
            reason = "workflow_attempt_budget_exhausted"
        if reason is not None:
            # A failed or exhausted workflow admits no further child attempts.
            now = time.time()
            db.execute("UPDATE tasks SET status='Failed',reason=?,finished_at=?,version=version+1 WHERE id=? AND status='Pending'",
                       (reason, now, task_id))
            event(db, "task.finished", task_id, status="Failed", reason=reason)
            return False
        db.execute("UPDATE workflow_roots SET attempts_used=attempts_used+1,updated_at=? WHERE id=?",
                   (time.time(), row["workflow_id"]))
        return True

    def record_usage_locked(self, db, task_id, tokens):
        row = db.execute("SELECT * FROM workflow_children WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
        if row is None or tokens is None:
            return
        root = self._root_locked(db, row["workflow_id"])
        used = root["tokens_used"] + max(0, int(tokens))
        db.execute("UPDATE workflow_roots SET tokens_used=?,updated_at=? WHERE id=?", (used, time.time(), row["workflow_id"]))
        if root["max_tokens"] is not None and used > root["max_tokens"]:
            db.execute("UPDATE workflow_roots SET state='failed',reason='workflow_token_budget_exhausted' WHERE id=?",
                       (row["workflow_id"],))

    def replan(self, workflow_id, plan, key=None):
        return self.settle_plan(workflow_id, plan, replan=True, key=key)


# A small functional alias is convenient for callers that only need validation.
def validate_plan(plan, max_children=100):
    return WorkflowStore._validate_plan_shape(plan, max_children)
