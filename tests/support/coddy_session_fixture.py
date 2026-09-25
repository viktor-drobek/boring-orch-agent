"""Loopback stand-in for the Coddy 1.2 session API used by the native job driver.

It serves the calls the driver makes: the bootstrap and streamed job turns on
POST /v1/responses, PATCH of session settings, permission answers, the
/coddy/events stream, background tasks and the transcript.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSION = "sess_0123456789abcdef01234567"


def permission_prompt(command, call_id="call-1", kind="run_command"):
    """The shape Coddy sends in event: permission and in subagent_permission requests."""
    return {"sessionId": "sess_child", "toolCall": {
        "toolCallId": call_id, "title": "[subagent general] Run: run_command", "kind": kind, "status": "pending",
        "content": [{"type": "content", "content": {
            "type": "text", "text": "Arguments: " + json.dumps({"command": command})}}]},
        "options": [{"optionId": "allow"}, {"optionId": "reject"}]}


class CoddySessionFixture:
    """Scriptable fake: set ``prompts``, ``send_done``, ``task_status`` and ``final`` before a run."""

    def __init__(self):
        self.prompts = []
        self.send_done = True
        self.task_status = "completed"
        self.final = '{"status": "SUCCESS"}'
        self.patch = None
        self.answers = []
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, body=None, headers=None, content_type="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def body(self):
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

            def known(self):
                return SESSION in self.path

            def do_POST(self):
                body = self.body()
                fixture.requests.append((self.path, body))
                if self.path == "/v1/responses" and not body.get("stream"):
                    return self.send(200, {"id": SESSION, "output": []}, {"X-Coddy-Session-ID": SESSION})
                if self.path == "/v1/responses":
                    self.send(200, content_type="text/event-stream")
                    frames = [("permission", p) for p in fixture.prompts]
                    frames += [(None, {"choices": [{"delta": {"content": fixture.final}}]}),
                               ("coddy_meta", {"metadata": {"stop_reason": "end_turn"}})]
                    for event, data in frames:
                        if event:
                            self.wfile.write(f"event: {event}\n".encode())
                        self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(0.05)
                    if fixture.send_done:
                        self.wfile.write(b"data: [DONE]\n\n")
                    return None
                if self.path.endswith("/permission") and self.known():
                    fixture.answers.append(body["optionId"])
                    return self.send(204)
                return self.send(404, {"error": {"message": "session not found"}})

            def do_PATCH(self):
                if not self.known():
                    return self.send(404, {"error": {"message": "session not found"}})
                fixture.patch = self.body()
                return self.send(200, {"settings": {"permissionMode": fixture.patch["permissionMode"],
                                                    "model": fixture.patch["selectedModelId"]}})

            def do_GET(self):
                if self.path == "/coddy/events":
                    self.send(200, content_type="text/event-stream")
                    self.wfile.write(b"event: ready\ndata: {}\n\n")
                    self.wfile.flush()
                    time.sleep(5)
                    return None
                if not self.known():
                    return self.send(404, {"error": {"message": "session not found"}})
                if self.path.endswith("/background-tasks"):
                    return self.send(200, {"data": [{
                        "id": "bg_1", "status": fixture.task_status, "label": "agent general: job",
                        "last_output_at": "2026-01-01T00:00:00.000000000+00:00", "elapsed_seconds": 1,
                        "agent": {"model": "provider/model", "session_id": "sess_child"}}]})
                if self.path.endswith("/messages"):
                    return self.send(200, {"messages": [{"role": "assistant", "content": fixture.final}]})
                return self.send(404, {"error": {"message": "not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
