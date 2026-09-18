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
        for path in sorted((ROOT / "examples" / "jobs").glob("*.json")):
            with self.subTest(template=path.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw = json.loads(path.read_text(encoding="utf-8"))
                store = Store(root / "state")
                store.initialize(root, allow_write=raw.get("sandbox") == "workspace-write")
                receipt = store.submit(raw, "sample-" + path.stem)
                task = store.task(receipt["task_id"])
                self.assertEqual(task["spec"]["objective"], raw["objective"])


if __name__ == "__main__":
    unittest.main()
