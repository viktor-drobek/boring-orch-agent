"""HTTP API v1 for durable task intake and observation.

Workers and managers remain local processes.  This server exposes the same
durable command boundary to people and other agents without creating a second
execution path.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit

from .artifacts import read_result
from .model import AgentError, Conflict, Invalid, NotFound, StorageError, strict_json
from .store import Store
from . import __version__


MAX_BODY_BYTES = 256_000


def _loopback(host: str) -> bool:
    return host.lower() == "localhost" or host in {"127.0.0.1", "::1"}


def _body(handler: BaseHTTPRequestHandler):
    value = handler.headers.get("Content-Length")
    try:
        length = int(value) if value is not None else -1
    except ValueError as exc:
        raise Invalid("Content-Length must be an integer") from exc
    if not 0 <= length <= MAX_BODY_BYTES:
        raise Invalid(f"Request body must contain 0–{MAX_BODY_BYTES} bytes")
    try:
        value = strict_json(handler.rfile.read(length).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise Invalid("Request body must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise Invalid("Request body must be a JSON object")
    return value


def handler_type(store: Store, auth_token: str):
    """Create a handler bound to one initialized store and optional bearer token."""
    class Handler(BaseHTTPRequestHandler):
        server_version = "boring-orch-agent/" + __version__

        def log_message(self, format, *args):
            # The CLI owns logging. Avoid recording request paths or bearer headers here.
            return

        def _reply(self, status: int, value, extra_headers=None):
            data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, header_value in (extra_headers or {}).items():
                self.send_header(name, header_value)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, error: AgentError):
            status = 404 if isinstance(error, NotFound) else 409 if isinstance(error, Conflict) else 503 if isinstance(error, StorageError) else 400
            self._reply(status, {"error": error.code, "message": str(error)})

        def _authorized(self) -> bool:
            if not auth_token:
                return True
            supplied = self.headers.get("Authorization", "")
            expected = "Bearer " + auth_token
            if hmac.compare_digest(supplied, expected):
                return True
            self._reply(401, {"error": "unauthorized", "message": "Bearer authentication is required"},
                        {"WWW-Authenticate": 'Bearer realm="boring-orch-agent"'})
            return False

        def _segments(self):
            path = urlsplit(self.path).path
            return [part for part in path.split("/") if part]

        def do_GET(self):
            if not self._authorized():
                return
            try:
                parts = self._segments()
                if parts == ["api", "v1", "health"]:
                    return self._reply(200, {"status": "ok", "version": __version__})
                if parts == ["api", "v1", "capacity"]:
                    return self._reply(200, store.capacity())
                if parts == ["api", "v1", "tasks"]:
                    return self._reply(200, {"tasks": store.tasks()})
                if len(parts) == 4 and parts[:3] == ["api", "v1", "tasks"]:
                    return self._reply(200, store.task(parts[3]))
                if len(parts) == 5 and parts[:3] == ["api", "v1", "tasks"] and parts[4] == "events":
                    return self._reply(200, {"events": store.events(parts[3])})
                if len(parts) == 5 and parts[:3] == ["api", "v1", "tasks"] and parts[4] == "result":
                    task = store.task(parts[3])
                    if task["status"] != "Succeeded":
                        raise Invalid(f"Task has no accepted result (status={task['status']})")
                    return self._reply(200, read_result(store, task["attempts"][-1], task["spec"]))
                self._reply(404, {"error": "not_found", "message": "Unknown API route"})
            except AgentError as exc:
                self._error(exc)

        def do_POST(self):
            if not self._authorized():
                return
            try:
                parts = self._segments()
                if parts == ["api", "v1", "tasks"]:
                    key = self.headers.get("Idempotency-Key", "")
                    return self._reply(202, store.submit(_body(self), key))
                if len(parts) == 5 and parts[:3] == ["api", "v1", "tasks"] and parts[4] == "cancel":
                    key = self.headers.get("Idempotency-Key", "")
                    return self._reply(202, store.cancel(parts[3], key))
                if len(parts) == 5 and parts[:3] == ["api", "v1", "attempts"] and parts[4] == "resolve":
                    payload = _body(self)
                    if set(payload) != {"note", "confirm_stopped"}:
                        raise Invalid("Resolution body must contain note and confirm_stopped")
                    return self._reply(200, store.resolve(parts[3], payload["note"], payload["confirm_stopped"]))
                self._reply(404, {"error": "not_found", "message": "Unknown API route"})
            except AgentError as exc:
                self._error(exc)

        def do_PUT(self):
            self._reply(405, {"error": "method_not_allowed", "message": "Use GET or POST"}, {"Allow": "GET, POST"})

        do_DELETE = do_PUT
        do_PATCH = do_PUT

    return Handler


def make_server(store: Store, host="127.0.0.1", port=8088, auth_token=""):
    """Return a server. Non-loopback listeners always require a bearer token."""
    store.settings()  # fail before binding when the store was never initialized
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise Invalid("port must be an integer from 0 to 65535")
    if not _loopback(host) and not auth_token:
        raise Invalid("A non-loopback API listener requires --auth-token or BOA_API_TOKEN")
    return ThreadingHTTPServer((host, port), handler_type(store, auth_token))
