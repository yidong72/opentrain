"""Transactional grid/random scheduling for the W&B agent protocol."""

import math
import random
import time
import uuid

import yaml

from .accounts import default_entity
from .store import decode, dumps


def validate_config(config):
    if not isinstance(config, dict) or config.get("method", "grid") not in (
        "grid",
        "random",
    ):
        raise ValueError(
            "Supported sweep methods: grid, random; Bayesian scheduling is not implemented"
        )
    if config.get("early_terminate") or config.get("controller"):
        raise ValueError("Early termination and custom controllers are not implemented")
    params = config.get("parameters")
    if not isinstance(params, dict) or not params:
        raise ValueError("Sweep requires a non-empty parameters mapping")
    cap = config.get("run_cap")
    if cap is not None and (not isinstance(cap, int) or cap < 1):
        raise ValueError("run_cap must be a positive integer")
    for name, spec in params.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Invalid parameter: {name}")
        if "value" in spec:
            continue
        if spec.get("values"):
            if spec.get("probabilities"):
                raise ValueError("Weighted categorical sampling is not implemented")
            continue
        if config.get("method") != "random":
            raise ValueError(f"Grid parameter {name} needs values or value")
        if spec.get("distribution") not in (
            "uniform",
            "int_uniform",
            "log_uniform_values",
            "log_uniform",
            "normal",
        ):
            raise ValueError(
                f"Unsupported distribution for {name}: {spec.get('distribution')}"
            )
        if spec["distribution"] == "normal":
            if spec.get("sigma", 1) <= 0:
                raise ValueError("normal sigma must be positive")
        elif "min" not in spec or "max" not in spec or spec["min"] > spec["max"]:
            raise ValueError(f"Invalid bounds for {name}")
        elif spec["distribution"] == "log_uniform_values" and spec["min"] <= 0:
            raise ValueError("log_uniform_values requires positive bounds")


def parameters(config, index, seed):
    rng = random.Random(f"{seed}:{index}")
    result = {}
    for name, spec in config["parameters"].items():
        if "value" in spec:
            value = spec["value"]
        elif "values" in spec:
            values = spec["values"]
            value = (
                values[index % len(values)]
                if config.get("method", "grid") == "grid"
                else rng.choice(values)
            )
            if config.get("method", "grid") == "grid":
                index //= len(values)
        else:
            distribution = spec["distribution"]
            if distribution == "normal":
                value = rng.gauss(spec.get("mu", 0), spec.get("sigma", 1))
            elif distribution == "int_uniform":
                value = rng.randint(spec["min"], spec["max"])
            elif distribution == "log_uniform_values":
                value = math.exp(
                    rng.uniform(math.log(spec["min"]), math.log(spec["max"]))
                )
            elif distribution == "log_uniform":
                value = math.exp(rng.uniform(spec["min"], spec["max"]))
            else:
                value = rng.uniform(spec["min"], spec["max"])
        result[name] = {"value": value}
    return result


class Sweeps:
    def __init__(self, store):
        self.store = store

    def upsert(self, data):
        entity = data.get("entityName") or default_entity()
        self.store.authorize(entity, True)
        if data.get("id"):
            raise ValueError("Updating sweeps is not implemented; create a new sweep")
        config = yaml.safe_load(data.get("config", ""))
        validate_config(config)
        uid, name = uuid.uuid4().hex, uuid.uuid4().hex[:8]
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO sweeps VALUES (?,?,?,?,?,?,?,?)",
                (
                    uid,
                    entity,
                    data.get("projectName") or "uncategorized",
                    name,
                    yaml.safe_dump(config),
                    "RUNNING",
                    0,
                    time.time(),
                ),
            )
            return dict(
                db.execute("SELECT * FROM sweeps WHERE uid=?", (uid,)).fetchone()
            )

    def get(self, entity, project, name):
        self.store.authorize(entity)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM sweeps WHERE entity=? AND project=? AND name=?",
                (entity, project, name),
            ).fetchone()
            return dict(row) if row else None

    def register(self, data):
        sweep = self.get(
            data.get("entityName") or default_entity(),
            data.get("projectName") or "uncategorized",
            data["sweep"],
        )
        if not sweep:
            raise ValueError("Sweep not found")
        self.store.authorize(sweep["entity"], True)
        uid = uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO agents VALUES (?,?,NULL,?)",
                (uid, sweep["uid"], time.time()),
            )
        return {"agent": {"id": uid}}

    def heartbeat(self, data):
        with self.store.connect(write=True) as db:
            agent = db.execute(
                "SELECT * FROM agents WHERE uid=?", (data["id"],)
            ).fetchone()
            if not agent:
                raise ValueError("Agent not found")
            sweep = dict(
                db.execute(
                    "SELECT * FROM sweeps WHERE uid=?", (agent["sweep"],)
                ).fetchone()
            )
            self.store.authorize(sweep["entity"], True)
            states = decode(data.get("runState"))
            command = decode(agent["assignment"]) if agent["assignment"] else None
            if command:
                run = self.store.get(
                    sweep["entity"], sweep["project"], command["run_id"], db=db
                )
                done = run and run["state"] in ("finished", "failed")
                if not done:
                    # Redeliver an unacknowledged assignment; never allocate twice on retry.
                    return {
                        "agent": {"id": agent["uid"]},
                        "commands": dumps(
                            [] if command["run_id"] in states else [command]
                        ),
                    }
            if states:
                return {"agent": {"id": agent["uid"]}, "commands": "[]"}
            config, index = yaml.safe_load(sweep["config"]), sweep["next_index"]
            total = config.get("run_cap", 100)
            if config.get("method", "grid") == "grid":
                total = min(
                    config.get("run_cap", 2**63 - 1),
                    math.prod(
                        len(p.get("values", [0])) for p in config["parameters"].values()
                    ),
                )
            if index >= total:
                commands = [{"type": "exit"}]
                finished = db.execute(
                    "SELECT COUNT(*) FROM runs WHERE entity=? AND project=? AND sweep=? AND state IN ('finished','failed')",
                    (sweep["entity"], sweep["project"], sweep["name"]),
                ).fetchone()[0]
                if finished >= total:
                    db.execute(
                        "UPDATE sweeps SET state='FINISHED' WHERE uid=?",
                        (sweep["uid"],),
                    )
            else:
                command = {
                    "type": "run",
                    "run_id": uuid.uuid4().hex[:8],
                    "args": parameters(config, index, sweep["uid"]),
                    "program": config.get("program", ""),
                    "project": sweep["project"],
                    "entity": sweep["entity"],
                }
                commands = [command]
                db.execute(
                    "UPDATE sweeps SET next_index=next_index+1 WHERE uid=?",
                    (sweep["uid"],),
                )
                db.execute(
                    "UPDATE agents SET assignment=?,updated=? WHERE uid=?",
                    (dumps(command), time.time(), agent["uid"]),
                )
            return {"agent": {"id": agent["uid"]}, "commands": dumps(commands)}
