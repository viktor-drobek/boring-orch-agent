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

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            entered.set()
            try:
                response = scripted.get_nowait()
            except queue.Empty:
                response = (400, {"error": "Unexpected extra request"})
            if response == "wait":
                release.wait(5)
                response = (200, completion("openai", {"action": "final", "result": {"ok": True}}))
            status, obj = response
            encoded = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if status == 302:
                self.send_header("Location", "/credential-leak")
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

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


