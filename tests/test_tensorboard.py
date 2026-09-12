import httpx
import pytest
from tensorboard.compat.proto import event_pb2, summary_pb2
from tensorboard.summary.writer.event_file_writer import EventFileWriter

from open_train.tensorboard import import_once


def test_watch_retries_outages(monkeypatch, capsys):
    from open_train import cli, tensorboard

    calls = []
    sleeps = []

    def upload(*args):
        calls.append(args)
        if len(calls) < 3:
            raise httpx.ConnectError("network unavailable")
        if len(calls) == 3:
            return [{"events_added": 5}]
        raise KeyboardInterrupt

    monkeypatch.setattr(tensorboard, "import_once", upload)
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    monkeypatch.setattr(cli.random, "uniform", lambda *args: 1)
    monkeypatch.setattr(
        "sys.argv",
        [
            "open-train",
            "import-tensorboard",
            "logs",
            "--project",
            "test",
            "--watch",
            "--interval",
            "2",
        ],
    )
    cli.main()
    assert len(calls) == 4
    assert sleeps == [2, 4, 2]
    assert "event files retained" in capsys.readouterr().err


def test_watch_does_not_retry_bad_credentials(monkeypatch):
    from open_train import cli, tensorboard

    def upload(*args):
        httpx.Response(
            401, request=httpx.Request("POST", "http://localhost")
        ).raise_for_status()

    monkeypatch.setattr(tensorboard, "import_once", upload)
    monkeypatch.setattr(
        "sys.argv",
        ["open-train", "import-tensorboard", "logs", "--project", "test", "--watch"],
    )
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1


def write_events(path, values, restart=None, base=100):
    writer = EventFileWriter(str(path))
    if restart is not None:
        writer.add_event(
            event_pb2.Event(
                step=restart,
                wall_time=base,
                session_log=event_pb2.SessionLog(status=event_pb2.SessionLog.START),
            )
        )
    for step, value in values:
        writer.add_event(
            event_pb2.Event(
                step=step,
                wall_time=base + step + 1,
                summary=summary_pb2.Summary(
                    value=[summary_pb2.Summary.Value(tag="loss", simple_value=value)]
                ),
            )
        )
    writer.close()


def test_multi_run_import_repeat_and_checkpoint_restart(tmp_path, server):
    write_events(tmp_path / "train-a", [(i, 1 / (i + 1)) for i in range(6)])
    write_events(tmp_path / "train-b", [(0, 10), (1, 9)])
    first = import_once(tmp_path, "tensorboard", server["url"], source="stable-test")
    assert len(first) == 2 and sum(r["events_added"] for r in first) == 8
    again = import_once(tmp_path, "tensorboard", server["url"], source="stable-test")
    assert all(r["events_added"] == 0 for r in again)
    write_events(tmp_path / "train-a", [(3, 0.1), (4, 0.05)], restart=3, base=200)
    resumed = import_once(tmp_path, "tensorboard", server["url"], source="stable-test")
    assert [r["run_id"] for r in resumed] == [r["run_id"] for r in first]
    uid = first[0]["uid"]
    points = httpx.get(
        f"{server['url']}/api/runs/{uid}/series", params={"key": "loss"}
    ).json()["points"]
    assert [p[0] for p in points] == [0, 1, 2, 3, 4]
    assert abs(points[-1][1] - 0.05) < 1e-6
    assert all(
        r["events_added"] == 0
        for r in import_once(
            tmp_path, "tensorboard", server["url"], source="stable-test"
        )
    )
    write_events(tmp_path / "train-a", [(5, 999)], base=110)
    import_once(tmp_path, "tensorboard", server["url"], source="stable-test")
    points = httpx.get(
        f"{server['url']}/api/runs/{uid}/series", params={"key": "loss"}
    ).json()["points"]
    assert [p[0] for p in points] == [0, 1, 2, 3, 4]


def test_tensor_scalars(tmp_path):
    from tensorboard.util import tensor_util

    from open_train.tensorboard import event_records

    writer = EventFileWriter(str(tmp_path))
    writer.add_event(
        event_pb2.Event(
            step=7,
            wall_time=123,
            summary=summary_pb2.Summary(
                value=[
                    summary_pb2.Summary.Value(
                        tag="accuracy", tensor=tensor_util.make_tensor_proto(0.75)
                    )
                ]
            ),
        )
    )
    writer.close()
    events, skipped = event_records(tmp_path)
    assert events[0]["values"]["accuracy"] == 0.75
    assert skipped == 0
