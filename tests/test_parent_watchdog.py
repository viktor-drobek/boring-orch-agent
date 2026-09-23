import unittest

from boring_agent.parent_watchdog import ParentIdleWatchdog


class ParentIdleWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.watchdog = ParentIdleWatchdog(lambda: self.now)
        self.watchdog.start(
            task_id="task-1",
            attempt_id="attempt-1",
            session_id="session-1",
            model="codex/gpt-5.6-luna",
            deadline_at=2500.0,
        )

    def test_threshold_is_strict_and_decision_contains_lineage(self):
        self.now += 1800
        self.assertIsNone(self.watchdog.poll())

        self.now += 0.1
        decision = self.watchdog.poll()
        self.assertEqual(decision.event, "NEEDS_MODEL_DECISION")
        self.assertEqual(decision.task_id, "task-1")
        self.assertEqual(decision.attempt_id, "attempt-1")
        self.assertEqual(decision.session_id, "session-1")
        self.assertEqual(decision.model, "codex/gpt-5.6-luna")
        self.assertAlmostEqual(decision.remaining_deadline_seconds, 599.9)

    def test_one_decision_per_idle_episode(self):
        self.now += 1801
        first = self.watchdog.poll()
        self.now += 100
        self.assertIsNone(self.watchdog.poll())
        self.assertFalse(first.as_dict()["automatic_switch"])
        self.assertFalse(first.as_dict()["replay_started"])
        self.assertFalse(first.as_dict()["child_cancelled"])

    def test_visible_progress_resets_timer(self):
        self.now += 1700
        self.watchdog.record_visible_progress()
        self.now += 1800
        self.assertIsNone(self.watchdog.poll())
        self.now += 1
        self.assertEqual(self.watchdog.poll().event, "NEEDS_MODEL_DECISION")

    def test_parent_heartbeat_does_not_reset_timer(self):
        self.now += 1801
        self.assertEqual(self.watchdog.poll().event, "NEEDS_MODEL_DECISION")
        self.now += 10
        self.assertIsNone(self.watchdog.poll())

    def test_progress_after_escalation_opens_new_idle_episode(self):
        self.now += 1801
        self.assertIsNotNone(self.watchdog.poll())
        self.watchdog.record_visible_progress()
        self.now += 1801
        self.assertIsNotNone(self.watchdog.poll())


if __name__ == "__main__":
    unittest.main()
