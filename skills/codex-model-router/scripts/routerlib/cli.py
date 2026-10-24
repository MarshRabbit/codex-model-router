"""Dependency-free command line interface."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import RouterError, SKILL_ROOT, load, read_json, tier_for, validate
from .engine import Engine
from .install import install


def parser():
    root = argparse.ArgumentParser(description="Codex model/effort router with independent verification and bounded retries")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("run", "plan", "routes", "doctor", "stats", "install"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--project", default=".", help="Target project directory")
        if name != "install":
            cmd.add_argument("--config", help="Explicit router JSON override")
        if name in {"run", "plan"}:
            cmd.add_argument("task", nargs="?", help="User objective; use --task-file for long requests")
            cmd.add_argument("--task-file")
            cmd.add_argument("--agent", help="Explicit agent, bypassing the planner")
            cmd.add_argument("--tier", help="Explicit initial model/effort tier (requires --agent)")
            cmd.add_argument("--plan-file", help="Execute a previously reviewed plan; its checks still come from trusted config")
            cmd.add_argument("--check", action="append", default=[], help="Additional trusted check, parsed as argv (no implicit shell)")
            cmd.add_argument("--max-attempts", type=int)
            cmd.add_argument("--quiet", action="store_true", help="Only print the final JSON record")
        if name == "install":
            cmd.add_argument("--enable-default", action="store_true", help="Append a marked routing policy to AGENTS.md")
    native = sub.add_parser("native", help="Supervise Codex native subagents without a nested CLI sandbox")
    actions = native.add_subparsers(dest="action", required=True)
    for action in ("start", "status", "worker-result", "review-result", "cancel"):
        cmd = actions.add_parser(action)
        cmd.add_argument("--project", default=".")
        if action == "start":
            cmd.add_argument("task", nargs="?")
            cmd.add_argument("--task-file")
            cmd.add_argument("--config")
            cmd.add_argument("--agent")
            cmd.add_argument("--tier")
            cmd.add_argument("--plan-file")
            cmd.add_argument("--check", action="append", default=[])
            cmd.add_argument("--max-attempts", type=int)
        else:
            cmd.add_argument("--run-id", required=True)
        if action in {"worker-result", "review-result"}:
            cmd.add_argument("--ticket", required=True)
            cmd.add_argument("--agent-id", required=True)
            cmd.add_argument("--result-file", required=True)
    return root


def native_command(args, project):
    from .native import Native
    if args.action != "start":
        native = Native(project, args.run_id)
        if args.action == "cancel":
            return native.cancel()
        if args.action == "status":
            return native.status()
        stage = "worker" if args.action == "worker-result" else "reviewer"
        return native.submit(stage, args.ticket, args.agent_id, args.result_file)
    config = load(project, args.config)
    if args.task and args.task_file:
        raise RouterError("Use task or --task-file")
    objective = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else args.task
    if not objective or not objective.strip():
        raise RouterError("A user objective is required")
    if args.tier and not args.agent:
        raise RouterError("--tier requires --agent")
    if bool(args.agent) == bool(args.plan_file):
        raise RouterError("Native start requires exactly one of --agent or --plan-file (the parent routes/decomposes)")
    for index, text in enumerate(args.check):
        config["checks"].append({"name": f"cli-check-{index + 1}", "argv": shlex.split(text)})
    if args.max_attempts is not None:
        config["policy"]["max_attempts"] = args.max_attempts
    validate(config)
    if args.agent:
        if args.agent not in config["agents"]:
            raise RouterError(f"Unknown agent: {args.agent}")
        tier = args.tier or config["agents"][args.agent]["tier"]
        plan = {"summary": objective, "tasks": [{"id": "task-1", "agent": args.agent, "tier": tier,
                 "instructions": objective, "depends_on": []}]}
    else:
        plan = read_json(args.plan_file)
    return Native.start(project, config, objective, plan, explicit_ceiling=bool(args.tier))


def progress(entry):
    if entry["type"] in {"attempt.started", "task.escalated", "run.finished"}:
        print(json.dumps(entry, ensure_ascii=False), file=sys.stderr, flush=True)


def doctor(config):
    command = config["codex_command"]
    executable = shutil.which(command[0])
    if not executable:
        raise RouterError(f"Codex command not found: {command[0]}")
    required = {"--output-schema", "--output-last-message", "--json", "--ephemeral", "--sandbox"}
    try:
        result = subprocess.run([*command, "exec", "--help"], text=True, capture_output=True, timeout=15)
        if result.returncode or not all(flag in result.stdout for flag in required):
            raise RouterError("Codex CLI does not expose the required exec flags; update Codex")
        auth = subprocess.run([*command, "login", "status"], text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RouterError(f"Codex preflight failed: {exc}") from exc
    # Do not print authentication output; it is neither a model-access check nor a guarantee of connectivity.
    return {"command": command, "exec_flags": "supported", "login_status_ok": auth.returncode == 0,
            "model_access": "not tested; run a small task with the configured profiles",
            "python": sys.version.split()[0]}


def stats(project):
    records = []
    for path in sorted((Path(project) / ".codex" / "router-runs").glob("*/summary.json")):
        try:
            record = read_json(path)
        except RouterError:
            continue
        native = record.get("transport") == "native"
        records.append({"run_id": record.get("run_id"), "status": record.get("status"),
                        "transport": "native" if native else "cli",
                        "codex_calls": None if native else record.get("codex_calls"),
                        "issued_dispatches": record.get("codex_calls") if native else None,
                        "usage": record.get("usage"),
                        "escalations": sum(max(0, len(t.get("attempts", [])) - 1) for t in record.get("tasks", []))})
    return {"runs": records, "pricing": "No inferred dollar savings. Usage is reported by Codex; pricing/quota depends on your account."}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        project = Path(args.project).resolve()
        if args.command == "native":
            result = native_command(args, project)
            code = {"running": 0, "completed": 0, "verification_failed": 2, "needs_input": 3,
                    "budget_exhausted": 4, "interrupted": 130}.get(result["status"], 1)
        elif args.command == "install":
            result, code = install(project, args.enable_default), 0
        else:
            config = load(project, args.config)
            if args.command == "routes":
                result, code = {"tiers": config["tiers"], "agents": config["agents"], "checks": config["checks"]}, 0
            elif args.command == "doctor":
                result = doctor(config)
                code = 0 if result["login_status_ok"] else 1
            elif args.command == "stats":
                result, code = stats(project), 0
            else:
                if args.task and args.task_file:
                    raise RouterError("Use either task or --task-file")
                objective = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else args.task
                if not objective or not objective.strip():
                    raise RouterError("A nonempty user objective is required")
                if args.tier and not args.agent:
                    raise RouterError("--tier requires --agent")
                if args.agent and args.plan_file:
                    raise RouterError("Use --agent or --plan-file, not both")
                for index, text in enumerate(args.check):
                    config["checks"].append({"name": f"cli-check-{index + 1}", "argv": shlex.split(text)})
                if args.max_attempts is not None:
                    config["policy"]["max_attempts"] = args.max_attempts
                validate(config)
                plan = read_json(args.plan_file) if args.plan_file else None
                if args.agent:
                    if args.agent not in config["agents"]:
                        raise RouterError(f"Unknown agent: {args.agent}")
                    tier = args.tier or config["agents"][args.agent]["tier"]
                    tier_for(config, tier)
                    plan = {"summary": objective, "tasks": [{"id": "task-1", "agent": args.agent,
                            "tier": tier, "instructions": objective, "depends_on": []}]}
                state, directory = Engine(config, project, None if args.quiet else progress).run(
                    objective, plan, args.command == "plan", explicit_ceiling=bool(args.tier))
                result = {"status": state["status"], "run_dir": str(directory),
                          "summary_file": str(directory / "summary.json"),
                          "codex_calls": state["codex_calls"], "usage": state["usage"],
                          "tasks": [{"id": t["id"], "status": t["status"], "attempts": len(t["attempts"]),
                                     "summary": t.get("summary"), "reason": t.get("reason"),
                                     "verification": t.get("verification")} for t in state["tasks"]]}
                if "error" in state:
                    result["error"] = state["error"]
                if args.command == "plan" and "plan" in state:
                    result["plan"] = state["plan"]
                code = {"completed": 0, "planned": 0, "verification_failed": 2,
                        "needs_input": 3, "budget_exhausted": 4, "interrupted": 130}.get(state["status"], 1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (RouterError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
