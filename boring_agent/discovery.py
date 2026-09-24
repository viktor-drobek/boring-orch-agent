"""Consent-aware, local discovery of executable and provider routes.

Discovery is deliberately separate from task execution.  Passive inventory only
inspects the local filesystem and environment.  Handshake probes are short-lived
process groups, and generative probes require a durable approval with a cost
policy.  Probe evidence is sanitized before it crosses the storage boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from .model import Conflict, Invalid, StorageError, canonical, digest


DEFAULT_ROUTES = (
    {"id": "coddy", "executable": "coddy", "args": ["--version"]},
    {"id": "codex", "executable": "codex", "args": ["--version"]},
    {"id": "claude", "executable": "claude", "args": ["--version"]},
)
DISCOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_inventory(
 id TEXT PRIMARY KEY, route_key TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL,
 recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_approvals(
 id TEXT PRIMARY KEY, route_key TEXT NOT NULL, tier TEXT NOT NULL,
 fingerprint TEXT NOT NULL, route TEXT NOT NULL, cost_policy TEXT,
 created_at REAL NOT NULL, revoked_at REAL);
CREATE INDEX IF NOT EXISTS discovery_approval_route ON discovery_approvals(route_key);
CREATE TABLE IF NOT EXISTS discovery_evidence(
 id TEXT PRIMARY KEY, route_key TEXT NOT NULL, tier TEXT NOT NULL,
 status TEXT NOT NULL, evidence TEXT NOT NULL, recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_audit(
 id TEXT PRIMARY KEY, route_key TEXT NOT NULL, action TEXT NOT NULL,
 outcome TEXT NOT NULL, details TEXT NOT NULL, recorded_at REAL NOT NULL);
"""

_SECRET_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|password|passwd|secret|credential|authorization)", re.I)
_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|password|passwd|secret|credential)\b\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_TOKEN_PREFIX = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|AKIA[A-Z0-9]{12,})\b")
_URL = re.compile(r"https?://[^\s\"'<>]+")


def _credential_name(key: str) -> bool:
    return bool(_SECRET_KEY.search(key)) and not key.lower().endswith(("_ref", "_refs", "-ref", "reference"))


def _reject_credential_values(value: Any, path: str = "route") -> None:
    """Reject credentials supplied as data; references are the only accepted form."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _credential_name(key):
                raise Invalid(f"{path}.{key} must be a credential reference, not a value")
            _reject_credential_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_credential_values(item, f"{path}[{index}]")


def _bounded_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else canonical(value)
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    urls = []
    for url in _URL.findall(text):
        if url not in urls:
            urls.append(url)
    suffix = "…[truncated]"
    if urls:
        suffix += " URLs: " + " ".join(urls)
    suffix_bytes = suffix.encode("utf-8")
    prefix_limit = max(0, limit - len(suffix_bytes))
    return data[:prefix_limit].decode("utf-8", "ignore") + suffix


def sanitize_evidence(value: Any, limit: int = 64 * 1024) -> str:
    """Redact credential-shaped values while retaining URLs and version text."""
    if isinstance(value, (dict, list)):
        text = canonical(value)
    else:
        text = str(value)
    text = _SECRET_VALUE.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub(r"\1[REDACTED]", text)
    text = _TOKEN_PREFIX.sub("[REDACTED]", text)
    return _bounded_text(text, limit)


def _sanitize_value(value: Any, limit: int) -> Any:
    """Sanitize a JSON value without truncating the enclosing JSON document."""
    if isinstance(value, Mapping):
        return {str(key): _sanitize_value(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, limit) for item in value]
    if isinstance(value, str):
        return sanitize_evidence(value, limit)
    return value


def _safe_metadata(route: Mapping[str, Any], limit: int) -> dict:
    metadata = {}
    for key in ("url", "base_url", "version", "provider", "model"):
        value = route.get(key)
        if value is not None:
            metadata[key] = sanitize_evidence(value, limit)
    return metadata


def _file_identity(path: str | None) -> dict | None:
    if not path:
        return None
    target = Path(path)
    try:
        stat = target.stat()
        if not target.is_file():
            return None
        checksum = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        return {"path": str(target), "realpath": str(target.resolve()), "device": stat.st_dev,
                "inode": stat.st_ino, "mode": stat.st_mode, "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "sha256": checksum.hexdigest()}
    except (OSError, ValueError):
        return None


def _route_id(route: Mapping[str, Any]) -> str:
    value = route.get("id", route.get("name"))
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or not value.strip():
        raise Invalid("discovery route id must be a nonempty string of at most 200 characters")
    return value


def _validate_route(route: Mapping[str, Any]) -> dict:
    if not isinstance(route, Mapping):
        raise Invalid("discovery route must be an object")
    _reject_credential_values(route)
    allowed = {"id", "name", "command", "executable", "args", "env", "env_overrides",
               "cwd", "provider", "base_url", "url", "model", "version", "metadata",
               "credential_ref", "credential_refs", "unlisted"}
    unknown = set(route) - allowed
    if unknown:
        raise Invalid("Unknown discovery route fields: " + ", ".join(sorted(unknown)))
    normalized = dict(route)
    normalized["id"] = _route_id(route)
    command = route.get("command")
    executable = route.get("executable")
    if executable is None and isinstance(command, list) and command:
        executable, normalized["args"] = command[0], command[1:]
    if not isinstance(executable, str) or not executable.strip():
        raise Invalid("discovery route executable is required")
    normalized["executable"] = executable
    args = normalized.get("args", [])
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args) or len(args) > 32:
        raise Invalid("discovery route args must contain at most 32 strings")
    normalized["args"] = args
    for name in ("env", "env_overrides"):
        values = normalized.get(name, {})
        if not isinstance(values, Mapping) or any(not isinstance(k, str) or not isinstance(v, str)
                                                  for k, v in values.items()):
            raise Invalid(f"discovery route {name} must map strings to strings")
    for name in ("credential_ref",):
        if name in normalized and (not isinstance(normalized[name], str) or not normalized[name].strip()):
            raise Invalid(f"{name} must be a nonempty reference")
    refs = normalized.get("credential_refs", {})
    if not isinstance(refs, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                                            for k, v in refs.items()):
        raise Invalid("credential_refs must map names to nonempty references")
    return normalized


def _resolve(route: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict:
    route = _validate_route(route)
    source_env = dict(os.environ if environ is None else environ)
    overrides = dict(route.get("env", {}))
    overrides.update(route.get("env_overrides", {}))
    process_env = dict(source_env)
    for key, value in overrides.items():
        if _credential_name(key):
            if value not in route.get("credential_refs", {}).values() and not value.startswith("ref:"):
                raise Invalid(f"environment override {key} must use a credential reference")
            # A reference is not a secret. Resolve the actual credential only from
            # the operator environment when a probe is explicitly approved.
            ref_name = value.removeprefix("ref:")
            process_env[key] = source_env.get(ref_name, "")
        else:
            process_env[key] = os.path.expandvars(value) if value.startswith("$") else value
    executable = route["executable"]
    if executable.startswith("$"):
        executable = process_env.get(executable[1:], "")
    resolved = shutil.which(executable, path=process_env.get("PATH")) if not os.path.isabs(executable) else executable
    resolved = str(Path(resolved).resolve()) if resolved else None
    identity = _file_identity(resolved)
    override_fingerprints = {
        key: digest({"value": process_env.get(key, ""), "source": overrides[key] if not _credential_name(key) else "credential-reference"})
        for key in sorted(overrides)
    }
    fingerprint_payload = {
        "route": route["id"], "executable": identity, "args": route["args"],
        "overrides": override_fingerprints, "provider": route.get("provider"),
        "base_url": route.get("base_url", route.get("url")), "model": route.get("model"),
        "credential_ref": route.get("credential_ref"), "credential_refs": route.get("credential_refs", {}),
    }
    return {"route": route, "route_key": route["id"], "executable": resolved, "identity": identity,
            "env": process_env, "override_fingerprints": override_fingerprints,
            "fingerprint": digest(fingerprint_payload)}


def passive_inventory(routes: list[Mapping[str, Any]] | None = None,
                      environ: Mapping[str, str] | None = None) -> list[dict]:
    """Inspect executable availability without starting a process or making a request."""
    values = routes if routes is not None else list(DEFAULT_ROUTES)
    env = dict(os.environ if environ is None else environ)
    result = []
    for raw in values:
        route = _validate_route(raw)
        resolved = _resolve(route, env)
        result.append({"route": route["id"], "available": resolved["identity"] is not None,
                       "executable": resolved["executable"], "executable_identity": resolved["identity"],
                       "metadata": _safe_metadata(route, 4096),
                       "credential_ref": route.get("credential_ref") or
                       ("env:BOA_API_KEY" if env.get("BOA_API_KEY") else None),
                       "tier": "passive", "recorded_at": time.time()})
    return result


def seed_passive_inventory(db, workspace_root: str | Path | None = None) -> list[dict]:
    ensure_schema(db)
    values = passive_inventory()
    now = time.time()
    for item in values:
        db.execute("INSERT OR REPLACE INTO discovery_inventory(id,route_key,metadata,recorded_at) VALUES(?,?,?,?)",
                   (str(uuid.uuid4()), item["route"], canonical(item), now))
    return values


def ensure_schema(db) -> None:
    db.executescript(DISCOVERY_SCHEMA)


@dataclass(frozen=True)
class _Authorization:
    resolved: dict
    tier: str
    approval_id: str | None
    unlisted: bool
    cost_policy: Any = None


class Discovery:
    """Durable inventory, approvals and consent-gated probe operations."""
    def __init__(self, store, *, max_output_bytes: int | None = None):
        self.store = store
        configured = store.settings().get("discovery_output_bytes", 64 * 1024)
        self.max_output_bytes = max_output_bytes or configured
        if isinstance(self.max_output_bytes, bool) or not 64 <= self.max_output_bytes <= 10 * 1024 * 1024:
            raise Invalid("discovery max_output_bytes must be between 64 and 10485760")

    def inventory(self, routes: list[Mapping[str, Any]] | None = None) -> list[dict]:
        if routes is not None:
            values = passive_inventory(routes)
            with self.store.transaction() as db:
                ensure_schema(db)
                for item in values:
                    db.execute("INSERT OR REPLACE INTO discovery_inventory(id,route_key,metadata,recorded_at) VALUES(?,?,?,?)",
                               (str(uuid.uuid4()), item["route"], canonical(item), time.time()))
            return values
        with self.store.reading() as db:
            ensure_schema(db)
            return [json.loads(row["metadata"]) for row in db.execute(
                "SELECT metadata FROM discovery_inventory ORDER BY route_key")]

    passive = inventory

    def approvals(self) -> list[dict]:
        with self.store.reading() as db:
            ensure_schema(db)
            return [{**dict(row), "route": json.loads(row["route"]),
                     "cost_policy": json.loads(row["cost_policy"]) if row["cost_policy"] else None}
                    for row in db.execute("SELECT * FROM discovery_approvals ORDER BY created_at")]

    def evidence(self) -> list[dict]:
        with self.store.reading() as db:
            ensure_schema(db)
            return [{**dict(row), "evidence": json.loads(row["evidence"])}
                    for row in db.execute("SELECT * FROM discovery_evidence ORDER BY recorded_at")]

    def audit(self) -> list[dict]:
        with self.store.reading() as db:
            ensure_schema(db)
            return [{**dict(row), "details": json.loads(row["details"])}
                    for row in db.execute("SELECT * FROM discovery_audit ORDER BY recorded_at")]

    def approve(self, route: Mapping[str, Any], tier: str = "handshake", *, cost_policy: Any = None) -> dict:
        if tier not in ("handshake", "generative"):
            raise Invalid("approval tier must be handshake or generative")
        if tier == "generative" and (cost_policy is None or cost_policy == "" or cost_policy == {}):
            raise Invalid("generative approval requires a stated cost policy")
        resolved = _resolve(route)
        if resolved["identity"] is None:
            raise Invalid("cannot approve a route whose executable is unavailable")
        route_record = self._route_record(resolved)
        approval_id = str(uuid.uuid4())
        with self.store.transaction() as db:
            ensure_schema(db)
            db.execute("INSERT INTO discovery_approvals VALUES(?,?,?,?,?,?,?,NULL)",
                       (approval_id, resolved["route_key"], tier, resolved["fingerprint"],
                        canonical(route_record), canonical(cost_policy) if cost_policy is not None else None, time.time()))
            self._audit_db(db, resolved["route_key"], "approval", "approved",
                           {"tier": tier, "approval_id": approval_id, "cost_policy": cost_policy})
        return {"approval_id": approval_id, "route": resolved["route_key"], "tier": tier,
                "fingerprint": resolved["fingerprint"], "cost_policy": cost_policy}

    approve_route = approve

    @staticmethod
    def _route_record(resolved: dict) -> dict:
        route = resolved["route"]
        return {"id": route["id"], "executable": resolved["executable"], "args": route["args"],
                "executable_identity": resolved["identity"], "override_fingerprints": resolved["override_fingerprints"],
                "provider": route.get("provider"), "base_url": route.get("base_url", route.get("url")),
                "model": route.get("model"), "credential_ref": route.get("credential_ref"),
                "credential_refs": route.get("credential_refs", {})}

    def _authorize(self, route, tier, approval_id, allow_unlisted) -> _Authorization:
        if tier not in ("handshake", "generative"):
            raise Invalid("probe tier must be handshake or generative")
        resolved = _resolve(route)
        if allow_unlisted:
            self._record_audit(resolved["route_key"], "unlisted_invocation", "approved",
                               {"tier": tier, "fingerprint": resolved["fingerprint"]})
            return _Authorization(resolved, tier, None, True)
        if not isinstance(approval_id, str) or not approval_id:
            self._record_audit(resolved["route_key"], "probe", "refused", {"tier": tier, "reason": "approval_required"})
            raise Conflict("explicit discovery approval is required")
        with self.store.reading() as db:
            ensure_schema(db)
            row = db.execute("SELECT * FROM discovery_approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None or row["revoked_at"] is not None or row["tier"] != tier:
            self._record_audit(resolved["route_key"], "probe", "refused", {"tier": tier, "reason": "approval_invalid"})
            raise Conflict("discovery approval is missing, revoked, or for another tier")
        if row["route_key"] != resolved["route_key"] or row["fingerprint"] != resolved["fingerprint"]:
            self._record_audit(resolved["route_key"], "probe", "refused", {"tier": tier, "reason": "route_changed"})
            raise Conflict("route changed; explicit re-approval is required")
        return _Authorization(resolved, tier, approval_id, False,
                              json.loads(row["cost_policy"]) if row["cost_policy"] else None)

    def _record_audit(self, route_key: str, action: str, outcome: str, details: Mapping[str, Any]) -> None:
        with self.store.transaction() as db:
            ensure_schema(db)
            self._audit_db(db, route_key, action, outcome, details)

    @staticmethod
    def _audit_db(db, route_key: str, action: str, outcome: str, details: Mapping[str, Any]) -> None:
        safe = _sanitize_value(details, 16 * 1024)
        db.execute("INSERT INTO discovery_audit VALUES(?,?,?,?,?,?)",
                   (str(uuid.uuid4()), route_key, action, outcome, canonical(safe), time.time()))

    def _record_evidence(self, auth: _Authorization, status: str, evidence: Mapping[str, Any]) -> dict:
        safe = _sanitize_value(evidence, self.max_output_bytes)
        result = {"route": auth.resolved["route_key"], "tier": auth.tier, "status": status, **safe}
        with self.store.transaction() as db:
            ensure_schema(db)
            db.execute("INSERT INTO discovery_evidence VALUES(?,?,?,?,?,?)",
                       (str(uuid.uuid4()), auth.resolved["route_key"], auth.tier, status,
                        canonical(result), time.time()))
            self._audit_db(db, auth.resolved["route_key"], "probe", status,
                           {"tier": auth.tier, "approval_id": auth.approval_id, "unlisted": auth.unlisted})
        return result

    def handshake(self, route: Mapping[str, Any], approval_id: str | None = None, *, timeout: float | None = None,
                  max_output_bytes: int | None = None, allow_unlisted: bool = False) -> dict:
        auth = self._authorize(route, "handshake", approval_id, allow_unlisted)
        limit = max_output_bytes or self.max_output_bytes
        if not isinstance(timeout or 10, (int, float)) or not .01 <= (timeout or 10) <= 3600:
            raise Invalid("handshake timeout must be between .01 and 3600 seconds")
        if not 64 <= limit <= 10 * 1024 * 1024:
            raise Invalid("handshake max_output_bytes is out of bounds")
        state_root = self.store.home / "discovery-state"
        state_root.mkdir(exist_ok=True, mode=0o700)
        state = state_root / str(uuid.uuid4())
        state.mkdir(mode=0o700)
        env = dict(auth.resolved["env"])
        env.update({"HOME": str(state), "XDG_CONFIG_HOME": str(state / "config"),
                    "XDG_CACHE_HOME": str(state / "cache"), "XDG_DATA_HOME": str(state / "data"),
                    "BOA_DISCOVERY_HANDSHAKE": "1"})
        for child in (state / "config", state / "cache", state / "data"):
            child.mkdir()
        command = [auth.resolved["executable"], *auth.resolved["route"]["args"]]
        return self._run_handshake(auth, command, env, state, timeout or 10, limit)

    probe_handshake = handshake

    def _run_handshake(self, auth, command, env, state, timeout, limit):
        stdout = _LimitedReader(limit)
        stderr = _LimitedReader(limit)
        process = None
        timed_out = False
        terminated = False
        try:
            process = subprocess.Popen(command, cwd=state, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            threads = [threading.Thread(target=reader, args=(stream, collector), daemon=True)
                       for stream, collector in ((process.stdout, stdout), (process.stderr, stderr))]
            for thread in threads:
                thread.start()
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminated = _terminate_group(process.pid)
                code = process.wait(timeout=2)
            else:
                # A successful parent can leave a descendant holding a pipe.  The
                # group is still explicitly torn down before evidence is stored.
                terminated = _terminate_group(process.pid) or terminated
            for thread in threads:
                thread.join(timeout=2)
            for stream in (process.stdout, process.stderr):
                stream.close()
            result = {"exit_code": code, "timed_out": timed_out, "process_group_terminated": terminated,
                      "stdout": sanitize_evidence(stdout.text(), limit),
                      "stderr": sanitize_evidence(stderr.text(), limit),
                      "metadata": _safe_metadata(auth.resolved["route"], limit),
                      "isolated_state": True}
            return self._record_evidence(auth, "timeout" if timed_out else "completed", result)
        except (OSError, subprocess.SubprocessError) as exc:
            if process is not None:
                _terminate_group(process.pid)
            return self._record_evidence(auth, "failed", {"error": str(exc), "metadata": _safe_metadata(auth.resolved["route"], limit)})
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def generative(self, route: Mapping[str, Any], prompt: str = "Return a bounded capability response.",
                   approval_id: str | None = None, *, requester: Callable | None = None,
                   output_tokens: int = 128, timeout: float = 30, allow_unlisted: bool = False) -> dict:
        auth = self._authorize(route, "generative", approval_id, allow_unlisted)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 16 * 1024:
            raise Invalid("generative probe prompt must be nonempty and at most 16 KiB")
        if type(output_tokens) is not int or not 1 <= output_tokens <= 4096:
            raise Invalid("generative output_tokens must be between 1 and 4096")
        if not isinstance(timeout, (int, float)) or not .01 <= timeout <= 300:
            raise Invalid("generative timeout must be between .01 and 300 seconds")
        model = auth.resolved["route"].get("model") or auth.resolved["env"].get("BOA_MODEL", "")
        if not model:
            raise Invalid("generative discovery requires an explicit model")
        try:
            if requester is None:
                from .providers import Provider
                route_value = auth.resolved["route"]
                kind = route_value.get("provider") or auth.resolved["env"].get("BOA_PROVIDER", "openai")
                base_url = route_value.get("base_url", route_value.get("url")) or auth.resolved["env"].get("BOA_BASE_URL", "")
                api_key = auth.resolved["env"].get("BOA_API_KEY", "")
                requester = Provider(kind, base_url, model, api_key=api_key).complete
            completion = requester([{"role": "user", "content": prompt}], model, output_tokens, timeout)
            text = getattr(completion, "text", completion)
            result = {"request_count": 1, "output": sanitize_evidence(text, self.max_output_bytes),
                      "metadata": _safe_metadata(auth.resolved["route"], self.max_output_bytes),
                      "cost_policy": auth.cost_policy}
            return self._record_evidence(auth, "completed", result)
        except Exception as exc:
            # A generative probe is one request, never retried by discovery.
            return self._record_evidence(auth, "failed", {"request_count": 1, "error": str(exc),
                                                            "metadata": _safe_metadata(auth.resolved["route"], self.max_output_bytes)})

    generative_probe = generative


class _LimitedReader:
    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def add(self, chunk: bytes):
        remaining = self.limit + 1 - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(self.data) > self.limit:
            self.truncated = True
            del self.data[self.limit:]

    def text(self):
        value = bytes(self.data).decode("utf-8", "replace")
        return value + ("…[truncated]" if self.truncated else "")


def reader(stream, collector: _LimitedReader):
    try:
        for chunk in iter(lambda: stream.read(8192), b""):
            collector.add(chunk)
    except (OSError, ValueError):
        return


def _terminate_group(pid: int) -> bool:
    terminated = False
    try:
        os.killpg(pid, signal.SIGTERM)
        terminated = True
    except ProcessLookupError:
        return terminated
    except OSError:
        return terminated
    deadline = time.monotonic() + .2
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return terminated
        except OSError:
            break
        time.sleep(.01)
    try:
        os.killpg(pid, signal.SIGKILL)
        terminated = True
    except (ProcessLookupError, OSError):
        pass
    return terminated


__all__ = ["Discovery", "DEFAULT_ROUTES", "passive_inventory", "sanitize_evidence", "seed_passive_inventory"]
