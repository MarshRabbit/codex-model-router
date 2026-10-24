"""Validated project registry. No global Codex settings are modified."""
import copy
import json
import math
import re
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class RouterError(Exception):
    pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RouterError(f"Cannot read JSON {path}: {exc}") from exc


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def positive(value, where, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RouterError(f"{where} must be a positive number")
    if maximum is not None and (not isinstance(value, int) or value > maximum):
        raise RouterError(f"{where} must be an integer <= {maximum}")


def validate(config):
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
        raise RouterError("Configuration version must be 1")
    allowed = {"version", "codex_command", "tiers", "agents", "planner", "reviewer", "checks", "policy"}
    unknown = set(config) - allowed
    if unknown:
        raise RouterError(f"Unknown configuration keys: {sorted(unknown)}")
    command = config.get("codex_command")
    if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
        raise RouterError("codex_command must be a nonempty argv array")
    tiers = config.get("tiers")
    if not isinstance(tiers, list) or not 2 <= len(tiers) <= 12:
        raise RouterError("tiers must contain 2..12 ordered model/effort profiles")
    names, routes = set(), set()
    for tier in tiers:
        if not isinstance(tier, dict) or set(tier) != {"name", "model", "effort"}:
            raise RouterError("Every tier needs exactly name, model, effort")
        if not isinstance(tier["name"], str) or not NAME.fullmatch(tier["name"]) or tier["name"] in names:
            raise RouterError("Tier names must be unique identifiers")
        if (not isinstance(tier["model"], str) or not tier["model"].strip()
                or not isinstance(tier["effort"], str) or tier["effort"] not in EFFORTS):
            raise RouterError("Invalid model or reasoning effort")
        route = (tier["model"], tier["effort"])
        if route in routes:
            raise RouterError("Each tier must use a different model/effort pair")
        names.add(tier["name"])
        routes.add(route)
    agents = config.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise RouterError("agents must be a nonempty registry")
    for name, agent in agents.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(agent, dict):
            raise RouterError("Invalid agent name/entry")
        if set(agent) - {"tier", "sandbox", "capabilities", "instructions"}:
            raise RouterError(f"Unknown fields for agent {name}")
        if (not isinstance(agent.get("tier"), str) or agent["tier"] not in names
                or not isinstance(agent.get("sandbox"), str) or agent["sandbox"] not in {"read-only", "workspace-write"}):
            raise RouterError(f"Invalid tier/sandbox for {name}")
        caps = agent.get("capabilities")
        if not isinstance(caps, list) or not caps or not all(isinstance(x, str) and x for x in caps):
            raise RouterError(f"Agent {name} needs capabilities")
        if "instructions" in agent and not isinstance(agent["instructions"], str):
            raise RouterError(f"Agent {name} instructions must be text")
        if "instructions" not in agent and not (SKILL_ROOT / "roles" / f"{name}.md").is_file():
            raise RouterError(f"Agent {name} needs inline instructions")
    for field in ("planner", "reviewer"):
        if (not isinstance(config.get(field), dict) or set(config[field]) != {"tier"}
                or not isinstance(config[field]["tier"], str) or config[field]["tier"] not in names):
            raise RouterError(f"{field} must specify one configured tier")
    policy = config.get("policy")
    limits = {"max_tasks": 32, "max_attempts": 12, "max_codex_calls": 200,
              "max_run_seconds": None, "codex_timeout_seconds": None, "check_timeout_seconds": None}
    if not isinstance(policy, dict) or set(policy) != set(limits):
        raise RouterError("Invalid policy keys")
    for field, limit in limits.items():
        positive(policy[field], f"policy.{field}", limit)
    checks = config.get("checks")
    if not isinstance(checks, list):
        raise RouterError("checks must be an array")
    check_names = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"name", "argv", "timeout_seconds"}:
            raise RouterError("Invalid check entry")
        name, argv = check.get("name"), check.get("argv")
        if not isinstance(name, str) or not name or name in check_names:
            raise RouterError("Check names must be unique nonempty strings")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise RouterError(f"Check {name} needs a nonempty argv array")
        positive(check.get("timeout_seconds", policy["check_timeout_seconds"]), f"check {name} timeout")
        check_names.add(name)
    return config


def load(project, config_path=None):
    config = read_json(SKILL_ROOT / "config" / "default.json")
    path = Path(config_path) if config_path else Path(project) / ".codex" / "model-router.json"
    if config_path or path.exists():
        overlay = read_json(path)
        if not isinstance(overlay, dict):
            raise RouterError("Project configuration must be an object")
        config = merge(config, overlay)
    return validate(config)


def tier_index(config, name):
    for index, tier in enumerate(config["tiers"]):
        if tier["name"] == name:
            return index
    raise RouterError(f"Unknown tier: {name}")


def tier_for(config, name):
    return config["tiers"][tier_index(config, name)]


def role_text(config, name):
    agent = config["agents"][name]
    if "instructions" in agent:
        return agent["instructions"]
    return (SKILL_ROOT / "roles" / f"{name}.md").read_text(encoding="utf-8")
