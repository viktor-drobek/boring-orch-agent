import json
from pathlib import Path
import tempfile
import unittest

from boring_agent.store import Store


ROOT = Path(__file__).resolve().parent.parent


def body(path):
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        return text.split("\n---\n", 1)[1].lstrip("\n")
    return text


class AgentGuidanceTests(unittest.TestCase):
    def test_claude_uses_the_canonical_agent_brief(self):
        brief = ROOT / "AGENTS.md"
        claude = ROOT / "CLAUDE.md"
        self.assertTrue(claude.is_symlink())
        self.assertEqual(claude.resolve(), brief)
        self.assertIn("Rules sync", brief.read_text(encoding="utf-8"))

    def test_cursor_and_claude_topic_rules_have_matching_bodies(self):
        names = {path.stem for path in (ROOT / ".cursor" / "rules").glob("*.mdc")}
        self.assertEqual(names, {path.stem for path in (ROOT / ".claude" / "rules").glob("*.md")})
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(body(ROOT / ".cursor" / "rules" / f"{name}.mdc"),
                                 body(ROOT / ".claude" / "rules" / f"{name}.md"))

    def test_codex_bridge_and_coddy_addendum_are_present(self):
        hook = ROOT / ".codex" / "hooks" / "attach_rules.py"
        self.assertTrue((ROOT / ".codex" / "hooks.json").is_file())
        self.assertIn(".cursor/rules/", hook.read_text(encoding="utf-8"))
        index = (ROOT / ".codex" / "rules.md").read_text(encoding="utf-8")
        for name in ("workflow", "architecture", "testing", "code-style", "implementation-order", "api-layer", "core-modules"):
            self.assertIn(name, index)
        self.assertIn("AGENTS.md", (ROOT / ".coddy" / "rules" / "boring-orch-agent.md").read_text(encoding="utf-8"))

    def test_public_job_templates_are_valid_task_documents(self):
        # Example validation is offline and does not launch an operational job.
        for path in sorted((ROOT / "examples" / "jobs").glob("*.json")):
            with self.subTest(template=path.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw = json.loads(path.read_text(encoding="utf-8"))
                store = Store(root / "state")
                store.initialize(root, allow_write=raw.get("sandbox") == "workspace-write")
                receipt = store.submit(raw, "sample-" + path.stem)
                task = store.task(receipt["task_id"])
                self.assertEqual(task["spec"]["objective"], raw["objective"])

    def test_exec_launch_contract_is_reachable_from_all_agent_rules(self):
        reference = "boring_agent/memory/.agents/references/exec.md"
        contract = (ROOT / reference).read_text(encoding="utf-8")
        for path in ("AGENTS.md", ".cursor/rules/workflow.mdc", ".claude/rules/workflow.md",
                     ".coddy/rules/boring-orch-agent.md"):
            with self.subTest(path=path):
                self.assertIn(reference, (ROOT / path).read_text(encoding="utf-8"))
        for marker in ("SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED", "HANDOFF",
                       "NEEDS_OPERATOR", "NEEDS_MODEL_DECISION", "notify_on_finish",
                       "Получатель результата", "Модель джобы", "Модель exec"):
            with self.subTest(marker=marker):
                self.assertIn(marker, contract)
        memory = ROOT / "boring_agent/memory"
        self.assertIn(".agents/references/exec.md", (memory / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("../.agents/references/exec.md",
                      (memory / "rules/job-launching.md").read_text(encoding="utf-8"))

    def test_exec_uses_native_job_model_and_ready_only_dispatch(self):
        contract = (ROOT / "boring_agent/memory/.agents/references/exec.md").read_text(encoding="utf-8")
        for marker in ("spawn_agent(model=job.model)", "READY", "NOT_READY", "MODEL_MISMATCH",
                       "execution_mode: coddy_native", "--count", "budget.max_steps"):
            self.assertIn(marker, contract)
        self.assertNotIn("PYTHON -m boring_agent --home HOME submit SPEC", contract)
        self.assertNotIn("PYTHON -m boring_agent.watch", contract)
        for path in ("AGENTS.md", ".cursor/rules/workflow.mdc", ".claude/rules/workflow.md",
                     ".coddy/rules/boring-orch-agent.md"):
            with self.subTest(path=path):
                self.assertIn("spawn_agent(model=job.model)", (ROOT / path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
