import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "codex-model-router" / "scripts"))
from routerlib.config import RouterError, load, validate
from routerlib.engine import Engine, project_lock
from routerlib.install import install
from routerlib.schemas import validate_plan


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex router ")
        self.base = Path(self.temp.name)
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.scenario = self.base / "scenario.json"
        self.config = load(self.project)
        self.config["codex_command"] = [sys.executable, str(ROOT / "tests" / "fake_codex.py"), str(self.scenario)]
        self.config["checks"] = [{"name": "acceptance", "argv": [sys.executable, "-B", "-c",
            "from answer import add; assert add(2,3)==5; assert add(-1,1)==0"]}]
        self.config["policy"]["codex_timeout_seconds"] = 10
        self.plan = {"summary": "Implement add", "tasks": [{"id": "add", "agent": "builder",
                     "tier": "standard", "instructions": "Implement add", "depends_on": []}]}

    def tearDown(self):
        self.temp.cleanup()

    def run_case(self, mode="repair", plan=True):
        self.scenario.write_text(json.dumps({"mode": mode}))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_ROUTER_ACTIVE", None)
            return Engine(self.config, self.project).run("Implement add with acceptance checks", self.plan if plan else None)

    def calls(self):
        return [json.loads(line) for line in self.scenario.with_suffix(".calls.jsonl").read_text().splitlines()]

    def test_failed_real_check_routes_to_higher_effort_then_completes(self):
        state, directory = self.run_case()
        self.assertEqual(state["status"], "completed")
        attempts = state["tasks"][0]["attempts"]
        self.assertEqual([x["tier"] for x in attempts], ["standard", "deep"])
        self.assertFalse(attempts[0]["checks"][0]["passed"])
        self.assertTrue(attempts[1]["checks"][0]["passed"])
        self.assertTrue(attempts[1]["review"]["approved"])
        self.assertIn("AssertionError", self.calls()[2]["prompt"])
        self.assertEqual(state["usage"]["input_tokens"], 40)
        self.assertEqual(json.loads((directory / "summary.json").read_text())["status"], "completed")
        self.assertFalse((self.project / ".codex" / "model-router.lock").exists())

    def test_persistent_failure_reaches_other_model_and_stops(self):
        state, _ = self.run_case("always_fail")
        self.assertEqual(state["status"], "verification_failed")
        attempts = state["tasks"][0]["attempts"]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[-1]["model"], "gpt-6-astra")
        self.assertEqual(state["codex_calls"], 6)

    def test_even_reviewer_cannot_override_a_failed_command(self):
        state, _ = self.run_case("liar")
        self.assertEqual(state["status"], "verification_failed")
        self.assertTrue(state["tasks"][0]["attempts"][0]["review"]["approved"])

    def test_checks_snapshot_survives_worker_config_change(self):
        state, _ = self.run_case("mutate_config")
        self.assertEqual(state["status"], "verification_failed")
        self.assertTrue(all(len(a["checks"]) == 1 for a in state["tasks"][0]["attempts"]))

    def test_no_checks_still_requires_independent_review(self):
        self.config["checks"] = []
        state, _ = self.run_case()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(state["tasks"][0]["attempts"]), 2)
        self.assertEqual(state["tasks"][0]["verification"], "independent-review-only")

    def test_environment_failure_does_not_buy_more_models(self):
        state, _ = self.run_case("environment")
        self.assertEqual(state["status"], "needs_input")
        self.assertEqual(state["codex_calls"], 2)

    def test_worker_blocker_stops_without_false_success(self):
        state, _ = self.run_case("blocked")
        self.assertEqual(state["status"], "needs_input")
        self.assertEqual(state["codex_calls"], 1)

    def test_worker_failure_with_passing_evidence_is_not_claimed_complete(self):
        state, _ = self.run_case("worker_failed")
        self.assertEqual(state["status"], "needs_input")

    def test_infrastructure_invalid_output_and_missing_terminal_fail_closed(self):
        for mode in ("infrastructure", "invalid_json", "no_terminal"):
            with self.subTest(mode=mode):
                state, _ = self.run_case(mode)
                self.assertEqual(state["status"], "execution_failed")
                self.assertEqual(state["codex_calls"], 1)

    def test_call_budget_stops_before_more_work(self):
        self.config["policy"]["max_codex_calls"] = 1
        state, _ = self.run_case()
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["codex_calls"], 1)

    def test_timeout_kills_descendants_and_releases_lock(self):
        self.config["policy"]["codex_timeout_seconds"] = 0.2
        state, _ = self.run_case("timeout")
        self.assertEqual(state["status"], "execution_failed")
        time.sleep(1.1)
        self.assertFalse((self.project / "leaked-child").exists())
        self.assertFalse((self.project / ".codex" / "model-router.lock").exists())

    def test_planner_and_review_use_read_only_and_requested_profiles(self):
        state, _ = self.run_case("pass", plan=False)
        self.assertEqual(state["status"], "completed")
        calls = self.calls()
        self.assertEqual([x["stage"] for x in calls], ["planner", "worker", "reviewer"])
        for call in calls:
            args = call["args"]
            self.assertIn("model_reasoning_effort=", " ".join(args))
            self.assertIn("agents.enabled=false", args)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
            expected = "workspace-write" if call["stage"] == "worker" else "read-only"
            self.assertEqual(args[args.index("--sandbox") + 1], expected)

    def test_plan_only_cannot_modify_project(self):
        self.scenario.write_text('{"mode":"pass"}')
        state, _ = Engine(self.config, self.project).run("Implement add", plan_only=True)
        self.assertEqual(state["status"], "planned")
        self.assertEqual(state["codex_calls"], 1)
        self.assertFalse((self.project / "answer.py").exists())

    def test_invalid_dependencies_or_low_tier_or_initial_ceiling_rejected(self):
        for change in ({"depends_on": ["future"]}, {"tier": "light"}, {"tier": "ceiling"}):
            plan = copy.deepcopy(self.plan)
            plan["tasks"][0].update(change)
            with self.assertRaises(RouterError):
                validate_plan(plan, self.config)

    def test_invalid_plan_never_starts_worker(self):
        self.plan["tasks"][0]["depends_on"] = ["add"]
        state, _ = self.run_case()
        self.assertEqual(state["status"], "execution_failed")
        self.assertEqual(state["codex_calls"], 0)

    def test_project_lock_prevents_parallel_writers(self):
        with project_lock(self.project):
            with self.assertRaises(RouterError):
                Engine(self.config, self.project).run("Implement add", self.plan)

    def test_nested_execution_is_rejected(self):
        with patch.dict(os.environ, {"CODEX_ROUTER_ACTIVE": "1"}):
            with self.assertRaises(RouterError):
                Engine(self.config, self.project).run("Implement add", self.plan)

    def test_installer_preserves_existing_settings_and_is_idempotent(self):
        (self.project / ".codex").mkdir()
        settings = self.project / ".codex" / "config.toml"
        settings.write_text('model = "user-choice"\n')
        instructions = self.project / "AGENTS.md"
        instructions.write_text("Preserve my policy.\n")
        first = install(self.project, enable_default=True)
        self.assertTrue(first["written"])
        self.assertEqual(settings.read_text(), 'model = "user-choice"\n')
        self.assertTrue(instructions.read_text().startswith("Preserve my policy."))
        self.assertEqual(install(self.project, enable_default=True)["written"], [])
        installed = self.project / ".agents" / "skills" / "codex-model-router" / "scripts" / "router.py"
        proc = subprocess.run([sys.executable, str(installed), "routes", "--project", str(self.project)],
                              text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["agents"]["builder"]["sandbox"], "workspace-write")

    def test_installer_conflict_has_no_partial_install(self):
        target = self.project / ".codex" / "agents" / "router_scout.toml"
        target.parent.mkdir(parents=True)
        target.write_text("existing user agent")
        with self.assertRaises(RouterError):
            install(self.project)
        self.assertFalse((self.project / ".agents").exists())
        self.assertEqual(target.read_text(), "existing user agent")

    def test_installer_does_not_follow_symlinks(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".agents").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(RouterError):
            install(self.project)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_config_rejects_unknown_keys_duplicate_profiles_and_invalid_budgets(self):
        for mutate in (lambda c: c.update({"danger": True}),
                       lambda c: c["tiers"].append(copy.deepcopy(c["tiers"][0])),
                       lambda c: c["policy"].update({"max_attempts": 0})):
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.assertRaises(RouterError):
                validate(config)


if __name__ == "__main__":
    unittest.main()
