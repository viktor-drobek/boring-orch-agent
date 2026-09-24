"""Fault injection and boundary invariants for core review findings F5, F6, F8, F13, F17, F18."""
from contextlib import redirect_stderr
import errno
from io import StringIO
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from boring_agent import cli as cli_module
from boring_agent import manager as manager_module
from boring_agent import store as store_module
from boring_agent.manager import Manager
from boring_agent.model import Invalid, StorageError, validate_spec
from boring_agent.runner import run_attempt
from boring_agent.store import Store
from boring_agent.workspace import Workspace


class CoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = Store(self.root / "state")
        self.store.initialize(self.workspace, max_active=4)
        self.store.register_worker("w", ["demo"], 4)
        self.manager = Manager(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, key):
        return self.store.submit({"objective": "core review", "runtime": "demo",
                                  "demo": {"delay_seconds": 0}}, key)["task_id"]

    def execute(self, task_id):
        attempt = self.store.task(task_id)["attempts"][-1]
        run_attempt(self.store, attempt["id"], attempt["worker_id"])
        return self.store.task(task_id)["attempts"][-1]


class WorkspacePathTests(CoreCase):
    def test_unrepresentable_paths_are_invalid_not_crashes(self):
        tools = Workspace(self.workspace, ["list_files", "read_file", "write_file"], self.store.home)
        for path in ("a\x00b", "\x00", "dir/\ud800", "\udcff"):
            for action in ({"action": "read_file", "path": path},
                           {"action": "list_files", "path": path},
                           {"action": "write_file", "path": path, "content": "x"}):
                with self.subTest(path=path, action=action["action"]), self.assertRaises(Invalid):
                    tools.call(action)

    def test_unencodable_write_content_is_invalid(self):
        tools = Workspace(self.workspace, ["write_file"], self.store.home)
        with self.assertRaises(Invalid):
            tools.call({"action": "write_file", "path": "out.txt", "content": "bad \ud800 text"})
        self.assertFalse((self.workspace / "out.txt").exists())

    def test_expected_file_that_cannot_be_statted_is_missing(self):
        tools = Workspace(self.workspace, [], self.store.home)
        name = "x" * 300  # longer than NAME_MAX on common Linux filesystems
        self.assertEqual(tools.missing([name, "a\x00b"]), [name, "a\x00b"])

    def test_submission_rejects_unrepresentable_expected_files(self):
        for path in ("a\x00b", "reports/\ud800.md"):
            with self.subTest(path=path), self.assertRaises(Invalid):
                validate_spec({"objective": "x", "runtime": "demo", "expect_files": [path]},
                              self.workspace, False)

    def test_os_error_message_has_no_host_path(self):
        exc = FileExistsError(errno.EEXIST, "File exists", str(self.workspace / "input.txt"))
        message = Workspace.os_error(exc, "input.txt/child.txt")
        self.assertIn("EEXIST", message)
        self.assertIn("'input.txt/child.txt'", message)
        self.assertNotIn(str(self.root), message)
        self.assertNotIn(str(self.root), Workspace.os_error(OSError("boom " + str(self.root)), None))


class SettlementIsolationTests(CoreCase):
    def test_vanished_artifact_fails_acceptance_even_after_restart(self):
        first, second = self.submit("a"), self.submit("b")
        self.manager.tick()
        vanished = self.execute(first)
        self.execute(second)
        (self.store.home / vanished["result_path"]).unlink()
        Manager(Store(self.store.home)).tick()  # a freshly started manager
        self.assertEqual(self.store.task(first)["status"], "Failed")
        self.assertIn("expired", self.store.task(first)["reason"])
        self.assertEqual(self.store.task(second)["status"], "Succeeded")
        self.assertEqual(self.store.capacity()["used"], 0)

    def test_one_unexpected_settlement_fault_does_not_block_other_tasks(self):
        first, second = self.submit("a"), self.submit("b")
        self.manager.tick()
        self.execute(first)
        self.execute(second)
        original = Manager._validate_and_settle

        def faulty(manager, task, attempt, spec):
            if task["id"] == first:
                raise RuntimeError("secret payload text " + str(self.root))
            return original(manager, task, attempt, spec)

        stderr = StringIO()
        with patch.object(Manager, "_validate_and_settle", faulty), redirect_stderr(stderr):
            self.manager.tick()
        self.assertEqual(self.store.task(second)["status"], "Succeeded")
        # The faulted attempt is not guessed at: it stays unsettled and reserved.
        self.assertNotIn(self.store.task(first)["status"], ("Succeeded", "Failed", "Cancelled"))
        self.assertEqual(self.store.capacity()["used"], 1)
        log = json.loads(stderr.getvalue().strip())
        self.assertEqual(log, {"event": "manager.settlement_failed", "error": "RuntimeError", "code": None,
                               "task_id": first, "attempt_id": self.store.task(first)["current_attempt_id"]})
        self.assertNotIn("secret", stderr.getvalue())
        # Without the fault the next tick settles it normally.
        self.manager.tick()
        self.assertEqual(self.store.task(first)["status"], "Succeeded")


class LoopResilienceTests(unittest.TestCase):
    def test_failed_tick_is_logged_without_message_and_the_loop_continues(self):
        calls = []

        def tick():
            calls.append(1)
            if len(calls) == 1:
                raise StorageError("Store operation failed: /home/secret/path")
            os.kill(os.getpid(), signal.SIGTERM)  # the loop's own handler stops it

        stderr = StringIO()
        with redirect_stderr(stderr):
            cli_module.loop(tick, .02)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(stderr.getvalue()),
                         {"event": "loop.tick_failed", "error": "StorageError", "code": "storage_error"})
        self.assertNotIn("secret", stderr.getvalue())

    def test_single_tick_mode_still_reports_the_failure(self):
        def tick():
            raise StorageError("locked")

        with self.assertRaises(StorageError):
            cli_module.loop(tick, .02, once=True)


class RetentionPhaseTests(CoreCase):
    def expired_task(self):
        task_id = self.submit("retain")
        self.manager.tick()
        self.execute(task_id)
        self.manager.tick()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET finished_at=0 WHERE id=?", (task_id,))
        return task_id

    def phase(self, task_id):
        with self.store.reading() as db:
            row = db.execute("SELECT phase FROM retention_intents WHERE task_id=?", (task_id,)).fetchone()
        return row and row[0]

    def test_stop_after_names_the_committed_phase(self):
        task_id = self.expired_task()
        self.store.retain(stop_after="dependents")
        self.assertEqual(self.phase(task_id), "task")
        self.store.retain(stop_after="task")
        self.assertEqual(self.phase(task_id), "artifact")
        self.assertEqual(self.store.retain()["deleted"], [task_id])

    def test_late_cancel_between_phases_does_not_block_later_intents(self):
        stale = self.expired_task()
        self.store.retain(stop_after="dependents")
        self.store.cancel(stale, "late")  # records task.cancel_ignored_terminal
        later = self.submit("later")
        self.manager.tick()
        self.execute(later)
        self.manager.tick()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET finished_at=1 WHERE id=?", (later,))
        result = self.store.retain()
        self.assertEqual(sorted(result["deleted"]), sorted([stale, later]))
        self.assertEqual(result["pending"], [])


class StoreOpeningTests(CoreCase):
    def tracked(self):
        connections = []
        real = sqlite3.connect

        def tracking(*args, **kwargs):
            connection = real(*args, **kwargs)
            connections.append(connection)
            return connection

        return connections, patch.object(store_module.sqlite3, "connect", side_effect=tracking)

    def assert_closed(self, connections):
        self.assertTrue(connections)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_connection_is_closed_when_migration_raises(self):
        connections, tracking = self.tracked()
        with tracking, patch.object(Store, "_migrate", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(StorageError):
                Store(self.store.home).connect()
        self.assert_closed(connections)

    def test_connection_is_closed_when_a_pragma_raises(self):
        class FailingPragma(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("PRAGMA synchronous"):
                    raise sqlite3.OperationalError("injected")
                return super().execute(sql, *args)

        connections, real = [], sqlite3.connect

        def failing_connect(*args, **kwargs):
            connections.append(real(*args, factory=FailingPragma, **kwargs))
            return connections[-1]

        with patch.object(store_module.sqlite3, "connect", side_effect=failing_connect):
            with self.assertRaises(StorageError):
                Store(self.store.home).connect()
        self.assert_closed(connections)

    def test_migration_schema_change_is_atomic_after_its_durable_intent(self):
        with self.store.transaction() as db:
            db.execute("PRAGMA user_version=1")
        original = Store._migration_tables
        calls = []

        def fail_second(db):
            calls.append(1)
            if len(calls) == 2:  # inside the schema-change transaction, after DROP/RENAME
                raise sqlite3.OperationalError("injected")
            return original(db)

        with patch.object(Store, "_migration_tables", staticmethod(fail_second)):
            with self.assertRaises(StorageError):
                Store(self.store.home).connect()
        raw = sqlite3.connect(self.store.path)
        try:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 1)
            tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("commands", tables)
            self.assertNotIn("commands_new", tables)
            self.assertEqual(raw.execute("SELECT state FROM schema_migrations WHERE version=2").fetchone()[0], "started")
        finally:
            raw.close()
        # The committed intent is resumed by the next opener.
        with Store(self.store.home).reading() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT outcome FROM schema_migrations WHERE version=2").fetchone()[0], "migrated")


if __name__ == "__main__":
    unittest.main()
