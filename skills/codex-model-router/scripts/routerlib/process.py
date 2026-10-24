"""Subprocess boundaries: argv only, bounded execution, descendants terminated on timeout."""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .config import RouterError, SKILL_ROOT
from .schemas import validate


def tail(path, limit=16000):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


def terminate(proc):
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        proc.wait()


def run_process(argv, cwd, directory, timeout, stdin=None, env=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    stdout, stderr = directory / "stdout.jsonl", directory / "stderr.log"
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=out, stderr=err, env=env, start_new_session=os.name == "posix")
            try:
                proc.communicate(None if stdin is None else stdin.encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate(proc)
            except BaseException:
                terminate(proc)
                raise
        return {"argv": argv, "returncode": proc.returncode, "timed_out": timed_out,
                "seconds": round(time.monotonic() - started, 3),
                "stdout": str(stdout), "stderr": str(stderr)}
    except OSError as exc:
        raise RouterError(f"Cannot execute {argv[0]}: {exc}") from exc


def events_of(path):
    usage = {}
    completed, failed = False, False
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.failed" or event.get("type") == "error":
                failed = True
            if event.get("type") == "turn.completed":
                completed = True
                for key, value in (event.get("usage") or {}).items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                        usage[key] = usage.get(key, 0) + value
    return completed, failed, usage


class CodexClient:
    def __init__(self, config, project):
        self.config, self.project = config, Path(project)

    def call(self, prompt, schema, tier, sandbox, directory, timeout):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        schema_path, answer_path = directory / "schema.json", directory / "answer.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        # Both installed and checkout copies are disabled only in this child, preventing recursive routing.
        skill_paths = {SKILL_ROOT / "SKILL.md", self.project / ".agents" / "skills" / "codex-model-router" / "SKILL.md"}
        disabled = "[" + ",".join("{path=" + json.dumps(str(p)) + ",enabled=false}" for p in sorted(skill_paths)) + "]"
        argv = [*self.config["codex_command"], "--no-daemon", "-a", "never", "exec", "--ephemeral",
                "--skip-git-repo-check", "--color", "never", "--json", "--sandbox", sandbox,
                "--cd", str(self.project), "--model", tier["model"],
                "-c", "model_reasoning_effort=" + json.dumps(tier["effort"]),
                "-c", "agents.enabled=false", "-c", "skills.config=" + disabled,
                "--output-schema", str(schema_path), "--output-last-message", str(answer_path), "-"]
        env = {**os.environ, "CODEX_ROUTER_ACTIVE": "1"}
        result = run_process(argv, self.project, directory, timeout, prompt, env)
        completed, failed, usage = events_of(result["stdout"])
        result["usage"] = usage
        if result["timed_out"] or result["returncode"] != 0 or failed or not completed:
            return None, result, "Codex execution failed, timed out, or emitted no successful turn; inspect logs"
        try:
            answer = json.loads(answer_path.read_text(encoding="utf-8"))
            validate(answer, schema)
            return answer, result, None
        except (OSError, ValueError, RouterError) as exc:
            return None, result, f"Invalid structured response: {exc}"
