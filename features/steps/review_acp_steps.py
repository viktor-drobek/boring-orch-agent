"""Steps for ACP launch-plan home and environment policy (docs/isolation.md)."""
from pathlib import Path
import tempfile

from behave import given, then, when

from boring_agent.acp import ACPError, prepare_launch


ADVERTISEMENT = {"modes": ["interactive"], "models": ["fixture-1"]}
AGENT_COMMAND = ["agent", "--stdio"]


@given("an ACP task with a private workspace and state directory")
def acp_task(context):
    root = Path(context.resources.enter_context(tempfile.TemporaryDirectory()))
    (root / "ws").mkdir()
    (root / "state").mkdir()
    context.acp_state = str((root / "state").resolve())
    context.acp_task = {"workspace": str(root / "ws"), "state_path": context.acp_state,
                        "sandbox": "read-only", "mode": "interactive", "model": "fixture-1"}
    context.acp_host_env = {"PATH": "/usr/bin:/bin"}
    context.acp_capability = None


@given("the ACP task requests workspace-write")
def acp_workspace_write(context):
    context.acp_task["sandbox"] = "workspace-write"


@given("bubblewrap isolation is {capability} for the ACP launch")
def acp_capability(context, capability):
    context.acp_capability = capability


@given("the ACP host environment contains:")
def acp_host_env(context):
    context.acp_host_env = {row["name"]: row["value"] for row in context.table}


@given('the ACP task declares environment pass-through "{name}"')
def acp_passthrough(context, name):
    context.acp_task["environment_passthrough"] = [name]


@when("the ACP launch plan is prepared")
def acp_prepare(context):
    context.acp_plan, context.acp_error = None, None
    try:
        context.acp_plan = prepare_launch(context.acp_task, AGENT_COMMAND, ADVERTISEMENT,
                                          capability=context.acp_capability,
                                          base_environment=context.acp_host_env)
    except ACPError as exc:
        context.acp_error = exc


def _plan(context):
    assert context.acp_error is None, f"launch refused: {context.acp_error}"
    return context.acp_plan


def _names(text):
    return [part.strip().strip('"') for part in text.replace(" and ", ",").split(",") if part.strip()]


@then('the ACP state directory is bound at "{mount}" in the sandbox')
def acp_state_bound(context, mount):
    command = list(_plan(context).command)
    pairs = [(command[i + 1], command[i + 2]) for i, arg in enumerate(command[:-2]) if arg == "--bind"]
    assert (context.acp_state, mount) in pairs, command


@then('the ACP environment sets "{name}" to "{value}"')
def acp_env_value(context, name, value):
    environment = _plan(context).environment
    assert environment.get(name) == value, (name, environment.get(name))


@then('the ACP environment sets "{name}" to the host state directory')
def acp_env_host_state(context, name):
    acp_env_value(context, name, context.acp_state)


def _xdg_under(context, prefix):
    environment = _plan(context).environment
    xdg = {k: v for k, v in environment.items() if k.startswith("XDG_")}
    assert {"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"} <= set(xdg), xdg
    for name, value in xdg.items():
        assert value == prefix or value.startswith(prefix.rstrip("/") + "/"), (name, value)


@then('every ACP XDG directory lies under "{prefix}"')
def acp_xdg_under(context, prefix):
    _xdg_under(context, prefix)


@then("every ACP XDG directory lies under the host state directory")
def acp_xdg_under_host(context):
    _xdg_under(context, context.acp_state)


@then("no ACP environment value names the host state directory")
def acp_no_host_state(context):
    leaked = {k: v for k, v in _plan(context).environment.items() if context.acp_state in v}
    assert not leaked, leaked


@then("the ACP launch is the unwrapped agent command")
def acp_unwrapped(context):
    plan = _plan(context)
    assert plan.isolation.tier == "B" and list(plan.command) == AGENT_COMMAND, plan.command


@then("the ACP environment keeps {names} unchanged")
def acp_env_keeps(context, names):
    environment = _plan(context).environment
    for name in _names(names):
        assert environment.get(name) == context.acp_host_env[name], (name, environment.get(name))


@then("the ACP environment omits {names}")
def acp_env_omits(context, names):
    environment = _plan(context).environment
    present = [name for name in _names(names) if name in environment]
    assert not present, present


@then('no ACP environment value contains "{text}"')
def acp_env_no_value(context, text):
    leaked = {k: v for k, v in _plan(context).environment.items() if text in v}
    assert not leaked, leaked


@then('the ACP launch is refused with a message mentioning "{text}"')
def acp_refused(context, text):
    assert context.acp_plan is None and context.acp_error is not None, "launch was not refused"
    assert text in str(context.acp_error), str(context.acp_error)
