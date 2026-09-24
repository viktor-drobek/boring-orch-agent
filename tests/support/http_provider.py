from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import threading


def completion(kind, action, usage=True, truncated=False):
    """A provider reply carrying `action`; `action=None` with truncated=True models a reply
    whose whole output budget went to reasoning, so no content arrived."""
    content = "" if action is None else json.dumps(action)
    if kind == "anthropic":
        return {"content": [{"type": "text", "text": content}],
                "stop_reason": "max_tokens" if truncated else "end_turn",
                **({"usage": {"input_tokens": 20, "output_tokens": 10}} if usage else {})}
    if kind == "ollama":
        return {"message": {"content": content}, "done": True,
                "done_reason": "length" if truncated else "stop",
                **({"prompt_eval_count": 20, "eval_count": 10} if usage else {})}
    return {"choices": [{"message": {"content": content}, "finish_reason": "length" if truncated else "stop"}],
            **({"usage": {"prompt_tokens": 20, "completion_tokens": 10}} if usage else {})}


def coddy_stream(content, *, usage=True, finish_reason="stop", stop_reason="end_turn",
                 session_id="sess_0123456789abcdef01234567"):
    """Coddy's /v1/responses dialect: chat deltas plus named SSE events."""
    frames = [
        'data: ' + json.dumps({
            "id": "resp_fixture",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }),
        'data: ' + json.dumps({
            "id": "resp_fixture",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }),
    ]
    if usage:
        frames.append("event: token_usage\n" +
                      'data: ' + json.dumps({"prompt_tokens": 20, "completion_tokens": 10,
                                             "total_tokens": 30}))
    frames.extend([
        'data: ' + json.dumps({
            "id": "resp_fixture",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }),
        "event: coddy_meta\n" +
        'data: ' + json.dumps({"session_id": session_id, "stop_reason": stop_reason}),
        "data: [DONE]",
        "",
    ])
    return (200, "text/event-stream", "\n\n".join(frames), {"X-Coddy-Session-ID": session_id})


def json_response(status, body, headers=None):
    return status, "application/json", json.dumps(body), headers or {}


@contextmanager
def server(responses):
    scripted = queue.Queue()
    for response in responses:
        scripted.put(response)
    requests = []
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw) if raw else None
            requests.append({"method": self.command, "path": self.path,
                             "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            entered.set()
            try:
                response = scripted.get_nowait()
            except queue.Empty:
                response = (400, {"error": "Unexpected extra request"})
            if response == "wait":
                release.wait(5)
                response = (200, completion("openai", {"action": "final", "result": {"ok": True}}))
            if len(response) == 2:
                status, obj = response
                content_type, encoded, extra_headers = "application/json", json.dumps(obj).encode(), {}
            else:
                status, content_type, payload, extra_headers = response
                encoded = payload.encode() if isinstance(payload, str) else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            if status == 302:
                self.send_header("Location", "/credential-leak")
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_POST = handle_request
        do_PATCH = handle_request
        do_GET = handle_request

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests, entered, release
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)


