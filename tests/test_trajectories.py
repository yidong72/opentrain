import json

from open_train.store import Store
from tests.test_sessions import writer


def log(store, uid, rows, offset=0):
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


def resumed(tmp_path, explicit=True):
    store = Store(tmp_path)
    uid = writer(store)
    first = [
        {
            "_step": i,
            "_timestamp": 101 + i,
            "train/global_step": step,
            "eval/global_step": step,
            "train/loss": step + 1,
            "eval/score": step + 2,
        }
        for i, step in enumerate([0, 40, 50, 60, 100])
    ]
    log(store, uid, first)
    writer(store, wid="two", start=200)
    second = [
        {
            "_step": 5,
            "_timestamp": 201,
            "train/global_step": 0,
            "eval/global_step": 0,
            **({"_open_train/resume_step": 50} if explicit else {}),
        },
        {"_step": 6, "_timestamp": 202, "eval/global_step": 60, "eval/score": 600},
        {"_step": 7, "_timestamp": 203, "train/global_step": 55, "train/loss": 550},
        {"_step": 8, "_timestamp": 204, "train/global_step": 70, "train/loss": 700},
    ]
    log(store, uid, second, offset=5)
    return store, uid


def test_latest_truncates_abandoned_tail_and_uses_checkpoint_for_sparse_evals(tmp_path):
    store, uid = resumed(tmp_path)
    before = store.history(uid)
    for key in ("train/loss", "eval/score"):
        latest = store.series(uid, key, view="latest", include_timestamps=True)
        assert latest["trajectory"]["hidden_points"] == 3
        assert latest["trajectory"]["stitch_sessions"] is True
        assert latest["trajectory"]["boundaries"][1]["step"] == 50
        assert latest["trajectory"]["boundaries"][1]["source"] == "checkpoint_metadata"
        assert latest["trajectory"]["warning"] is None
        assert 100 not in [p[0] for p in latest["points"]]
        assert latest["stats"]["count"] == len(latest["points"])
        assert latest["stats"]["scope"] == "latest_trajectory"
        all_rows = store.series(uid, key, view="all")
        assert all_rows["trajectory"]["stitch_sessions"] is False
        assert all_rows["trajectory"]["resume_smoothing"] is True
        assert all_rows["total"] == latest["total"] + 3
        assert set(all_rows["point_sessions"]) == {"sdk:one", "sdk:two"}
    assert (
        store.series(uid, "train/loss", x="_timestamp", view="latest")["trajectory"][
            "hidden_points"
        ]
        == 3
    )
    assert store.history(uid) == before
    assert store.sessions.resume(uid)["last_step"] == 8


def test_inference_ignores_setup_zero_and_prefers_train_over_sparse_eval(tmp_path):
    store, uid = resumed(tmp_path, explicit=False)
    for key in ("train/loss", "eval/score"):
        result = store.series(uid, key, view="latest")
        assert result["trajectory"]["boundaries"][1]["step"] == 55
        assert result["trajectory"]["hidden_points"] == 2
        assert "inferred" in result["trajectory"]["warning"]


def test_multiple_rewinds_remove_all_abandoned_branches(tmp_path):
    store, uid = resumed(tmp_path)
    writer(store, wid="three", start=300)
    log(
        store,
        uid,
        [
            {
                "_step": 9,
                "_timestamp": 301,
                "_open_train/resume_step": 30,
                "train/global_step": 35,
                "train/loss": 350,
            }
        ],
        offset=9,
    )
    result = store.series(uid, "train/loss", view="latest")
    assert result["points"] == [[0, 1], [35, 350]]
    assert result["point_sessions"] == ["sdk:one", "sdk:three"]
    assert result["stats"]["mean"] == 175.5


def test_shared_writers_never_truncate_each_other(tmp_path):
    store = Store(tmp_path)
    uid = store.upsert({"name": "shared"})[0]["uid"]
    with store.connect(write=True) as db:
        for i, (sid, step) in enumerate([("rank0", 100), ("rank1", 50)]):
            store.records.put(
                db,
                uid,
                "history",
                str(i),
                {
                    "_step": i,
                    "_timestamp": 100 + i,
                    "train/global_step": step,
                    "train/loss": 2,
                },
                i,
                "sdk_shared",
                sid,
            )
    result = store.series(uid, "train/loss", view="latest")
    assert result["total"] == 2
    assert "Shared distributed" in result["trajectory"]["warning"]
    assert result["trajectory"]["resume_smoothing"] is False
    assert result["trajectory"]["stitch_sessions"] is False
    assert set(result["point_sessions"]) == {"rank0", "rank1"}


def test_ambiguous_session_order_is_not_guessed(tmp_path):
    store = Store(tmp_path)
    uid = store.upsert({"name": "ambiguous"})[0]["uid"]
    with store.connect(write=True) as db:
        for i, (sid, stamp, step) in enumerate(
            [("a", 100, 100), ("a", 300, 110), ("b", 200, 50)]
        ):
            store.records.put(
                db,
                uid,
                "history",
                str(i),
                {
                    "_step": i,
                    "_timestamp": stamp,
                    "train/global_step": step,
                    "train/loss": 1,
                },
                i,
                "sdk",
                sid,
            )
    result = store.series(uid, "train/loss", view="latest")
    assert result["total"] == 3
    assert result["trajectory"]["hidden_points"] == 0
    assert "ambiguous" in result["trajectory"]["warning"]
    assert result["trajectory"]["resume_smoothing"] is False
    assert result["trajectory"]["stitch_sessions"] is False


def test_missing_training_coordinate_is_retained_and_reported(tmp_path):
    store, uid = resumed(tmp_path)
    log(store, uid, [{"_step": 9, "_timestamp": 106, "train/loss": 999}], offset=9)
    result = store.series(uid, "train/loss", view="latest", x="_timestamp")
    assert [106, 999] in result["points"]
    assert "without a paired training coordinate" in result["trajectory"]["warning"]
    assert result["trajectory"]["resume_smoothing"] is False
    assert result["trajectory"]["stitch_sessions"] is False


def test_empty_initial_session_does_not_break_later_trajectory(tmp_path):
    store = Store(tmp_path)
    uid = writer(store)
    log(store, uid, [{"_step": 0, "_timestamp": 101, "_runtime": 1}])
    writer(store, wid="two", start=200)
    log(
        store,
        uid,
        [{"_step": 1, "_timestamp": 201, "train/global_step": 31, "train/loss": 2}],
        offset=1,
    )
    writer(store, wid="three", start=300)
    log(
        store,
        uid,
        [{"_step": 2, "_timestamp": 301, "train/global_step": 61, "train/loss": 1}],
        offset=2,
    )
    result = store.series(uid, "train/loss", view="latest")
    assert result["trajectory"]["boundaries"][0]["step"] is None
    assert result["trajectory"]["stitch_sessions"] is True


def test_sampling_keeps_each_session_and_stats_use_full_selected_trajectory(tmp_path):
    store = Store(tmp_path)
    uid = writer(store)
    for i, wid in enumerate(["one", "two", "three"]):
        writer(store, wid=wid, start=100 + i * 100)
        log(
            store,
            uid,
            [
                {
                    "_step": i * 20 + j,
                    "_timestamp": 101 + i * 100 + j,
                    "train/global_step": j,
                    "train/loss": i * 100 + j,
                }
                for j in range(20)
            ],
            offset=i * 20,
        )
    all_rows = store.series(uid, "train/loss", limit=10, include_timestamps=True)
    assert len(all_rows["points"]) <= 10
    assert all_rows["stats"]["count"] == 60
    assert set(all_rows["point_sessions"]) == {"sdk:one", "sdk:two", "sdk:three"}
    for sid in set(all_rows["point_sessions"]):
        xs = [
            p[0]
            for p, s in zip(all_rows["points"], all_rows["point_sessions"], strict=True)
            if s == sid
        ]
        assert min(xs) == 0 and max(xs) == 19
    latest = store.series(uid, "train/loss", limit=10, view="latest")
    assert latest["stats"]["count"] == 20
    assert latest["stats"]["mean"] == 209.5
    assert set(latest["point_sessions"]) == {"sdk:three"}
