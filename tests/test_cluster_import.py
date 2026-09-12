import hashlib
from types import SimpleNamespace

from scripts.import_cluster_tensorboard import analyze, import_run
from tests.test_tensorboard import write_events


def test_cluster_sessions_merge_verify_and_repeat(tmp_path, server):
    root = tmp_path / "snapshots"
    first = root / "experiment" / "tb" / "rl_1"
    second = root / "experiment" / "tb" / "rl_2"
    write_events(first, [(0, 3), (1, 2), (2, 1)])
    write_events(second, [(1, 0.5)], restart=1, base=200)
    run = {
        "cluster": "test-cluster",
        "root": "/test/outputs",
        "tree": "test",
        "tag": "experiment",
        "name": "test-cluster/test/experiment",
        "source": "test-cluster:/test/outputs/experiment",
        "run_id": "tb-cluster-test",
        "local": str(root),
        "files": [
            {
                "relative": str(p.relative_to(root)),
                "bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in root.rglob("events.out.tfevents.*")
        ],
    }
    analyzed = analyze(run, tmp_path)
    assert len(analyzed["sessions"]) == 2
    assert analyzed["expected_keys"] == {"loss": {"count": 2, "last_step": 1}}
    assert analyzed["check_series"] == {"loss": [[0, 3.0], [1, 0.5]]}
    args = SimpleNamespace(
        base_url=server["url"],
        project="cluster-test",
        workspace=tmp_path,
        upload_events=True,
    )
    first = import_run(analyzed, args, "local" + "0" * 35, "local")
    assert first["verified"] and first["events_added"] == 5
    assert first["raw_event_files_verified"] == 2
    again = import_run(analyzed, args, "local" + "0" * 35, "local")
    assert again["events_added"] == 0 and again["uid"] == first["uid"]
