"""Regression invariants for the Coddy native job driver and its permission policy."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.support.coddy_session_fixture import CoddySessionFixture, permission_prompt
from tools.coddy_driver import driver as driver_module
from tools.coddy_driver.policy import Policy

PY = "/opt/project/.venv/bin/python"
ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="policy-ws-"))
        self.policy = Policy(self.workspace, PY)

    def check(self, cases):
        for command, expected in cases.items():
            with self.subTest(command=command):
                allowed, why = self.policy.command_allowed(command)
                self.assertEqual(allowed, expected, why)

    def test_reads_and_project_checks_are_allowed(self):
        ws = self.workspace
        self.check({
            f"cd {ws} && sha256sum build/job.json": True,
            "git worktree list && echo --- && git branch --show-current && git status --short": True,
            f"{PY} -m behave features/x.feature 2>&1 | tail -40": True,
            f"{PY} -m unittest discover -s tests": True,
            f"cd {ws} && grep -rhoE '(Scenario|Outline): .*' features/*.feature"
            " | sed 's/^[^:]*: //' | sort -u && ls features/steps/*.py | wc -l": True,
            f"{PY} -m json.tool x.json": False,  # not a project check
        })

    def test_every_escape_route_is_rejected(self):
        ws = self.workspace
        self.check({
            "ls; rm -rf x": False, "ls && git commit -m x": False, "git checkout -b x": False,
            "git branch -D x": False, "git -C /tmp status": False, "rm -rf features": False,
            "curl https://example.invalid": False, "pip install foo": False,
            "python3 scripts/check_cucumber.py": False, "python3 -c \"\nimport json\nprint(1)\n\"": False,
            "cat a >> b": False, "ls &": False, "(rm -rf x)": False, "echo `id`": False,
            "find . -name '*.py' -delete": False, "find . -exec rm {} ;": False,
            "sed 's/a/b/w out' f": False, "cd && ls": False,
            f"{PY} scripts/export_docx.py build": False, f"{PY} scripts/check_editorial.py x --out r.json": False,
            "rm -rf /tmp/s && mkdir -p /tmp/s\ncat > /tmp/x <<'EOF'\nhi\nEOF": False,
            "": False, "   ": False, "'unterminated": False, f"cd {ws}/../other && ls": False,
        })

    def test_garbled_and_unknown_prompts_fail_closed(self):
        garbled = {"toolCall": {"kind": "run_command", "content": [{"content": {"text": "Arguments: {bad"}}]}}
        self.assertEqual(self.policy.decide(garbled)[0], "reject")
        self.assertEqual(self.policy.decide({"toolCall": {"kind": "http_request"}})[0], "reject")
        self.assertEqual(self.policy.decide({"toolCall": {"kind": "run_command", "args": {"command": "ls"}}}),
                         ("allow", "allowlisted"))

    def test_edits_stay_inside_the_workspace_and_deletes_are_never_automatic(self):
        decide = self.policy.decide
        self.assertEqual(decide({"toolCall": {"kind": "write_file", "args": {"path": "features/a.feature"}}})[0],
                         "allow")
        self.assertEqual(decide({"toolCall": {"kind": "edit_file", "args": {"path": "/etc/hosts"}}})[0], "reject")
        self.assertEqual(decide({"toolCall": {"kind": "edit_file",
                                              "args": {"path": f"{self.workspace}-evil/x"}}})[0], "reject")
        self.assertEqual(decide({"toolCall": {"kind": "delete_file", "args": {"path": "a"}}})[0], "reject")


class DriverTests(unittest.TestCase):
    def launched(self, **fixture_settings):
        coddy = CoddySessionFixture()
        self.addCleanup(coddy.close)
        for key, value in fixture_settings.items():
            setattr(coddy, key, value)
        root = Path(tempfile.mkdtemp(prefix="driver-"))
        (root / "ws").mkdir()
        job = root / "job.json"
        job.write_text(json.dumps({"id": "j", "runtime": "coddy_native", "model": "p/m", "objective": "o",
                                   "workspace": str(root / "ws"), "budget": {"deadline_seconds": 60},
                                   "metadata": {"permission_mode": "accept_edits", "python": PY}}))
        (root / "token").write_text("t")
        (root / "prompt").write_text("p")
        saved = driver_module.QUIET_SECONDS, driver_module.POLL_SECONDS
        driver_module.QUIET_SECONDS, driver_module.POLL_SECONDS = 0, 0.1
        self.addCleanup(lambda: (setattr(driver_module, "QUIET_SECONDS", saved[0]),
                                 setattr(driver_module, "POLL_SECONDS", saved[1])))
        driver = driver_module.Driver(SimpleNamespace(
            job=str(job), token_file=str(root / "token"), base=coddy.base, out=str(root / "run"), python=None,
            permission_mode=None, run_id="r", prompt_file=str(root / "prompt")))
        self.addCleanup(driver.stop.set)
        driver.launch()
        return coddy, driver, json.loads((root / "run" / "state.json").read_text())

    def test_a_repeated_prompt_is_answered_once(self):
        coddy, driver, state = self.launched(prompts=[permission_prompt("ls", "c1"), permission_prompt("ls", "c1")])
        self.assertEqual(coddy.answers, ["allow"])
        self.assertEqual(state["decisions"], {"allow": 1, "reject": 0})

    def test_a_child_still_running_at_finish_keeps_the_outcome_unknown(self):
        coddy, driver, state = self.launched()
        self.assertEqual(state["outcome"], "parent-reported:SUCCESS")
        # The parent claims success, but a child is still running when supervision ends.
        driver.tasks["bg_1"]["status"] = "running"
        driver.finish()
        self.assertEqual(json.loads((driver.out / "state.json").read_text())["outcome"], "unknown")

    def test_a_lost_server_keeps_the_outcome_unknown(self):
        coddy, driver, state = self.launched()
        driver.server_lost = True
        driver.finish()
        self.assertEqual(json.loads((driver.out / "state.json").read_text())["outcome"], "unknown")

    def test_state_records_session_tasks_and_outcome(self):
        coddy, driver, state = self.launched()
        self.assertEqual(state["session"], "sess_0123456789abcdef01234567")
        self.assertEqual(state["tasks"]["bg_1"]["status"], "completed")
        self.assertEqual(state["phase"], "finished")

    def test_server_timestamps_map_onto_the_monotonic_clock(self):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000000000+00:00", time.gmtime(time.time() - 120))
        self.assertAlmostEqual(time.monotonic() - driver_module.Driver.monotonic_at(stamp), 120, delta=2)

    def test_launcher_script_is_valid_shell(self):
        subprocess.run(["bash", "-n", str(ROOT / "tools/coddy_driver/run_job.sh")], check=True)


if __name__ == "__main__":
    unittest.main()
