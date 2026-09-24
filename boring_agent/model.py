"""Public task contract. Execution observations never enter through this schema."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

TERMINAL = frozenset({"Succeeded", "Failed", "Cancelled"})
ACTIVE = frozenset({"Queued", "Launching", "Running", "Unknown"})


class AgentError(Exception):
    code = "agent_error"


class Invalid(AgentError):
    code = "invalid_request"


class NotFound(AgentError):
    code = "not_found"


class Conflict(AgentError):
    code = "conflict"


class StorageError(AgentError):
    code = "storage_error"


class Gone(AgentError):
    code = "gone"


class SessionError(AgentError):
    code = "session_error"


class SessionConflict(SessionError, Conflict):
    code = "session_conflict"


class SessionNotFound(SessionError, NotFound):
    code = "session_not_found"


SESSION_DIGEST_MAX_BYTES = 24 * 1024
SESSION_MENTION_RE = re.compile(r"^@session:([A-Za-z0-9][A-Za-z0-9._:-]{0,199})$")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def session_id_from_mention(value: str | None) -> str | None:
    """Return the ID from Coddy's read-only session mention, if present."""
    if value is None:
        return None
    if not isinstance(value, str) or SESSION_MENTION_RE.fullmatch(value) is None:
        raise Invalid("session must be an exact @session:<id> mention")
    return SESSION_MENTION_RE.fullmatch(value).group(1)


def session_mention(session_id: str) -> str:
    if not isinstance(session_id, str) or SESSION_MENTION_RE.fullmatch("@session:" + session_id) is None:
        raise Invalid("session ID is not valid for an @session mention")
    return "@session:" + session_id


def validate_native_job(raw: dict) -> dict:
    """Validate the immutable document consumed by native exec.

    This intentionally does not widen ``validate_spec``: the latter is the
    legacy manager/worker task contract. Native jobs are admitted by the
    lifecycle scheduler and are not silently routed through that runtime.
    """
    fields(raw, {"id", "job_id", "objective", "runtime", "model", "session",
                 "dependencies", "budget", "workspace", "output_schema",
                 "expect_files", "metadata"}, "native job")
    job_id = raw.get("job_id", raw.get("id"))
    if not isinstance(job_id, str) or not 1 <= len(job_id) <= 200:
        raise Invalid("native job id must contain 1–200 characters")
    objective = raw.get("objective", "")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 100_000:
        raise Invalid("objective must contain 1–100000 characters")
    runtime = raw.get("runtime")
    if runtime not in ("acp", "coddy_native"):
        raise Invalid("native job runtime must be acp or coddy_native")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise Invalid("native job model is required and must be a nonempty model identifier")
    mentioned_session = session_id_from_mention(raw.get("session"))
    dependencies = raw.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item.strip() for item in dependencies):
        raise Invalid("dependencies must be a list of nonempty job IDs")
    if len(set(dependencies)) != len(dependencies):
        raise Invalid("dependencies must not contain duplicates")
    budget = raw.get("budget", {})
    if not isinstance(budget, dict):
        raise Invalid("budget must be an object")
    fields(budget, {"deadline_seconds", "attempt_seconds", "max_steps", "max_tokens"}, "native job budget")
    normalized_budget = {"deadline_seconds": 1800, "attempt_seconds": 1800,
                         "max_steps": None, "max_tokens": None, **budget}
    number(normalized_budget["deadline_seconds"], "deadline_seconds", .1, 86400)
    number(normalized_budget["attempt_seconds"], "attempt_seconds", .1, 86400)
    if normalized_budget["max_steps"] is not None:
        number(normalized_budget["max_steps"], "max_steps", 1, 100, True)
    if normalized_budget["max_tokens"] is not None:
        number(normalized_budget["max_tokens"], "max_tokens", 1, 1_000_000_000, True)
    workspace = raw.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        raise Invalid("native job workspace is required")
    normalized = {"id": job_id, "objective": objective, "runtime": runtime, "model": model,
                  "session": "@session:" + mentioned_session if mentioned_session else None,
                  "dependencies": sorted(set(dependencies)), "budget": normalized_budget,
                  "workspace": raw.get("workspace"), "output_schema": raw.get("output_schema"),
                  "expect_files": raw.get("expect_files", []), "metadata": raw.get("metadata", {})}
    if len(canonical(normalized).encode()) > 256_000:
        raise Invalid("Native job specification exceeds 256000 bytes")
    return normalized


def strict_json(text: str) -> Any:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise Invalid(f"Duplicate JSON field: {key}")
            out[key] = value
        return out

    def invalid_constant(value):
        raise Invalid(f"Non-finite JSON number: {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise Invalid(f"Invalid JSON: {exc}") from exc


def fields(value, allowed, label):
    if not isinstance(value, dict):
        raise Invalid(f"{label} must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise Invalid(f"Unknown {label} fields: {', '.join(sorted(unknown))}")


def number(value, label, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid(f"{label} must be a number")
    if not low <= value <= high or not math.isfinite(value):
        raise Invalid(f"{label} must be between {low} and {high}")
    if integer and not isinstance(value, int):
        raise Invalid(f"{label} must be an integer")
    return value


def validate_spec(raw: dict, workspace_root: Path, allow_write: bool) -> dict:
    fields(raw, {"schema_version", "objective", "runtime", "workspace", "model",
                 "sandbox", "tools", "output_schema", "budget", "retry", "demo", "expect_files",
                 "workflow"}, "task")
    if type(raw.get("schema_version", 1)) is not int or raw.get("schema_version", 1) != 1:
        raise Invalid("Only schema_version 1 is supported")
    objective = raw.get("objective")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 100_000:
        raise Invalid("objective must contain 1–100000 characters")
    runtime = raw.get("runtime", "llm")
    if runtime not in ("llm", "demo"):
        raise Invalid("runtime must be llm or demo")
    workspace = raw.get("workspace", str(workspace_root))
    if not isinstance(workspace, str):
        raise Invalid("workspace must be a path string")
    path = Path(workspace)
    path = (workspace_root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_dir() or not path.is_relative_to(workspace_root):
        raise Invalid("workspace must be an existing directory inside the configured workspace root")
    sandbox = raw.get("sandbox", "read-only")
    if sandbox not in ("read-only", "workspace-write"):
        raise Invalid("sandbox must be read-only or workspace-write")
    if sandbox == "workspace-write" and not allow_write:
        raise Invalid("This installation has not enabled workspace-write tasks")
    tools = raw.get("tools", ["list_files", "read_file"])
    if not isinstance(tools, list) or any(x not in ("list_files", "read_file", "write_file") for x in tools):
        raise Invalid("tools may contain list_files, read_file and write_file only")
    if "write_file" in tools and sandbox != "workspace-write":
        raise Invalid("write_file requires sandbox=workspace-write")
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200):
        raise Invalid("model must be a nonempty model identifier")
    schema = raw.get("output_schema", {"type": "object"})
    if not isinstance(schema, dict) or len(canonical(schema)) > 32768:
        raise Invalid("output_schema must be a JSON Schema object of at most 32768 characters")

    def check_refs(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("$ref", "$dynamicRef") and not (isinstance(value, str) and value.startswith("#")):
                    raise Invalid("Only local JSON Schema references are allowed")
                check_refs(value)
        elif isinstance(obj, list):
            for item in obj:
                check_refs(item)
    check_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise Invalid(f"Invalid output_schema: {exc.message}") from exc
    budget = raw.get("budget", {})
    fields(budget, {"deadline_seconds", "attempt_seconds", "max_output_bytes", "max_tokens",
                    "max_steps", "request_seconds", "output_tokens"}, "budget")
    budget = {"deadline_seconds": 600, "attempt_seconds": 300, "max_output_bytes": 1_048_576,
              "max_tokens": None, "max_steps": 12, "request_seconds": 60, "output_tokens": 2048, **budget}
    number(budget["deadline_seconds"], "deadline_seconds", .1, 86400)
    number(budget["attempt_seconds"], "attempt_seconds", .1, 86400)
    number(budget["max_output_bytes"], "max_output_bytes", 64, 10_485_760, True)
    if budget["max_tokens"] is not None:
        number(budget["max_tokens"], "max_tokens", 1, 1_000_000_000, True)
    number(budget["max_steps"], "max_steps", 1, 100, True)
    number(budget["request_seconds"], "request_seconds", .1, 300)
    number(budget["output_tokens"], "output_tokens", 1, 65536, True)
    retry = raw.get("retry", {})
    fields(retry, {"max_attempts", "replay_safe", "backoff_seconds", "on"}, "retry")
    retry = {"max_attempts": 1, "replay_safe": False, "backoff_seconds": 1,
             "on": ["transient"], **retry}
    number(retry["max_attempts"], "max_attempts", 1, 10, True)
    number(retry["backoff_seconds"], "backoff_seconds", .01, 3600)
    if not isinstance(retry["replay_safe"], bool):
        raise Invalid("replay_safe must be boolean")
    if not isinstance(retry["on"], list) or any(x not in ("transient", "validation") for x in retry["on"]):
        raise Invalid("retry.on may contain transient and validation only")
    demo = raw.get("demo", {})
    fields(demo, {"delay_seconds", "fail_attempts", "failure_kind", "result"}, "demo")
    if demo and runtime != "demo":
        raise Invalid("demo options require the demo runtime")
    demo = {"delay_seconds": .1, "fail_attempts": 0, "failure_kind": "transient", **demo}
    number(demo["delay_seconds"], "delay_seconds", 0, 3600)
    number(demo["fail_attempts"], "fail_attempts", 0, 10, True)
    if demo["failure_kind"] not in ("transient", "permanent"):
        raise Invalid("demo failure_kind must be transient or permanent")
    expect_files = raw.get("expect_files", [])
    if not isinstance(expect_files, list) or len(expect_files) > 50:
        raise Invalid("expect_files must be a list of at most 50 relative paths")
    for item in expect_files:
        if not isinstance(item, str) or not item or len(item) > 4096 or Path(item).is_absolute() or \
                any(part.startswith(".") for part in Path(item).parts):
            raise Invalid("expect_files entries must be visible relative paths inside the workspace")
    workflow = raw.get("workflow")
    if workflow is not None:
        fields(workflow, {"enabled", "max_children", "max_tokens", "max_attempts",
                          "planner_context_threshold", "planner", "authority"}, "workflow")
        if not isinstance(workflow.get("enabled", True), bool):
            raise Invalid("workflow.enabled must be boolean")
        for name, low, high in (("max_children", 1, 1000), ("max_tokens", 1, 1_000_000_000),
                                ("max_attempts", 1, 1000), ("planner_context_threshold", 1, 1_000_000_000)):
            value = workflow.get(name)
            if value is not None:
                number(value, f"workflow.{name}", low, high, True)
        planner = workflow.get("planner", {})
        if not isinstance(planner, dict):
            raise Invalid("workflow.planner must be an object")
        fields(planner, {"objective", "runtime", "demo", "budget", "model", "tools"}, "workflow.planner")
        authority = workflow.get("authority", {})
        if not isinstance(authority, dict):
            raise Invalid("workflow.authority must be an object")
    normalized_workflow = None
    if workflow is not None:
        normalized_workflow = {
            "enabled": workflow.get("enabled", True),
            "max_children": workflow.get("max_children", 100),
            "max_tokens": workflow.get("max_tokens", budget["max_tokens"]),
            "max_attempts": workflow.get("max_attempts", retry["max_attempts"]),
            "planner_context_threshold": workflow.get("planner_context_threshold"),
            "planner": workflow.get("planner", {}),
            "authority": workflow.get("authority", {}),
        }
    spec = {"schema_version": 1, "objective": objective, "runtime": runtime,
            "workspace": str(path), "model": model, "sandbox": sandbox,
            "tools": sorted(set(tools)), "output_schema": schema, "budget": budget, "retry": retry, "demo": demo,
            "expect_files": sorted(set(expect_files))}
    if normalized_workflow is not None:
        spec["workflow"] = normalized_workflow
    if len(canonical(spec).encode()) > 256_000:
        raise Invalid("Task specification exceeds 256000 bytes")
    return spec
