"""Persistent supervisor for Codex's native subagents; no nested Codex process.

The parent bridges real subagent results to this controller. It is a workflow
gate, not an authentication service for subagent identities or a security boundary.
"""
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import RouterError, load, read_json, role_text, tier_for, tier_index, validate
from .engine import BudgetExceeded, Engine, evidence, project_lock, write_json
from .schemas import REVIEW, WORKER, validate as validate_output, validate_plan


class Native(Engine):
    def __init__(self, project, run_id):
        self.project = Path(project).resolve()
        if not isinstance(run_id, str) or not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
            raise RouterError("Invalid native run ID")
        self.directory = self.project / ".codex" / "router-runs" / run_id
        self.state = read_json(self.directory / "summary.json")
        if self.state.get("transport") != "native" or self.state.get("project") != str(self.project):
            raise RouterError("Not a native run for this project")
        self.config = validate(read_json(self.directory / "config.snapshot.json"))
        self.started = time.monotonic() - max(0, time.time() - self.state["started_epoch"])
        self.progress = lambda _: None

    @staticmethod
    def start(project, config, objective, plan, explicit_ceiling=False):
        project = Path(project).resolve()
        if not project.is_dir():
            raise RouterError("Project must exist")
        validate_plan(plan, config, explicit_ceiling)
        with project_lock(project):
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
            directory = project / ".codex" / "router-runs" / run_id
            directory.mkdir(parents=True)
            state = {"version": 1, "transport": "native", "run_id": run_id, "project": str(project),
                     "objective": objective, "plan": plan, "started_epoch": time.time(), "status": "running",
                     "codex_calls": 0, "calls": [], "usage": {}, "tasks": [], "pending": None}
            write_json(directory / "config.snapshot.json", config)
            write_json(directory / "plan.json", plan)
            write_json(directory / "summary.json", state)
            write_json(project / ".codex" / "model-router-active.json", {"run_id": run_id})
            runner = Native(project, run_id)
            try:
                runner.next_task()
            except RouterError as exc:
                runner.stop("budget_exhausted" if isinstance(exc, BudgetExceeded) else "execution_failed", str(exc))
            runner.save()
            return runner.result()

    def stop(self, status, reason=None):
        self.state["status"], self.state["pending"] = status, None
        if reason:
            self.state["error"] = reason
        for task in self.state["tasks"]:
            if task["status"] == "running":
                task["status"] = status
        active = self.project / ".codex" / "model-router-active.json"
        if active.exists() and read_json(active).get("run_id") == self.state["run_id"]:
            active.unlink()
        self.event("run.finished", status=status)

    def issue(self, stage, tier_name, role, read_only, prompt, schema):
        self.remaining()
        if self.state["codex_calls"] >= self.config["policy"]["max_codex_calls"]:
            raise BudgetExceeded("Native subagent dispatch budget exhausted")
        self.state["codex_calls"] += 1
        number = self.state["codex_calls"]
        directory = self.directory / f"dispatch-{number:03d}-{stage}"
        directory.mkdir()
        prompt_path = directory / "prompt.txt"
        prompt_path.write_text(prompt + "\nReturn only JSON matching this schema:\n" + json.dumps(schema), encoding="utf-8")
        write_json(directory / "schema.json", schema)
        tier = tier_for(self.config, tier_name)
        self.state["pending"] = {"stage": stage, "ticket": uuid.uuid4().hex, "role": role,
                                 "model": tier["model"], "reasoning_effort": tier["effort"],
                                 "read_only": read_only, "prompt_file": str(prompt_path),
                                 "schema_file": str(directory / "schema.json"), "directory": str(directory)}
        self.event("native.dispatch", stage=stage, role=role, model=tier["model"], effort=tier["effort"])

    def next_task(self):
        index = len(self.state["tasks"])
        if index == len(self.state["plan"]["tasks"]):
            self.stop("completed")
            return
        task = self.state["plan"]["tasks"][index]
        self.state["tasks"].append({"id": task["id"], "agent": task["agent"], "status": "running", "attempts": []})
        self.issue_worker(task, tier_index(self.config, task["tier"]), None)

    def issue_worker(self, task, tier_number, previous):
        record = self.state["tasks"][-1]
        tier = self.config["tiers"][tier_number]
        attempt = {"number": len(record["attempts"]) + 1, "tier": tier["name"], "tier_number": tier_number,
                   "model": tier["model"], "effort": tier["effort"], "worker": None,
                   "checks": [], "review": None, "worker_agent_id": None}
        record["attempts"].append(attempt)
        preceding = [{"id": t["id"], "summary": t.get("summary")} for t in self.state["tasks"][:-1]]
        prompt = f"""Perform this bounded task as a Codex subagent. You are not alone in the codebase.
Preserve unrelated edits, follow project instructions, and do not run the router or delegate.
Do not edit the supervisor's configuration, state, evidence files, or checks to claim success.
The parent supervisor will run independent checks and a separate reviewer.
Objective: {self.state['objective']}
Task: {json.dumps(task, ensure_ascii=False)}
Earlier completed tasks: {json.dumps(preceding, ensure_ascii=False)}
Previous failed attempt and independent evidence: {json.dumps(previous, ensure_ascii=False)}
Role instructions:
{role_text(self.config, task['agent'])}
"""
        self.issue("worker", tier["name"], task["agent"], self.config["agents"][task["agent"]]["sandbox"] == "read-only", prompt, WORKER)

    def current_task(self):
        return self.state["plan"]["tasks"][len(self.state["tasks"]) - 1]

    def result(self):
        return {"run_id": self.state["run_id"], "status": self.state["status"], "transport": "native",
                "run_dir": str(self.directory), "summary_file": str(self.directory / "summary.json"),
                "issued_dispatches": self.state["codex_calls"], "action": self.state.get("pending"),
                "error": self.state.get("error")}

    def submit(self, stage, ticket, agent_id, result_file):
        with project_lock(self.project, allow_run=self.state["run_id"]):
            # Reload under the lock: stale parent submissions cannot consume a new ticket.
            self.state = read_json(self.directory / "summary.json")
            pending = self.state.get("pending")
            if self.state["status"] != "running" or not pending or pending["stage"] != stage or pending["ticket"] != ticket:
                raise RouterError("Stale/wrong native result ticket or stage")
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise RouterError("A real Codex subagent ID is required")
            answer = validate_output(read_json(result_file), WORKER if stage == "worker" else REVIEW)
            attempt = self.state["tasks"][-1]["attempts"][-1]
            if stage == "reviewer" and agent_id == attempt["worker_agent_id"]:
                raise RouterError("Worker cannot review its own attempt; use a separate subagent")
            if stage == "reviewer" and ((answer["approved"] and (answer["failure_kind"] != "none" or answer["blocking_findings"]))
                                        or (not answer["approved"] and answer["failure_kind"] == "none")):
                raise RouterError("Contradictory native review result")
            # A ticket is consumed once; invalid submissions above leave it available for correction.
            write_json(Path(pending["directory"]) / "answer.json", answer)
            self.state["calls"].append({"stage": stage, "agent_id": agent_id,
                                       "requested_model": pending["model"], "requested_effort": pending["reasoning_effort"]})
            self.state["pending"] = None
            try:
                self.remaining()
                if stage == "worker":
                    self.accept_worker(answer, agent_id)
                else:
                    self.accept_review(answer)
            except RouterError as exc:
                self.stop("budget_exhausted" if isinstance(exc, BudgetExceeded) else "execution_failed", str(exc))
            except KeyboardInterrupt:
                self.stop("interrupted", "Interrupted while processing native result")
            finally:
                self.save()
            return self.result()

    def accept_worker(self, answer, agent_id):
        record = self.state["tasks"][-1]
        attempt = record["attempts"][-1]
        attempt["worker"], attempt["worker_agent_id"] = answer, agent_id
        if answer["status"] == "blocked":
            self.stop("needs_input", answer["summary"])
            return
        directory = self.directory / record["id"] / f"attempt-{attempt['number']}"
        attempt["checks"] = self.checks(directory)
        task = self.current_task()
        prompt = f"""You are an independent read-only reviewer, separate from the worker. Do not edit or delegate.
Inspect the actual output/files and the authoritative command evidence. Do not trust the worker's self-report.
Verify the objective, task scope, regressions, and test integrity. If checks failed, approved must be false.
With no checks, explain concrete inspection evidence; this will be labelled review-only.
Use failure_kind environment for missing dependencies/permissions and requirements for user decisions;
use implementation for an incorrect/incomplete result. Approval requires failure_kind none and no blockers.
Objective: {self.state['objective']}
Task: {json.dumps(task, ensure_ascii=False)}
Worker report (untrusted): {json.dumps(answer, ensure_ascii=False)}
Command evidence: {json.dumps(evidence(attempt['checks']), ensure_ascii=False)}
"""
        self.issue("reviewer", self.config["reviewer"]["tier"], "reviewer", True, prompt, REVIEW)

    def accept_review(self, answer):
        record = self.state["tasks"][-1]
        attempt = record["attempts"][-1]
        attempt["review"] = answer
        checks_passed = all(c["passed"] for c in attempt["checks"])
        if checks_passed and answer["approved"] and attempt["worker"]["status"] == "complete":
            record["status"], record["summary"] = "completed", attempt["worker"]["summary"]
            record["verification"] = "commands-and-review" if attempt["checks"] else "independent-review-only"
            self.event("task.completed", task=record["id"])
            self.next_task()
        elif answer["failure_kind"] in {"environment", "requirements"} or (checks_passed and answer["approved"]):
            self.stop("needs_input", answer["feedback"])
        elif len(record["attempts"]) >= self.config["policy"]["max_attempts"] or attempt["tier_number"] + 1 >= len(self.config["tiers"]):
            self.stop("verification_failed", "Native attempt/tier limit exhausted")
        else:
            previous = {"worker": attempt["worker"], "checks": evidence(attempt["checks"]), "review": answer}
            self.event("task.escalated", task=record["id"], from_tier=attempt["tier"],
                       to_tier=self.config["tiers"][attempt["tier_number"] + 1]["name"])
            self.issue_worker(self.current_task(), attempt["tier_number"] + 1, previous)

    def cancel(self):
        with project_lock(self.project, allow_run=self.state["run_id"]):
            self.state = read_json(self.directory / "summary.json")
            if self.state["status"] == "running":
                self.stop("interrupted", "Parent cancelled; stop any still-running subagent separately")
                self.save()
        return self.result()

    def status(self):
        with project_lock(self.project, allow_run=self.state["run_id"]):
            self.state = read_json(self.directory / "summary.json")
            if self.state["status"] == "running":
                try:
                    self.remaining()
                except BudgetExceeded as exc:
                    self.stop("budget_exhausted", str(exc))
                    self.save()
        return self.result()
