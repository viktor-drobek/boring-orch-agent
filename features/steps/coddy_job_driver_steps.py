"""Steps for the Coddy native job driver; they drive tools.coddy_driver against a loopback fixture."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from behave import given, then, when

from tests.support.coddy_session_fixture import CoddySessionFixture, permission_prompt
from tools.coddy_driver import driver as driver_module
from tools.coddy_driver.policy import Policy

PROJECT_PYTHON = "/opt/project/.venv/bin/python"


def job_workspace(context):
    if not hasattr(context, "driver_root"):
        context.driver_root = Path(tempfile.mkdtemp(prefix="coddy-driver-"))
        (context.driver_root / "workspace").mkdir()
    return context.driver_root / "workspace"


@given("an unattended driver policy for a job workspace")
def unattended_policy(context):
    context.driver_policy = Policy(job_workspace(context), PROJECT_PYTHON)


@when('a child asks to run "{command}"')
def child_asks(context, command):
    workspace = str(job_workspace(context))
    command = (command.replace("WORKSPACE", workspace).replace("PROJECT_PYTHON", PROJECT_PYTHON)
               .replace(" NEWLINE ", "\n"))
    context.driver_answer = context.driver_policy.decide(permission_prompt(command))


@when("a child's permission prompt carries no readable command")
def unreadable_prompt(context):
    context.driver_answer = context.driver_policy.decide(
        {"toolCall": {"toolCallId": "c", "kind": "run_command", "content": []}})


@then('the driver answers "{answer}"')
def driver_answers(context, answer):
    assert context.driver_answer[0] == answer, context.driver_answer


def fixture(context):
    context.coddy = CoddySessionFixture()
    context.add_cleanup(context.coddy.close)
    return context.coddy


@given('a Coddy session fixture whose job turn asks to run "{first}" and then "{second}"')
def fixture_with_prompts(context, first, second):
    fixture(context).prompts = [permission_prompt(first, "c1"), permission_prompt(second, "c2"),
                                permission_prompt(second, "c2")]


@given("a Coddy session fixture whose job turn stream ends without data: [DONE]")
def fixture_without_done(context):
    fixture(context).send_done = False


def driver_args(context, **extra):
    root = context.driver_root if hasattr(context, "driver_root") else Path(tempfile.mkdtemp(prefix="coddy-driver-"))
    context.driver_root = root
    (root / "workspace").mkdir(exist_ok=True)
    job = root / "job.json"
    job.write_text(json.dumps({
        "id": "driver-job", "runtime": "coddy_native", "model": "provider/model", "objective": "test",
        "workspace": str(root / "workspace"), "budget": {"deadline_seconds": 60},
        "metadata": {"permission_mode": "accept_edits", "python": PROJECT_PYTHON}}))
    (root / "token").write_text("fixture-token")
    (root / "prompt.md").write_text("run the job")
    return SimpleNamespace(job=str(job), token_file=str(root / "token"), base=context.coddy.base,
                           out=str(root / "run"), python=None, permission_mode=None, run_id="run-1",
                           prompt_file=str(root / "prompt.md"), **extra)


def fast(context):
    saved = driver_module.QUIET_SECONDS, driver_module.POLL_SECONDS
    driver_module.QUIET_SECONDS, driver_module.POLL_SECONDS = 0, 0.1

    def restore():
        driver_module.QUIET_SECONDS, driver_module.POLL_SECONDS = saved
    context.add_cleanup(restore)


@when("the operator launches the native job through the driver")
def launch(context):
    fast(context)
    context.driver = driver_module.Driver(driver_args(context))
    context.add_cleanup(context.driver.stop.set)
    context.driver.launch()


def state(context):
    return json.loads((Path(context.driver.out) / "state.json").read_text())


def events(context):
    return [json.loads(line) for line in (Path(context.driver.out) / "events.jsonl").read_text().splitlines()]


@then("the session was pinned to the job model and permission mode before the job turn")
def pinned(context):
    assert context.coddy.patch["permissionMode"] == "accept_edits", context.coddy.patch
    assert context.coddy.patch["selectedModelId"] == "provider/model", context.coddy.patch
    paths = [path for path, _ in context.coddy.requests]
    streamed = [i for i, (path, body) in enumerate(context.coddy.requests)
                if path == "/v1/responses" and body.get("stream")]
    assert paths[0] == "/v1/responses" and streamed, paths


@then('the driver answered the prompts "{first}" then "{second}"')
def answered(context, first, second):
    assert context.coddy.answers == [first, second], context.coddy.answers


@then('the recorded outcome is "{outcome}"')
def outcome(context, outcome):
    assert state(context)["outcome"] == outcome, state(context)


@then("no idle watchdog decision was recorded")
def no_watchdog(context):
    assert not [e for e in events(context) if e["kind"] == "watchdog"]


@when("the server answers 404 for the session's background tasks")
def server_404(context):
    context.driver.session_id = "sess_unknown"
    context.driver_rows = context.driver.poll_tasks()


@then("the driver does not consider the server lost")
def not_lost(context):
    assert context.driver_rows == [] and not context.driver.server_lost


@when("the server becomes unreachable for repeated polls")
def unreachable(context):
    context.driver.base = "http://127.0.0.1:9"
    for _ in range(driver_module.SERVER_LOST_AFTER):
        context.driver.poll_tasks()


@then("the driver considers the server lost")
def lost(context):
    assert context.driver.server_lost


@when("the operator attaches the driver to an unknown session")
def attach_unknown(context):
    fast(context)
    context.driver = driver_module.Driver(driver_args(context, session="sess_unknown"))
    try:
        context.driver.attach()
        context.driver_exit = 0
    except SystemExit as exc:
        context.driver_exit = exc.code


@then("the driver exits with status {code:d}")
def exits(context, code):
    assert context.driver_exit == code, context.driver_exit
