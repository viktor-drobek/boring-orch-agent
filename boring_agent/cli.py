"""JSON CLI: accepted receipts are different from terminal task outcomes."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

from . import PRODUCT_NAME
from .artifacts import read_result
from .api import make_server
from .manager import Manager
from .model import AgentError, Invalid, TERMINAL, strict_json
from .process import lock, supported
from .providers import Provider
from .store import Store
from .worker import Worker


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), flush=True)


def parser():
    p = argparse.ArgumentParser(prog=PRODUCT_NAME, description="Durable local agent orchestrator")
    p.add_argument("--home", default=os.environ.get("BOA_HOME", ".boa"), help="SQLite/artifact directory (default: .boa)")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize a store without overwriting an existing one")
    init.add_argument("--workspace", default=".")
    init.add_argument("--max-active", type=int, default=2)
    init.add_argument("--allow-workspace-write", action="store_true")
    submit = sub.add_parser("submit", help="Durably accept an immutable task document")
    submit.add_argument("file", help="JSON task file, or - for stdin")
    submit.add_argument("--key", required=True, help="Stable caller-provided idempotency key")
    cancel = sub.add_parser("cancel", help="Request cancellation; does not claim execution already stopped")
    cancel.add_argument("task_id")
    cancel.add_argument("--key", required=True)
    status = sub.add_parser("status")
    status.add_argument("task_id", nargs="?")
    for name in ("result", "events"):
        sub.add_parser(name).add_argument("task_id")
    sub.add_parser("capacity")
    resolve = sub.add_parser("resolve", help="Record an operator's independently verified cessation of an Unknown attempt")
    resolve.add_argument("attempt_id")
    resolve.add_argument("--confirm-stopped", action="store_true")
    resolve.add_argument("--note", required=True)
    manager = sub.add_parser("manager", help="Run the single local manager; Ctrl-C exits without killing attempts")
    manager.add_argument("--once", action="store_true")
    manager.add_argument("--poll", type=float, default=.2)
    worker = sub.add_parser("worker", help="Run a local worker; active attempt supervisors survive its exit")
    worker.add_argument("--id", default="local")
    worker.add_argument("--runtime", choices=("demo", "llm"), action="append", default=None)
    worker.add_argument("--slots", type=int, default=1)
    worker.add_argument("--allow-workspace-write", action="store_true")
    worker.add_argument("--poll", type=float, default=.2)
    serve = sub.add_parser("serve", help="Run the authenticated HTTP task API; manager and workers stay separate")
    serve.add_argument("--host", default=os.environ.get("BOA_API_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("BOA_API_PORT", "8088")))
    serve.add_argument("--auth-token", default=os.environ.get("BOA_API_TOKEN", ""),
                       help="Bearer token; required for non-loopback listeners")
    wait = sub.add_parser("wait", help="Wait for a terminal result (exit 0 success, 1 failure, 3 timeout/unknown)")
    wait.add_argument("task_id")
    wait.add_argument("--timeout", type=float, default=60)
    demo = sub.add_parser("demo", help="Run an offline task with one deliberate, safely replayed failure")
    demo.add_argument("--timeout", type=float, default=20)
    return p


def loop(tick, poll, once=False):
    if not .02 <= poll <= 1:
        raise Invalid("poll must be between .02 and 1 seconds")
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        while not stopped:
            try:
                tick()
            except (AgentError, OSError, UnicodeError) as exc:
                if once:
                    raise
                # A long-lived loop survives one failed tick (for example a locked or
                # briefly unavailable store) and retries on the next poll. Log only the
                # error class and code: messages can carry host paths or payload text.
                print(json.dumps({"event": "loop.tick_failed", "error": type(exc).__name__,
                                  "code": getattr(exc, "code", "io_error")}), file=sys.stderr, flush=True)
            if once:
                break
            time.sleep(poll)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def require_platform(command):
    reason = supported()
    if reason is not None:
        raise Invalid(f"{PRODUCT_NAME} runs on Linux only (needs /proc process identity and flock); "
                      f"cannot start {command}: {reason}")


def execute(args):
    store = Store(args.home)
    if args.command in ("manager", "worker", "demo", "serve"):
        require_platform(args.command)
    if args.command == "init":
        store.initialize(args.workspace, args.max_active, args.allow_workspace_write)
        emit({"home": str(store.home), "settings": store.settings()})
    elif args.command == "submit":
        if args.file == "-":
            raw = sys.stdin.read(256001)
        else:
            with open(args.file, encoding="utf-8") as file:
                raw = file.read(256001)
        if len(raw.encode()) > 256000:
            raise Invalid("Task document exceeds 256000 bytes")
        emit(store.submit(strict_json(raw), args.key))
    elif args.command == "cancel":
        emit(store.cancel(args.task_id, args.key))
    elif args.command == "status":
        emit(store.task(args.task_id) if args.task_id else store.tasks())
    elif args.command == "capacity":
        emit(store.capacity())
    elif args.command == "events":
        emit(store.events(args.task_id))
    elif args.command == "result":
        task = store.task(args.task_id)
        if task["status"] != "Succeeded":
            raise Invalid(f"Task has no accepted result (status={task['status']})")
        emit(read_result(store, task["attempts"][-1], task["spec"]))
    elif args.command == "resolve":
        emit(store.resolve(args.attempt_id, args.note, args.confirm_stopped))
    elif args.command == "manager":
        store.settings()
        with lock(store.home, "manager"):
            loop(Manager(store).tick, args.poll, args.once)
    elif args.command == "worker":
        store.settings()
        runtimes = args.runtime or ["llm"]
        if "llm" in runtimes:
            Provider.from_env()  # Fail configuration errors before advertising capacity.
        with lock(store.home, "worker:" + args.id):
            worker = Worker(store, args.id, runtimes, args.slots, args.allow_workspace_write)
            loop(worker.tick, args.poll)
    elif args.command == "serve":
        server = make_server(store, args.host, args.port, args.auth_token)
        try:
            server.serve_forever(poll_interval=.2)
        finally:
            server.server_close()
    elif args.command == "wait":
        if not 0 <= args.timeout <= 86400:
            raise Invalid("timeout must be between 0 and 86400 seconds")
        end = time.monotonic() + args.timeout
        while True:
            task = store.task(args.task_id)
            if task["status"] in TERMINAL or task["observation_condition"] == "Unknown" or time.monotonic() >= end:
                emit(task)
                return 0 if task["status"] == "Succeeded" else 1 if task["status"] in TERMINAL else 3
            time.sleep(.1)
    elif args.command == "demo":
        if not .1 <= args.timeout <= 86400:
            raise Invalid("demo timeout must be between .1 and 86400 seconds")
        if not store.path.exists():
            store.initialize(Path.cwd())
        worker_id = "demo-" + str(uuid.uuid4())
        with lock(store.home, "manager"), lock(store.home, "worker:" + worker_id):
            task_id = store.submit({"objective": "Demonstrate durable execution and safe retry", "runtime": "demo",
                                    "demo": {"fail_attempts": 1},
                                    "retry": {"max_attempts": 2, "replay_safe": True, "backoff_seconds": .05}},
                                   "demo-" + str(uuid.uuid4()))["task_id"]
            manager, worker = Manager(store), Worker(store, worker_id, ["demo"])
            end = time.monotonic() + args.timeout
            while time.monotonic() < end:
                worker.tick()
                manager.tick()
                task = store.task(task_id)
                if task["status"] in TERMINAL:
                    emit({"task_id": task_id, "status": task["status"], "attempts": len(task["attempts"]),
                          "result": read_result(store, task["attempts"][-1], task["spec"]) if task["status"] == "Succeeded" else None})
                    return 0 if task["status"] == "Succeeded" else 1
                time.sleep(.05)
        emit({"task_id": task_id, "status": "still_pending", "message": "Run manager and worker to continue"})
        return 3
    return 0


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except (AgentError, OSError, UnicodeError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "io_error"), "message": str(exc)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
