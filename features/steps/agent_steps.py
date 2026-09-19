"""Domain-language steps call production behavior, not unittest test methods."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time
from unittest.mock import patch

from behave import given, when, then

from boring_agent.artifacts import read_result
from boring_agent.manager import Manager
from boring_agent.model import Conflict
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
