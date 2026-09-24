"""Intent, admission, settlement and retry decisions, serialized by SQLite."""
from dataclasses import replace
import json
import time
import uuid

from .artifacts import ResultVerdict, artifact_checksum, prevalidate_result
from .model import Invalid, TERMINAL, canonical, digest
from .process import alive
from .store import event, executions


class Manager:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def finish(db, task, status, reason, result=None):
        db.execute("""UPDATE tasks SET status=?,reason=?,finished_at=?,version=version+1,
                   observation_condition='Fresh',result_path=?,result_sha256=? WHERE id=?""",
                   (status, reason, time.time(), result["result_path"] if result else None,
                    result["result_sha256"] if result else None, task["id"]))
        event(db, "task.finished", task["id"], task["current_attempt_id"], status=status, reason=reason)

    @staticmethod
    def attempt_row(db, attempt_id):
        return db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()

    def tick(self):
        validations = []
        with self.store.transaction() as db:
            cfg = self.store.settings_from(db)
            now = time.time()
            tasks = db.execute("SELECT * FROM tasks WHERE status NOT IN ('Succeeded','Failed','Cancelled')").fetchall()
            for task in tasks:
                spec = json.loads(task["spec"])
                attempt = self.attempt_row(db, task["current_attempt_id"])
                success_before_deadline = bool(
                    attempt is not None and attempt["state"] == "Succeeded"
                    and attempt["finished_at"] is not None
                    and attempt["finished_at"] <= task["deadline"]
                )
                if (now >= task["deadline"] and task["desired_action"] == "Run"
                        and not success_before_deadline):
                    db.execute("UPDATE tasks SET desired_action='Cancel',reason='deadline_exceeded',version=version+1 WHERE id=?",
                               (task["id"],))
                    event(db, "task.deadline_exceeded", task["id"])
                    task = self.store.require_task(db, task["id"])
                if task["status"] == "Pending":
                    if task["desired_action"] == "Cancel":
                        self.finish(db, task, "Failed" if task["reason"] == "deadline_exceeded" else "Cancelled", task["reason"])
                    continue
                attempt = self.attempt_row(db, task["current_attempt_id"])
                if attempt is None:
                    continue
                if attempt["state"] == "Queued":
                    # The runner must commit Launching before any effect, in a transaction that
                    # requires state Queued. Holding this transaction while the attempt is still
                    # Queued therefore proves that nothing has run: it is safe to end it here.
                    if task["desired_action"] == "Cancel":
                        db.execute("""UPDATE attempts SET state='Cancelled',sequence=sequence+1,
                                   finished_at=?,heartbeat=?,tokens=0,error_kind='cancelled',
                                   error_message='Cancelled before launch' WHERE id=?""", (now, now, attempt["id"]))
                    else:
                        worker = db.execute("SELECT last_seen FROM workers WHERE id=?", (attempt["worker_id"],)).fetchone()
                        if worker is None or now - worker["last_seen"] > cfg["worker_ttl"]:
                            db.execute("""UPDATE attempts SET state='Cancelled',sequence=sequence+1,
                                       finished_at=?,heartbeat=?,tokens=0,error_kind='not_started',
                                       error_message='Worker lost before launch; nothing ran' WHERE id=?""",
                                       (now, now, attempt["id"]))
                            event(db, "attempt.worker_lost", task["id"], attempt["id"], worker_id=attempt["worker_id"])
                    attempt = self.attempt_row(db, attempt["id"])
                if attempt["state"] in ("Launching", "Running") and not alive(attempt["runner_pid"], attempt["runner_start"]):
                    message = f"Runner lost; runtime outcome is unknown. Inspect logs/{attempt['id']}.log"
                    db.execute("""UPDATE attempts SET state='Unknown',sequence=sequence+1,
                               tokens=NULL,error_kind='unknown',error_message=? WHERE id=?""", (message, attempt["id"]))
                    event(db, "attempt.runner_lost", task["id"], attempt["id"], log=f"logs/{attempt['id']}.log")
                    attempt = self.attempt_row(db, attempt["id"])
                # Only a runner produces observations that can go stale; Queued is waiting, not silence.
                condition = ("Unknown" if attempt["state"] == "Unknown" else
                             "Stale" if attempt["state"] in ("Launching", "Running") and now - attempt["heartbeat"] > cfg["observation_ttl"]
                             else "Fresh")
                db.execute("UPDATE tasks SET observation_condition=?,observed_at=? WHERE id=?",
                           (condition, attempt["heartbeat"], task["id"]))
                if attempt["state"] == "Running" and task["status"] == "Scheduled":
                    db.execute("UPDATE tasks SET status='Running',version=version+1 WHERE id=?", (task["id"],))
                if attempt["state"] in TERMINAL and not attempt["settled"]:
                    if attempt["state"] == "Succeeded":
                        # Artifact validation is deliberately outside this write transaction.
                        validations.append((dict(task), dict(attempt), spec))
                    else:
                        self.settle(db, task, attempt, spec, now)
            self.schedule(db, cfg, now)
        for task, attempt, spec in validations:
            self._validate_and_settle(task, attempt, spec)

    @staticmethod
    def _invalid_verdict(attempt, spec, desired_action, message):
        return ResultVerdict(attempt_id=attempt["id"], result_path=attempt.get("result_path"),
                             result_sha256=attempt.get("result_sha256"), spec_sha256=digest(spec),
                             desired_action=desired_action, error=message)

    def _validate_and_settle(self, task_snapshot, attempt_snapshot, spec_snapshot):
        try:
            verdict = prevalidate_result(self.store, attempt_snapshot, spec_snapshot,
                                         task_snapshot["desired_action"])
        except (Invalid, RecursionError) as exc:
            verdict = self._invalid_verdict(attempt_snapshot, spec_snapshot,
                                            task_snapshot["desired_action"], str(exc))

        # A valid verdict also carries a content identity. Rechecking this outside the
        # write transaction catches an artifact replaced after pre-validation.
        if verdict.error is None and task_snapshot["desired_action"] == "Run":
            try:
                current_checksum = artifact_checksum(self.store, attempt_snapshot, spec_snapshot)
            except (Invalid, RecursionError) as exc:
                verdict = replace(verdict, error=str(exc))
            else:
                if current_checksum != verdict.result_sha256:
                    verdict = replace(verdict, error="Artifact changed after pre-validation")

        with self.store.transaction() as db:
            task = self.store.require_task(db, task_snapshot["id"])
            attempt = self.attempt_row(db, attempt_snapshot["id"])
            if attempt is None or task["current_attempt_id"] != attempt["id"]:
                return
            if attempt["state"] != "Succeeded" or attempt["settled"] or task["status"] in TERMINAL:
                return
            # Cancellation/deadline intent is authoritative even if validation succeeded.
            if task["desired_action"] != verdict.desired_action:
                if task["desired_action"] == "Cancel":
                    self.settle(db, task, attempt, json.loads(task["spec"]), time.time())
                return
            spec = json.loads(task["spec"])
            stale = []
            if attempt["id"] != verdict.attempt_id:
                stale.append("attempt_id")
            if attempt["result_path"] != verdict.result_path:
                stale.append("result_path")
            if attempt["result_sha256"] != verdict.result_sha256:
                stale.append("checksum")
            if digest(spec) != verdict.spec_sha256:
                stale.append("spec")
            if stale:
                verdict = replace(verdict, error="Pre-validated result is stale: " + ", ".join(stale))
            self.settle(db, task, attempt, spec, time.time(), verdict)

    def settle(self, db, task, attempt, spec, now, verdict=None):
        # Only terminal execution evidence releases concurrency. Usage is accounted separately.
        db.execute("UPDATE attempts SET reserved=0,settled=1 WHERE id=?", (attempt["id"],))
        tokens = task["tokens_used"] + attempt["known_tokens"]
        unknown_usage = bool(task["usage_unknown"] or attempt["tokens"] is None)
        db.execute("UPDATE tasks SET tokens_used=?,usage_unknown=? WHERE id=?", (tokens, int(unknown_usage), task["id"]))
        from .workflows import WorkflowStore
        workflow_store = WorkflowStore(self.store, ensure=False)
        workflow_store.record_usage_locked(db, task["id"], attempt["known_tokens"])
        if task["desired_action"] == "Cancel":
            self.finish(db, task, "Failed" if task["reason"] == "deadline_exceeded" else "Cancelled", task["reason"])
            return
        if attempt["error_kind"] == "not_started":
            # A confirmed non-start consumed no execution attempt and needs no replay safety.
            db.execute("""UPDATE tasks SET status='Pending',current_attempt_id=NULL,next_run_at=?,
                       reason=?,version=version+1 WHERE id=?""", (now, attempt["error_message"], task["id"]))
            event(db, "task.requeued", task["id"], attempt["id"], reason=attempt["error_message"])
            return
        kind, message = attempt["error_kind"], attempt["error_message"]
        if attempt["state"] == "Succeeded":
            if verdict is None or verdict.error:
                kind, message = "validation", (verdict.error if verdict else "Missing result validation verdict")
                event(db, "attempt.result_rejected", task["id"], attempt["id"], reason=message)
                db.execute("UPDATE attempts SET error_kind=?,error_message=? WHERE id=?", (kind, message, attempt["id"]))
            else:
                self.finish(db, task, "Succeeded", "output_validated", attempt)
                planner = db.execute("SELECT id FROM workflow_roots WHERE planner_task_id=?", (task["id"],)).fetchone()
                if planner is not None:
                    # The planner is an ordinary task.  Its already validated
                    # JSON result becomes workflow metadata in this same commit.
                    from .artifacts import read_result
                    plan = read_result(self.store, dict(attempt), spec)
                    workflow_store._settle_plan_locked(db, planner["id"], plan)
                return
        policy = spec["retry"]
        executed = executions(db, task["id"])
        delay = policy["backoff_seconds"] * (2 ** (executed - 1))
        max_tokens = spec["budget"]["max_tokens"]
        budget_ok = max_tokens is None or (not unknown_usage and tokens < max_tokens)
        if (policy["replay_safe"] and kind in policy["on"] and
                executed < policy["max_attempts"] and now + delay < task["deadline"] and budget_ok):
            db.execute("""UPDATE tasks SET status='Pending',current_attempt_id=NULL,next_run_at=?,
                       reason=?,version=version+1 WHERE id=?""", (now + delay, message, task["id"]))
            event(db, "task.retry_scheduled", task["id"], attempt["id"], next_run_at=now + delay, reason=message)
        else:
            self.finish(db, task, "Failed", message or kind or "execution_failed")

    def schedule(self, db, cfg, now):
        used = db.execute("SELECT count(*) FROM attempts WHERE reserved=1").fetchone()[0]
        pending = db.execute("SELECT * FROM tasks WHERE status='Pending' AND desired_action='Run' AND next_run_at<=? ORDER BY submitted_at,id", (now,)).fetchall()
        cursor = cfg["last_worker"]
        for task in pending:
            if used >= cfg["max_active"]:
                break
            spec = json.loads(task["spec"])
            workers = db.execute("""SELECT w.*, (SELECT count(*) FROM attempts a WHERE a.worker_id=w.id AND a.reserved=1) AS used
                                  FROM workers w WHERE last_seen>=? ORDER BY id""", (now - cfg["worker_ttl"],)).fetchall()
            feasible = [w for w in workers if w["used"] < w["slots"] and spec["runtime"] in json.loads(w["runtimes"])
                        and (spec["sandbox"] == "read-only" or w["allow_write"])]
            if not feasible:
                continue
            from .workflows import WorkflowStore
            workflow_store = WorkflowStore(self.store, ensure=False)
            dependency = workflow_store._deliver_dependencies_locked(db, task["id"])
            if dependency["state"] in ("waiting", "failed"):
                continue
            if not workflow_store.claim_attempt_locked(db, task["id"]):
                continue
            worker = next((w for w in feasible if w["id"] > cursor), feasible[0])
            attempt_id = str(uuid.uuid4())
            number = db.execute("SELECT count(*)+1 FROM attempts WHERE task_id=?", (task["id"],)).fetchone()[0]
            db.execute("""INSERT INTO attempts(id,task_id,number,worker_id,created_at,heartbeat) VALUES(?,?,?,?,?,?)""",
                       (attempt_id, task["id"], number, worker["id"], now, now))
            db.execute("INSERT INTO outbox(attempt_id,worker_id) VALUES(?,?)", (attempt_id, worker["id"]))
            db.execute("UPDATE tasks SET status='Scheduled',current_attempt_id=?,reason=NULL,version=version+1 WHERE id=?",
                       (attempt_id, task["id"]))
            event(db, "task.scheduled", task["id"], attempt_id, worker_id=worker["id"], attempt_number=number)
            cursor, used = worker["id"], used + 1
        db.execute("UPDATE settings SET value=? WHERE key='last_worker'", (canonical(cursor),))
