"""Small schemas plus local validation: schema-constrained output is still checked."""
from .config import RouterError


def array(items, **kwargs):
    return {"type": "array", "items": items, **kwargs}


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
TEXTS = array(TEXT)
WORKER = obj({"status": {"type": "string", "enum": ["complete", "failed", "blocked"]},
              "summary": {"type": "string", "minLength": 1}, "checks_reported": TEXTS, "uncertainties": TEXTS})
REVIEW = obj({"approved": {"type": "boolean"},
              "failure_kind": {"type": "string", "enum": ["none", "implementation", "environment", "requirements"]},
              "feedback": {"type": "string", "minLength": 1}, "blocking_findings": TEXTS})


def plan_schema(config):
    task = obj({"id": {"type": "string", "minLength": 1},
                "agent": {"type": "string", "enum": list(config["agents"])},
                "tier": {"type": "string", "enum": [t["name"] for t in config["tiers"]]},
                "instructions": {"type": "string", "minLength": 1}, "depends_on": TEXTS})
    return obj({"summary": TEXT, "tasks": array(task, minItems=1, maxItems=config["policy"]["max_tasks"])})


def validate(value, schema, path="response"):
    kind = schema["type"]
    checks = {"object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list),
              "string": lambda: isinstance(value, str), "boolean": lambda: isinstance(value, bool)}
    if not checks[kind]():
        raise RouterError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise RouterError(f"{path} is not an allowed value")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise RouterError(f"{path} has missing or unknown fields")
        for key, child in schema["properties"].items():
            validate(value[key], child, f"{path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10000):
            raise RouterError(f"{path} has an invalid number of items")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]")
    elif kind == "string" and len(value.strip()) < schema.get("minLength", 0):
        raise RouterError(f"{path} cannot be blank")
    return value


def validate_plan(plan, config, explicit_ceiling=False):
    validate(plan, plan_schema(config), "plan")
    from .config import NAME, tier_index
    seen = set()
    for task in plan["tasks"]:
        if not NAME.fullmatch(task["id"]) or task["id"] in seen:
            raise RouterError("Task IDs must be unique identifiers")
        if len(set(task["depends_on"])) != len(task["depends_on"]) or not set(task["depends_on"]) <= seen:
            raise RouterError("Dependencies must name earlier tasks exactly once (no cycles/forward references)")
        rank = tier_index(config, task["tier"])
        minimum = tier_index(config, config["agents"][task["agent"]]["tier"])
        if rank < minimum:
            raise RouterError(f"Task {task['id']} tier is below its agent's minimum")
        if rank == len(config["tiers"]) - 1 and not explicit_ceiling:
            raise RouterError("Initial ceiling requires an explicit --tier selection; reserve it for verified retries")
        seen.add(task["id"])
    return plan
