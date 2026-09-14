import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from open_train.app import create_app
from open_train.shared import SharedStreams
from open_train.store import Store


def writer(store, name="run", wid="one", start=100):
    return store.upsert(
        {
            "name": name,
            "modelName": "sessions",
            "entityName": "local",
            "config": {
                "_wandb": {
                    "value": {
                        "e": {
                            wid: {
                                "writerId": wid,
                                "startedAt": datetime.fromtimestamp(
                                    start, timezone.utc
                                ).isoformat(),
                                "host": "worker-" + wid,
                                "program": "train.py",
                            }
                        }
                    }
                }
            },
        }
    )[0]["uid"]


def send(store, uid, steps, start=100, offset=0, stream="history"):
    rows = [
        {
            "_step": s,
            "_timestamp": start + i,
            "train/global_step": offset + i,
            "train/loss": 1 / (offset + i + 1),
        }
        for i, s in enumerate(steps)
    ]
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl"
                if stream == "history"
                else "wandb-events.jsonl": {
                    "offset": offset,
                    "content": [json.dumps(r) for r in rows],
                }
            }
        },
    )
    return rows


def test_writer_metadata_not_upserts_defines_sessions_and_resets_keep_all_rows(
    tmp_path,
):
    store = Store(tmp_path)
    uid = writer(store)
    first = send(store, uid, [0, 1], start=101)
    for _ in range(3):
        writer(store)
    assert store.sessions.count(uid, {}) == 1
    writer(store, wid="two", start=200)
    second = send(store, uid, [0, 1], start=201, offset=2)
    store.stream(uid, {"complete": True, "exitcode": 0})
    assert store.history(uid) == first + second
    assert len(store.series(uid, "train/loss")["points"]) == 4
    sessions = store.sessions.list(uid)
    assert [s["id"] for s in sessions] == ["sdk:one", "sdk:two"]
    assert [s["records"] for s in sessions] == [2, 2]
    assert sessions[0]["end_reason"] == "next_session"
    assert sessions[0]["ended"] == 102
    assert sessions[1]["state"] == "finished"
    assert sessions[1]["axes"]["train/global_step"] == {"first": 2, "last": 3}
    assert store.sessions.resume(uid)["line_count"] == 4


def test_late_metadata_and_system_only_sessions(tmp_path):
    store = Store(tmp_path)
    uid = writer(store)
    send(store, uid, [0, 1], start=101, stream="system")
    send(store, uid, [0, 1], start=201)
    writer(store, wid="two", start=200)
    sessions = store.sessions.list(uid)
    assert sessions[0]["records"] == 0
    assert sessions[0]["system_records"] == 2
    assert "no training history" in sessions[0]["warning"]
    assert sessions[0]["last_seen"] == 102
    assert sessions[1]["last_seen"] == 202
    assert sessions[1]["records"] == 2


def test_migration_is_lossless_and_recovered_metadata_splits_inferred_history(tmp_path):
    store = Store(tmp_path)
    uid = writer(store, wid="latest", start=300)
    send(store, uid, [0, 1], start=101)
    send(store, uid, [2, 3], start=201, offset=2)
    send(store, uid, [4, 5], start=301, offset=4)
    with store.connect(write=True) as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM lines ORDER BY offset")]
        db.execute("DELETE FROM sdk_deliveries")
        db.execute("DELETE FROM sdk_sessions")
        db.execute("DELETE FROM sdk_session_migrations")
        db.execute("UPDATE history_records SET session='sdk'")
    migrated = Store(tmp_path)
    assert len(migrated.sessions.list(uid)) == 2  # lower bound, not three jobs
    writer(migrated, wid="one", start=100)
    writer(migrated, wid="two", start=200)
    sessions = migrated.sessions.list(uid)
    assert [s["records"] for s in sessions] == [2, 2, 2]
    assert [s["ended"] for s in sessions] == [102, 202, None]
    assert all(not s["inferred"] for s in sessions)
    with migrated.connect() as db:
        assert before == [
            tuple(r) for r in db.execute("SELECT * FROM lines ORDER BY offset")
        ]
    assert len(Store(tmp_path).sessions.list(uid)) == 3


def test_resume_preserves_sparse_steps_raw_offsets_and_quarantined_high_water(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    uid = writer(store)
    rows = send(store, uid, [0, 50])
    # Exact event replay advances delivery offset, not event count.
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {"offset": 2, "content": [json.dumps(rows[1])]}
            }
        },
    )
    rid = store.records.catalog(uid)["records"][-1]["id"]
    store.records.quarantine(uid, [rid], None, False, "test", False)
    send(store, uid, [0], start=201, offset=3)
    # A system-style history row without _step must not erase the resume tail.
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {
                    "offset": 4,
                    "content": ['{"_timestamp":202,"loss":9}'],
                }
            }
        },
    )
    status = store.sessions.resume(uid)
    assert status["line_count"] == 5
    assert status["last_step"] == 50
    with TestClient(app) as client:
        result = client.post(
            "/graphql",
            json={
                "query": 'query RunResumeStatus { model(name:"sessions",entityName:"local") { bucket(name:"run") { historyLineCount historyTail summaryMetrics } } }'
            },
        ).json()
        run = result["data"]["model"]["bucket"]
        assert run["historyLineCount"] == 5
        assert json.loads(json.loads(run["historyTail"])[0])["_step"] == 50
        assert json.loads(run["summaryMetrics"])["_step"] == 50


def test_inferred_resets_and_auto_axis_rest_default(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    uid = store.upsert(
        {
            "name": "run",
            "config": {
                "_wandb": {"value": {"m": [{"1": "train/*", "4": "train/global_step"}]}}
            },
        }
    )[0]["uid"]
    send(store, uid, [0, 1], start=101)
    send(store, uid, [0, 1], start=201, offset=2)
    with TestClient(app) as client:
        detail = client.get(f"/api/runs/{uid}").json()
        assert detail["session_count"] == len(detail["sessions"]) == 2
        assert all(s["inferred"] for s in detail["sessions"])
        series = client.get(
            f"/api/runs/{uid}/series", params={"key": "train/loss"}
        ).json()
        assert series["axis"] == "train/global_step"
        assert [p[0] for p in series["points"]] == [0, 1, 2, 3]


def test_shared_writers_remain_independent_overlapping_provenance(tmp_path):
    store = Store(tmp_path)
    uid = writer(store)
    shared = SharedStreams(store)
    for wid in ("one", "two"):
        shared.stream(
            uid,
            wid,
            {
                "files": {
                    "wandb-history.jsonl": {
                        "offset": 0,
                        "content": [
                            json.dumps({"_step": 0, "_timestamp": 101, "loss": 1})
                        ],
                    }
                }
            },
        )
    sessions = store.sessions.list(uid)
    assert len(sessions) == store.sessions.count(uid, {}) == 2
    assert {s["id"] for s in sessions} == {"one", "two"}
    assert all(s["source"] == "sdk_shared" and s["ended"] is None for s in sessions)


def test_plot_session_starts_ignore_setup_steps_and_precede_sampling(tmp_path):
    store = Store(tmp_path)
    uid = writer(store)
    send(store, uid, range(20), start=101)
    writer(store, wid="two", start=200)
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {
                    "offset": 20,
                    "content": [
                        json.dumps(
                            {"_step": 0, "_timestamp": 200, "train/global_step": 0}
                        )
                    ],
                }
            }
        },
    )
    send(store, uid, range(1, 21), start=201, offset=21)
    series = store.series(uid, "train/loss", x="train/global_step", limit=10)
    assert series["sampled"]
    assert series["session_starts"]["sdk:two"] == {"x": 21, "timestamp": 201}
    assert store.sessions.list(uid)[1]["axes"]["train/global_step"]["first"] == 0
