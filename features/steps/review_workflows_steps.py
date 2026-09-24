"""Steps for features/review_workflows.feature.

They drive the production Store, Manager, WorkflowStore and HTTP API. SQL is used
only for deliberate fault injection and for reading durable rows.
"""
from contextlib import contextmanager
import http.client
import json
import os
import threading

from behave import given, when, then

from boring_agent.api import make_server
from boring_agent.process import identity
from boring_agent.workflows import WorkflowStore


SESSION = "@session:sess_" + "a" * 24
OTHER_SESSION = "@session:sess_" + "b" * 24


def _root(kind, **workflow):
    if kind == "plain demo":
        raw = {"objective": "Plan bounded work", "runtime": "demo",
               "demo": {"delay_seconds": 0, "result": {"children": []}}}
    elif kind == "plain llm":
        raw = {"objective": "Plan bounded work", "runtime": "llm"}
    elif kind == "coddy llm":
        raw = {"objective": "Plan bounded work", "runtime": "llm",
               "coddy": {"session": SESSION, "permission_mode": "accept_edits"}}
    else:
        raise AssertionError(f"unknown root kind {kind}")
    raw["workflow"] = {"enabled": True, **workflow}
    return raw


def _demo_child(child_id, **task):
    return {"id": child_id, "task": {"objective": "child " + child_id, "runtime": "demo",
                                     "demo": {"delay_seconds": 0}, **task}}


ESCALATIONS = {
    "switches to the llm runtime": {"objective": "escalate", "runtime": "llm", "demo": {}},
    "chooses its own model": {"objective": "escalate", "runtime": "demo", "model": "planner-picked-model"},
    "declares a nested workflow": {"objective": "escalate", "runtime": "demo",
                                   "workflow": {"enabled": True, "max_children": 5}},
    "resumes a coddy session": {"objective": "escalate", "runtime": "llm",
                                "coddy": {"session": SESSION, "permission_mode": "bypass"}},
    "mentions a bypass exec subagent": {"objective": "escalate", "runtime": "llm",
                                        "coddy": {"mention": {"agent": "exec", "permission_mode": "bypass"}}},
    "widens the coddy permission mode to bypass": {"objective": "escalate", "runtime": "llm",
                                                   "coddy": {"permission_mode": "bypass"}},
    "switches to another coddy session": {"objective": "escalate", "runtime": "llm",
                                          "coddy": {"session": OTHER_SESSION}},
    "adds a bypass exec mention": {"objective": "escalate", "runtime": "llm",
                                   "coddy": {"mention": {"agent": "exec", "permission_mode": "bypass"}}},
    "narrows the coddy permission mode to ask": {"objective": "narrow", "runtime": "llm",
                                                 "coddy": {"permission_mode": "ask"}},
}


@given('a planner-trust workflow root of kind "{kind}"')
def planner_trust_root(context, kind):
    context.receipt = context.agent.store.create_workflow(_root(kind), "planner-trust-" + kind)


@when("the untrusted plan proposes a child that {escalation}")
def untrusted_child(context, escalation):
    plan = {"children": [{"id": "child", "task": ESCALATIONS[escalation]}]}
    context.settled = context.agent.store.settle_workflow_plan(context.receipt["workflow_id"], plan)


@then("the untrusted plan is accepted")
def untrusted_accepted(context):
    assert context.settled["state"] == "accepted", context.settled


@then('the narrowed child keeps the root coddy session with permission mode "{mode}"')
def narrowed_child(context, mode):
    child = context.agent.store.workflow_children(context.receipt["workflow_id"])[0]
    coddy = context.agent.store.task(child["task_id"])["spec"]["coddy"]
    assert (coddy["session"], coddy["permission_mode"], coddy["mention"]) == (SESSION, mode, None), coddy


@given("a token-bounded review workflow with a ceiling of {tokens:d} tokens")
def token_bounded_root(context, tokens):
    context.receipt = context.agent.store.create_workflow(
        _root("plain demo", max_tokens=tokens, max_attempts=10), "token-bounded-review")
    context.token_ceiling = tokens


@given("the untrusted plan proposes {count:d} children that each request {tokens:d} tokens")
@when("the untrusted plan proposes {count:d} children that each request {tokens:d} tokens")
def children_request_tokens(context, count, tokens):
    plan = {"children": [_demo_child(f"c{index}", budget={"max_tokens": tokens}) for index in range(count)]}
    context.settled = context.agent.store.settle_workflow_plan(context.receipt["workflow_id"], plan)


@when("the untrusted plan proposes {count:d} children without token ceilings")
def children_without_ceiling(context, count):
    plan = {"children": [_demo_child(f"c{index}") for index in range(count)]}
    context.settled = context.agent.store.settle_workflow_plan(context.receipt["workflow_id"], plan)


@then("every review child has a token ceiling and together they total at most {tokens:d}")
def children_share(context, tokens):
    store = context.agent.store
    ceilings = [store.task(child["task_id"])["spec"]["budget"]["max_tokens"]
                for child in store.workflow_children(context.receipt["workflow_id"])]
    assert ceilings and all(isinstance(value, int) and value >= 1 for value in ceilings), ceilings
    assert sum(ceilings) <= tokens, ceilings


@when("the first review child reports {tokens:d} tokens of usage")
def first_child_usage(context, tokens):
    assert context.settled["state"] == "accepted", context.settled
    store = context.agent.store
    first = store.workflow_children(context.receipt["workflow_id"])[0]
    with store.transaction() as db:
        # The manager's settlement path records reported child usage this way.
        WorkflowStore(store, ensure=False).record_usage_locked(db, first["task_id"], tokens)


@when("the manager schedules the pending review children")
def schedule_review_children(context):
    context.agent.tick()


@then('the second review child fails with "{reason}" before any attempt')
def second_child_blocked(context, reason):
    store = context.agent.store
    second = store.workflow_children(context.receipt["workflow_id"])[1]
    task = store.task(second["task_id"])
    assert (task["status"], task["reason"], task["attempts"]) == ("Failed", reason, []), \
        (task["status"], task["reason"], len(task["attempts"]))


# --- HTTP plan command -----------------------------------------------------

def _serve(context):
    server = make_server(context.agent.store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    context.resources.callback(stop)
    context.server = server


def _post(context, route, payload, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = key
    connection = http.client.HTTPConnection(*context.server.server_address, timeout=5)
    try:
        connection.request("POST", f"/api/v1/workflows/{context.receipt['workflow_id']}/{route}",
                           body=json.dumps(payload), headers=headers)
        response = connection.getresponse()
        value = (response.status, json.loads(response.read()))
    finally:
        connection.close()
    context.posts = getattr(context, "posts", []) + [value]
    return value


@given("a review workflow root served over the HTTP API")
def served_root(context):
    context.receipt = context.agent.store.create_workflow(_root("plain demo", max_attempts=10), "served-review")
    _serve(context)


@when('the plan for child "{child}" is posted twice with idempotency key "{key}"')
def post_twice(context, child, key):
    plan = {"children": [_demo_child(child)]}
    _post(context, "plan", plan, key)
    _post(context, "plan", plan, key)


@when('the plan for child "{child}" is posted with idempotency key "{key}"')
def post_with_key(context, child, key):
    _post(context, "plan", {"children": [_demo_child(child)]}, key)


@when('the plan for child "{child}" is posted without an idempotency key')
def post_without_key(context, child):
    _post(context, "plan", {"children": [_demo_child(child)]})


@when('an invalid replan is posted with idempotency key "{key}"')
def post_invalid_replan(context, key):
    cycle = {"children": [{**_demo_child("x"), "dependencies": ["y"]}, {**_demo_child("y"), "dependencies": ["x"]}]}
    _post(context, "replan", cycle, key)


@then("both plan posts return the same receipt and the second is marked duplicate")
def same_receipt(context):
    (first_status, first), (second_status, second) = context.posts[-2:]
    assert (first_status, second_status) == (202, 202), context.posts
    assert first.get("state") == "accepted" and first.get("duplicate") is False, first
    assert {**first, "duplicate": True} == second, (first, second)


@then("the last plan post is answered with HTTP {status:d}")
def last_status(context, status):
    assert context.posts[-1][0] == status, context.posts[-1]


@then("the last plan post records a rejected revision")
def last_rejected(context):
    status, body = context.posts[-1]
    assert (status, body.get("state")) == (202, "rejected"), context.posts[-1]


@then('the served workflow has {plans:d} plan revision and {tasks:d} task for child "{child}"')
def served_counts(context, plans, tasks, child):
    store = context.agent.store
    root = store.workflow(context.receipt["workflow_id"])
    children = [c for c in root["children"] if c["child_key"] == child]
    assert len(root["plans"]) == plans, root["plans"]
    assert len({c["task_id"] for c in children}) == tasks, children
    assert len(root["children"]) == tasks, root["children"]


@then('the served workflow is still executing with child "{child}" pending')
def still_executing(context, child):
    store = context.agent.store
    root = store.workflow(context.receipt["workflow_id"])
    assert root["state"] == "executing", (root["state"], root["reason"])
    current = [c for c in root["children"] if c["child_key"] == child]
    assert current and store.task(current[-1]["task_id"])["status"] == "Pending", current


# --- Replan of a launched child -------------------------------------------

@given('a review workflow whose child "{child}" has a launched attempt')
def launched_child(context, child):
    h = context.agent
    context.receipt = h.store.create_workflow(_root("plain demo", max_attempts=10), "launched-review")
    assert h.store.settle_workflow_plan(context.receipt["workflow_id"],
                                        {"children": [_demo_child(child)]})["state"] == "accepted"
    task_id = h.store.workflow_children(context.receipt["workflow_id"])[0]["task_id"]
    h.tick()
    attempt = h.store.task(task_id)["attempts"][-1]
    assert h.store.observe(attempt["id"], attempt["worker_id"], 1, "Launching",
                           runner_pid=os.getpid(), runner_start=identity(os.getpid()))
    context.launched = {"task_id": task_id, "attempt_id": attempt["id"], "worker_id": attempt["worker_id"]}


@when('the workflow accepts a replan that drops "{child}"')
def replan_drops(context, child):
    context.settled = context.agent.store.replan_workflow(
        context.receipt["workflow_id"], {"children": [_demo_child("replacement")]})
    assert context.settled["state"] == "accepted", context.settled


def _launched_attempt(context):
    task = context.agent.store.task(context.launched["task_id"])
    return task, [a for a in task["attempts"] if a["id"] == context.launched["attempt_id"]][0]


@then('"{child}" is asked to cancel but is not yet reported cancelled')
def asked_to_cancel(context, child):
    task, _ = _launched_attempt(context)
    assert task["status"] != "Cancelled", (task["status"], task["reason"])
    assert task["desired_action"] == "Cancel", task["desired_action"]


@then('the launched attempt of "{child}" still holds its reservation')
def reservation_held(context, child):
    _, attempt = _launched_attempt(context)
    assert (attempt["state"], attempt["reserved"]) == ("Launching", 1), attempt


@when('the runner confirms that "{child}" stopped')
def runner_confirms(context, child):
    h = context.agent
    assert h.store.observe(context.launched["attempt_id"], context.launched["worker_id"], 2, "Cancelled",
                           error_kind="cancelled", error_message="stopped on request")
    h.tick()


@then('"{child}" is cancelled and its reservation is released')
def cancelled_released(context, child):
    task, attempt = _launched_attempt(context)
    assert task["status"] == "Cancelled", (task["status"], task["reason"])
    assert attempt["reserved"] == 0, attempt


# --- Workflow schema setup inside a transaction ----------------------------

@when("a transaction records a marker, ensures the workflow schema and then fails")
def schema_in_failing_transaction(context):
    store = context.agent.store
    real = store.transaction

    class InjectedFailure(Exception):
        pass

    @contextmanager
    def failing_transaction():
        with real() as db:
            db.execute("INSERT INTO settings(key,value) VALUES('review_marker','1')")
            yield db
            raise InjectedFailure("fault injected after workflow schema setup")

    store.transaction = failing_transaction
    try:
        WorkflowStore(store)
    except InjectedFailure:
        context.schema_failure = True
    else:
        context.schema_failure = False
    finally:
        del store.transaction


@then("the marker is rolled back with the rest of that transaction")
def marker_rolled_back(context):
    assert context.schema_failure
    with context.agent.store.reading() as db:
        row = db.execute("SELECT value FROM settings WHERE key='review_marker'").fetchone()
    assert row is None, "workflow schema setup committed the surrounding transaction early"
