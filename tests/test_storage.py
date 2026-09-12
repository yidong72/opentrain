import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from open_train.store import Store


def run(store, name="one"):
    return store.upsert({"name": name, "entityName": "local", "modelName": "test"})[0][
        "uid"
    ]


def chunk(offset=0, count=4):
    return {
        "files": {
            "wandb-history.jsonl": {
                "offset": offset,
                "content": [
                    json.dumps({"_step": i, "loss": 1 / (i + 1), "_timestamp": 100 + i})
                    for i in range(offset, offset + count)
                ],
            }
        }
    }


def test_retry_resume_and_restart(tmp_path):
    store = Store(tmp_path)
    uid = run(store)
    store.stream(uid, chunk())
    store.stream(uid, chunk())
    assert len(store.series(uid, "loss")["points"]) == 4
    store = Store(tmp_path)
    assert store.count(uid, "wandb-history.jsonl") == 4
    store.stream(uid, chunk(4, 3))
    store.stream(uid, {"complete": True, "exitcode": 0})
    assert [row["_step"] for row in store.history(uid)] == list(range(7))
    assert store.get(uid=uid)["state"] == "finished"


def test_conflicting_retry_rolls_back_whole_transaction(tmp_path):
    store = Store(tmp_path)
    uid = run(store)
    store.stream(uid, chunk())
    conflict = chunk()
    conflict["files"]["wandb-history.jsonl"]["content"][1] = '{"_step":1,"loss":999}'
    conflict["files"] = {
        "wandb-summary.json": {"offset": 0, "content": ['{"wrong":true}']},
        **conflict["files"],
    }
    with pytest.raises(ValueError, match="Conflicting"):
        store.stream(uid, conflict)
    assert "wrong" not in store.get(uid=uid)["summary"]
    with pytest.raises(ValueError, match="Gap"):
        store.stream(uid, chunk(9))
    assert store.count(uid, "wandb-history.jsonl") == 4


def test_concurrent_runs_do_not_mix(tmp_path):
    store = Store(tmp_path)
    ids = [run(store, str(i)) for i in range(16)]

    def write(uid):
        for offset in range(0, 100, 10):
            store.stream(uid, chunk(offset, 10))
        store.stream(uid, {"complete": True, "exitcode": 0})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, ids))
    for uid in ids:
        assert store.count(uid, "wandb-history.jsonl") == 100
        assert len(store.series(uid, "loss")["points"]) == 100


def test_downsampling_keeps_spike_and_endpoints(tmp_path):
    store = Store(tmp_path)
    uid = run(store)
    rows = [
        json.dumps({"_step": i, "loss": 1000 if i == 503 else 0}) for i in range(2000)
    ]
    store.stream(
        uid, {"files": {"wandb-history.jsonl": {"offset": 0, "content": rows}}}
    )
    result = store.series(uid, "loss", limit=100)
    assert result["sampled"] and result["total"] == 2000
    assert len(result["points"]) <= 100
    assert [503, 1000] in result["points"]
    assert result["points"][0][0] == 0 and result["points"][-1][0] == 1999


def test_nonfinite_values_and_custom_axes(tmp_path):
    store = Store(tmp_path)
    uid = run(store)
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {
                    "offset": 0,
                    "content": [
                        '{"_step":0,"global_step":100,"loss":NaN}',
                        '{"_step":1,"global_step":101,"loss":0.5}',
                    ],
                }
            }
        },
    )
    assert store.series(uid, "loss", x="global_step")["points"] == [
        [100, None],
        [101, 0.5],
    ]
    assert store.history(uid)[0]["loss"] is None
