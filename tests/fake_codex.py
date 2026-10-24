"""Offline Codex protocol fixture. Never contacts a model service."""
import json
import subprocess
import sys
import time
from pathlib import Path

scenario_file = Path(sys.argv[1])
scenario = json.loads(scenario_file.read_text())
args = sys.argv[2:]
project = Path(args[args.index("--cd") + 1])
answer_file = Path(args[args.index("--output-last-message") + 1])
prompt = sys.stdin.read()
stage = "planner" if "read-only task router" in prompt else "reviewer" if "independent read-only verifier" in prompt else "worker"
counts_file = scenario_file.with_suffix(".counts.json")
counts = json.loads(counts_file.read_text()) if counts_file.exists() else {}
counts[stage] = counts.get(stage, 0) + 1
counts_file.write_text(json.dumps(counts))
with scenario_file.with_suffix(".calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"stage": stage, "args": args, "prompt": prompt}) + "\n")
mode = scenario.get("mode", "repair")
if mode == "infrastructure":
    print("simulated unavailable service", file=sys.stderr)
    raise SystemExit(1)
if mode == "timeout":
    child = "import time; from pathlib import Path; time.sleep(1); Path('leaked-child').write_text('bad')"
    subprocess.Popen([sys.executable, "-c", child], cwd=project)
    time.sleep(20)
if stage == "planner":
    answer = scenario.get("plan", {"summary": "Implement add", "tasks": [
        {"id": "add", "agent": "builder", "tier": "standard", "instructions": "Implement add(a,b)", "depends_on": []}]})
elif stage == "worker":
    correct = counts[stage] > 1 or mode in {"pass", "blocked", "worker_failed"}
    if mode in {"always_fail", "liar", "environment", "mutate_config"}:
        correct = False
    if mode == "mutate_config":
        (project / ".codex" / "model-router.json").write_text('{"checks": []}')
    (project / "answer.py").write_text("def add(a, b):\n    return a + b\n" if correct else "def add(a, b):\n    return 0\n")
    status = "blocked" if mode == "blocked" else "failed" if mode == "worker_failed" else "complete"
    answer = {"status": status, "summary": "Worker claims success", "checks_reported": ["all pass"], "uncertainties": []}
else:
    correct = "return a + b" in (project / "answer.py").read_text()
    approved = correct or mode == "liar"
    failure = "none" if approved else "environment" if mode == "environment" else "implementation"
    answer = {"approved": approved, "failure_kind": failure, "feedback": "Inspected answer.py",
              "blocking_findings": [] if approved else ["Implementation does not add"]}
if mode == "invalid_json":
    answer = {"unrecognized": True}
answer_file.parent.mkdir(parents=True, exist_ok=True)
answer_file.write_text(json.dumps(answer))
print(json.dumps({"type": "thread.started", "thread_id": "offline-fixture"}))
if mode != "no_terminal":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5}}))
