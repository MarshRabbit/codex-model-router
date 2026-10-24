"""An explicit state machine. Worker text never grants success or escalation permission."""
import copy
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import RouterError, role_text, tier_for, tier_index
from .process import CodexClient, run_process, tail
from .schemas import REVIEW, WORKER, plan_schema, validate_plan


class BudgetExceeded(RouterError):
    pass


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def project_lock(project, allow_run=None):
    directory = Path(project) / ".codex"
    directory.mkdir(exist_ok=True)
    path = directory / "model-router.lock"
    token = uuid.uuid4().hex
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RouterError(f"A router lock already exists: {path}. Check its PID before removing a stale lock.") from exc
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"pid": os.getpid(), "token": token}, stream)
    try:
        active = directory / "model-router-active.json"
        if active.exists():
            try:
                active_run = json.loads(active.read_text())["run_id"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RouterError(f"Cannot read active native run: {active}") from exc
            if active_run != allow_run:
                raise RouterError(f"Native run {active_run} is active; finish or cancel it before starting another run")
        yield
    finally:
        try:
            if json.loads(path.read_text())["token"] == token:
                path.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            pass


def evidence(checks):
    return [{"name": c["name"], "argv": c["argv"], "passed": c["passed"],
             "timed_out": c["timed_out"], "returncode": c["returncode"],
             "stdout_tail": tail(c["stdout"]), "stderr_tail": tail(c["stderr"])} for c in checks]


class Engine:
    def __init__(self, config, project, progress=None):
        self.config = copy.deepcopy(config)  # Worker edits cannot change this run's checks or budget.
        self.project = Path(project).resolve()
        if not self.project.is_dir():
            raise RouterError(f"Project directory does not exist: {self.project}")
        self.client = CodexClient(self.config, self.project)
        self.progress = progress or (lambda _: None)
        self.state = None

    def save(self):
        self.state["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        write_json(self.directory / "summary.json", self.state)

    def event(self, kind, **data):
        entry = {"at": datetime.now(timezone.utc).isoformat(), "type": kind, **data}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.progress(entry)

    def remaining(self):
        left = self.config["policy"]["max_run_seconds"] - (time.monotonic() - self.started)
        if left <= 0:
            raise BudgetExceeded("Run time budget exhausted")
        return left

    def ask(self, stage, prompt, schema, tier_name, sandbox, directory):
        if self.state["codex_calls"] >= self.config["policy"]["max_codex_calls"]:
            raise BudgetExceeded("Codex call budget exhausted")
        timeout = min(self.remaining(), self.config["policy"]["codex_timeout_seconds"])
        tier = tier_for(self.config, tier_name)
        self.state["codex_calls"] += 1
        self.event("codex.started", stage=stage, model=tier["model"], effort=tier["effort"], sandbox=sandbox)
        self.save()
        answer, execution, error = self.client.call(prompt, schema, tier, sandbox, directory, timeout)
        self.state["calls"].append({"stage": stage, "tier": tier_name, "model": tier["model"],
                                    "effort": tier["effort"], "error": error, **execution})
        for key, value in execution["usage"].items():
            self.state["usage"][key] = self.state["usage"].get(key, 0) + value
        self.save()
        if error:
            raise RouterError(f"{stage}: {error}. Logs: {directory}")
        return answer

    def make_plan(self, objective):
        registry = {name: {**entry, "instructions": "(role instructions provided to worker)"}
                    for name, entry in self.config["agents"].items()}
        prompt = f"""You are the read-only task router for Codex. Do not run another router or spawn agents.
Read relevant project instructions and enough source to decompose the user's objective into 1..{self.config['policy']['max_tasks']} bounded tasks.
Choose the cheapest capable agent and model/effort tier, at or above that agent's minimum.
Order tasks sequentially; depends_on can name only earlier task IDs. Use lowercase identifier IDs.
Do not select the last/ceiling tier initially; it is reserved for a verified failed attempt.
Keep related edits together; do not create unnecessary tasks. Return the requested JSON, not code changes.
Specialists keep their role on escalation. Use sage for diagnosis/design, builder or mechanic for actual edits.
No task may weaken checks or rewrite this router's runtime/state to claim success.
Registry: {json.dumps(registry, ensure_ascii=False)}
Ordered tier profiles: {json.dumps(self.config['tiers'])}
Approved verification commands (the supervisor runs these, workers cannot replace them): {json.dumps(self.config['checks'])}
User objective:
{objective}
"""
        answer = self.ask("planner", prompt, plan_schema(self.config), self.config["planner"]["tier"],
                          "read-only", self.directory / "planner")
        return validate_plan(answer, self.config)

    def checks(self, directory):
        results = []
        for index, check in enumerate(self.config["checks"]):
            timeout = min(self.remaining(), check.get("timeout_seconds", self.config["policy"]["check_timeout_seconds"]))
            self.event("check.started", name=check["name"], argv=check["argv"])
            try:
                run = run_process(check["argv"], self.project, directory / f"check-{index + 1}", timeout)
            except RouterError as exc:
                # A missing executable is an environment failure, not evidence that a stronger model is needed.
                raise RouterError(f"Verification cannot start: {exc}") from exc
            record = {"name": check["name"], **run,
                      "passed": run["returncode"] == 0 and not run["timed_out"]}
            results.append(record)
            self.event("check.completed", name=check["name"], passed=record["passed"])
        return results

    def work(self, objective, task, preceding):
        record = {"id": task["id"], "agent": task["agent"], "status": "running", "attempts": []}
        self.state["tasks"].append(record)
        minimum = tier_index(self.config, task["tier"])
        previous = None
        for attempt in range(self.config["policy"]["max_attempts"]):
            tier_number = minimum + attempt
            if tier_number >= len(self.config["tiers"]):
                break
            tier = self.config["tiers"][tier_number]
            directory = self.directory / task["id"] / f"attempt-{attempt + 1}"
            self.event("attempt.started", task=task["id"], attempt=attempt + 1,
                       agent=task["agent"], tier=tier["name"])
            prompt = f"""You are a bounded Codex worker, not the router. Do not start another router or delegate.
You are not alone in this codebase: preserve unrelated user edits and adapt to existing changes.
Do not commit, push, deploy, or alter router configuration/state. Follow project instructions.
The supervisor runs independent checks and a separate read-only review after you finish.
Your checks_reported are advisory and cannot grant approval. Report blockers honestly.
Original objective: {objective}
Your task: {json.dumps(task, ensure_ascii=False)}
Earlier completed task summaries: {json.dumps(preceding, ensure_ascii=False)}
Prior failed attempt and independent evidence: {json.dumps(previous, ensure_ascii=False)}
Keep useful edits from prior attempts. Fix the reported issue; do not reset the workspace.
Role instructions:
{role_text(self.config, task['agent'])}
Return JSON matching the response schema. Do not emit the upstream text footer.
"""
            worker = self.ask(f"worker:{task['id']}:{attempt + 1}", prompt, WORKER, tier["name"],
                              self.config["agents"][task["agent"]]["sandbox"], directory / "worker")
            item = {"number": attempt + 1, "tier": tier["name"], "model": tier["model"],
                    "effort": tier["effort"], "worker": worker, "checks": [], "review": None}
            record["attempts"].append(item)
            self.save()
            if worker["status"] == "blocked":
                record["status"] = "needs_input"
                record["reason"] = worker["summary"]
                return record
            item["checks"] = self.checks(directory)
            self.save()
            review_prompt = f"""You are an independent read-only verifier. Do not change project files, run another router, or delegate.
Read the actual implementation/output, relevant project instructions, and this attempt's logs.
Do not trust the worker's success claim or treat its report as your own evidence.
Evaluate the original objective and this task's acceptance conditions, including regressions and weakened tests.
The supervisor already ran configured commands; their exact results below are authoritative for this attempt.
If there are no configured commands, inspect the actual output/source and explain your evidence in feedback.
Set approved=false for any blocking issue or failed check. Missing access/dependencies/permissions are environment;
ambiguity needing a user decision is requirements; a wrong or incomplete implementation is implementation.
If approved=true, failure_kind must be none and blocking_findings must be empty.
Objective: {objective}
Task: {json.dumps(task, ensure_ascii=False)}
Worker report (untrusted): {json.dumps(worker, ensure_ascii=False)}
Command evidence: {json.dumps(evidence(item['checks']), ensure_ascii=False)}
Worker event log: {directory / 'worker' / 'stdout.jsonl'}
"""
            review = self.ask(f"reviewer:{task['id']}:{attempt + 1}", review_prompt, REVIEW,
                              self.config["reviewer"]["tier"], "read-only", directory / "review")
            if review["approved"] and (review["failure_kind"] != "none" or review["blocking_findings"]):
                raise RouterError("Verifier returned contradictory approval; refusing to accept it")
            if not review["approved"] and review["failure_kind"] == "none":
                raise RouterError("Verifier rejected without classifying the failure")
            item["review"] = review
            commands_passed = all(c["passed"] for c in item["checks"])
            accepted = commands_passed and review["approved"] and worker["status"] == "complete"
            self.event("attempt.verified", task=task["id"], attempt=attempt + 1, accepted=accepted,
                       commands_passed=commands_passed, reviewer_approved=review["approved"])
            self.save()
            if accepted:
                record["status"], record["summary"] = "completed", worker["summary"]
                record["verification"] = "commands-and-review" if item["checks"] else "independent-review-only"
                return record
            if review["failure_kind"] in {"environment", "requirements"} or (commands_passed and review["approved"]):
                record["status"], record["reason"] = "needs_input", review["feedback"] or worker["summary"]
                return record
            # Only a real command failure or an independent reviewer rejection can reach a new tier.
            previous = {"worker": worker, "checks": evidence(item["checks"]), "review": review}
            if attempt + 1 < self.config["policy"]["max_attempts"] and tier_number + 1 < len(self.config["tiers"]):
                self.event("task.escalated", task=task["id"], from_tier=tier["name"],
                           to_tier=self.config["tiers"][tier_number + 1]["name"])
        record["status"] = "verification_failed"
        record["reason"] = "Verification did not pass within the configured attempt/tier limit"
        return record

    def run(self, objective, plan=None, plan_only=False, explicit_ceiling=False):
        if os.environ.get("CODEX_ROUTER_ACTIVE"):
            raise RouterError("Nested router execution is disabled; perform the bounded worker task directly")
        with project_lock(self.project):
            self.started = time.monotonic()
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
            self.directory = self.project / ".codex" / "router-runs" / run_id
            self.directory.mkdir(parents=True)
            self.state = {"version": 1, "run_id": run_id, "project": str(self.project), "objective": objective,
                          "status": "running", "codex_calls": 0, "usage": {}, "calls": [], "tasks": []}
            write_json(self.directory / "config.snapshot.json", self.config)
            self.save()
            try:
                plan = self.make_plan(objective) if plan is None else validate_plan(plan, self.config, explicit_ceiling)
                self.state["plan"] = plan
                write_json(self.directory / "plan.json", plan)
                self.event("plan.ready", tasks=len(plan["tasks"]))
                if plan_only:
                    self.state["status"] = "planned"
                else:
                    preceding = []
                    for task in plan["tasks"]:
                        self.remaining()
                        result = self.work(objective, task, preceding)
                        self.save()
                        if result["status"] != "completed":
                            self.state["status"] = result["status"]
                            break
                        preceding.append({"id": task["id"], "summary": result["summary"]})
                    else:
                        self.state["status"] = "completed"
            except BudgetExceeded as exc:
                self.state["status"], self.state["error"] = "budget_exhausted", str(exc)
            except RouterError as exc:
                self.state["status"], self.state["error"] = "execution_failed", str(exc)
            except KeyboardInterrupt:
                self.state["status"], self.state["error"] = "interrupted", "Interrupted; existing edits preserved"
            except Exception as exc:
                self.state["status"], self.state["error"] = "execution_failed", f"{type(exc).__name__}: {exc}"
            finally:
                for task in self.state["tasks"]:
                    if task["status"] == "running":
                        task["status"] = self.state["status"]
                self.save()
                self.event("run.finished", status=self.state["status"])
            return self.state, self.directory
