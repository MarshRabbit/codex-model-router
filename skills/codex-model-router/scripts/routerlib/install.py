"""Project-scoped installation, with collision checks before any write."""
import json
from pathlib import Path

from .config import RouterError, SKILL_ROOT, load, role_text, tier_for

START = "<!-- codex-model-router:start -->"
END = "<!-- codex-model-router:end -->"
DEFAULT_POLICY = f"""{START}
For coding work that needs model routing or delegated implementation, use the
`codex-model-router` skill at `.agents/skills/codex-model-router/SKILL.md`.
Use deterministic tools directly for a known one-step operation. For larger work,
use its native transport: dispatch the selected Codex model/effort profiles and
let the persistent supervisor enforce checks, review stages, and bounded retries.
The user's explicit model, scope, and authorization take priority.
Inside a routed worker (`CODEX_ROUTER_ACTIVE=1`), perform only the assigned task;
do not start another router. Native `router_*` agents are an optional manual path;
their prompts alone do not enforce the runner's verification gates.
{END}
"""


def native_agents(config):
    files = {}
    for name, agent in config["agents"].items():
        tier = tier_for(config, agent["tier"])
        instructions = (role_text(config, name) + "\n\nYou are not alone in the codebase. Preserve other edits. "
                        "Report summary, checks, uncertainties, and whether escalation is needed. "
                        "The parent must verify your output; your self-report is not approval.")
        text = (f'name = "router_{name}"\n'
                f'description = {json.dumps("Router specialist: " + ", ".join(agent["capabilities"]))}\n'
                f'model = {json.dumps(tier["model"])}\n'
                f'model_reasoning_effort = {json.dumps(tier["effort"])}\n'
                f'developer_instructions = {json.dumps(instructions)}\n')
        # Codex custom agents inherit permissions. Do not put unsupported sandbox/tool-allowlist fields here.
        files[Path(".codex") / "agents" / f"router_{name}.toml"] = text.encode("utf-8")
    return files


def install(project, enable_default=False):
    project = Path(project).resolve()
    if not project.is_dir():
        raise RouterError("Install target must be an existing project directory")
    config = load(project)
    files = {}
    for source in SKILL_ROOT.rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts and source.suffix != ".pyc":
            files[Path(".agents") / "skills" / "codex-model-router" / source.relative_to(SKILL_ROOT)] = source.read_bytes()
    files.update(native_agents(config))
    config_path = Path(".codex") / "model-router.json"
    if not (project / config_path).exists():
        files[config_path] = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if enable_default:
        path = project / "AGENTS.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if START not in existing and END not in existing:
            files[Path("AGENTS.md")] = (existing.rstrip() + "\n\n" + DEFAULT_POLICY).lstrip().encode("utf-8")
        elif existing.count(START) != 1 or existing.count(END) != 1 or existing.index(START) > existing.index(END):
            raise RouterError("Malformed existing router block in AGENTS.md")
    # AGENTS.md is the sole append operation, and is explicitly selected by --enable-default.
    for relative, content in files.items():
        target = project / relative
        for ancestor in (target, *target.parents):
            if ancestor == project:
                break
            if ancestor.is_symlink():
                raise RouterError(f"Refusing an installation through a symlink: {ancestor}")
        if relative != Path("AGENTS.md") and target.exists() and target.read_bytes() != content:
            raise RouterError(f"Installation collision; existing file preserved: {target}")
    changed = []
    for relative, content in files.items():
        target = project / relative
        if target.exists() and target.read_bytes() == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        changed.append(str(relative))
    return {"project": str(project), "written": changed, "default_policy": enable_default,
            "note": "Reopen the project/start a new Codex chat to discover the skill and custom agents."}
