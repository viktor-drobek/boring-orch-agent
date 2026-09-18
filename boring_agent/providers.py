"""Small HTTP adapters. No SDK retries: only the manager owns execution retries."""
from dataclasses import dataclass
import os
import urllib.error
import urllib.parse
import urllib.request

from .model import Invalid, canonical, strict_json


TRANSIENT_HTTP = frozenset({429})
PERMANENT_HTTP = frozenset({400, 401, 402, 403, 404, 405, 413, 415, 422})


class ExecutionError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


@dataclass
class Completion:
    text: str
    tokens: int | None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Provider:
    """Operator configuration, never accepted from a model or a submitted task."""
    def __init__(self, kind, base_url, model, api_key="", json_mode=True):
        if kind not in ("openai", "anthropic", "ollama"):
            raise Invalid("BOA_PROVIDER must be openai, anthropic or ollama")
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise Invalid("BOA_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise Invalid("Remote providers require HTTPS; HTTP is supported for loopback only")
        if not model:
            raise Invalid("Set BOA_MODEL before starting an llm worker")
        self.kind, self.base_url, self.model, self.api_key = kind, base_url.rstrip("/"), model, api_key
        self.json_mode = bool(json_mode)

    @classmethod
    def from_env(cls):
        kind = os.environ.get("BOA_PROVIDER", "openai")
        defaults = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1",
                    "ollama": "http://localhost:11434"}
        # The agent loop needs one JSON object per turn. Ollama is already asked for JSON
        # below; this asks an OpenAI-compatible server for the same. Servers that reject the
        # standard field are accommodated with BOA_JSON_MODE=off.
        json_mode = os.environ.get("BOA_JSON_MODE", "on").strip().lower() not in ("off", "0", "false", "no")
        return cls(kind, os.environ.get("BOA_BASE_URL", defaults.get(kind, "")),
                   os.environ.get("BOA_MODEL", ""), os.environ.get("BOA_API_KEY", ""), json_mode)

    def complete(self, messages, model=None, output_tokens=2048, timeout=60):
        model = model or self.model
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
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
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
            elif self.kind == "ollama":
                if obj.get("done") is not True:
                    raise ExecutionError("unknown", "Ollama did not confirm completion")
                content = obj["message"]["content"]
                counts = [obj.get("prompt_eval_count"), obj.get("eval_count")]
            else:
                content = obj["choices"][0]["message"]["content"]
                counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
            if not isinstance(content, str):
                raise ValueError("Non-text completion")
            tokens = sum(counts) if all(type(x) is int and x >= 0 for x in counts) else None
            return Completion(content, tokens)
        except (Invalid, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise ExecutionError("permanent", "Provider returned a malformed completion; usage is unknown") from exc
