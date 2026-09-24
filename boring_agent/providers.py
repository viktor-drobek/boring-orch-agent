"""Small HTTP adapters. No SDK retries: only the manager owns execution retries."""
from dataclasses import dataclass
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .model import Invalid, canonical, strict_json


TRANSIENT_HTTP = frozenset({429})
PERMANENT_HTTP = frozenset({400, 401, 402, 403, 404, 405, 413, 415, 422})
CODDY_TRANSIENT_HTTP = frozenset({409, 429})
CODDY_SESSION_RE = re.compile(r"^sess_[0-9a-f]{24}$")
PERMISSION_RANK = {"ask": 0, "accept_edits": 1, "bypass": 2}
MENTION_FIELDS = frozenset({
    "agent", "prompt", "description", "background", "expected_seconds",
    "timeout_seconds", "model", "reasoning", "notify_on_finish", "permission_mode",
})
SPAWN_FIELDS = tuple(
    field for field in (
        "agent", "prompt", "description", "background", "expected_seconds",
        "timeout_seconds", "model", "reasoning", "notify_on_finish",
    )
)


class ExecutionError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


@dataclass
class Completion:
    text: str
    tokens: int | None
    truncated: bool = False  # the server stopped at its output limit
    session_id: str | None = None
    stop_reason: str | None = None
    events: tuple[dict, ...] = ()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Provider:
    """Operator configuration, never accepted from a model or a submitted task."""
    def __init__(self, kind, base_url, model, api_key="", json_mode=True, *,
                 session_id=None, permission_mode=None, stream=None):
        if kind not in ("openai", "anthropic", "ollama", "coddy"):
            raise Invalid("BOA_PROVIDER must be openai, anthropic, ollama or coddy")
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise Invalid("BOA_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise Invalid("Remote providers require HTTPS; HTTP is supported for loopback only")
        if not model:
            raise Invalid("Set BOA_MODEL before starting an llm worker")
        if session_id is not None and CODDY_SESSION_RE.fullmatch(session_id) is None:
            raise Invalid("BOA_CODDY_SESSION_ID must be sess_ followed by 24 lowercase hexadecimal characters")
        if permission_mode is not None and permission_mode not in PERMISSION_RANK:
            raise Invalid("BOA_PERMISSION_MODE must be ask, accept_edits or bypass")
        normalized_base = base_url.rstrip("/")
        if kind == "coddy" and not normalized_base.endswith("/v1"):
            normalized_base += "/v1"
        self.kind, self.base_url, self.model, self.api_key = kind, normalized_base, model, api_key
        self.json_mode = bool(json_mode)
        self.session_id = session_id
        self.permission_mode = permission_mode
        self.stream = kind == "coddy" if stream is None else bool(stream)
        self.opener = urllib.request.build_opener(NoRedirect())

    @classmethod
    def from_env(cls, *, session_id=None, permission_mode=None):
        kind = os.environ.get("BOA_PROVIDER", "openai")
        defaults = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1",
                    "ollama": "http://localhost:11434", "coddy": "http://127.0.0.1:12345/v1"}
        # The agent loop needs one JSON object per turn. Ollama is already asked for JSON
        # below; this asks an OpenAI-compatible server for the same. Servers that reject the
        # standard field are accommodated with BOA_JSON_MODE=off.
        json_mode = os.environ.get("BOA_JSON_MODE", "on").strip().lower() not in ("off", "0", "false", "no")
        stream = os.environ.get("BOA_CODDY_STREAM", "on").strip().lower() not in ("off", "0", "false", "no")
        return cls(kind, os.environ.get("BOA_BASE_URL", defaults.get(kind, "")),
                   os.environ.get("BOA_MODEL", ""), os.environ.get("BOA_API_KEY", ""), json_mode,
                   session_id=session_id or os.environ.get("BOA_CODDY_SESSION_ID") or None,
                   permission_mode=permission_mode or os.environ.get("BOA_PERMISSION_MODE") or None,
                   stream=stream if kind == "coddy" else False)

    def complete(self, messages, model=None, output_tokens=2048, timeout=60, mention=None):
        model = model or self.model
        if self.kind == "coddy":
            return self._complete_coddy(messages, model, output_tokens, timeout, mention)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.kind == "anthropic":
            endpoint = "/messages"
            headers.update({"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
            body = {"model": model, "system": messages[0]["content"], "messages": messages[1:],
                    "max_tokens": output_tokens, "stream": False}
        elif self.kind == "ollama":
            endpoint = "/api/chat"
            body = {"model": model, "messages": messages, "stream": False,
                    "format": "json", "options": {"num_predict": output_tokens}}
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
        else:
            endpoint = "/chat/completions"
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
            # max_tokens remains the widely supported compatibility field. Model-specific
            # APIs that require a different field should use a compatibility gateway.
            body = {"model": model, "messages": messages, "max_tokens": output_tokens, "stream": False}
            if self.json_mode:
                body["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(self.base_url + endpoint, canonical(body).encode(), headers, method="POST")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(2_097_153)
        except urllib.error.HTTPError as exc:
            exc.close()
            # An error response is not evidence that an upstream generation stopped. In
            # particular a proxy can return 5xx while its model server is still working.
            # Only explicit admission/input rejections are classified as known outcomes;
            # ambiguous errors retain the reservation and cannot trigger automatic retry.
            kind = ("transient" if exc.code in TRANSIENT_HTTP else
                    "permanent" if exc.code in PERMANENT_HTTP else "unknown")
            # Don't persist provider error bodies: they can echo secrets or submitted data.
            raise ExecutionError(kind, f"Provider HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ExecutionError("unknown", "Provider connection interrupted; remote completion is unconfirmed") from exc
        if len(data) > 2_097_152:
            raise ExecutionError("unknown", "Provider response exceeded 2 MiB; completion is unconfirmed")
        try:
            obj = strict_json(data.decode())
            usage = obj.get("usage", {})
            if self.kind == "anthropic":
                content = "".join(x["text"] for x in obj["content"] if x.get("type") == "text")
                counts = [usage.get("input_tokens"), usage.get("output_tokens"),
                          usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0)]
                truncated = obj.get("stop_reason") == "max_tokens"
            elif self.kind == "ollama":
                if obj.get("done") is not True:
                    raise ExecutionError("unknown", "Ollama did not confirm completion")
                content = obj["message"]["content"]
                counts = [obj.get("prompt_eval_count"), obj.get("eval_count")]
                truncated = obj.get("done_reason") == "length"
            else:
                choice = obj["choices"][0]
                content = choice["message"]["content"]
                counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
                truncated = choice.get("finish_reason") == "length"
            if not isinstance(content, str):
                raise ValueError("Non-text completion")
            tokens = sum(counts) if all(type(x) is int and x >= 0 for x in counts) else None
            return Completion(content, tokens, truncated)
        except (Invalid, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise ExecutionError("permanent", "Provider returned a malformed completion; usage is unknown") from exc

    @staticmethod
    def _messages_input(messages):
        if not isinstance(messages, list) or not messages:
            raise Invalid("Coddy messages must be a nonempty list")
        contents = []
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise Invalid("Coddy messages must contain text content")
            contents.append(message["content"])
        return "\n\n".join(contents)

    @staticmethod
    def _normalize_mention(mention, parent_permission_mode):
        if not isinstance(mention, dict):
            raise Invalid("coddy.mention must be an object")
        unknown = set(mention) - MENTION_FIELDS
        if unknown:
            raise Invalid("Unknown coddy.mention fields: " + ", ".join(sorted(unknown)))
        agent, prompt = mention.get("agent"), mention.get("prompt")
        if not isinstance(agent, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", agent) is None:
            raise Invalid("coddy.mention.agent must be a valid subagent name")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 32 * 1024:
            raise Invalid("coddy.mention.prompt must contain 1–32768 UTF-8 bytes")
        for name in ("description", "model", "reasoning"):
            if name in mention and mention[name] is not None and not isinstance(mention[name], str):
                raise Invalid(f"coddy.mention.{name} must be text")
        for name in ("background", "notify_on_finish"):
            if name in mention and not isinstance(mention[name], bool):
                raise Invalid(f"coddy.mention.{name} must be boolean")
        for name in ("expected_seconds", "timeout_seconds"):
            value = mention.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
                raise Invalid(f"coddy.mention.{name} must be a positive number")
        requested = mention.get("permission_mode") or parent_permission_mode or "ask"
        parent = parent_permission_mode or "ask"
        if requested not in PERMISSION_RANK:
            raise Invalid("coddy.mention.permission_mode must be ask, accept_edits or bypass")
        effective = Provider.narrow_permission_mode(parent, requested)
        arguments = {name: mention[name] for name in SPAWN_FIELDS if name in mention}
        return agent, arguments, effective

    @staticmethod
    def narrow_permission_mode(parent, requested):
        if parent not in PERMISSION_RANK or requested not in PERMISSION_RANK:
            raise Invalid("permission mode must be ask, accept_edits or bypass")
        return min((parent, requested), key=PERMISSION_RANK.get)

    def _coddy_headers(self, accept="application/json"):
        headers = {"Content-Type": "application/json", "Accept": accept}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        if self.session_id:
            headers["X-Coddy-Session-ID"] = self.session_id
        return headers

    @staticmethod
    def _coddy_error_kind(status):
        if status in CODDY_TRANSIENT_HTTP:
            return "transient"
        if status in PERMANENT_HTTP:
            return "permanent"
        return "unknown"

    @staticmethod
    def _safe_error(status, data, prefix="Coddy"):
        details = []
        try:
            obj = strict_json(data.decode()) if data else {}
            error = obj.get("error", obj) if isinstance(obj, dict) else {}
            if isinstance(error, dict):
                for name in ("type", "upstream_status", "code"):
                    value = error.get(name)
                    if isinstance(value, (str, int)) and not isinstance(value, bool):
                        details.append(f"{name}={value}")
        except (Invalid, UnicodeError):
            pass
        suffix = " (" + ", ".join(details) + ")" if details else ""
        return f"{prefix} HTTP {status}{suffix}"

    def _open_coddy(self, method, path, body, timeout):
        encoded = canonical(body).encode()
        accept = "text/event-stream" if body.get("stream") else "application/json"
        request = urllib.request.Request(self.base_url + path, encoded,
                                         self._coddy_headers(accept), method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(2_097_153)
                response_headers = {name.lower(): value for name, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read(65_537)
            finally:
                exc.close()
            raise ExecutionError(self._coddy_error_kind(exc.code), self._safe_error(exc.code, data)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ExecutionError("unknown", "Coddy connection interrupted; remote completion is unconfirmed") from exc
        if len(data) > 2_097_152:
            raise ExecutionError("unknown", "Coddy response exceeded 2 MiB; completion is unconfirmed")
        session_id = response_headers.get("x-coddy-session-id")
        if session_id:
            if CODDY_SESSION_RE.fullmatch(session_id) is None:
                raise ExecutionError("permanent", "Coddy returned a malformed session identifier")
            if self.session_id and session_id != self.session_id:
                raise ExecutionError("permanent", "Coddy returned a different session identifier")
            self.session_id = session_id
        return data, response_headers

    def _set_permission_mode(self, permission_mode, timeout):
        if not self.session_id:
            raise Invalid("A Coddy subagent mention requires a session ID")
        body = {"permissionMode": permission_mode}
        # Session helpers are JSON even when the following turn is streamed.
        encoded = canonical(body).encode()
        request = urllib.request.Request(
            self.base_url.removesuffix("/v1") + "/coddy/sessions/" + self.session_id,
            encoded, self._coddy_headers("application/json"), method="PATCH",
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(65_537)
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read(65_537)
            finally:
                exc.close()
            raise ExecutionError(self._coddy_error_kind(exc.code), self._safe_error(exc.code, data)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ExecutionError("unknown", "Coddy session permission update was interrupted") from exc
        if len(data) > 65_536:
            raise ExecutionError("unknown", "Coddy session settings response exceeded 64 KiB")
        self.permission_mode = permission_mode

    def set_permission_mode(self, permission_mode, timeout=30):
        if permission_mode not in PERMISSION_RANK:
            raise Invalid("permission mode must be ask, accept_edits or bypass")
        self._set_permission_mode(permission_mode, timeout)

    def session_snapshot(self, timeout=30):
        """Read the current Coddy session without creating or mutating it."""
        if self.kind != "coddy" or not self.session_id:
            raise Invalid("A Coddy session snapshot requires a session ID")
        url = (self.base_url.removesuffix("/v1") + "/coddy/sessions/" +
               self.session_id + "/messages")
        request = urllib.request.Request(url, headers=self._coddy_headers("application/json"), method="GET")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(2_097_153)
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read(65_537)
            finally:
                exc.close()
            if exc.code == 404:
                return None
            raise ExecutionError(self._coddy_error_kind(exc.code), self._safe_error(exc.code, data)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ExecutionError("unknown", "Coddy session lookup was interrupted") from exc
        if len(data) > 2_097_152:
            raise ExecutionError("unknown", "Coddy session snapshot exceeded 2 MiB")
        try:
            value = strict_json(data.decode())
        except (Invalid, UnicodeError) as exc:
            raise ExecutionError("permanent", "Coddy returned a malformed session snapshot") from exc
        if not isinstance(value, dict):
            raise ExecutionError("permanent", "Coddy returned a non-object session snapshot")
        settings = value.get("settings") if isinstance(value.get("settings"), dict) else {}
        inherited = settings.get("permissionMode", value.get("permissionMode"))
        # Only an observed session mode is inherited. A requested mode (for
        # example the task's bypass) is never authority by itself, so an
        # unreported or unrecognized mode fails closed to ask.
        self.permission_mode = inherited if inherited in PERMISSION_RANK else "ask"
        return value

    def model_contexts(self, timeout=30):
        """Return context limits explicitly advertised by Coddy's model catalog."""
        if self.kind != "coddy":
            raise Invalid("Model catalog lookup is available only for the coddy provider")
        request = urllib.request.Request(
            self.base_url + "/models", headers=self._coddy_headers("application/json"), method="GET",
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(2_097_153)
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read(65_537)
            finally:
                exc.close()
            raise ExecutionError(self._coddy_error_kind(exc.code), self._safe_error(exc.code, data)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ExecutionError("unknown", "Coddy model catalog lookup was interrupted") from exc
        if len(data) > 2_097_152:
            raise ExecutionError("unknown", "Coddy model catalog exceeded 2 MiB")
        try:
            value = strict_json(data.decode())
            models = value.get("data") if isinstance(value, dict) else None
            if not isinstance(models, list):
                raise ValueError("catalog data is not a list")
            contexts = {}
            for item in models:
                if not isinstance(item, dict):
                    continue
                model_id, context = item.get("id"), item.get("max_context_tokens")
                if isinstance(model_id, str) and model_id and type(context) is int and context > 0:
                    contexts[model_id] = context
            return contexts
        except (Invalid, UnicodeError, ValueError) as exc:
            raise ExecutionError("permanent", "Coddy returned a malformed model catalog") from exc

    @staticmethod
    def prepared_warmup_evidence(snapshot):
        """Extract only ordered, explicitly successful warm-up command records."""
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("messages"), list):
            return ()
        successful = []
        pending = None
        for message in snapshot["messages"]:
            if not isinstance(message, dict):
                pending = None
                continue
            role = message.get("role")
            command = message.get("command")
            content = message.get("content")
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            statuses = (message.get("status"), metadata.get("status"))
            failed = (any(status in ("failed", "failure", "cancelled", "canceled", "error")
                          for status in statuses) or
                      message.get("success") is False)
            succeeded = (not failed and
                         (any(status in ("succeeded", "completed", "success") for status in statuses) or
                          message.get("success") is True))
            if (command in ("/compact", "/rpa-init") and succeeded and
                    ("role" not in message or role == "user")):
                successful.append(command)
                pending = None
                continue
            user_command = command if command in ("/compact", "/rpa-init") else content
            if role == "user" and user_command in ("/compact", "/rpa-init") and not failed:
                pending = user_command
                continue
            if (pending and role == "assistant" and succeeded and "command" not in message and
                    content not in ("/compact", "/rpa-init")):
                successful.append(pending)
                pending = None
                continue
            pending = None
        return tuple(successful)

    def _complete_coddy(self, messages, model, output_tokens, timeout, mention):
        input_text = self._messages_input(messages)
        request_model = model
        metadata = None
        if mention is not None:
            if self.permission_mode is None:
                self.session_snapshot(timeout)
            agent, arguments, permission_mode = self._normalize_mention(mention, self.permission_mode)
            self._set_permission_mode(permission_mode, timeout)
            input_text = (
                f"@agent:{agent}\n"
                "Delegate the requested work exactly once with spawn_agent using these arguments:\n"
                + canonical(arguments)
                + f"\nEffective inherited permission_mode: {permission_mode}\n"
                + "Additional parent-session context:\n" + input_text
            )
            request_model = "agent"
            metadata = {"model": model}
        body = {"model": request_model, "input": input_text, "stream": self.stream}
        # Coddy's direct-model profile honors this Responses API field. Agent,
        # plan and ask profiles currently own their generation limits internally.
        if request_model not in {"agent", "plan", "ask"}:
            body["max_output_tokens"] = output_tokens
        if metadata is not None:
            body["metadata"] = metadata
        data, headers = self._open_coddy("POST", "/responses", body, timeout)
        if self.stream:
            return self._parse_coddy_stream(data, headers)
        return self._parse_coddy_json(data, headers)

    def command(self, command, model=None, timeout=60):
        """Run one Coddy command in this provider's live session.

        Commands use the agent profile so Coddy resolves built-ins and skills;
        ``metadata.model`` pins the backend selected for the command turn.
        """
        if self.kind != "coddy":
            raise Invalid("Session commands are available only for the coddy provider")
        if not isinstance(command, str) or not command.strip():
            raise Invalid("Coddy command must be nonempty text")
        selected = model or self.model
        body = {"model": "agent", "input": command, "stream": self.stream,
                "metadata": {"model": selected}}
        data, headers = self._open_coddy("POST", "/responses", body, timeout)
        return self._parse_coddy_stream(data, headers) if self.stream else self._parse_coddy_json(data, headers)

    @staticmethod
    def _usage_tokens(usage):
        if not isinstance(usage, dict):
            return None
        total = usage.get("total_tokens")
        if type(total) is int and total >= 0:
            return total
        inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
        outputs = usage.get("output_tokens", usage.get("completion_tokens"))
        return inputs + outputs if type(inputs) is int and inputs >= 0 and type(outputs) is int and outputs >= 0 else None

    def _parse_coddy_json(self, data, headers):
        try:
            obj = strict_json(data.decode())
            if not isinstance(obj, dict):
                raise ValueError("response is not an object")
            if "error" in obj:
                error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
                status = error.get("upstream_status", 500)
                status = status if type(status) is int else 500
                raise ExecutionError(self._coddy_error_kind(status), self._safe_error(status, data, "Coddy stream"))
            finish_reason = None
            if isinstance(obj.get("output_text"), str):
                content = obj["output_text"]
            elif isinstance(obj.get("choices"), list) and obj["choices"]:
                choice = obj["choices"][0]
                content = choice["message"]["content"]
                finish_reason = choice.get("finish_reason")
            else:
                parts = []
                for output in obj.get("output", []):
                    for part in output.get("content", []):
                        text = part.get("text") or part.get("output_text")
                        if isinstance(text, str):
                            parts.append(text)
                content = "".join(parts)
            if not isinstance(content, str):
                raise ValueError("non-text completion")
            metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            session_id = headers.get("x-coddy-session-id") or metadata.get("session_id") or self.session_id
            stop_reason = metadata.get("stop_reason")
            truncated = finish_reason == "length" or stop_reason in {"max_tokens", "max_turns"}
            return Completion(content, self._usage_tokens(obj.get("usage")), truncated,
                              session_id, stop_reason, ())
        except ExecutionError:
            raise
        except (Invalid, UnicodeError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise ExecutionError("permanent", "Coddy returned a malformed completion; usage is unknown") from exc

    def _parse_coddy_stream(self, data, headers):
        try:
            text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            raise ExecutionError("unknown", "Coddy stream was not valid UTF-8; completion is unconfirmed") from exc
        content, events = [], []
        usage = None
        finish_reason = stop_reason = response_session = None
        done = activity = False
        for block in text.split("\n\n"):
            event_name, data_lines = None, []
            for line in block.splitlines():
                if not line or line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line.partition(":")[2].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.partition(":")[2].lstrip())
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            if payload == "[DONE]":
                done = True
                continue
            try:
                obj = strict_json(payload)
            except Invalid as exc:
                raise ExecutionError("unknown", "Coddy returned malformed SSE data; completion is unconfirmed") from exc
            if not isinstance(obj, dict):
                raise ExecutionError("unknown", "Coddy returned a non-object SSE event; completion is unconfirmed")
            if event_name == "error" or "error" in obj:
                error = obj.get("error", obj)
                status = error.get("upstream_status", 500) if isinstance(error, dict) else 500
                status = status if type(status) is int else 500
                kind = "unknown" if content or activity else self._coddy_error_kind(status)
                raise ExecutionError(kind, self._safe_error(status, payload.encode(), "Coddy stream"))
            if event_name:
                events.append({"event": event_name, "data": obj})
                if event_name == "token_usage":
                    usage = obj
                elif event_name == "coddy_meta":
                    stop_reason = obj.get("stop_reason")
                    response_session = obj.get("session_id")
                else:
                    # Any other named event may represent a tool, permission, or
                    # command side effect. A later error therefore cannot be replayed.
                    activity = True
                continue
            choices = obj.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0]
                delta = choice.get("delta", {})
                piece = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(piece, str):
                    content.append(piece)
                if isinstance(delta, dict) and (delta.get("tool_calls") or delta.get("function_call")):
                    activity = True
                if choice.get("finish_reason") is not None:
                    finish_reason = choice.get("finish_reason")
        if not done:
            raise ExecutionError("unknown", "Coddy stream ended before data: [DONE]; completion is unconfirmed")
        valid_finish_reason = isinstance(finish_reason, str) and bool(finish_reason.strip())
        valid_stop_reason = isinstance(stop_reason, str) and bool(stop_reason.strip())
        if not valid_finish_reason and not valid_stop_reason:
            raise ExecutionError("unknown", "Coddy stream ended without a terminal reason; completion is unconfirmed")
        session_id = headers.get("x-coddy-session-id") or response_session or self.session_id
        if response_session and self.session_id and response_session != self.session_id:
            raise ExecutionError("permanent", "Coddy stream metadata named a different session")
        truncated = finish_reason == "length" or stop_reason in {"max_tokens", "max_turns"}
        return Completion("".join(content), self._usage_tokens(usage), truncated,
                          session_id, stop_reason, tuple(events))
