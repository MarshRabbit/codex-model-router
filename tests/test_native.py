import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "codex-model-router" / "scripts"))
from routerlib.config import RouterError, load
from routerlib.engine import Engine
from routerlib.native import Native


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native router ")
        self.project = Path(self.temp.name)
        self.config = load(self.project)
        self.config["checks"] = [{"name": "acceptance", "argv": [sys.executable, "-B", "-c",
                                      "from answer import add; assert add(2,3)==5"]}]
        self.plan = {"summary": "Implement add", "tasks": [{"id": "add", "agent": "builder", "tier": "standard",
                                                              "instructions": "Implement add", "depends_on": []}]}
        self.worker = {"status": "complete", "summary": "Implemented add", "checks_reported": ["pass"], "uncertainties": []}
        self.good = {"approved": True, "failure_kind": "none", "feedback": "Inspected source and command results", "blocking_findings": []}
        self.bad = {"approved": False, "failure_kind": "implementation", "feedback": "Source does not add", "blocking_findings": ["Incorrect output"]}
        self.count = 0

    def tearDown(self):
        self.temp.cleanup()

    def start(self):
        return Native.start(self.project, self.config, "Implement add", self.plan)

    def submit(self, result, answer, agent_id):
        self.count += 1
        path = self.project / f"result-{self.count}.json"
        path.write_text(json.dumps(answer))
        action = result["action"]
        return Native(self.project, result["run_id"]).submit(action["stage"], action["ticket"], agent_id, path)

    def write_answer(self, good):
        (self.project / "answer.py").write_text("def add(a,b): return a+b\n" if good else "def add(a,b): return 0\n")

    def test_independent_verified_retry_changes_profile_then_completes(self):
        result = self.start()
        self.assertEqual(result["action"]["model"], "gpt-6.1-sol")
        self.write_answer(False)
        result = self.submit(result, self.worker, "worker-1")
        self.assertEqual(result["action"]["stage"], "reviewer")
        self.assertIn("AssertionError", Path(result["action"]["prompt_file"]).read_text())
        result = self.submit(result, self.bad, "reviewer-1")
        self.assertEqual(result["action"]["reasoning_effort"], "high")
        self.assertIn("Source does not add", Path(result["action"]["prompt_file"]).read_text())
        self.write_answer(True)
        result = self.submit(result, self.worker, "worker-2")
        result = self.submit(result, self.good, "reviewer-2")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["issued_dispatches"], 4)
        self.assertFalse((self.project / ".codex" / "model-router-active.json").exists())

    def test_parent_report_or_reviewer_approval_cannot_override_failed_check(self):
        self.config["policy"]["max_attempts"] = 1
        result = self.start()
        self.write_answer(False)
        result = self.submit(result, self.worker, "worker")
        result = self.submit(result, self.good, "reviewer")
        self.assertEqual(result["status"], "verification_failed")

    def test_self_review_and_stale_ticket_are_rejected(self):
        original = self.start()
        self.write_answer(True)
        result = self.submit(original, self.worker, "same-id")
        with self.assertRaises(RouterError):
            self.submit(result, self.good, "same-id")
        with self.assertRaises(RouterError):
            self.submit(original, self.worker, "other-id")
        self.assertEqual(self.submit(result, self.good, "different-reviewer")["status"], "completed")

    def test_native_active_run_blocks_both_native_and_cli_writers(self):
        result = self.start()
        with self.assertRaises(RouterError):
            self.start()
        with self.assertRaises(RouterError):
            Engine(self.config, self.project).run("another task", self.plan)
        Native(self.project, result["run_id"]).cancel()
        self.assertEqual(self.start()["status"], "running")

    def test_status_does_not_duplicate_dispatch_and_cancel_is_terminal(self):
        result = self.start()
        again = Native(self.project, result["run_id"]).status()
        self.assertEqual(again["action"]["ticket"], result["action"]["ticket"])
        self.assertEqual(again["issued_dispatches"], 1)
        cancelled = Native(self.project, result["run_id"]).cancel()
        self.assertEqual(cancelled["status"], "interrupted")
        with self.assertRaises(RouterError):
            self.submit(result, self.worker, "late-worker")

    def test_dispatch_budget_does_not_release_an_unverified_task(self):
        self.config["policy"]["max_codex_calls"] = 1
        result = self.start()
        self.write_answer(True)
        result = self.submit(result, self.worker, "worker")
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertIsNone(result["action"])

    def test_worker_blocker_stops_without_spending_review_call(self):
        result = self.start()
        worker = {**self.worker, "status": "blocked"}
        result = self.submit(result, worker, "worker")
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["issued_dispatches"], 1)

    def test_no_checks_is_explicitly_review_only(self):
        self.config["checks"] = []
        result = self.start()
        result = self.submit(result, self.worker, "worker")
        result = self.submit(result, self.good, "reviewer")
        summary = json.loads(Path(result["summary_file"]).read_text())
        self.assertEqual(summary["tasks"][0]["verification"], "independent-review-only")

    def test_dependencies_run_sequentially_only_after_approval(self):
        second = {**self.plan["tasks"][0], "id": "next", "depends_on": ["add"]}
        self.plan["tasks"].append(second)
        result = self.start()
        self.write_answer(True)
        result = self.submit(result, self.worker, "worker-1")
        summary = json.loads(Path(result["summary_file"]).read_text())
        self.assertEqual(len(summary["tasks"]), 1)
        result = self.submit(result, self.good, "reviewer-1")
        self.assertEqual(result["action"]["stage"], "worker")
        summary = json.loads(Path(result["summary_file"]).read_text())
        self.assertEqual(summary["tasks"][-1]["id"], "next")
        self.assertEqual(summary["tasks"][0]["status"], "completed")

    def test_invalid_or_contradictory_review_preserves_ticket(self):
        result = self.start()
        self.write_answer(True)
        result = self.submit(result, self.worker, "worker")
        with self.assertRaises(RouterError):
            self.submit(result, {**self.good, "blocking_findings": ["still broken"]}, "reviewer")
        self.assertEqual(Native(self.project, result["run_id"]).status()["action"]["ticket"], result["action"]["ticket"])


if __name__ == "__main__":
    unittest.main()
