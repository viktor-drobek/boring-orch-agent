"""Every agent instruction tree carries the same Coddy transport preference."""
from pathlib import Path

from behave import given, then


ROOT = Path(__file__).resolve().parents[2]
HEADING = "## Coddy transport order"
API = "HTTP Responses API (`coddy serve`"
ACP = "Agent Client Protocol (`coddy acp`"
PLAIN = "plain CLI prompts (`coddy -p`"


def _section(text):
    if HEADING not in text:
        return text
    rest = text.split(HEADING, 1)[1]
    return rest.split("\n## ", 1)[0]


@given('the agent instruction file "{path}"')
def agent_instruction_file(context, path):
    target = ROOT / path
    assert target.is_file(), f"missing instruction file: {path}"
    context.instruction_path = path
    context.instruction_text = target.read_text(encoding="utf-8")


@then("it contains the canonical Coddy transport order section")
def canonical_section(context):
    canonical = _section((ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    assert HEADING in context.instruction_text, f"{context.instruction_path} has no '{HEADING}' section"
    assert _section(context.instruction_text).strip() == canonical.strip(), \
        f"{context.instruction_path} diverges from the AGENTS.md transport section"


@then("the transport order is API, then ACP, then plain CLI")
def transport_order(context):
    section = _section(context.instruction_text)
    positions = [section.find(marker) for marker in (API, ACP, PLAIN)]
    assert -1 not in positions, f"{context.instruction_path} does not name all three transports: {positions}"
    assert positions == sorted(positions), f"{context.instruction_path} lists transports out of order: {positions}"
    assert "in that order" in section


@then("the first run in a new project tells the operator the selected transport")
def first_run_notice(context):
    section = _section(context.instruction_text)
    assert "first run in a new project" in section
    assert "tell the operator which transport was selected" in section


@then("an Unknown outcome is never retried on another transport")
def unknown_not_retried(context):
    section = _section(context.instruction_text)
    assert "never switch transports to retry" in section and "`Unknown`" in section


@then("the first run in a new project asks the operator for the permission mode and model")
def first_run_asks_permissions_and_model(context):
    section = _section(context.instruction_text)
    assert "ask the operator which permission mode and which model" in section
    assert "may only narrow" in section and "`bypass`" in section
    assert "`job.model`" in section, "native job selectors stay explicit"
