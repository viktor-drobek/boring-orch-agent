import tempfile
import unittest
from pathlib import Path

from boring_agent.acp import (
    ACPError,
    SANDBOX_STATE_PATH,
    environment_passthrough,
    prepare_launch,
    secure_agent,
    secure_environment,
)


ADVERTISEMENT = {"modes": [], "models": []}


class ACPLaunchEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "ws").mkdir()
        (root / "state").mkdir()
        self.state = str((root / "state").resolve())
        self.task = {"workspace": str(root / "ws"), "state_path": self.state, "sandbox": "workspace-write"}

    def tearDown(self):
        self.temp.cleanup()

    def test_tier_a_state_mount_matches_home_and_every_xdg_directory(self):
        plan = prepare_launch(self.task, ["agent"], ADVERTISEMENT, capability="available",
                              base_environment={"HOME": "/home/operator", "XDG_RUNTIME_DIR": "/run/user/1"})
        command = list(plan.command)
        index = command.index(self.state)
        self.assertEqual(command[index - 1:index + 2], ["--bind", self.state, SANDBOX_STATE_PATH])
        self.assertEqual(plan.environment["HOME"], SANDBOX_STATE_PATH)
        for name, value in plan.environment.items():
            if name.startswith("XDG_"):
                self.assertTrue(value.startswith(SANDBOX_STATE_PATH + "/"), (name, value))
        self.assertNotIn("XDG_RUNTIME_DIR", plan.environment)

    def test_explicit_empty_base_does_not_fall_back_to_the_process_environment(self):
        security = secure_agent({}, self.state)
        env = secure_environment({}, security)
        self.assertNotIn("PATH", env)
        self.assertEqual(env["HOME"], self.state)

    def test_allowlist_filter_also_drops_secret_shaped_locale_names(self):
        security = secure_agent({}, self.state)
        env = secure_environment({"LC_ALL": "C", "LC_API_TOKEN": "x", "BOA_HOME": "/store"}, security)
        self.assertEqual(env["LC_ALL"], "C")
        self.assertNotIn("LC_API_TOKEN", env)
        self.assertNotIn("BOA_HOME", env)

    def test_passthrough_declaration_is_validated(self):
        self.assertEqual(environment_passthrough({}), ())
        self.assertEqual(environment_passthrough({"environment_passthrough": ["NODE_OPTIONS"]}), ("NODE_OPTIONS",))
        for bad in ("NODE_OPTIONS", [""], ["A=B"], [3]):
            with self.assertRaisesRegex(ACPError, "list of variable names"):
                environment_passthrough({"environment_passthrough": bad})
        for secret in ("BOA_STORE", "boa_api_key", "ANTHROPIC_API_KEY", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY"):
            with self.assertRaisesRegex(ACPError, "pass-through"):
                environment_passthrough({"environment_passthrough": [secret]})
        with self.assertRaisesRegex(ACPError, "pass-through"):
            secure_environment({}, secure_agent({}, self.state), passthrough=["OPENAI_API_KEY"])

    def test_undeclared_passthrough_name_missing_from_host_is_simply_absent(self):
        task = dict(self.task, environment_passthrough=["NODE_OPTIONS"])
        plan = prepare_launch(task, ["agent"], ADVERTISEMENT, capability="unavailable", base_environment={})
        self.assertNotIn("NODE_OPTIONS", plan.environment)
        self.assertEqual(plan.environment["HOME"], self.state)


if __name__ == "__main__":
    unittest.main()
