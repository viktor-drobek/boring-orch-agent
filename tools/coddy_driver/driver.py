#!/usr/bin/env python3
"""Drive one boring-agent native job through the Coddy HTTP API with SSE.

The driver is the operator side only: it starts the parent session and
supervises it. The job itself runs inside Coddy, where the parent agent
delegates to a child through spawn_agent.

  launch  create a fresh session (one bootstrap turn), pin permission mode, mode
          and model, then stream the job turn from POST /v1/responses
  attach  supervise a session whose job turn already ran
  status  print the last recorded state of a run

While supervising, the driver
- answers permission prompts from the parent stream, woken turns and detached
  children with policy.Policy (fail closed);
- follows woken turns (notify_on_finish) on the session composer stream;
- measures visible progress from child output for boring-agent's
  ParentIdleWatchdog and hands NEEDS_MODEL_DECISION to the parent (no retry,
  no model switch);
- keeps supervising while the parent turn or any background task is active,
  bounded by the job budget, and records a lost server as an unknown outcome;
- writes state.json after every poll and on SIGTERM.

Run it detached with tools/coddy_driver/run_job.sh so it outlives the shell that started it.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script (run_job.sh): make the repository root importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boring_agent.model import validate_native_job  # noqa: E402
from boring_agent.parent_watchdog import ParentIdleWatchdog  # noqa: E402
from tools.coddy_driver.policy import Policy  # noqa: E402

ACTIVE = {"running", "pending", "queued", "waiting", "blocked"}
QUIET_SECONDS = 90
POLL_SECONDS = 15
SERVER_LOST_AFTER = 3  # consecutive failed polls
SERVER_LOST_GIVE_UP = 600


class Driver:
    def __init__(self, args):
        self.args = args
        job_path = Path(args.job).resolve()
        raw = json.loads(job_path.read_text(encoding="utf-8"))
        self.job = validate_native_job(raw)
        self.job_path = job_path
        self.model = self.job["model"]
        self.workspace = Path(self.job["workspace"]).resolve()
        self.python = args.python or raw.get("metadata", {}).get("python") or sys.executable
        self.permission_mode = args.permission_mode or raw.get("metadata", {}).get("permission_mode", "ask")
        self.policy = Policy(self.workspace, self.python)
        self.out = Path(args.out or job_path.parent / self.job["id"]).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.base = args.base.rstrip("/")
        self.token = Path(args.token_file).read_text().strip()
        self.run_id = args.run_id or f"{self.job['id']}-{datetime.now():%Y%m%dT%H%M%S}"
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.session_id = None
        self.watchdog = ParentIdleWatchdog(time.monotonic)
        self.started = time.monotonic()
        budget = self.job["budget"]["deadline_seconds"]
        self.deadline = self.started + budget
        # Supervise past the job deadline only to observe children finishing; never beyond this cap.
        self.hard_cap = self.started + budget + 3600 + 600
        self.own_turn = threading.Event()
        self.composer_busy = threading.Lock()
        self.running_turns = 0
        self.last_activity = time.monotonic()
        self.answered: set[tuple[str, str]] = set()
        self.decisions = {"allow": 0, "reject": 0}
        self.tasks: dict[str, dict] = {}
        self.seen_output: dict[str, str] = {}
        self.failed_polls = 0
        self.server_lost = False
        self.phase = "starting"
        self.outcome = None
        self.incomplete_turns = 0

    # ---------- files ----------
    def path(self, name):
        return self.out / name

    def log(self, kind, **data):
        row = {"t": round(time.time(), 3), "kind": kind, **data}
        line = json.dumps(row, ensure_ascii=False)
        with self.lock, self.path("events.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if kind not in {"text", "reasoning", "event", "raw"}:
            print(line[:500], flush=True)

    def write_state(self):
        state = {
            "job": self.job["id"], "job_path": str(self.job_path), "run_id": self.run_id, "model": self.model,
            "workspace": str(self.workspace), "session": self.session_id, "phase": self.phase,
            "outcome": self.outcome, "server_lost": self.server_lost, "decisions": self.decisions,
            "tasks": self.tasks, "watchdog_idle_s": round(time.monotonic() - self.watchdog._last_visible_progress)
            if self.watchdog._run else None,
            "deadline_remaining_s": round(self.deadline - time.monotonic()),
            "changed_files": self.changed_files(), "pid": os.getpid(),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        tmp = self.path("state.json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path("state.json"))

    def changed_files(self):
        result = subprocess.run(["git", "-C", str(self.workspace), "status", "--short"],
                                capture_output=True, text=True)
        return [line[3:] for line in result.stdout.splitlines() if line.strip()]

    # ---------- http ----------
    def request(self, method, path, body=None, accept="application/json", session=None, timeout=60):
        headers = {"Authorization": "Bearer " + self.token, "Accept": accept}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if session:
            headers["X-Coddy-Session-ID"] = session
        data = json.dumps(body).encode() if body is not None else None
        return urllib.request.urlopen(urllib.request.Request(self.base + path, data, headers, method=method),
                                      timeout=timeout)

    def get_json(self, path):
        with self.request("GET", path) as resp:
            return json.load(resp)

    @staticmethod
    def sse(response):
        event, lines = None, []
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if lines:
                    yield event, "\n".join(lines)
                event, lines = None, []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                lines.append(line.partition(":")[2].lstrip())
        if lines:
            yield event, "\n".join(lines)

    # ---------- prompts ----------
    def answer_permission(self, session, payload: dict):
        request = payload.get("request") or payload
        call = request.get("toolCall") or {}
        tool_call_id = call.get("toolCallId") or payload.get("toolCallId")
        if not tool_call_id or (session, tool_call_id) in self.answered:
            return
        self.answered.add((session, tool_call_id))
        option, why = self.policy.decide(request)
        self.decisions[option] = self.decisions.get(option, 0) + 1
        command = self.policy.tool_arguments(call).get("command")
        self.log("decision", session=session, toolCallId=tool_call_id, option=option, why=why,
                 tool=call.get("kind") or call.get("title"), command=(command or "")[:400])
        try:
            self.request("POST", f"/coddy/sessions/{session}/permission",
                         {"toolCallId": tool_call_id, "optionId": option}).close()
        except urllib.error.HTTPError as exc:
            self.log("error", where="permission", status=exc.code, body=exc.read(2000).decode("utf-8", "replace"))

    def answer_question(self, session, payload: dict):
        answer = ["Operator unavailable during this run: follow the job document; if it does not decide this, "
                  "return BLOCKED with the exact question."]
        self.log("decision", session=session, question=json.dumps(payload, ensure_ascii=False)[:800],
                 answer=answer[0])
        try:
            self.request("POST", f"/coddy/sessions/{session}/question", {
                "requestId": payload.get("requestId"),
                "answers": [answer for _ in (payload.get("questions") or [{}])],
            }).close()
        except urllib.error.HTTPError as exc:
            self.log("error", where="question", status=exc.code, body=exc.read(2000).decode("utf-8", "replace"))

    # ---------- streams ----------
    def handle(self, source, event, payload_text):
        if payload_text == "[DONE]":
            self.log("turn", source=source, phase="done")
            return "done"
        try:
            obj = json.loads(payload_text)
        except ValueError:
            self.log("raw", source=source, event=event, data=payload_text[:2000])
            return None
        self.last_activity = time.monotonic()
        if event == "permission":
            self.answer_permission(self.session_id, obj)
        elif event == "question":
            self.answer_question(self.session_id, obj)
        elif event == "error" or (event is None and "error" in obj):
            self.log("error", source=source, data=obj)
        elif event == "coddy_meta":
            self.log("turn", source=source, phase="meta", data=obj)
        elif event == "tool_call" and "spawn_agent" in payload_text:
            self.log("subagent", source=source, data=payload_text[:600])
        elif event:
            self.log("event", source=source, event=event, data=payload_text[:1500])
        else:
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
            if delta.get("content"):
                self.log("text", source=source, text=delta["content"])
            if delta.get("reasoning_content"):
                self.log("reasoning", source=source, text=delta["reasoning_content"])
        return None

    def stream_turn(self, source, response):
        done = False
        for event, payload in self.sse(response):
            if self.handle(source, event, payload) == "done":
                done = True
        if not done:
            self.incomplete_turns += 1
            self.log("error", source=source, message="stream ended before data: [DONE]; outcome unknown")
        return done

    def post_turn(self, source, text):
        """Stream one parent turn we start ourselves; composer following skips it."""
        self.own_turn.set()
        try:
            with self.request("POST", "/v1/responses", {
                "model": "agent", "stream": True, "metadata": {"model": self.model}, "input": text,
            }, accept="text/event-stream", session=self.session_id, timeout=None) as resp:
                return self.stream_turn(source, resp)
        finally:
            self.own_turn.clear()

    def follow_composer(self):
        if not self.composer_busy.acquire(blocking=False):
            return
        try:
            with self.request("GET", f"/coddy/sessions/{self.session_id}/composer-stream",
                              accept="text/event-stream", timeout=None) as resp:
                self.stream_turn("composer", resp)
        except Exception as exc:
            self.log("error", where="composer", message=repr(exc))
        finally:
            self.composer_busy.release()

    def watch_events(self):
        while not self.stop.is_set():
            try:
                with self.request("GET", "/coddy/events", accept="text/event-stream", timeout=None) as resp:
                    for event, payload in self.sse(resp):
                        if self.stop.is_set():
                            return
                        try:
                            obj = json.loads(payload)
                        except ValueError:
                            continue
                        self.on_server_event(event, obj)
            except Exception as exc:  # reconnect; the server keeps turns running
                if not self.stop.is_set():
                    self.log("error", where="events", message=repr(exc))
                    time.sleep(5)

    def on_server_event(self, event, obj):
        sid = obj.get("sessionId") or obj.get("parentSessionId")
        if event in ("turn_started", "turn_ended") and sid == self.session_id:
            self.log("turn", source="events", phase=event)
            with self.lock:
                self.running_turns = max(0, self.running_turns + (1 if event == "turn_started" else -1))
            self.last_activity = time.monotonic()
            if event == "turn_started" and not self.own_turn.is_set():
                threading.Thread(target=self.follow_composer, daemon=True).start()
        elif event == "background_wake" and sid == self.session_id:
            self.log("turn", source="events", phase="background_wake",
                     tasks=[t.get("id") for t in obj.get("tasks", [])])
            threading.Thread(target=self.follow_composer, daemon=True).start()
        elif event == "subagent_permission" and obj.get("parentSessionId") == self.session_id \
                and obj.get("phase") == "asked":
            self.answer_permission(obj.get("childSessionId"), obj.get("request") or {})

    # ---------- supervision ----------
    def escalate(self, decision):
        """Hand NEEDS_MODEL_DECISION to the parent, which owns the decision. No retry, no switch."""
        payload = decision.as_dict()
        text = ("Operator watchdog event (boring-agent ParentIdleWatchdog), not a new task:\n"
                + json.dumps(payload, ensure_ascii=False)
                + "\nThe child has shown no visible output for longer than the idle threshold. Decide per "
                  "docs/exec.md: keep waiting, or stop the child and report FAILED/HANDOFF. Do not switch the "
                  "model, replay the job, or resolve an unknown outcome yourself. Report the decision in one "
                  "short paragraph.")
        try:
            self.request("POST", f"/coddy/sessions/{self.session_id}/queue", {"text": text}).close()
            self.log("watchdog", phase="queued-to-parent", data=payload)
            return
        except urllib.error.HTTPError as exc:
            body = exc.read(2000).decode("utf-8", "replace")
            if exc.code != 409 or "no_active_turn" not in body:
                self.log("error", where="escalate", status=exc.code, body=body)
                return
        self.log("watchdog", phase="new-parent-turn", data=payload)
        threading.Thread(target=self.post_turn, args=("escalation", text), daemon=True).start()

    @staticmethod
    def monotonic_at(stamp: str) -> float:
        wall = datetime.fromisoformat(stamp[:26] + stamp[stamp.index("+", 19):] if "+" in stamp[19:]
                                      else stamp[:26]).timestamp()
        return time.monotonic() - (time.time() - wall)

    def poll_tasks(self):
        try:
            rows = self.get_json(f"/coddy/sessions/{self.session_id}/background-tasks").get("data") or []
        except urllib.error.HTTPError as exc:
            # The server answered: it is up. A 404 means it does not know this session's tasks.
            body = exc.read(500).decode("utf-8", "replace")
            if self.failed_polls == 0 or exc.code != 404:
                self.log("error", where="background-tasks", status=exc.code, body=body)
            self.failed_polls, self.server_lost = self.failed_polls + 1, False
            return [] if exc.code == 404 else None
        except (urllib.error.URLError, OSError) as exc:
            self.failed_polls += 1
            if self.failed_polls >= SERVER_LOST_AFTER and not self.server_lost:
                self.server_lost = True
                self.log("status", phase="server-lost", message=repr(exc),
                         note="running children are lost with the server; their outcome is unknown, "
                              "nothing will be replayed")
            return None
        if self.server_lost:
            self.log("status", phase="server-back")
        self.failed_polls, self.server_lost = 0, False
        for row in rows:
            task_id, stamp = row.get("id"), row.get("last_output_at") or ""
            agent = row.get("agent") or {}
            previous = self.tasks.get(task_id, {})
            self.tasks[task_id] = {
                "status": row.get("status"), "label": row.get("label"), "model": agent.get("model"),
                "child_session": agent.get("session_id"), "elapsed_s": row.get("elapsed_seconds"),
                "timeout_s": row.get("timeout_seconds"), "last_output_at": stamp,
                "input_tokens": agent.get("input_tokens"), "output_tokens": agent.get("output_tokens"),
            }
            if previous.get("status") != row.get("status"):
                self.log("task", task=task_id, status=row.get("status"), label=row.get("label"),
                         model=agent.get("model"))
            if stamp and task_id not in self.seen_output:
                self.watchdog.record_visible_progress(at=self.monotonic_at(stamp))
            elif stamp and self.seen_output[task_id] != stamp:
                # New child output is the visible progress the watchdog measures.
                self.watchdog.record_visible_progress()
                self.last_activity = time.monotonic()
                self.log("progress", task=task_id, last_output_at=stamp[11:19],
                         output_tokens=agent.get("output_tokens"), changed_files=self.changed_files())
            if stamp:
                self.seen_output[task_id] = stamp
        return rows

    def supervise(self):
        self.phase = "supervising"
        while not self.stop.is_set():
            now = time.monotonic()
            rows = self.poll_tasks()
            active = [r for r in rows or [] if str(r.get("status", "")).lower() in ACTIVE]
            # Idle time only matters while a child is supposed to be working.
            decision = self.watchdog.poll() if active else None
            if decision is not None:
                self.log("watchdog", data=decision.as_dict())
                self.escalate(decision)
            if self.running_turns > 0 or self.own_turn.is_set() or active:
                self.last_activity = now
            elif rows is None:
                if self.server_lost and now - self.last_activity > SERVER_LOST_GIVE_UP:
                    self.log("status", phase="server-gone", message="server unreachable for 10 min; stopping")
                    break
            elif now - self.last_activity > QUIET_SECONDS:
                break
            if now > self.hard_cap:
                self.log("status", phase="hard-cap", message="supervision cap reached; nothing cancelled")
                break
            self.write_state()
            self.stop.wait(POLL_SECONDS)
        self.finish()

    def finish(self):
        self.phase = "finished"
        statuses = [t.get("status") for t in self.tasks.values()]
        final = ""
        try:
            snapshot = self.get_json(f"/coddy/sessions/{self.session_id}/messages")
            self.path("transcript.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
            final = next((m.get("content") for m in reversed(snapshot.get("messages", []))
                          if m.get("role") == "assistant" and m.get("content")), "") or ""
        except Exception as exc:
            self.log("error", where="transcript", message=repr(exc))
        self.path("result.md").write_text(final if isinstance(final, str) else json.dumps(final), encoding="utf-8")
        reported = None
        start = final.find("{") if isinstance(final, str) else -1
        if start >= 0:
            try:
                reported = json.JSONDecoder().raw_decode(final[start:])[0].get("status")
            except (ValueError, AttributeError):
                reported = None
        if self.server_lost or self.incomplete_turns or any(s in ACTIVE for s in statuses):
            self.outcome = "unknown"
        elif reported:
            self.outcome = f"parent-reported:{reported}"
        else:
            self.outcome = "no-parent-report"
        self.log("status", phase="finished", outcome=self.outcome, tasks=statuses, decisions=self.decisions)
        self.write_state()

    # ---------- entry points ----------
    def start_watchers(self):
        self.path("session-id").write_text(self.session_id + "\n")
        self.watchdog.start(task_id=self.job["id"], attempt_id=self.run_id, model=self.model,
                            session_id=self.session_id, deadline_at=self.deadline)
        threading.Thread(target=self.watch_events, daemon=True).start()
        time.sleep(1)  # let the events stream connect before anything can ask for permission

    def launch(self):
        prompt = Path(self.args.prompt_file).read_text(encoding="utf-8")
        with self.request("POST", "/v1/responses", {
            "model": "agent", "stream": False, "metadata": {"model": self.model},
            "input": "Session bootstrap for an operator-run job. Reply with the single word OK and use no tools.",
        }, timeout=300) as resp:
            self.session_id = resp.headers.get("x-coddy-session-id")
            resp.read()
        with self.request("PATCH", f"/coddy/sessions/{self.session_id}", {
            "permissionMode": self.permission_mode, "mode": "agent", "selectedModelId": self.model,
            "title": f"{self.job['id']} ({self.run_id})",
        }) as resp:
            settings = json.load(resp).get("settings", {})
        self.log("status", phase="session-ready", session=self.session_id,
                 permission_mode=settings.get("permissionMode"), model=settings.get("model"))
        self.start_watchers()
        self.phase = "job-turn"
        self.write_state()
        done = self.post_turn("responses", prompt)
        self.log("status", phase="job-turn-finished", done=done)
        self.supervise()

    def attach(self):
        self.session_id = self.args.session
        try:
            self.get_json(f"/coddy/sessions/{self.session_id}/messages")
        except urllib.error.HTTPError as exc:
            self.phase, self.outcome = "not-attached", "session-unknown"
            self.log("error", where="attach", status=exc.code,
                     message=f"server does not know session {self.session_id} (check sessions.dir in config.yaml)")
            self.write_state()
            raise SystemExit(3)
        self.log("status", phase="attached", session=self.session_id)
        self.start_watchers()
        self.supervise()


def status(out: Path) -> int:
    state = json.loads((out / "state.json").read_text())
    alive = False
    try:
        os.kill(state.get("pid", 0), 0)
        alive = True
    except OSError:
        pass
    print(json.dumps({**state, "driver_alive": alive}, ensure_ascii=False, indent=1))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("launch", "attach"):
        s = sub.add_parser(name)
        s.add_argument("--job", required=True, help="native job document (validated with boring-agent)")
        s.add_argument("--token-file", required=True)
        s.add_argument("--base", default="http://127.0.0.1:12345")
        s.add_argument("--out", help="run directory (default: <job dir>/<job id>)")
        s.add_argument("--python", help="project interpreter allowed to run checks (default: job metadata)")
        s.add_argument("--permission-mode", help="default: job metadata, else ask")
        s.add_argument("--run-id")
        if name == "launch":
            s.add_argument("--prompt-file", required=True)
        else:
            s.add_argument("--session", required=True)
    s = sub.add_parser("status")
    s.add_argument("--out", required=True)
    args = p.parse_args()
    if args.command == "status":
        return status(Path(args.out))
    driver = Driver(args)

    def terminate(signum, _frame):
        driver.log("status", phase="terminated", signal=signum)
        driver.stop.set()
        driver.outcome = "unknown"
        driver.phase = "terminated"
        driver.write_state()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    (driver.path("driver.pid")).write_text(f"{os.getpid()}\n")
    driver.launch() if args.command == "launch" else driver.attach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
