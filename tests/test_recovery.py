import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from open_train.app import create_app
from open_train.records import metric_axes
from open_train.store import Store


def make_run(store, name="test"):
    return store.upsert({"entityName": "local", "modelName": "recovery", "name": name})[
        0
    ]["uid"]


def send(store, uid, rows, offset=0):
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {
                    "offset": offset,
                    "content": [json.dumps(r) for r in rows],
                }
            }
        },
    )


def test_custom_axis_never_joins_different_records_or_sessions(tmp_path):
    store = Store(tmp_path)
    uid = make_run(store)
    rows = [
        {"_step": 0, "_timestamp": 10, "loss": 99},
        {"_step": 0, "_timestamp": 20, "train/global_step": 100, "reward": 1},
        {"_step": 0, "_timestamp": 30, "train/global_step": 101, "loss": 2},
    ]
    send(store, uid, rows)
    result = store.series(uid, "loss", x="train/global_step")
    assert result["points"] == [[101, 2]]
    assert result["missing_axis"] == 1
    assert store.series(uid, "loss")["points"] == [[0, 99], [0, 2]]
    assert store.history(uid) == rows
    assert Store(tmp_path).series(uid, "loss", x="train/global_step") == result


def test_offline_replay_with_reordered_keys_and_new_offset_is_idempotent(tmp_path):
    store = Store(tmp_path)
    uid = make_run(store)
    row = {"_step": 0, "_timestamp": 100, "loss": 1}
    send(store, uid, [row])
    send(store, uid, [{"loss": 1, "_timestamp": 100, "_step": 0}], 1)
    assert store.count(uid, "wandb-history.jsonl") == 2
    assert store.history(uid) == [row]
    rid = store.records.catalog(uid)["records"][0]["id"]
    store.records.quarantine(uid, [rid], None, False, "test", False)
    send(store, uid, [row], 2)
    assert store.history(uid) == []
    send(store, uid, [{**row, "_timestamp": 101}], 3)
    assert len(store.history(uid)) == 1


def test_migration_rebuilds_from_raw_lines_without_rewriting_them(tmp_path):
    store = Store(tmp_path)
    uid = make_run(store)
    rows = [{"_step": 0, "loss": 99}, {"_step": 0, "train/global_step": 50}]
    send(store, uid, rows)
    # Simulate an old database without the new index; legacy raw data stays intact.
    with sqlite3.connect(store.path) as db:
        db.executescript(
            "DROP TABLE record_values; DROP TABLE history_records; DROP TABLE record_migrations;"
        )
    migrated = Store(tmp_path)
    assert migrated.history(uid) == rows
    assert migrated.series(uid, "loss", x="train/global_step")["points"] == []
    assert migrated.count(uid, "wandb-history.jsonl") == 2
    assert Store(tmp_path).records.catalog(uid)["total"] == 2


def test_legacy_projection_keeps_per_metric_timestamps_without_fabricated_axis(
    tmp_path,
):
    store = Store(tmp_path)
    uid = make_run(store)
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM record_migrations WHERE run=?", (uid,))
        db.executemany(
            "INSERT INTO metrics VALUES (?,?,?,?,?,?)",
            [
                (uid, "history", 0, "loss", 99, 10),
                (uid, "history", 0, "global_step", 100, 20),
            ],
        )
    migrated = Store(tmp_path)
    assert migrated.series(uid, "loss", x="global_step")["points"] == []
    result = migrated.series(uid, "loss", include_timestamps=True)
    assert result["points"] == [[0, 99]]
    assert result["timestamps"] == [10]
    assert result["legacy_projection_points"] == 1


def test_quarantine_restores_without_changing_upload_offsets(tmp_path):
    store = Store(tmp_path)
    uid = make_run(store)
    send(store, uid, [{"_step": 0, "loss": 99}, {"_step": 1, "loss": 1}])
    rid = store.records.catalog(uid)["records"][0]["id"]
    assert (
        store.records.quarantine(uid, [rid], None, False, "bad import")["changed"] == 1
    )
    assert len(store.history(uid)) == 2  # dry-run default
    store.records.quarantine(uid, [rid], None, False, "bad import", False)
    assert store.history(uid) == [{"_step": 1, "loss": 1}]
    assert store.count(uid, "wandb-history.jsonl") == 2
    send(store, uid, [{"_step": 0, "loss": 99}])  # old retry cannot resurrect it
    assert len(store.history(uid)) == 1
    send(store, uid, [{"_step": 2, "loss": 0.5}], 2)
    assert len(store.history(uid)) == 2
    store.records.quarantine(uid, [rid], None, True, "restore", False)
    assert len(store.history(uid)) == 3


def test_import_batches_atomic_idempotent_partial_and_replacement(tmp_path):
    store = Store(tmp_path)
    uid = make_run(store)

    def upload(batch, rows, status="partial", expected=None, replacement=None):
        return store.records.import_rows(
            uid,
            batch,
            "slurm",
            "job-1",
            rows,
            status,
            expected,
            {"recovered_from": "logs"},
            replacement,
        )

    one = [{"id": "line-1", "data": {"_step": 10, "train/global_step": 10, "loss": 1}}]
    assert upload("old", one)["status"] == "partial"
    assert upload("old", one)["records_added"] == 0
    with pytest.raises(ValueError, match="expected_records"):
        upload("old", one, "complete")
    upload("old", one, "complete", 1)
    with pytest.raises(ValueError, match="Completed"):
        upload("old", [{"id": "line-2", "data": {"_step": 11}}], "complete", 2)
    with pytest.raises(ValueError, match="match expected"):
        upload("bad", one, "complete", 2, "old")
    assert store.records.catalog(uid)["total"] == 1
    assert len(store.records.imports(uid)["batches"]) == 1
    upload(
        "replacement",
        [{"id": "line-1", "data": {"_step": 10, "train/global_step": 10, "loss": 0.2}}],
        "complete",
        1,
        "old",
    )
    assert store.series(uid, "loss", x="train/global_step")["points"] == [[10, 0.2]]
    assert store.records.catalog(uid, include_inactive=True)["total"] == 2
    store.records.quarantine(uid, [], "old", True, "undo", False)
    assert store.records.catalog(uid)["total"] == 2
    with pytest.raises(ValueError, match="explicit _step"):
        upload("no-step", [{"id": "x", "data": {"loss": 1}}])


def test_tensorboard_merges_within_session_only_and_restart_drops_stale_keys(tmp_path):
    store = Store(tmp_path)
    uid = make_run(store)

    def event(session, t, values, restart=False):
        return {
            "step": 1,
            "wall_time": t,
            "values": values,
            "restart": restart,
            "session": session,
        }

    store.import_events(
        uid, [event("a", 10, {"loss": 99}), event("b", 20, {"global_step": 1})]
    )
    assert store.series(uid, "loss", x="global_step")["points"] == []
    store.import_events(uid, [event("a", 11, {"global_step": 1})])
    assert store.series(uid, "loss", x="global_step")["points"] == [[1, 99]]
    store.import_events(uid, [event("a", 30, {}, True), event("a", 31, {"loss": 0.5})])
    assert store.series(uid, "loss")["points"] == [[1, 0.5]]
    assert store.series(uid, "loss", x="global_step")["points"] == []


def test_default_metric_axis_definition_with_sdk_indexes(tmp_path):
    store = Store(tmp_path)
    uid = make_run(store)
    definitions = [
        {"1": "train/global_step"},
        {"1": "loss", "5": 1},
        {"2": "eval/*", "4": "eval/step"},
    ]
    config = {"_wandb": {"value": {"m": definitions}}}
    assert metric_axes(config) == {"loss": "train/global_step", "eval/*": "eval/step"}
    store.upsert({"id": uid, "config": config})
    send(store, uid, [{"_step": 0, "train/global_step": 12, "loss": 0.5}])
    assert store.series(uid, "loss", x="auto")["points"] == [[12, 0.5]]
    assert store.series(uid, "loss", x="_step")["points"] == [[0, 0.5]]


def test_delete_restore_and_reuse_name(tmp_path):
    store = Store(tmp_path)
    uid = make_run(store)
    send(store, uid, [{"_step": 0, "loss": 1}])
    store.delete_run(uid)
    assert store.get(uid=uid) is None and store.list_runs() == []
    assert store.delete_run(uid)["recoverable"]
    store.restore_run(uid)
    assert store.history(uid) == [{"_step": 0, "loss": 1}]
    store.delete_run(uid)
    replacement = make_run(store)
    assert replacement != uid
    with pytest.raises(ValueError, match="reused"):
        store.restore_run(uid)


def test_recovery_endpoints_permissions_and_reader_isolation(tmp_path, monkeypatch):
    from open_train.accounts import principal

    monkeypatch.setenv("OPEN_TRAIN_AUTH_MODE", "accounts")
    app = create_app(tmp_path)
    client = TestClient(app)

    def account(subject):
        user = app.state.accounts.login_identity(
            "github", subject, f"{subject}@example.com", subject
        )
        key = app.state.accounts.issue(user["id"], "api_key", "test", 1)["key"]
        return user, {"Authorization": f"Bearer {key}"}

    owner, headers = account("owner")
    reader, rh = account("reader")
    stranger, sh = account("stranger")
    context = principal.set(owner)
    try:
        uid = app.state.store.upsert(
            {"entityName": owner["username"], "modelName": "p", "name": "r"}
        )[0]["uid"]
    finally:
        principal.reset(context)
    with app.state.store.connect() as db:
        db.execute(
            "INSERT INTO memberships VALUES (?,?,?)",
            (owner["username"], reader["id"], "reader"),
        )
    body = {
        "batch_id": "logs",
        "source": "slurm",
        "session": "job",
        "records": [{"id": "l1", "data": {"_step": 0, "loss": 1}}],
        "status": "complete",
        "expected_records": 1,
    }
    assert (
        client.post(f"/api/runs/{uid}/imports", headers=rh, json=body).status_code
        == 403
    )
    assert (
        client.post(f"/api/runs/{uid}/imports", headers=headers, json=body).status_code
        == 200
    )
    assert client.get(f"/api/runs/{uid}/imports", headers=sh).status_code == 403
    assert (
        client.get(f"/api/runs/{uid}/imports", headers=rh).json()["batches"][0][
            "status"
        ]
        == "complete"
    )
    rid = client.get(f"/api/runs/{uid}/records", headers=headers).json()["records"][0][
        "id"
    ]
    quarantine = {"record_ids": [rid], "reason": "test", "dry_run": False}
    assert (
        client.post(
            f"/api/runs/{uid}/quarantine", headers=rh, json=quarantine
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/runs/{uid}/quarantine", headers=headers, json=quarantine
        ).status_code
        == 200
    )
    assert client.get(f"/api/runs/{uid}/history", headers=rh).json()["total"] == 0
    query = "mutation($id:ID!){deleteRun(input:{id:$id}){clientMutationId}}"
    assert (
        client.post(
            "/graphql", headers=rh, json={"query": query, "variables": {"id": uid}}
        )
        .json()
        .get("errors")
    )
    assert (
        not client.post(
            "/graphql", headers=headers, json={"query": query, "variables": {"id": uid}}
        )
        .json()
        .get("errors")
    )
    assert (
        client.post(
            f"/api/runs/{uid}/restore", headers=rh, json={"confirm": True}
        ).status_code
        == 403
    )
    assert client.post(
        f"/api/runs/{uid}/restore", headers=headers, json={"confirm": True}
    ).json()["restored"]


def test_official_sdk_metric_axes_and_delete(sdk, server):
    sdk("""
import wandb
with wandb.init(project="recovery-sdk", id="axis-delete") as run:
    run.define_metric("train/global_step")
    run.define_metric("loss", step_metric="train/global_step")
    run.log({"train/global_step": 100, "loss": 0.5})
api = wandb.Api()
r = api.run("local/recovery-sdk/axis-delete")
import httpx, os
base = os.environ["WANDB_BASE_URL"]
detail = httpx.get(base + "/api/runs/" + r.storage_id).json()
assert detail["metric_axes"]["loss"] == "train/global_step", detail["metric_axes"]
series = httpx.get(base + "/api/runs/" + r.storage_id + "/series", params={"key":"loss","x":"auto"}).json()
assert series["points"] == [[100, 0.5]], series
r.delete()
assert httpx.get(base + "/api/runs/" + r.storage_id).status_code == 404
assert httpx.post(base + "/api/runs/" + r.storage_id + "/restore", json={"confirm":True}).status_code == 200
assert len(list(wandb.Api().run("local/recovery-sdk/axis-delete").scan_history())) == 1
""")


def test_offline_recovery_keeps_source_and_reports_truncated_prefix(tmp_path):
    import struct
    import zlib

    from wandb.proto import wandb_internal_pb2 as pb

    from open_train.offline_recovery import recover

    source = tmp_path / "journal.wandb"
    journal = bytearray(struct.pack("<4sHB", b":W&B", 0xBEE1, 0))

    def write(record):
        data = record.SerializeToString()
        journal.extend(
            struct.pack("<IHB", zlib.crc32(b"\x01" + data) & 0xFFFFFFFF, len(data), 1)
            + data
        )

    for step in range(3):
        row = pb.Record()
        row.history.item.add(key="_step", value_json=str(step))
        row.history.item.add(key="train/global_step", value_json=str(100 + step))
        row.history.item.add(key="loss", value_json=str(1 / (step + 1)))
        write(row)
    exit_record = pb.Record()
    exit_record.exit.exit_code = 0
    write(exit_record)
    source.write_bytes(journal)
    original = source.read_bytes()
    complete = recover(source, tmp_path / "complete.jsonl")
    assert complete["status"] == "complete" and complete["history_rows"] == 3
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match="already exists"):
        recover(source, tmp_path / "complete.jsonl")
    damaged = tmp_path / "damaged.wandb"
    damaged.write_bytes(original[:-3])
    partial = recover(damaged, tmp_path / "partial.jsonl")
    assert partial["status"] == "partial" and partial["history_rows"] == 3
    assert damaged.read_bytes() == original[:-3]
    assert (
        json.loads((tmp_path / "partial.jsonl").read_text().splitlines()[0])[
            "train/global_step"
        ]
        == 100
    )


def test_recover_real_sdk_journal_with_fragmented_records(sdk, tmp_path):
    from open_train.offline_recovery import recover

    sdk("""
import wandb
with wandb.init(project="recovery-sdk", id="journal", mode="offline", dir=".") as run:
    run.config.update({"padding":"x" * 70000})
    run.log({"train/global_step":100,"loss":0.5})
""")
    source = next(tmp_path.glob("wandb/offline-run-*/run-*.wandb"))
    result = recover(source, tmp_path / "recovered.jsonl")
    assert result["status"] == "complete", result
    assert result["history_rows"] == 1
    assert (
        json.loads((tmp_path / "recovered.jsonl").read_text())["train/global_step"]
        == 100
    )


def test_sdk_delete_keeps_artifacts_and_delete_artifacts_is_reversible(sdk):
    sdk("""
import os, pathlib, wandb, httpx
pathlib.Path("checkpoint.txt").write_text("test")
with wandb.init(project="recovery-sdk", id="artifact-delete") as run:
    a = wandb.Artifact("recovery-checkpoint", type="model")
    a.add_file("checkpoint.txt")
    run.log_artifact(a)
api = wandb.Api()
r = api.run("local/recovery-sdk/artifact-delete")
uid = r.storage_id
r.delete()
a = wandb.Api().artifact("local/recovery-sdk/recovery-checkpoint:latest")
assert pathlib.Path(a.download(), "checkpoint.txt").read_text() == "test"
base = os.environ["WANDB_BASE_URL"]
assert httpx.post(base + "/api/runs/" + uid + "/restore", json={"confirm":True}).status_code == 200
wandb.Api().run("local/recovery-sdk/artifact-delete").delete(delete_artifacts=True)
assert httpx.post(base + "/api/runs/" + uid + "/restore", json={"confirm":True}).status_code == 200
a = wandb.Api().artifact("local/recovery-sdk/recovery-checkpoint:latest")
assert pathlib.Path(a.download(root="restored"), "checkpoint.txt").read_text() == "test"
""")
