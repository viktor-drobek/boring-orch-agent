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

    def test_workflow_ends_with_complete_rules_sync_contract(self):
        workflow = body(ROOT / ".cursor" / "rules" / "workflow.mdc")
        sync = workflow.rsplit("## Rules Sync", 1)[1]
        for step in range(1, 8):
            with self.subTest(step=step):
                self.assertIn(f"\n{step}.", sync)
        for marker in ("every rule tree", "frontmatter and inline links", "same language",
                       "CLAUDE.md", ".codex/rules.md", "same commit", "tool-specific"):
            with self.subTest(marker=marker):
                self.assertIn(marker, sync)
        self.assertTrue(sync.rstrip().endswith("intentional and visible."))

    def test_architecture_requires_lower_layers_before_dependents(self):
        architecture = body(ROOT / ".cursor" / "rules" / "architecture.mdc")
        self.assertIn("Dependencies flow from outer layers to established inner layers only.", architecture)
        self.assertIn("Implement and test each lower layer before adding behavior to a dependent layer.", architecture)

    def test_architecture_keeps_coddy_session_state_out_of_stateless_providers(self):
        architecture = body(ROOT / ".cursor" / "rules" / "architecture.mdc")
        self.assertIn("POST /v1/responses", architecture)
        self.assertIn("stable `X-Coddy-Session-ID`", architecture)
        self.assertIn("may only narrow it", architecture)

    def test_every_cursor_rule_is_reachable(self):
        for path in (ROOT / ".cursor" / "rules").glob("*.mdc"):
            with self.subTest(rule=path.name):
                text = path.read_text(encoding="utf-8")
                head = text.split("\n---\n", 1)[0]
                has_globs = any(line.startswith("globs:") and line.partition(":")[2].strip()
                                for line in head.splitlines())
                self.assertTrue("alwaysApply: true" in head or has_globs)

    def test_codex_bridge_and_coddy_addendum_are_present(self):
        hook = ROOT / ".codex" / "hooks" / "attach_rules.py"
        self.assertTrue((ROOT / ".codex" / "hooks.json").is_file())
        self.assertIn(".cursor/rules/", hook.read_text(encoding="utf-8"))
        index = (ROOT / ".codex" / "rules.md").read_text(encoding="utf-8")
        for name in ("workflow", "architecture", "testing", "code-style", "implementation-order", "api-layer", "core-modules"):
            self.assertIn(name, index)
        self.assertIn("AGENTS.md", (ROOT / ".coddy" / "rules" / "boring-agent.md").read_text(encoding="utf-8"))

    def test_coddy_project_agent_is_canonical_and_inherits_its_model(self):
        definition = (ROOT / ".coddy" / "agents" / "boring-agent.md").read_text(encoding="utf-8")
        frontmatter, body = definition.split("\n---\n", 1)
        self.assertIn("name: boring-agent", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertNotIn("\nmodel:", frontmatter)
        self.assertIn("root AGENTS.md", body)
        self.assertIn("Never widen the parent permission mode", body)

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

    def test_exec_contract_is_a_committed_document_referenced_by_every_rule_tree(self):
        reference = "docs/exec.md"
        contract = (ROOT / reference).read_text(encoding="utf-8")
        for path in ("AGENTS.md", ".cursor/rules/workflow.mdc", ".claude/rules/workflow.md",
                     ".coddy/rules/boring-agent.md"):
            with self.subTest(path=path):
                text = (ROOT / path).read_text(encoding="utf-8")
                self.assertIn(reference, text)
                self.assertNotIn("memory/.agents/references", text, "no rule may point at an uncommitted file")
                self.assertIn("spawn_agent(model=job.model)", text)
        for marker in ("ParentIdleWatchdog", "NEEDS_MODEL_DECISION", "HANDOFF", "/compact", "/rpa-init", "1800"):
            with self.subTest(marker=marker):
                self.assertIn(marker, contract)
        # The document describes the columns the lifecycle store actually has.
        self.assertIn("attempt_count", contract)
        self.assertNotIn("execution_authorized", contract)
        self.assertNotIn("native_attempt_count", contract)

if __name__ == "__main__":
    unittest.main()
