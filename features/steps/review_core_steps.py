"""Steps for features/review_core.feature (core review findings F5, F6, F13, F17, F18)."""
import json
import os
import sqlite3
import time
from unittest.mock import patch

from behave import given, when, then

from boring_agent import store as store_module
from boring_agent.runner import run_attempt
from boring_agent.store import Store
from tests.support.http_provider import completion, server


def _run_llm(context, **changes):
    h = context.agent
    base, context.requests, _, _ = context.resources.enter_context(server(context.responses))
    context.resources.enter_context(patch.dict(os.environ, {
        "BOA_PROVIDER": "openai", "BOA_BASE_URL": base,
        "BOA_MODEL": "fixture-model", "BOA_API_KEY": "fixture-only"}))
    h.submit(runtime="llm", demo={}, budget={"request_seconds": 2}, **changes)
    h.tick()
    h.deliver()
    h.tick()


def _tool_results(context):
    results = []
    for request in context.requests:
        for message in (request["body"] or {}).get("messages", []):
            content = message.get("content")
            if message.get("role") == "user" and isinstance(content, str) and content.startswith("Tool result"):
                results.append(content)
    return results


def _last_tool_error(context):
    results = _tool_results(context)
    assert results, "no tool result reached the model"
    payload = json.loads(results[-1].split(": ", 1)[1])
    assert "error" in payload, payload
    return payload["error"]


@given("the fixture model first reads a path containing a NUL byte then returns a final answer")
def nul_read_script(context):
    context.responses = [(200, completion("openai", {"action": "read_file", "path": "a\u0000b"})),
                         (200, completion("openai", {"action": "final", "result": {"answer": 42}}))]


@given("the installation and worker permit workspace writes")
def permit_writes(context):
    h = context.agent
    with h.store.transaction() as db:
        db.execute("UPDATE settings SET value='true' WHERE key='allow_write'")
    h.store.register_worker("provider", ["llm"], 1, allow_write=True)


@given("the fixture model first writes beneath a regular file then returns a final answer")
def write_under_file_script(context):
    context.responses = [(200, completion("openai", {"action": "write_file", "path": "input.txt/child.txt",
                                                     "content": "x"})),
                         (200, completion("openai", {"action": "final", "result": {"answer": 42}}))]


@when("the core-review LLM agent runs against the fixture")
def core_llm(context):
    _run_llm(context)


@when("the core-review writing LLM agent runs against the fixture")
def core_writing_llm(context):
    _run_llm(context, sandbox="workspace-write", tools=["read_file", "write_file"])


@then("the model received a tool error for its NUL path")
def nul_tool_error(context):
    message = _last_tool_error(context)
    assert "NUL" in message, message


@then('the model received a tool error naming the relative path "{path}"')
def relative_tool_error(context, path):
    message = _last_tool_error(context)
    assert path in message, message


@then("no tool result sent to the model contains an absolute host path")
def no_host_path(context):
    h = context.agent
    roots = {str(h.root), str(h.root.resolve()), str(h.workspace.resolve()), str(h.store.home)}
    for result in _tool_results(context):
        for root in roots:
            assert root not in result, result


@when("I submit a task expecting a file path containing a NUL byte")
def submit_nul_expectation(context):
    context.agent.capture(lambda: context.agent.submit(expect_files=["a\u0000b"]))


@given("two demo tasks have produced valid results awaiting settlement")
def two_awaiting(context):
    h = context.agent
    context.awaiting = [h.submit("awaiting-1")["task_id"], h.submit("awaiting-2")["task_id"]]
    h.tick()
    for task_id in context.awaiting:
        attempt = h.store.task(task_id)["attempts"][-1]
        run_attempt(h.store, attempt["id"], attempt["worker_id"])
        assert h.store.task(task_id)["attempts"][-1]["state"] == "Succeeded"


@given("the first awaiting task's result artifact has vanished")
def vanished_artifact(context):
    h = context.agent
    attempt = h.store.task(context.awaiting[0])["attempts"][-1]
    (h.store.home / attempt["result_path"]).unlink()


@then("the first awaiting task fails acceptance")
def first_rejected(context):
    h = context.agent
    task = h.store.task(context.awaiting[0])
    assert task["status"] == "Failed", (task["status"], task["reason"])
    assert task["result_path"] is None
    assert "attempt.result_rejected" in [e["kind"] for e in h.store.events(task["id"])]


@then("the second awaiting task is accepted")
def second_accepted(context):
    task = context.agent.store.task(context.awaiting[1])
    assert task["status"] == "Succeeded", (task["status"], task["reason"])


@given("retention is interrupted right after its dependents phase")
def retention_after_dependents(context):
    h = context.agent
    h.store.retain(now=time.time(), stop_after="dependents")
    with h.store.reading() as db:
        intent = db.execute("SELECT phase FROM retention_intents WHERE task_id=?",
                            (context.retained_task,)).fetchone()
        assert intent is not None and intent[0] == "task", intent and tuple(intent)
        assert db.execute("SELECT count(*) FROM events WHERE task_id=?", (context.retained_task,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM tasks WHERE id=?", (context.retained_task,)).fetchone()[0] == 1


@given("a cancel with a new key arrives for the retained terminal task")
def late_cancel(context):
    h = context.agent
    receipt = h.store.cancel(context.retained_task, "late-cancel-after-dependents")
    assert receipt["duplicate"] is False


@given("the store records an unsupported future schema version")
def future_schema(context):
    with context.agent.store.transaction() as db:
        db.execute("PRAGMA user_version=999")


@when("a process tries to open that store while connections are tracked")
def tracked_open(context):
    h = context.agent
    context.connections = []
    real_connect = sqlite3.connect

    def tracking(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        context.connections.append(connection)
        return connection

    with patch.object(store_module.sqlite3, "connect", side_effect=tracking):
        h.capture(lambda: Store(h.store.home).settings())


@then("every tracked database connection is closed")
def tracked_closed(context):
    assert context.connections, "no connection was opened"
    for connection in context.connections:
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue
        raise AssertionError("a database connection was left open")
