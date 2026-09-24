"""A failed/partial behavioral gate must prevent release work, even with exit code 0."""
from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

from tools.check_bdd import inventory, validate_report
from tools.pipeline import run


def passed_report():
    return [{"name": "One feature", "location": "features/one.feature:2", "status": "passed",
             "elements": [{"type": "scenario", "name": "One scenario", "status": "passed",
                           "steps": [{"name": "the outcome is visible", "result": {"status": "passed"}}]}]}]


class AcceptanceGateTests(unittest.TestCase):
    def setUp(self):
        self.expected = Counter({("one.feature", "One scenario"): 1})

    def test_empty_report_never_passes(self):
        with self.assertRaises(ValueError):
            validate_report([], self.expected)

    def test_every_step_must_have_run_and_passed(self):
        for state in ("skipped", "pending", "undefined", "failed", "error"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                report = passed_report()
                report[0]["elements"][0]["steps"][0]["result"]["status"] = state
                validate_report(report, self.expected)

    def test_filtered_or_duplicated_scenarios_never_pass(self):
        for expected, report in ((self.expected + Counter({("one.feature", "Missing"): 1}), passed_report()),
                                 (self.expected, passed_report() * 2)):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                validate_report(report, expected)

    def test_all_notes_and_requirements_have_executable_examples(self):
        root = Path(__file__).resolve().parent.parent
        self.assertGreater(sum(inventory(root).values()), 0)

    def test_direct_runtime_imports_are_declared_as_dependencies(self):
        root = Path(__file__).resolve().parent.parent
        with (root / "pyproject.toml").open("rb") as file:
            dependencies = tomllib.load(file)["project"]["dependencies"]
        names = {dependency.split("=", 1)[0].split("<", 1)[0].split(">", 1)[0]
                 for dependency in dependencies}
        self.assertTrue({"jsonschema", "referencing"}.issubset(names))


class PipelineOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "features").mkdir()
        tags = " ".join([*(f"@R{i}" for i in range(1, 9)), *(f"@ch{i:02}" for i in range(1, 14))])
        (self.root / "features" / "one.feature").write_text(
            f"{tags}\nFeature: One feature\n  Scenario: One scenario\n    Then the outcome is visible\n")
        self.stages = []

    def runner(self, stage, args, root, reports):
        self.stages.append(stage)
        if stage == "acceptance":
            (reports / "behave.json").write_text(json.dumps(passed_report()))

    def test_failed_behavior_command_stops_before_unit_tests_and_packaging(self):
        def fail(stage, args, root, reports):
            self.runner(stage, args, root, reports)
            raise subprocess.CalledProcessError(1, args)
        with self.assertRaises(subprocess.CalledProcessError):
            run(self.root, runner=fail)
        self.assertEqual(self.stages, ["acceptance"])
        self.assertFalse((self.root / "dist" / "verified").exists())

    def test_zero_exit_with_skipped_behavior_still_blocks_build(self):
        def skipped(stage, args, root, reports):
            self.runner(stage, args, root, reports)
            report = passed_report()
            report[0]["elements"][0]["status"] = "skipped"
            (reports / "behave.json").write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            run(self.root, runner=skipped)
        self.assertEqual(self.stages, ["acceptance"])

    def test_regression_failure_is_not_bypassed(self):
        def fail(stage, args, root, reports):
            self.runner(stage, args, root, reports)
            if stage == "regression":
                raise subprocess.CalledProcessError(1, args)
        with self.assertRaises(subprocess.CalledProcessError):
            run(self.root, runner=fail)
        self.assertEqual(self.stages, ["acceptance", "regression"])

    def test_failed_packaged_smoke_removes_publishable_artifacts(self):
        def fail(stage, args, root, reports):
            self.runner(stage, args, root, reports)
            if stage == "package":
                output = root / "dist" / "verified"
                output.mkdir(parents=True)
                (output / "fixture.whl").write_text("fixture")
            elif stage == "wheel-smoke":
                raise subprocess.CalledProcessError(1, args)
        with self.assertRaises(subprocess.CalledProcessError):
            run(self.root, runner=fail)
        self.assertEqual(self.stages, ["acceptance", "regression", "package", "wheel-smoke"])
        self.assertFalse((self.root / "dist" / "verified").exists())

    def test_check_only_runs_behaviors_before_regressions(self):
        summary = run(self.root, check_only=True, runner=self.runner)
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(self.stages, ["acceptance", "regression"])


if __name__ == "__main__":
    unittest.main()
