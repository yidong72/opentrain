from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from open_train.store import Store, decode
from open_train.sweeps import Sweeps, parameters, validate_config


def test_unique_concurrent_assignments_and_retry(tmp_path):
    sweeps = Sweeps(Store(tmp_path))
    sweep = sweeps.upsert(
        {
            "config": yaml.safe_dump(
                {"method": "grid", "parameters": {"lr": {"values": [1, 2, 3, 4]}}}
            )
        }
    )
    agents = [
        sweeps.register({"sweep": sweep["name"]})["agent"]["id"] for _ in range(4)
    ]

    def heartbeat(uid):
        return decode(sweeps.heartbeat({"id": uid})["commands"])[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        commands = list(pool.map(heartbeat, agents))
    assert sorted(c["args"]["lr"]["value"] for c in commands) == [1, 2, 3, 4]
    assert len({c["run_id"] for c in commands}) == 4
    assert heartbeat(agents[0]) == commands[0]
    restored = Sweeps(Store(tmp_path))
    assert decode(restored.heartbeat({"id": agents[0]})["commands"])[0] == commands[0]


def test_random_and_unsupported_sweeps():
    config = {
        "method": "random",
        "parameters": {
            "lr": {"distribution": "log_uniform_values", "min": 0.001, "max": 0.1}
        },
    }
    validate_config(config)
    values = [parameters(config, i, "test")["lr"]["value"] for i in range(100)]
    assert all(0.001 <= v <= 0.1 for v in values)
    assert parameters(config, 0, "test") == parameters(config, 0, "test")
    with pytest.raises(ValueError, match="Bayesian"):
        validate_config({**config, "method": "bayes"})
