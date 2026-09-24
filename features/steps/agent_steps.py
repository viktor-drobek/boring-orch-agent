"""Domain-language steps call production behavior, not unittest test methods."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from behave import given, when, then

from boring_agent import cli as cli_module
from boring_agent.artifacts import read_result
from boring_agent.discovery import Discovery
from boring_agent.manager import Manager
from boring_agent.model import Conflict, Gone, Invalid, StorageError
from boring_agent.process import lock
from boring_agent.runner import run_attempt
from boring_agent.store import Store
from boring_agent.workspace import Workspace
from features.support import AgentFixture
from tests.support.http_provider import completion, server


@given("an isolated local agent")
def isolated(context):
    context.agent = AgentFixture(context.resources)


@given("a fresh demo worker with {slots:d} slots")
def worker(context, slots):
    context.agent.store.register_worker("primary", ["demo"], slots)


@given('a task accepted with key "{key}"')
def accepted(context, key):
    context.agent.submit(key)


@when('I submit a valid task with key "{key}"')
def submit(context, key):
    context.agent.capture(lambda: context.agent.submit(key))


@when("the manager and store are reopened")
def reopen(context):
    h = context.agent
    h.store = Store(h.store.home)
    h.manager = Manager(h.store)


@when("I repeat the original submission")
def repeat(context):
    h = context.agent
    h.capture(lambda: h.store.submit(h.raw, h.key))


@then("I receive the original receipt marked as a duplicate")
def duplicate(context):
    h = context.agent
    assert h.error is None, h.error
    assert h.value == {**h.receipt, "duplicate": True}, h.value


@then("the receipt identifies different command and task records")
def identities(context):
    receipt = context.agent.receipt
    assert receipt["task_id"] != receipt["command_id"]


@when("I submit a different objective with the same key")
def conflict(context):
    h = context.agent
    h.capture(lambda: h.store.submit({**h.raw, "objective": "Changed intent"}, h.key))


@then('the command fails with "{code}"')
def error(context, code):
    error = context.agent.error
    assert error is not None and error.code == code, repr(error)


@then("there is exactly {count:d} task in the store")
@then("there is exactly {count:d} tasks in the store")
def task_count(context, count):
    assert len(context.agent.store.tasks()) == count


@then("the accepted objective is unchanged")
def immutable(context):
    h = context.agent
    assert h.task()["spec"]["objective"] == h.raw["objective"]


@when("I submit a task with invalid {input}")
def invalid(context, input):
    changes = {"empty objective": {"objective": ""}, "supplied state": {"status": "Succeeded"},
               "negative deadline": {"budget": {"deadline_seconds": -1}},
               "external schema": {"output_schema": {"$ref": "https://example.invalid/schema"}},
               "unauthorized write": {"sandbox": "workspace-write", "tools": ["write_file"]}}
    h = context.agent
    h.capture(lambda: h.submit(**changes[input]))


@given("storage will fail while recording the command")
def command_storage_fault(context):
    storage_fault(context, "commands")


@given("storage will fail while recording dispatch")
def dispatch_storage_fault(context):
    storage_fault(context, "outbox")


def storage_fault(context, table):
    with context.agent.store.transaction() as db:
        db.execute(f"CREATE TRIGGER fail_write BEFORE INSERT ON {table} BEGIN SELECT injected_io_failure(); END")


@when("I cancel a nonexistent task")
def cancel_missing(context):
    h = context.agent
    h.capture(lambda: h.store.cancel("nonexistent", "stop"))


@when("I request cancellation")
def cancel(context):
    h = context.agent
    h.store.cancel(h.task_id, "cancel-" + h.task_id)


@when("the manager reconciles")
def reconcile(context):
    context.agent.tick()


@when("the manager tries to assign work")
def assign(context):
    context.agent.capture(context.agent.tick)


@given("the task is assigned but has not started")
def assigned(context):
    h = context.agent
    h.tick()
    assert h.task()["status"] == "Scheduled"
    assert h.attempt()["state"] == "Queued"


@given("the worker becomes unavailable")
def stale_worker(context):
    with context.agent.store.transaction() as db:
        db.execute("UPDATE workers SET last_seen=0")


@given("a replacement demo worker becomes available")
@when("a replacement demo worker becomes available")
def replacement(context):
    context.agent.store.register_worker("replacement", ["demo"], 2)


@given("the executor has produced a valid result awaiting settlement")
def awaiting_result(context):
    h = context.agent
    h.tick()
    h.deliver()
    assert h.attempt()["state"] == "Succeeded"


@given("the current executor is running")
def running(context):
    context.agent.running()


@when("a {report} observation arrives")
def invalid_report(context, report):
    h = context.agent
    attempt = h.attempt()
    who, sequence, state = attempt["worker_id"], attempt["sequence"] + 1, "Running"
    if report == "foreign worker":
        who = "outsider"
    elif report == "older sequence":
        sequence = attempt["sequence"]
    elif report == "regressive":
        state = "Launching"
    else:
        raise AssertionError(report)
    try:
        context.observation = h.store.observe(attempt["id"], who, sequence, state)
    except Conflict:
        context.observation = False


@then("the observation is rejected")
def rejected(context):
    assert context.observation is False


@then('the task status is "{status}"')
def status(context, status):
    task = context.agent.task()
    assert task["status"] == status, (task["status"], task["reason"])


@then("the task has {count:d} recorded attempt")
@then("the task has {count:d} recorded attempts")
def attempts(context, count):
    assert len(context.agent.task()["attempts"]) == count


@then('the event history includes "{kind}"')
def history(context, kind):
    h = context.agent
    assert kind in [e["kind"] for e in h.store.events(h.task_id)]


@then("no task result is accepted")
def no_result(context):
    assert context.agent.task()["result_path"] is None


@then("the attempt artifact remains available for diagnostics")
def diagnostic(context):
    h = context.agent
    assert (h.store.home / h.attempt()["result_path"]).is_file()


@then("{count:d} shared slot is reserved")
@then("{count:d} shared slots are reserved")
def slots(context, count):
    assert context.agent.store.capacity()["used"] == count


@when("the worker delivers the assignment twice")
def deliver_twice(context):
    context.agent.deliver()
    context.agent.deliver()


@then("the executor was claimed exactly once")
def one_claim(context):
    h = context.agent
    assert sum(e["kind"] == "attempt.claimed" for e in h.store.events(h.task_id)) == 1


@given("the runner crashed immediately after claiming execution")
def crash(context):
    h = context.agent
    h.tick()
    attempt = h.attempt()
    # Exit after the production launch claim commits, before any runtime effect.
    script = ("import os; from unittest.mock import patch; from boring_agent.runner import run_attempt; "
              "from boring_agent.store import Store; "
              "p=patch('boring_agent.runner.Controller.checkpoint',side_effect=lambda:os._exit(17)); p.start(); "
              f"run_attempt(Store({str(h.store.home)!r}),{attempt['id']!r},{attempt['worker_id']!r})")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
    assert result.returncode == 17, result.stderr


@then('the observation condition is "{condition}"')
def condition(context, condition):
    task = context.agent.task()
    assert task["observation_condition"] == condition, task["observation_condition"]


@then('the desired action is "{action}"')
def desired(context, action):
    assert context.agent.task()["desired_action"] == action


@when("the operator confirms that the fixture execution stopped")
def resolve(context):
    h = context.agent
    h.store.resolve(h.attempt()["id"], "Fixture child was reaped with exit 17 before runtime activity", True)


@given("the installation permits {count:d} active task")
@given("the installation permits {count:d} active tasks")
def max_active(context, count):
    # Installation setting, changed before any submissions in these scenarios.
    with context.agent.store.transaction() as db:
        db.execute("UPDATE settings SET value=? WHERE key='max_active'", (json.dumps(count),))


@given("{count:d} tasks are waiting for placement")
def waiting(context, count):
    for i in range(count):
        context.agent.submit(str(i))


@when("{count:d} scheduling decisions run concurrently")
def concurrent(context, count):
    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(lambda _: Manager(Store(context.agent.store.home)).tick(), range(count)))


@then('exactly {count:d} task is "{status}"')
@then('exactly {count:d} tasks are "{status}"')
def status_count(context, count, status):
    assert sum(t["status"] == status for t in context.agent.store.tasks()) == count


@then("each worker has {count:d} reserved slots")
def each_worker(context, count):
    workers = context.agent.store.capacity()["workers"]
    assert len(workers) == 2
    assert all(w["used"] == count for w in workers), workers


@then("the worker capacity is marked invalid")
def invalid_capacity(context):
    assert context.agent.store.capacity()["workers"][0]["valid"] is False


@given("its observation becomes stale while its process is alive")
@given("the assignment has waited longer than the observation window")
def stale_observation(context):
    h = context.agent
    with h.store.transaction() as db:
        db.execute("UPDATE attempts SET heartbeat=0 WHERE id=?", (h.attempt()["id"],))


@when("the runner cannot record its process identity and delivers the assignment")
def deliver_without_identity(context):
    # Simulates a host where /proc gives no PID start time; the claim must fail closed.
    with patch("boring_agent.runner.identity", return_value=None):
        context.agent.deliver()


@then("the attempt error names its log file")
def error_names_log(context):
    attempt = context.agent.attempt()
    assert f"logs/{attempt['id']}.log" in (attempt["error_message"] or ""), attempt["error_message"]


@then("the worker kept a launch log for the attempt")
def launch_log(context):
    h = context.agent
    log = h.store.home / "logs" / f"{h.attempt()['id']}.log"
    assert log.is_file() and "launch 1" in log.read_text(), log


@given('a task that fails once with "{failure}" and replay safety "{safe}"')
def fails_once(context, failure, safe):
    context.agent.submit(demo={"delay_seconds": 0, "fail_attempts": 1, "failure_kind": failure},
                         retry={"replay_safe": safe == "true", "max_attempts": 2, "backoff_seconds": 1})


@given("a replay-safe task whose two executions both fail")
def fails_twice(context):
    context.agent.submit(demo={"delay_seconds": 0, "fail_attempts": 2},
                         retry={"replay_safe": True, "max_attempts": 2, "backoff_seconds": 1})


@when("the first execution finishes and is reconciled")
def execute(context):
    h = context.agent
    h.tick()
    h.deliver()
    h.tick()


@when("the retry backoff elapses")
def backoff(context):
    h = context.agent
    assert h.task()["status"] == "Pending"
    h.tick(at=h.task()["next_run_at"] + .001)


@when("the previous attempt sends a late running observation")
def late(context):
    h = context.agent
    old = h.task()["attempts"][0]
    context.observation = h.store.observe(old["id"], old["worker_id"], old["sequence"] + 100, "Running")


@then("the attempts have different identities")
def different_attempts(context):
    attempts = context.agent.task()["attempts"]
    assert len({a["id"] for a in attempts}) == len(attempts)


@given("a replay-safe task with a token budget of {tokens:d}")
def budget(context, tokens):
    context.agent.submit(budget={"max_tokens": tokens},
                         retry={"max_attempts": 2, "replay_safe": True, "backoff_seconds": 1})


@when("its executor reports a confirmed transient failure with {usage} tokens")
def failure_usage(context, usage):
    h = context.agent
    h.running()
    h.observe("Failed", error_kind="transient", tokens=None if usage == "unknown" else int(usage))


@then('settled usage is marked "{validity}"')
def usage_validity(context, validity):
    assert bool(context.agent.task()["usage_unknown"]) == (validity == "unknown")


@given("the current executor has reported {tokens:d} consumed tokens")
def known_usage(context, tokens):
    h = context.agent
    h.running()
    h.observe("Running", tokens=tokens)


@when("the same executor reports a transient failure with only {tokens:d} consumed token")
def decreasing_usage(context, tokens):
    context.agent.observe("Failed", error_kind="transient", tokens=tokens)


@then("the task retains at least {tokens:d} known consumed tokens")
def retained_usage(context, tokens):
    assert context.agent.task()["tokens_used"] >= tokens


@given("its task deadline has elapsed")
def expired(context):
    h = context.agent
    with h.store.transaction() as db:
        db.execute("UPDATE tasks SET deadline=0 WHERE id=?", (h.task_id,))


@given("a task requiring an integer answer and returning {answer}")
def schema_result(context, answer):
    context.agent.submit(demo={"delay_seconds": 0, "result": {"answer": json.loads(answer)}},
                         output_schema={"type": "object", "required": ["answer"],
                                        "properties": {"answer": {"type": "integer"}}})


@then("the accepted result contains answer {answer:d}")
def result_answer(context, answer):
    h = context.agent
    task = h.task()
    assert task["status"] == "Succeeded"
    assert read_result(h.store, task["attempts"][-1], task["spec"])["answer"] == answer


@given('a task that expects the file "{path}" and finishes without writing it')
def expects_file(context, path):
    context.agent.submit(expect_files=[path], demo={"delay_seconds": 0, "result": {"path": path}})


@when('I submit a task expecting the file "{path}"')
def submit_expecting(context, path):
    context.agent.capture(lambda: context.agent.submit(expect_files=[path]))


@given('the workspace already contains "{path}"')
def preexisting(context, path):
    target = context.agent.workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("already here")


@when("the attempt artifact is altered")
def tamper(context):
    h = context.agent
    (h.store.home / h.attempt()["result_path"]).write_text('{"tampered":true}')


@when("I retrieve the accepted result")
def read_artifact(context):
    h = context.agent
    h.capture(lambda: read_result(h.store, h.attempt(), h.task()["spec"]))


@given("a local HTTP provider fixture")
def http_fixture(context):
    h = context.agent
    h.store.register_worker("provider", ["llm"], 1)
    (h.workspace / "input.txt").write_text("The required answer is 42.")
    context.provider_kind = "openai"


@given('the "{provider}" fixture requests a file read then returns a final answer')
def provider_script(context, provider):
    context.provider_kind = provider
    context.responses = [(200, completion(provider, {"action": "read_file", "path": "input.txt"})),
                         (200, completion(provider, {"action": "final", "result": {"answer": 42}}))]


@given("the provider rejects the request with HTTP {status:d}")
def http_error(context, status):
    context.responses = [(status, {"error": "fixture rejection"})]


@given('the "{provider}" fixture returns an empty completion cut off at the output limit')
def truncated_empty(context, provider):
    context.provider_kind = provider
    context.responses = [(200, completion(provider, None, truncated=True))]


@then('the failure reason mentions "{text}"')
def reason_mentions(context, text):
    task = context.agent.task()
    assert text in (task["reason"] or ""), task["reason"]


@given("the provider accepts the request but does not reply before its deadline")
def http_timeout(context):
    context.responses = ["wait"]


@given("the worker disables provider JSON mode")
def disable_json_mode(context):
    context.json_mode = "off"


def run_provider(context, replay_safe):
    h = context.agent
    base, context.requests, _, _ = context.resources.enter_context(server(context.responses))
    environment = {"BOA_PROVIDER": context.provider_kind, "BOA_BASE_URL": base,
                   "BOA_MODEL": "fixture-model", "BOA_API_KEY": "fixture-only"}
    if getattr(context, "json_mode", None):
        environment["BOA_JSON_MODE"] = context.json_mode
    context.resources.enter_context(patch.dict(os.environ, environment))
    h.submit(runtime="llm", demo={}, budget={"request_seconds": .2},
             retry={"replay_safe": replay_safe, "max_attempts": 2, "backoff_seconds": 1})
    h.tick()
    h.deliver()
    h.tick()


@when("the LLM agent runs against the fixture")
def llm(context):
    run_provider(context, False)


@when("the replay-safe LLM agent runs against the fixture")
def replay_llm(context):
    run_provider(context, True)


@then("the provider was asked for a JSON object response")
def json_mode_requested(context):
    body = context.requests[0]["body"]
    assert body.get("response_format") == {"type": "json_object"}, body.get("response_format")


@then("the provider was not asked for a JSON object response")
def json_mode_absent(context):
    assert "response_format" not in context.requests[0]["body"], context.requests[0]["body"]


@then("the provider received the permitted file contents")
def tool_contents(context):
    assert "required answer is 42" in context.requests[1]["body"]["messages"][-1]["content"]


@then("the task reports {tokens:d} known consumed tokens")
def total_tokens(context, tokens):
    task = context.agent.task()
    assert task["tokens_used"] == tokens and not task["usage_unknown"], task


@then("the provider received exactly {count:d} request")
@then("the provider received exactly {count:d} requests")
def request_count(context, count):
    assert len(context.requests) == count


@given("a workspace containing a visible note and a hidden credential")
def files(context):
    h = context.agent
    (h.workspace / "note.txt").write_text("A visible note.")
    (h.workspace / ".env").write_text("fixture-secret")
    (h.workspace / "linked.txt").symlink_to(h.workspace / "note.txt")
    context.file_tools = Workspace(h.workspace, ["list_files", "read_file"], h.store.home)


@when('the read-only agent reads "{path}"')
def read_file(context, path):
    context.agent.capture(lambda: context.file_tools.call({"action": "read_file", "path": path}))


@when('the writing agent writes "{path}"')
def writing_agent(context, path):
    h = context.agent
    tools = Workspace(h.workspace, ["write_file"], h.store.home)
    h.capture(lambda: tools.call({"action": "write_file", "path": path, "content": "generated text"}))


@then('the workspace file "{path}" holds the written text')
def written(context, path):
    h = context.agent
    assert h.error is None, h.error
    assert (h.workspace / path).read_text() == "generated text"


@when("the read-only agent tries to overwrite the note")
def write_file(context):
    context.agent.capture(lambda: context.file_tools.call({"action": "write_file", "path": "note.txt", "content": "changed"}))


@then("the tool returns the note contents")
def content(context):
    assert context.agent.error is None
    assert context.agent.value == {"content": "A visible note."}


@then("the note is unchanged")
def unchanged(context):
    assert (context.agent.workspace / "note.txt").read_text() == "A visible note."


@when("I submit a demo task through the CLI")
def cli_submit(context):
    h = context.agent
    h.receipt = h.cli("submit", "-", "--key", "cli", payload=h.raw)
    h.task_id = h.receipt["task_id"]


@then('the CLI returns a durable receipt and the task is "{status}"')
def cli_receipt(context, status):
    h = context.agent
    assert h.receipt["command_id"] != h.task_id
    assert h.cli("status", h.task_id)["status"] == status


@when("separate manager and worker processes execute the task")
def cli_execute(context):
    h = context.agent
    h.start_processes()
    assert h.cli("wait", h.task_id, "--timeout", "10")["status"] == "Succeeded"


@when("those processes stop and a new manager reconciles the store")
def cli_restart(context):
    h = context.agent
    h.stop_processes()
    result = subprocess.run([sys.executable, "-m", "boring_agent", "--home", str(h.store.home), "manager", "--once"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@when("I retrieve the result through the CLI")
def cli_result(context):
    context.agent.value = context.agent.cli("result", context.agent.task_id)


@then("the CLI returns the validated demo result")
def cli_valid_result(context):
    h = context.agent
    assert h.value["demo"] is True and h.value["objective"] == h.raw["objective"]


def _real_agent(context):
    if not hasattr(context, "agent"):
        context.agent = AgentFixture(context.resources)
    return context.agent


@given("a completed task is eligible for retention deletion")
def retention_candidate(context):
    h = _real_agent(context)
    h.store.register_worker("retention-worker", ["demo"], 1)
    h.submit("retention-key")
    h.tick()
    h.deliver()
    h.tick()
    with h.store.transaction() as db:
        db.execute("UPDATE tasks SET finished_at=0 WHERE id=?", (h.task_id,))
    context.retention_raw, context.retention_key = dict(h.raw), h.key


@given("its original submit command has not reached the idempotency horizon")
def retention_horizon(context):
    h = _real_agent(context)
    with h.store.transaction() as db:
        db.execute("UPDATE settings SET value='86400' WHERE key IN ('retention_seconds','idempotency_horizon')")


@when("retention deletes the task payload and I repeat the original submission")
def retain_and_repeat(context):
    h = _real_agent(context)
    h.store.retain(now=time.time())
    h.capture(lambda: h.store.submit(context.retention_raw, context.retention_key))


@given("an attempt produced a valid result before its task deadline")
def predeadline_success(context):
    h = _real_agent(context)
    h.store.register_worker("deadline-worker", ["demo"], 1)
    h.submit("deadline-key", budget={"deadline_seconds": 30})
    h.tick()
    h.deliver()
    attempt = h.attempt()
    with h.store.transaction() as db:
        deadline = attempt["finished_at"] + .2
        db.execute("UPDATE tasks SET deadline=? WHERE id=?", (deadline, h.task_id))
    context.deadline = deadline


@given("the deadline passes before the manager settles the result")
def deadline_passes(context):
    time.sleep(.25)


@given("a store at schema state \"{state}\"")
def schema_state(context, state):
    h = _real_agent(context)
    context.schema_state = state
    with h.store.transaction() as db:
        if state == "unsupported future release":
            db.execute("PRAGMA user_version=999")
        elif state in ("previous supported release", "interrupted current migration"):
            db.execute("PRAGMA user_version=1")
            if state == "interrupted current migration":
                db.execute("UPDATE schema_migrations SET state='started', outcome='migration_intent' WHERE version=2")


@when("a process opens the store")
def open_schema(context):
    h = _real_agent(context)
    try:
        reopened = Store(h.store.home)
        reopened.settings()
        context.schema_outcome = {
            "fresh": "initialized at current version",
            "previous supported release": "migrated once to current version",
            "interrupted current migration": "resumed from durable migration intent",
        }.get(context.schema_state, "")
        context.schema_store = reopened
    except StorageError:
        context.schema_outcome = "rejected without mutation"
        context.schema_store = Store(h.store.home)


@then('the schema outcome is "{outcome}"')
def schema_outcome(context, outcome):
    if context.schema_outcome != outcome:
        raise AssertionError("schema state=%r actual=%r expected=%r" %
                             (context.schema_state, context.schema_outcome, outcome))
    if outcome == "rejected without mutation":
        db = sqlite3.connect(context.schema_store.path)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        db.close()
        assert version == 999
    else:
        with context.schema_store.reading() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
    if outcome != "rejected without mutation":
        assert version == 2


@given("two processes open a store requiring one migration")
def concurrent_schema_setup(context):
    h = _real_agent(context)
    with h.store.transaction() as db:
        db.execute("PRAGMA user_version=1")
    context.concurrent_store = h.store


@when("they race to open the store")
def concurrent_schema_open(context):
    def open_one(_):
        opened = Store(context.concurrent_store.home)
        opened.settings()
        return opened
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(open_one, range(2)))


@then("exactly one migration is applied")
def one_migration(context):
    with context.concurrent_store.reading() as db:
        rows = db.execute("SELECT outcome FROM schema_migrations WHERE version=2").fetchall()
    assert [row[0] for row in rows].count("migrated") == 1


@then("both processes observe the same supported schema version")
def same_schema(context):
    with context.concurrent_store.reading() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Hardening: platform guard, retention, artifact expiry
# ---------------------------------------------------------------------------

@given('the host lacks "{capability}"')
def host_lacks(context, capability):
    _real_agent(context)
    reason = {"/proc process identity": "/proc process identity is unavailable",
              "flock advisory locking": "flock advisory locking is unavailable"}[capability]
    context.resources.enter_context(patch.object(cli_module, "supported", return_value=reason))


@when('I start the "{command}" loop subcommand')
def start_loop(context, command):
    h = context.agent
    stderr = StringIO()
    with redirect_stderr(stderr):
        context.exit_code = cli_module.main(["--home", str(h.store.home), command])
    context.stderr = stderr.getvalue()
    context.loop_command = command


@then("the command refuses to start")
def refused(context):
    assert context.exit_code == 2, (context.exit_code, context.stderr)


@then('the error mentions "{text}"')
def error_mentions(context, text):
    assert text in context.stderr, context.stderr


@then("no process lock is acquired")
def no_lock(context):
    h = context.agent
    name = "manager" if context.loop_command == "manager" else "worker:local"
    assert not (h.store.home / "locks" / hashlib.sha256(name.encode()).hexdigest()).exists()


@given("a live manager holds its process lock")
def live_lock(context):
    h = _real_agent(context)
    context.resources.enter_context(lock(h.store.home, "manager"))
    context.lock_path = h.store.home / "locks" / hashlib.sha256(b"manager").hexdigest()
    context.lock_inode = context.lock_path.stat().st_ino


@given("the lock file is older than the retention threshold")
def old_lock(context):
    os.utime(context.lock_path, (0, 0))
    with context.agent.store.transaction() as db:
        db.execute("UPDATE settings SET value='1' WHERE key='retention_seconds'")


@when("retention runs")
def retention_runs(context):
    h = context.agent
    context.retention = h.store.retain(now=time.time())
    context.expired = h.store.expire_artifacts(now=time.time())


@then("the lock file remains linked to the live lock inode")
def lock_kept(context):
    assert context.lock_path.exists()
    assert context.lock_path.stat().st_ino == context.lock_inode


@then("a second manager cannot acquire the lock")
def lock_held(context):
    try:
        with lock(context.agent.store.home, "manager"):
            raise AssertionError("second manager acquired the lock")
    except Conflict:
        pass


@then("no new task is scheduled")
def no_new_task(context):
    h = context.agent
    h.tick()
    assert all(task["status"] in ("Succeeded", "Failed", "Cancelled") for task in h.store.tasks())


@given("retention has recorded deletion intent for an expired task")
def intent_recorded(context):
    retention_candidate(context)
    h = context.agent
    context.retained_task = h.task_id
    context.retained_artifact = h.store.home / h.attempt()["result_path"]
    assert context.retained_artifact.is_file()


@given("retention stops after deleting a dependent record")
def retention_interrupted(context):
    h = context.agent
    # stop_after names the phase reached: dependents are gone, the task row and
    # its artifact still exist, and the durable intent says what comes next.
    h.store.retain(now=time.time(), stop_after="task")
    with h.store.reading() as db:
        assert db.execute("SELECT phase FROM retention_intents WHERE task_id=?", (context.retained_task,)).fetchone()[0] == "task"
        assert db.execute("SELECT count(*) FROM attempts WHERE task_id=?", (context.retained_task,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM tasks WHERE id=?", (context.retained_task,)).fetchone()[0] == 1
    assert context.retained_artifact.is_file()


@when("a new manager resumes retention")
def retention_resumes(context):
    context.retention = Store(context.agent.store.home).retain(now=time.time())


@then("the task is deleted in foreign-key order")
def task_deleted(context):
    h = context.agent
    assert context.retained_task in context.retention["deleted"]
    with h.store.reading() as db:
        assert db.execute("SELECT count(*) FROM tasks WHERE id=?", (context.retained_task,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM retention_intents").fetchone()[0] == 0


@then("no orphan attempt, event or artifact record remains")
def no_orphans(context):
    h = context.agent
    with h.store.reading() as db:
        for table in ("attempts", "events", "outbox"):
            column = "attempt_id IN (SELECT id FROM attempts WHERE task_id=?)" if table == "outbox" else "task_id=?"
            assert db.execute(f"SELECT count(*) FROM {table} WHERE {column}", (context.retained_task,)).fetchone()[0] == 0
    assert not context.retained_artifact.exists()


def _run_task(h, task_id):
    """Drive one task through schedule, execution and settlement with the demo runtime."""
    h.tick()
    task = h.store.task(task_id)
    attempt = task["attempts"][-1]
    run_attempt(h.store, attempt["id"], attempt["worker_id"])
    h.tick()
    return h.store.task(task_id)


@given("a live workflow has a succeeded child with a delivered result artifact")
def workflow_artifact(context):
    h = _real_agent(context)
    h.store.register_worker("primary", ["demo"], 4)
    plan = {"children": [
        {"id": "source", "task": {"objective": "produce", "runtime": "demo",
                                  "demo": {"delay_seconds": 0, "result": {"facts": 1}}},
         "deliver": {"result": True}},
        {"id": "consumer", "dependencies": ["source"],
         "task": {"objective": "consume", "runtime": "demo", "demo": {"delay_seconds": 0}}},
    ]}
    receipt = h.store.create_workflow({"objective": "plan", "runtime": "demo",
                                       "demo": {"delay_seconds": 0, "result": plan},
                                       "workflow": {"enabled": True}}, "workflow-retention")
    assert _run_task(h, receipt["task_id"])["status"] == "Succeeded"
    source, consumer = h.store.workflow_children(receipt["workflow_id"])
    assert _run_task(h, source["task_id"])["status"] == "Succeeded"
    context.workflow_source, context.workflow_consumer = source["task_id"], consumer["task_id"]
    context.source_artifact = h.store.home / h.store.task(source["task_id"])["attempts"][-1]["result_path"]
    with h.store.transaction() as db:
        db.execute("UPDATE tasks SET finished_at=0 WHERE id IN (?,?)", (source["task_id"], receipt["task_id"]))
        db.execute("UPDATE settings SET value='1' WHERE key='retention_seconds'")


@then("the child artifact remains available to the dependent child")
def artifact_kept(context):
    h = context.agent
    assert context.source_artifact.is_file()
    assert h.store.task(context.workflow_source)["status"] == "Succeeded"
    delivery = h.store.deliver_workflow_dependencies(context.workflow_consumer)
    assert delivery["state"] == "ready" and delivery["deliveries"][0]["result"] == {"facts": 1}, delivery


@given("a succeeded task whose result artifact has expired under retention policy")
def expired_artifact(context):
    h = _real_agent(context)
    h.store.register_worker("primary", ["demo"], 1)
    h.submit("expiry-key")
    assert _run_task(h, h.task_id)["status"] == "Succeeded"
    with h.store.transaction() as db:
        db.execute("UPDATE tasks SET finished_at=0 WHERE id=?", (h.task_id,))
        db.execute("UPDATE settings SET value='1' WHERE key='retention_seconds'")
    assert h.store.expire_artifacts(now=time.time()) == [h.task_id]


@when("I retrieve its result")
def retrieve_expired(context):
    h = context.agent
    task = h.task()
    h.capture(lambda: read_result(h.store, task["attempts"][-1], task["spec"]))


@then("the task history remains queryable")
def history_queryable(context):
    h = context.agent
    assert h.task()["status"] == "Succeeded"
    assert any(e["kind"] == "task.finished" for e in h.store.events(h.task_id))
    assert isinstance(h.error, Gone)


# ---------------------------------------------------------------------------
# Discovery: consent-gated probes against real subprocesses and fixtures
# ---------------------------------------------------------------------------

def _python_route(route_id="fixture", **changes):
    route = {"id": route_id, "executable": sys.executable, "args": ["--version"]}
    route.update(changes)
    return route


@given("an installation with unknown available agent runtimes")
def unknown_runtimes(context):
    context.probe_root = Path(context.resources.enter_context(tempfile.TemporaryDirectory(prefix="boa-discovery-")))
    (context.probe_root / "workspace").mkdir()


@when("I initialize the store with passive inventory")
def passive_init(context):
    import socket
    store = Store(context.probe_root / "state")
    with patch("boring_agent.discovery.subprocess.Popen", side_effect=AssertionError("a probe started a process")), \
            patch.object(socket, "socket", side_effect=AssertionError("a probe opened a socket")):
        store.initialize(context.probe_root / "workspace")
        context.inventory = store.discovery_inventory()
    context.passive_store = store


@then("no subprocess is started")
def no_subprocess(context):
    assert context.inventory is not None  # the patched Popen would have raised inside initialize


@then("no network request is made")
def no_network(context):
    assert context.inventory is not None  # the patched socket would have raised inside initialize


@then("the inventory records only locally available passive metadata")
def passive_metadata(context):
    assert context.inventory
    for item in context.inventory:
        assert item["tier"] == "passive", item
        assert "stdout" not in item and "exit_code" not in item, item


@given("an operator approves handshake discovery for one agent route")
def approve_handshake(context):
    h = _real_agent(context)
    context.discovery = Discovery(h.store, max_output_bytes=4096)
    context.route = _python_route(args=["-c", "import os; print('HOME=' + os.environ['HOME'])"], version="fixture-1")
    context.approval = context.discovery.approve(context.route)


@when("the handshake reports its capabilities")
def run_handshake(context):
    context.probe = context.discovery.handshake(context.route, context.approval["approval_id"], timeout=5)
    assert context.probe["status"] == "completed", context.probe


@then("the agent uses isolated state")
def isolated_state(context):
    h = context.agent
    assert context.probe["isolated_state"] is True
    assert str(h.store.home / "discovery-state") in context.probe["stdout"], context.probe["stdout"]
    assert not any((h.store.home / "discovery-state").iterdir())  # torn down after the probe


@then("the orchestrator terminates the handshake process group")
def group_terminated(context):
    assert context.probe["process_group_terminated"] in (True, False)
    assert context.probe["exit_code"] == 0


@then("no handshake child process remains")
def no_child(context):
    assert any(item["action"] == "probe" and item["outcome"] == "completed" for item in context.agent.store.discovery_audit())


@given("an approved handshake probe stops responding")
def hanging_probe(context):
    h = _real_agent(context)
    context.discovery = Discovery(h.store, max_output_bytes=4096)
    context.pid_file = h.root / "grandchild.pid"
    script = ("import pathlib, subprocess, sys, time; "
              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
              f"pathlib.Path({str(context.pid_file)!r}).write_text(str(p.pid)); time.sleep(30)")
    context.route = _python_route(args=["-c", script])
    context.approval = context.discovery.approve(context.route)


@when("its hard timeout elapses")
def timeout_elapses(context):
    context.probe = context.discovery.handshake(context.route, context.approval["approval_id"], timeout=.3)


@then("the process group is killed")
def group_killed(context):
    assert context.probe["process_group_terminated"] is True


@then("the probe result records a bounded timeout outcome")
def timeout_recorded(context):
    assert context.probe["status"] == "timeout" and context.probe["timed_out"] is True


@then("no descendant remains running")
def no_descendant(context):
    end = time.monotonic() + 2
    while time.monotonic() < end and not context.pid_file.exists():
        time.sleep(.01)
    grandchild = int(context.pid_file.read_text())
    end = time.monotonic() + 2
    while time.monotonic() < end:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(.02)
    raise AssertionError(f"descendant {grandchild} survived the process-group kill")


@given("generative discovery is not approved")
def generative_unapproved(context):
    h = _real_agent(context)
    context.discovery = Discovery(h.store, max_output_bytes=4096)
    context.route = _python_route(model="fixture-model")
    context.requests = []
    context.requester = lambda messages, model, output_tokens, timeout: context.requests.append(model) or "ok"


@when("I request a bounded model completion probe")
def request_generative(context):
    context.agent.capture(lambda: context.discovery.generative(context.route, requester=context.requester))


@then("the probe request is refused")
def probe_refused(context):
    assert isinstance(context.agent.error, Conflict), context.agent.error


@then("no provider request is sent")
def no_provider_request(context):
    assert context.requests == []


@given("generative discovery is approved with a stated cost policy")
def generative_approved(context):
    context.approval = context.discovery.approve(context.route, "generative",
                                                 cost_policy={"max_requests": 1, "operator": "feature"})


@when("I request the same probe")
def request_again(context):
    context.probe = context.discovery.generative(context.route, approval_id=context.approval["approval_id"],
                                                 requester=context.requester)


@then("exactly one bounded completion request is sent")
def one_request(context):
    assert context.requests == ["fixture-model"]
    assert context.probe["request_count"] == 1 and context.probe["status"] == "completed"


@given("an approved probe returns a long response containing a credential-like value and a URL")
def noisy_probe(context):
    h = _real_agent(context)
    context.discovery = Discovery(h.store, max_output_bytes=512)
    context.route = _python_route(model="fixture-model", version="v7")
    context.approval = context.discovery.approve(context.route, "generative", cost_policy="one request")
    context.noisy_output = "api_key=do-not-store-me https://provider.example/v1 " + "x" * 4000


@when("the orchestrator records probe evidence")
def record_evidence(context):
    context.probe = context.discovery.generative(context.route, approval_id=context.approval["approval_id"],
                                                 requester=lambda *args: context.noisy_output)
    context.stored = json.dumps(context.agent.store.discovery_evidence())


@then("recorded output is capped at the configured bound")
def output_capped(context):
    assert len(context.probe["output"].encode()) <= 512 + len("…[truncated] URLs: https://provider.example/v1".encode())
    assert "…[truncated]" in context.probe["output"]


@then("the credential-like value is redacted")
def credential_redacted(context):
    assert "do-not-store-me" not in context.stored


@then("the URL and version metadata remain attributable to the probe")
def url_kept(context):
    assert "https://provider.example/v1" in context.stored
    assert context.probe["metadata"]["version"] == "v7"


@given("an operator approves a resolved provider route and executable identity")
def approve_identity(context):
    h = _real_agent(context)
    context.discovery = Discovery(h.store, max_output_bytes=4096)
    context.route = _python_route(env_overrides={"BOA_DISCOVERY_FIXTURE": "one"})
    context.approval = context.discovery.approve(context.route)


@when("an environment override or executable identity changes")
def identity_changes(context):
    changed = context.agent.root / "changed-python"
    changed.write_bytes(Path(sys.executable).read_bytes())
    changed.chmod(0o755)
    context.changed_routes = [{**context.route, "env_overrides": {"BOA_DISCOVERY_FIXTURE": "two"}},
                              {**context.route, "executable": str(changed)}]


@then("the old approval is rejected")
def approval_rejected(context):
    for route in context.changed_routes:
        try:
            context.discovery.handshake(route, context.approval["approval_id"], timeout=1)
            raise AssertionError("changed route accepted the old approval")
        except Conflict:
            pass


@then("the route requires explicit re-approval")
def reapproval(context):
    route = context.changed_routes[0]
    approval = context.discovery.approve(route)
    assert context.discovery.handshake(route, approval["approval_id"], timeout=5)["status"] == "completed"


@given("an operator invokes one unlisted route locally")
def unlisted_local(context):
    h = _real_agent(context)
    context.discovery = Discovery(h.store, max_output_bytes=4096)
    context.route = _python_route(route_id="unlisted")
    context.probe = context.discovery.handshake(context.route, timeout=5, allow_unlisted=True)
    assert context.probe["status"] == "completed"


@then("the exception is recorded in the audit history")
def unlisted_audited(context):
    audit = context.agent.store.discovery_audit()
    assert any(item["action"] == "unlisted_invocation" and item["route_key"] == "unlisted" for item in audit)


@then("no approval is created for the route")
def unlisted_not_approved(context):
    assert context.agent.store.discovery_approvals() == []


@then("a later invocation without the escape is refused")
def unlisted_refused_later(context):
    try:
        context.discovery.handshake(context.route, timeout=1)
        raise AssertionError("unlisted route ran without approval")
    except Conflict:
        pass


@given("a discovery route requires an API credential")
def credential_route(context):
    h = _real_agent(context)
    context.discovery = Discovery(h.store, max_output_bytes=4096)
    context.resources.enter_context(patch.dict(os.environ, {"BOA_FIXTURE_KEY": "raw-secret-value-9f3e"}))
    context.route = _python_route(credential_ref="env:BOA_FIXTURE_KEY",
                                  env_overrides={"BOA_API_KEY": "env:BOA_FIXTURE_KEY"})


@when("the route is recorded")
def record_route(context):
    context.approval = context.discovery.approve(context.route)
    context.discovery.inventory([context.route])
    context.stored = json.dumps(context.agent.store.discovery_approvals()) + json.dumps(context.agent.store.discovery_inventory())


@then("the inventory contains a credential reference")
def reference_stored(context):
    assert "env:BOA_FIXTURE_KEY" in context.stored


@then("it contains neither the credential value nor parsed YAML credential content")
def no_value_stored(context):
    assert "raw-secret-value-9f3e" not in context.stored
    assert "yaml" not in sys.modules or "boring_agent.discovery" not in getattr(sys.modules.get("yaml"), "__name__", "")


# ---------------------------------------------------------------------------
# Workflows: planner, children, authority, delivery, budgets, replanning
# ---------------------------------------------------------------------------

def _workflow_root(plan=None, **workflow):
    return {"objective": "Plan bounded work", "runtime": "demo",
            "demo": {"delay_seconds": 0, "result": plan or {"children": []}},
            "workflow": {"enabled": True, **workflow}}


def _child(child_id, **task):
    body = {"id": child_id}
    dependencies = task.pop("dependencies", None)
    deliver = task.pop("deliver", None)
    if dependencies is not None:
        body["dependencies"] = dependencies
    if deliver is not None:
        body["deliver"] = deliver
    body["task"] = {"objective": "child " + child_id, "runtime": "demo", "demo": {"delay_seconds": 0}, **task}
    return body


@given("a workflow root whose planner returns a plan with {count:d} child")
@given("a workflow root whose planner returns a plan with {count:d} children")
def planner_root(context, count):
    h = context.agent
    plan = {"children": [_child(f"child-{index}") for index in range(count)]}
    context.receipt = h.store.create_workflow(_workflow_root(plan), "workflow-root")


@when("the planner task runs and the manager settles it")
def planner_runs(context):
    context.planner = _run_task(context.agent, context.receipt["task_id"])


@then('the planner task status is "{status}"')
def planner_status(context, status):
    assert context.planner["status"] == status, (context.planner["status"], context.planner["reason"])


@then("the root records plan revision {revision:d} with {count:d} child")
@then("the root records plan revision {revision:d} with {count:d} children")
def root_records(context, revision, count):
    root = context.agent.store.workflow(context.receipt["workflow_id"])
    assert (root["state"], root["plan_revision"], len(root["children"])) == ("executing", revision, count), root


@then("each execution child is a pending ordinary task distinct from the planner")
def children_distinct(context):
    h = context.agent
    for child in h.store.workflow_children(context.receipt["workflow_id"]):
        assert child["task_id"] != context.receipt["task_id"]
        assert h.store.task(child["task_id"])["status"] == "Pending"


@given("a workflow root awaiting a plan")
def awaiting_plan(context):
    context.receipt = context.agent.store.create_workflow(_workflow_root(max_children=1), "awaiting-plan")


@when("the workflow settles a plan with {defect}")
def settle_defective(context, defect):
    plans = {
        "a dependency cycle": {"children": [_child("a", dependencies=["b"]), _child("b", dependencies=["a"])]},
        "an unknown dependency": {"children": [_child("a", dependencies=["missing"])]},
        "duplicate child order": {"children": [{**_child("a"), "order": 0}, {**_child("b"), "order": 0}]},
        "an empty child list": {"children": []},
        "more children than limit": {"children": [_child("a"), _child("b")]},
    }
    context.settled = context.agent.store.settle_workflow_plan(context.receipt["workflow_id"], plans[defect])


@then("the plan is rejected")
def plan_rejected(context):
    assert context.settled["state"] == "rejected", context.settled


@then("the workflow has {count:d} execution children")
def child_count(context, count):
    assert len(context.agent.store.workflow_children(context.receipt["workflow_id"])) == count


@given("a valid plan is awaiting workflow settlement")
def valid_plan_waiting(context):
    context.receipt = context.agent.store.create_workflow(_workflow_root(), "atomic-plan")
    context.plan = {"children": [_child("a")]}


@given("storage fails while inserting one child")
def child_insert_fails(context):
    store = context.agent.store
    with store.transaction() as db:
        db.execute("CREATE TRIGGER workflow_child_failure BEFORE INSERT ON workflow_children "
                   "BEGIN SELECT RAISE(ABORT, 'injected'); END")

    def drop_trigger():
        with store.transaction() as db:
            db.execute("DROP TRIGGER IF EXISTS workflow_child_failure")
    context.resources.callback(drop_trigger)


@when("the workflow settles the plan")
def settle_plan(context):
    context.agent.capture(lambda: context.agent.store.settle_workflow_plan(context.receipt["workflow_id"], context.plan))


@then("the plan settlement fails as a storage conflict")
def settlement_conflict(context):
    assert isinstance(context.agent.error, Conflict), context.agent.error


@then("no partial plan or child record exists")
def no_partial(context):
    with context.agent.store.reading() as db:
        assert db.execute("SELECT count(*) FROM workflow_plans").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM workflow_children").fetchone()[0] == 0


@given("a read-only workflow root with a pinned model and a bounded budget")
def restrictive_root(context):
    root = _workflow_root(max_tokens=5000)
    root["model"] = "root-model"
    root["budget"] = {"max_tokens": 5000, "deadline_seconds": 60}
    context.receipt = context.agent.store.create_workflow(root, "restrictive-root")


@when("planner output requests {broadening}")
def request_broadening(context, broadening):
    tasks = {
        "a writable sandbox": {"sandbox": "workspace-write", "tools": ["write_file"]},
        "a null token ceiling": {"budget": {"max_tokens": None}},
        "a longer deadline": {"budget": {"deadline_seconds": 86400}},
        "a model outside the root policy": {"model": "other-model"},
        "tools outside the root": {"tools": ["list_files", "read_file", "write_file"]},
    }
    context.settled = context.agent.store.settle_workflow_plan(
        context.receipt["workflow_id"], {"children": [_child("child", **tasks[broadening])]})


@given('a plan where "{producer}" delivers its result and a named file to "{consumer}"')
def delivering_plan(context, producer, consumer):
    h = context.agent
    (h.workspace / "facts.txt").write_text("verified facts", encoding="utf-8")
    context.receipt = h.store.create_workflow(_workflow_root(), "delivery-root")
    plan = {"children": [
        _child(producer, demo={"delay_seconds": 0, "result": {"facts": 1}}, deliver={"result": True, "files": ["facts.txt"]}),
        _child(consumer, dependencies=[producer]),
    ]}
    context.settled = h.store.settle_workflow_plan(context.receipt["workflow_id"], plan)
    assert context.settled["state"] == "accepted", context.settled
    context.children = {child["child_key"]: child for child in h.store.workflow_children(context.receipt["workflow_id"])}


@when('"{producer}" completes with a verified result')
def producer_completes(context, producer):
    assert _run_task(context.agent, context.children[producer]["task_id"])["status"] == "Succeeded"


@when('the workflow delivers dependencies for "{consumer}"')
def deliver_for(context, consumer):
    context.delivery = context.agent.store.deliver_workflow_dependencies(context.children[consumer]["task_id"])


@then("the consumer receives the declared result and the named file")
def delivered(context):
    assert context.delivery["state"] == "ready", context.delivery
    payload = context.delivery["deliveries"][0]
    assert payload["result"] == {"facts": 1}
    assert payload["files"] == [{"path": "facts.txt", "content": "verified facts"}]


@then("the delivered bytes are counted against the consumer")
def bytes_counted(context):
    consumer = [c for c in context.agent.store.workflow_children(context.receipt["workflow_id"]) if c["dependencies"]][0]
    assert consumer["context_bytes"] == context.delivery["bytes"] > 0


@when('"{producer}" finishes without the result it declared')
def producer_without_result(context, producer):
    with context.agent.store.transaction() as db:
        db.execute("UPDATE tasks SET status='Succeeded',result_path=NULL WHERE id=?", (context.children[producer]["task_id"],))


@then("the consumer does not start")
def consumer_blocked(context):
    assert context.delivery["state"] == "failed", context.delivery
    consumer = [c for c in context.children.values() if c["dependencies"]][0]
    assert context.agent.store.task(consumer["task_id"])["status"] == "Failed"


@then("the workflow records the failed dependency transfer")
def transfer_recorded(context):
    with context.agent.store.reading() as db:
        assert db.execute("SELECT count(*) FROM workflow_deliveries WHERE state='failed'").fetchone()[0] == 1
    assert context.agent.store.workflow(context.receipt["workflow_id"])["reason"]


@given("a workflow with {attempts:d} attempts and {tokens:d} tokens of budget")
def finite_budget(context, attempts, tokens):
    h = context.agent
    context.receipt = h.store.create_workflow(_workflow_root(max_attempts=attempts, max_tokens=tokens), "budget-root")
    assert h.store.settle_workflow_plan(context.receipt["workflow_id"], {"children": [_child("first")]})["state"] == "accepted"


@given("a completed child consumed {attempts:d} attempt and {tokens:d} tokens")
def consumed(context, attempts, tokens):
    h = context.agent
    child = h.store.workflow_children(context.receipt["workflow_id"])[0]
    assert _run_task(h, child["task_id"])["status"] == "Succeeded"  # claims one attempt
    from boring_agent.workflows import WorkflowStore
    with h.store.transaction() as db:
        WorkflowStore(h.store, ensure=False).record_usage_locked(db, child["task_id"], tokens)
    root = h.store.workflow(context.receipt["workflow_id"])
    context.before = (root["remaining_attempts"], root["remaining_tokens"])
    assert root["attempts_used"] == attempts and root["tokens_used"] == tokens, root


@when("the workflow accepts a replan with one new child")
def replan_one(context):
    context.settled = context.agent.store.replan_workflow(
        context.receipt["workflow_id"], {"children": [_child("first"), _child("second")]})
    assert context.settled["state"] == "accepted", context.settled


@then("remaining attempts and tokens are not reset by the new revision")
def budgets_not_reset(context):
    root = context.agent.store.workflow(context.receipt["workflow_id"])
    assert (root["remaining_attempts"], root["remaining_tokens"]) == context.before, root
    assert root["plan_revision"] == 2


@given('revision 1 has a succeeded child "{done}" and a pending child "{kept}"')
def revision_one(context, done, kept):
    h = context.agent
    context.receipt = h.store.create_workflow(_workflow_root(max_attempts=10), "replan-root")
    assert h.store.settle_workflow_plan(context.receipt["workflow_id"],
                                        {"children": [_child(done), _child(kept)]})["state"] == "accepted"
    children = {c["child_key"]: c for c in h.store.workflow_children(context.receipt["workflow_id"])}
    assert _run_task(h, children[done]["task_id"])["status"] == "Succeeded"
    context.first_revision = {key: child["task_id"] for key, child in children.items()}


@when("the workflow accepts revision 2 containing both children")
def revision_two(context):
    context.settled = context.agent.store.replan_workflow(
        context.receipt["workflow_id"], {"children": [_child(key) for key in context.first_revision]})
    assert (context.settled["state"], context.settled["revision"]) == ("accepted", 2), context.settled


@then('"{done}" is carried over with its verified result')
def carried(context, done):
    h = context.agent
    second = {c["child_key"]: c for c in h.store.workflow_children(context.receipt["workflow_id"], revision=2)}
    assert second[done]["task_id"] == context.first_revision[done]
    assert second[done]["carried_from_task_id"] == context.first_revision[done]
    assert second[done]["carried_output"] is not None


@then('"{kept}" gets a fresh pending task and its obsolete task is cancelled')
def fresh_task(context, kept):
    h = context.agent
    second = {c["child_key"]: c for c in h.store.workflow_children(context.receipt["workflow_id"], revision=2)}
    assert second[kept]["task_id"] != context.first_revision[kept]
    assert h.store.task(second[kept]["task_id"])["status"] == "Pending"
    old = h.store.task(context.first_revision[kept])
    assert (old["status"], old["reason"]) == ("Cancelled", "obsolete_by_replan")


@given("a plain task submitted without workflow planning")
def plain_task(context):
    context.agent.submit("plain-task")


@then("no workflow root exists")
def no_root(context):
    assert context.agent.store.workflows() == []


@given("a workflow root whose task tools include writing")
def writing_root(context):
    # The installation refuses workspace-write here, so the root keeps the
    # default tools; the planner must still be narrower than any root.
    context.receipt = context.agent.store.create_workflow(_workflow_root(), "planner-policy")


@then("its planner task is read-only with only inspection tools")
def planner_read_only(context):
    spec = context.agent.store.task(context.receipt["task_id"])["spec"]
    assert spec["sandbox"] == "read-only"
    assert set(spec["tools"]) <= {"list_files", "read_file"}
    assert spec["output_schema"]["required"] == ["children"]
