"""Steps for fail-closed Coddy permission and native session lifecycle evidence."""
from contextlib import contextmanager
import json

from behave import given, then, when

from boring_agent.model import SessionConflict
from boring_agent.providers import Provider
from boring_agent.session_lifecycle import SessionLifecycle
from tests.support.http_provider import coddy_stream, json_response, server


FIXTURE_SESSION = "sess_0123456789abcdef01234567"


def _catalog():
    return json_response(200, {"data": [{"id": "fixture-model", "max_context_tokens": 131072}]})


def _native_job(context, job_id, dependencies=None, **extra):
    return {
        "id": job_id,
        "objective": "Review fixture work",
        "runtime": "coddy_native",
        "model": "codex/gpt-5.6-luna",
        "workspace": str(context.agent.workspace),
        "dependencies": dependencies or [],
        **extra,
    }


def _truncated_stream(content, session_id):
    """A Coddy stream that never reaches ``data: [DONE]``."""
    frame = "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": content},
                                                "finish_reason": None}]})
    return 200, "text/event-stream", frame + "\n\n", {"X-Coddy-Session-ID": session_id}


def _coddy_warmup_executor(context, base, session_id):
    provider = Provider("coddy", base + "/v1", "fixture-model", "fixture-only", session_id=session_id)
    context.warmup_keys = []

    def execute(command, model, received_session, key):
        context.warmup_keys.append((command, key))
        return provider.command(command, model=model, timeout=2)

    return execute


def _start(context, job_id, executor):
    context.start_error = None
    context.started_run = None
    try:
        context.started_run = context.lifecycle.start_job(job_id, executor)
    except SessionConflict as exc:
        context.start_error = exc


# F3: an unobserved resumed permission mode fails closed to ask.

@given("the Coddy fixture resumes a session without a reported permission mode for a bypass mention")
def resumed_session_without_permission(context):
    context.provider_kind = "coddy"
    context.coddy_session_id = FIXTURE_SESSION
    context.coddy = {
        "session": "@session:" + FIXTURE_SESSION,
        "permission_mode": "bypass",
        "mention": {"agent": "exec", "prompt": "Read input.txt and return the answer",
                    "permission_mode": "bypass"},
    }
    context.responses = [
        _catalog(),
        json_response(200, {"messages": []}),
        coddy_stream("session compacted", session_id=FIXTURE_SESSION),
        coddy_stream("project initialized", session_id=FIXTURE_SESSION),
        json_response(200, {}),
        coddy_stream(json.dumps({"action": "final", "result": {"answer": 42}}),
                     session_id=FIXTURE_SESSION),
    ]


@then('the subagent session permission was set to "{mode}" and never to bypass')
def subagent_permission_set(context, mode):
    patches = [request for request in context.requests if request["method"] == "PATCH"]
    assert [request["body"] for request in patches] == [{"permissionMode": mode}], context.requests
    work = [request["body"]["input"] for request in context.requests
            if request["method"] == "POST" and "@agent:exec" in (request["body"] or {}).get("input", "")]
    assert work and all(f"permission_mode: {mode}" in text for text in work), work
    assert all("bypass" not in json.dumps(request["body"]) for request in patches), patches


@then('the lifecycle records resumed session permission mode "{mode}"')
def lifecycle_session_permission(context, mode):
    session = SessionLifecycle(context.agent.store).session(FIXTURE_SESSION)
    assert session["permission_mode"] == mode, session


# F4: an uncertain warm-up outcome is recovering and needs an operator.

@given('a registered native job whose Coddy warm-up stream for "{command}" ends without DONE')
def native_job_truncated_warmup(context, command):
    assert command == "/compact"
    context.lifecycle = SessionLifecycle(context.agent.store)
    job = context.lifecycle.register_job(_native_job(context, "uncertain-warmup"))
    context.native_job_id, context.native_session_id = job["id"], job["session_id"]
    base, context.requests, _, _ = context.resources.enter_context(server([
        _truncated_stream("compacting", context.native_session_id),
        coddy_stream("session compacted", session_id=context.native_session_id),
        coddy_stream("project initialized", session_id=context.native_session_id),
    ]))
    context.warmup_executor = _coddy_warmup_executor(context, base, context.native_session_id)


@when("the native job is started with the Coddy warm-up executor")
def start_with_coddy_executor(context):
    _start(context, context.native_job_id, context.warmup_executor)


@then("starting the native job is refused")
def start_refused(context):
    assert context.started_run is None, context.started_run
    assert isinstance(context.start_error, SessionConflict), context.start_error


@then('the native job session is "{state}" with retry key for "{command}"')
def native_session_state(context, state, command):
    session = context.lifecycle.session(context.native_session_id)
    assert session["state"] == state, session
    assert session["recovery"].get("retry_key") == f"warmup:{context.native_session_id}:{command}", session
    assert context.warmup_keys == [(command, f"warmup:{context.native_session_id}:{command}")], context.warmup_keys


@then("an unconfirmed native warm-up retry is refused without sending a command")
def unconfirmed_retry_refused(context):
    sent = len(context.requests)
    try:
        context.lifecycle.retry_warmup(context.native_session_id, context.warmup_executor)
    except SessionConflict:
        pass
    else:
        raise AssertionError("an uncertain warm-up was replayed without operator confirmation")
    assert len(context.requests) == sent, context.requests
    assert context.lifecycle.session(context.native_session_id)["state"] == "recovering"


@then('an operator-confirmed native warm-up retry reuses the "{command}" key and prepares the session')
def confirmed_retry(context, command):
    first_key = context.warmup_keys[0][1]
    context.warmup_keys.clear()
    context.lifecycle.retry_warmup(context.native_session_id, context.warmup_executor,
                                   operator_confirmed=True)
    assert context.warmup_keys[0] == (command, first_key), context.warmup_keys
    assert [item[0] for item in context.warmup_keys] == ["/compact", "/rpa-init"], context.warmup_keys
    session = context.lifecycle.session(context.native_session_id)
    assert (session["state"], session["compact_status"], session["rpa_init_status"]) == \
        ("ready", "succeeded", "succeeded"), session


# F11: a job never runs on an unprepared session.

@given('a registered native job whose Coddy warm-up "{command}" was rejected with HTTP {status:d}')
def native_job_rejected_warmup(context, command, status):
    assert command == "/compact"
    context.lifecycle = SessionLifecycle(context.agent.store)
    job = context.lifecycle.register_job(_native_job(context, "rejected-warmup"))
    context.native_job_id, context.native_session_id = job["id"], job["session_id"]
    base, context.requests, _, _ = context.resources.enter_context(server([
        json_response(status, {"error": {"type": "invalid_request"}}),
    ]))
    _start(context, context.native_job_id, _coddy_warmup_executor(context, base, context.native_session_id))
    assert isinstance(context.start_error, SessionConflict), context.start_error
    assert context.lifecycle.session(context.native_session_id)["state"] == "failed"


@when("the native job is started again with a recording warm-up executor")
def start_again_recording(context):
    context.recorded_warmup = []
    _start(context, context.native_job_id,
           lambda *args: context.recorded_warmup.append(args) or True)


@then("no native run was claimed and no warm-up command was recorded")
def no_run_claimed(context):
    assert context.lifecycle.run_history(context.native_job_id) == []
    assert context.recorded_warmup == [], context.recorded_warmup
    assert context.lifecycle.job(context.native_job_id)["state"] == "ready"


# F12: an explicit session does not prevent dependency readiness.

@given("a warmed native root job and two dependents that mention the root session")
def root_and_session_dependents(context):
    context.lifecycle = SessionLifecycle(context.agent.store)
    root = context.lifecycle.register_job(_native_job(context, "root"))
    context.root_session_id = root["session_id"]
    mention = "@session:" + root["session_id"]
    for job_id in ("first", "second"):
        dependent = context.lifecycle.register_job(
            _native_job(context, job_id, dependencies=["root"], session=mention))
        assert dependent["state"] == "pending", dependent
    context.lifecycle.warm_session(root["session_id"], lambda *args: True)


@when("the native root job succeeds")
def root_succeeds(context):
    run = context.lifecycle.start_job("root")
    context.lifecycle.complete_run(run["id"], "succeeded", result={"answer": 42})
    context.ready_ids = [job["id"] for job in context.lifecycle.ready_jobs()]


@then("both dependent native jobs are ready on the root session with delivered transfers")
def dependents_ready(context):
    assert context.ready_ids == ["first", "second"], context.ready_ids
    for job_id in ("first", "second"):
        job = context.lifecycle.job(job_id)
        assert (job["state"], job["session_id"]) == ("ready", context.root_session_id), job
        transfers = [item for item in context.lifecycle.transfers(job_id) if item["target_job_id"] == job_id]
        assert [item["state"] for item in transfers] == ["delivered"], transfers


@then("the second dependent cannot start while the first dependent is running")
def no_concurrent_session(context):
    context.first_run = context.lifecycle.start_job("first")
    _start(context, "second", None)
    assert isinstance(context.start_error, SessionConflict), context.start_error
    assert context.lifecycle.job("second")["state"] == "ready"


@then("the second dependent starts on the root session after the first completes")
def sequential_reuse(context):
    context.lifecycle.complete_run(context.first_run["id"], "succeeded", result={"side": "first"})
    run = context.lifecycle.start_job("second")
    assert run["session_id"] == context.root_session_id, run


# F8: lifecycle DDL must not end the caller's BEGIN IMMEDIATE transaction.

class _FailingAfterSetupStore:
    def __init__(self, store):
        self.store, self.home = store, store.home
        self.open_after_setup = None

    @contextmanager
    def transaction(self):
        with self.store.transaction() as db:
            yield db
            self.open_after_setup = db.in_transaction
            raise RuntimeError("injected failure after lifecycle schema setup")


@when("the lifecycle schema is created in a store transaction that fails after setup")
def schema_in_failing_transaction(context):
    context.failing_store = _FailingAfterSetupStore(context.agent.store)
    try:
        SessionLifecycle(context.failing_store)
    except RuntimeError as exc:
        assert "injected failure" in str(exc)
    else:
        raise AssertionError("the injected transaction failure was not raised")


@then("the store transaction was still open after lifecycle schema setup")
def transaction_still_open(context):
    assert context.failing_store.open_after_setup is True, context.failing_store.open_after_setup


@then("the failed transaction left no lifecycle table behind")
def no_lifecycle_table(context):
    with context.agent.store.reading() as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'lifecycle_%'")]
    assert tables == [], tables
