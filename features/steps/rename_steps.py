import tomllib
from pathlib import Path

from behave import given, then

from boring_agent.cli import parser


ROOT = Path(__file__).resolve().parents[2]


@given("the installed command metadata")
def installed_command_metadata(context):
    with (ROOT / "pyproject.toml").open("rb") as file:
        context.console_scripts = tomllib.load(file)["project"]["scripts"]


@then('"{name}" is the primary console command')
def primary_console_command(context, name):
    scripts = context.console_scripts
    assert next(iter(scripts)) == name
    assert scripts[name] == "boring_agent.cli:main"


@then('"{name}" remains a compatibility console command')
def compatibility_console_command(context, name):
    assert context.console_scripts[name] == "boring_agent.cli:main"


@then('CLI help names the program "{name}"')
def cli_help_name(context, name):
    assert parser().prog == name


@given("the canonical boring-agent definition")
def canonical_agent_definition(context):
    context.agent_definition = (ROOT / ".coddy" / "agents" / "boring-agent.md").read_text(encoding="utf-8")


@then('the agent requires the "{name}" subagent for implementation and verification')
def mandatory_execution_subagent(context, name):
    definition = context.agent_definition
    assert "Mandatory exec delegation" in definition
    assert f'agent="{name}"' in definition
    assert "implementation, test, build, packaging, and release operations" in definition


@then('the agent refuses to execute directly when "{name}" is unavailable')
def no_direct_execution_fallback(context, name):
    definition = context.agent_definition
    assert f"If `{name}` is unavailable" in definition
    assert "report `BLOCKED`" in definition
    assert "do not execute the work directly" in definition


@then("installation requires nested subagent depth {depth:d}")
def nested_subagent_depth(context, depth):
    install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert f"subagents.max_depth: {depth}" in install
