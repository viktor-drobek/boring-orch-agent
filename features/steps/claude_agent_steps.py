"""Observable contract of the Claude Code project agent and its exec subagent."""
from pathlib import Path

from behave import given, then


ROOT = Path(__file__).resolve().parents[2]


def _split(text):
    if not text.startswith("---\n"):
        raise AssertionError("agent definition has no frontmatter")
    head, body = text[4:].split("\n---\n", 1)
    fields = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, body


def _paragraphs(body):
    return [block.strip() for block in body.split("\n\n") if block.strip()]


@given('the Claude Code "{name}" agent definition')
def claude_agent_definition(context, name):
    path = ROOT / ".claude" / "agents" / f"{name}.md"
    assert path.is_file(), f"missing Claude agent definition: {path.relative_to(ROOT)}"
    context.claude_fields, context.claude_body = _split(path.read_text(encoding="utf-8"))
    assert context.claude_fields.get("name") == name, context.claude_fields
    assert context.claude_fields.get("description"), "description is required"


@then("the Claude definition inherits its model and permission mode")
def claude_inherits_model_and_mode(context):
    for field in ("model", "permissionMode"):
        assert field not in context.claude_fields, f"{field} must be inherited, not pinned"


@then("the Claude coordinator can spawn subagents but cannot edit files or run shell commands")
def claude_coordinator_tools(context):
    tools = {tool.strip() for tool in context.claude_fields.get("tools", "").split(",") if tool.strip()}
    assert "Agent" in tools, tools
    assert {"Read", "Grep", "Glob"} <= tools, tools
    assert not tools & {"Edit", "Write", "NotebookEdit", "Bash"}, tools


@then('the Claude coordinator delegates implementation and verification to "{name}"')
def claude_coordinator_delegates(context, name):
    body = context.claude_body
    assert "## Mandatory exec delegation" in body
    assert f"`{name}` subagent" in body
    assert "implementation, test, build, packaging, and release operations" in body


@then('the Claude coordinator reports BLOCKED instead of executing directly when "{name}" is unavailable')
def claude_coordinator_blocked(context, name):
    body = context.claude_body
    assert f"If `{name}` is unavailable" in body
    assert "report `BLOCKED`" in body
    assert "do not execute the work directly" in body


@then("its project brief paragraphs match the Coddy project agent verbatim")
def claude_brief_matches_coddy(context):
    _, coddy = _split((ROOT / ".coddy" / "agents" / "boring-agent.md").read_text(encoding="utf-8"))
    # The first three paragraphs are the tool-neutral project brief; delegation wording is tool-specific.
    expected = _paragraphs(coddy)[:3]
    assert _paragraphs(context.claude_body)[:3] == expected


@then("the Claude exec subagent may use the implementation tools")
def claude_exec_tools(context):
    tools = context.claude_fields.get("tools")
    if tools is None:
        return  # Omitted tools inherit every tool the session grants.
    granted = {tool.strip() for tool in tools.split(",")}
    assert {"Read", "Edit", "Write", "Bash"} <= granted, granted


@then("the Claude exec subagent runs the focused checks and the release pipeline")
def claude_exec_checks(context):
    body = context.claude_body
    assert "Gherkin scenario" in body
    assert "python tools/pipeline.py" in body
    assert "Do not weaken" in body


@then("the Claude exec subagent never widens authority or replays an Unknown outcome")
def claude_exec_authority(context):
    body = context.claude_body
    assert "Never widen" in body
    assert "`Unknown`" in body and "never replay" in body.lower()
    assert "do not spawn further subagents" in body.lower()


@then("the Claude exec subagent refuses native operational jobs that belong to Coddy exec")
def claude_exec_refuses_native_jobs(context):
    body = context.claude_body
    assert "docs/exec.md" in body
    assert "spawn_agent(model=job.model)" in body
    assert "`BLOCKED`" in body


@given("the package data configuration")
def package_data_configuration(context):
    context.pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    context.smoke = (ROOT / "tools" / "smoke_wheel.py").read_text(encoding="utf-8")


@then("the Claude agent definitions are packaged and checked by the wheel smoke test")
def claude_agents_packaged(context):
    assert '".claude/agents" = [".claude/agents/*.md"]' in context.pyproject
    for name in ("boring-agent", "exec"):
        assert f'".claude/agents/{name}.md"' in context.smoke, name
