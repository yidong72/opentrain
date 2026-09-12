"""Exercise the real SDK's durable transaction log, not a replacement client."""

import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest


def eventually(check, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("Condition did not become true")


@pytest.fixture
def proxy(server):
    down = threading.Event()
    disconnect = threading.Event()
    lost_ack = threading.Event()
    failures = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if down.is_set():
                failures.set()
                if disconnect.is_set():
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_error(503, "Simulated outage")
                return
            response = httpx.request(
                self.command,
                server["url"] + self.path,
                headers=dict(self.headers),
                content=body,
                timeout=10,
            )
            if "/file_stream" in self.path and lost_ack.is_set():
                lost_ack.clear()
                failures.set()
                self.send_error(503, "Simulated lost acknowledgment")
                return
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in {
                    "content-length",
                    "transfer-encoding",
                    "content-encoding",
                    "connection",
                }:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_POST = do_PUT = do_PATCH = do_GET

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    yield {
        "url": f"http://127.0.0.1:{httpd.server_port}",
        "down": down,
        "disconnect": disconnect,
        "lost_ack": lost_ack,
        "failures": failures,
    }
    httpd.shutdown()
    httpd.server_close()
    worker.join()


@pytest.mark.integration
@pytest.mark.parametrize("server", ["accounts"], indirect=True)
@pytest.mark.parametrize("fault", ["503", "disconnect"])
def test_training_checkpoint_resume_through_outage(server, proxy, tmp_path, fault):
    """Do real optimization without catching SDK errors in the training process."""
    name = "training-checkpoint-" + fault
    env = dict(server["env"], WANDB_BASE_URL=proxy["url"], WANDB_DIR=str(tmp_path))
    code = r"""
import json, math, sys, time
from pathlib import Path
import wandb

attempt = int(sys.argv[1])
checkpoint = Path("checkpoint.json")
state = json.loads(checkpoint.read_text()) if attempt else {"step": 0, "weight": 0., "bias": 0.}
start = state["step"]
initial_weight = state["weight"]
settings = wandb.Settings(init_timeout=20, x_disable_stats=True,
    x_file_stream_transmit_interval=.1, x_file_stream_retry_wait_min_seconds=.1,
    x_file_stream_retry_wait_max_seconds=1)

def wait_for(name):
    deadline = time.monotonic() + 60
    while not Path(name).exists():
        assert time.monotonic() < deadline, name
        time.sleep(.05)

with wandb.init(project="training-integration", id=sys.argv[2],
                resume="must" if attempt else "never", config={"learning_rate": .1},
                settings=settings) as run:
    assert bool(run.resumed) == bool(attempt)
    assert run.starting_step == start, (run.starting_step, start)
    wandb.define_metric("train/*", step_metric="train/global_step")
    for step in range(start, 40 if attempt == 0 else 80):
        if step == 10:
            Path("ready").touch()
            wait_for("continue")
        # Full-batch gradient descent on y = 3*x + 2; no ML runtime needed.
        errors = [(x, state["weight"] * x + state["bias"] - (3*x + 2))
                  for x in (-2., -1., 0., 1., 2.)]
        loss = sum(error**2 for _, error in errors) / len(errors)
        assert math.isfinite(loss)
        state["weight"] -= .1 * 2 * sum(x*error for x, error in errors) / len(errors)
        state["bias"] -= .1 * 2 * sum(error for _, error in errors) / len(errors)
        run.log({"train/loss": loss, "train/global_step": step,
                 "train/weight": state["weight"], "attempt": attempt}, step=step, commit=True)
        state["step"] = step + 1
        checkpoint.write_text(json.dumps(state))
    if not attempt:
        Path("optimized-during-outage").touch()
        wait_for("finish")
    run.summary["checkpoint_step"] = state["step"]
    run.save(str(checkpoint.resolve()), base_path=str(Path.cwd()), policy="now")
Path(f"result-{attempt}.json").write_text(json.dumps({
    "start": start, "end": state["step"], "loss": loss,
    "initial_weight": initial_weight, "final_weight": state["weight"]}))
"""
    headers = {"Authorization": "Bearer " + server["env"]["WANDB_API_KEY"]}
    with httpx.Client(base_url=server["url"], headers=headers) as client:

        def points():
            runs = client.get(
                "/api/runs", params={"project": "training-integration"}
            ).json()["runs"]
            run = next((r for r in runs if r["name"] == name), None)
            if run is None:
                return []
            response = client.get(
                f"/api/runs/{run['uid']}/series",
                params={"key": "train/loss", "x": "auto"},
            )
            response.raise_for_status()
            series = response.json()
            # The run is created before asynchronous metric definitions arrive.
            if series["axis"] != "train/global_step":
                return []
            assert series["missing_axis"] == 0
            return series["points"]

        with (tmp_path / "training.log").open("w+") as output:
            training = subprocess.Popen(
                [sys.executable, "-c", code, "0", name],
                cwd=tmp_path,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                eventually(lambda: len(points()) == 10)
                if fault == "disconnect":
                    proxy["disconnect"].set()
                proxy["down"].set()
                (tmp_path / "continue").touch()
                eventually(lambda: (tmp_path / "optimized-during-outage").exists())
                eventually(proxy["failures"].is_set)
                assert training.poll() is None, (tmp_path / "training.log").read_text()
                checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
                assert checkpoint["step"] == 40
                assert checkpoint["weight"] == pytest.approx(3.0, abs=1e-6)
                assert len(points()) == 10  # optimization progressed without delivery
                assert list(tmp_path.glob("wandb/run-*/*.wandb"))
                proxy["down"].clear()
                eventually(lambda: len(points()) == 40)
                (tmp_path / "finish").touch()
                training.wait(60)
                assert training.returncode == 0, (tmp_path / "training.log").read_text()
            finally:
                proxy["down"].clear()
                if training.poll() is None:
                    training.kill()
                    training.wait()

        resumed = subprocess.run(
            [sys.executable, "-c", code, "1", name],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        first = json.loads((tmp_path / "result-0.json").read_text())
        second = json.loads((tmp_path / "result-1.json").read_text())
        assert (first["start"], first["end"], second["start"], second["end"]) == (
            0,
            40,
            40,
            80,
        )
        assert second["initial_weight"] == first["final_weight"]
        actual = points()
        assert [p[0] for p in actual] == list(range(80))
        assert actual[-1][1] < 1e-12
        assert all(b[1] <= a[1] for a, b in zip(actual, actual[1:], strict=False))
        runs = client.get(
            "/api/runs", params={"project": "training-integration"}
        ).json()["runs"]
        run = next(r for r in runs if r["name"] == name)
        assert run["state"] == "finished"
        assert run["summary"]["checkpoint_step"] == 80


def run_points(server, name):
    runs = httpx.get(server["url"] + "/api/runs").json()["runs"]
    run = next((r for r in runs if r["name"] == name), None)
    if not run:
        return []
    return httpx.get(
        f"{server['url']}/api/runs/{run['uid']}/series", params={"key": "loss"}
    ).json()["points"]


@pytest.mark.integration
def test_online_outage_and_lost_ack(server, proxy, tmp_path):
    env = dict(server["env"], WANDB_BASE_URL=proxy["url"], WANDB_DIR=str(tmp_path))
    code = """
from pathlib import Path
import time
import wandb
settings = wandb.Settings(x_disable_stats=True, x_file_stream_transmit_interval=.1,
                         x_file_stream_retry_wait_min_seconds=.1, x_file_stream_retry_wait_max_seconds=1)
with wandb.init(project="network", id="online-outage", settings=settings) as run:
    for step in range(3): run.log({"loss":step}, step=step, commit=True)
    Path("ready").touch()
    while not Path("continue").exists(): time.sleep(.1)
    for step in range(3,20): run.log({"loss":step}, step=step, commit=True)
    Path("logged-offline").touch()
    while not Path("finish").exists(): time.sleep(.1)
"""
    with (tmp_path / "training.log").open("w+") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            eventually(lambda: len(run_points(server, "online-outage")) == 3)
            proxy["down"].set()
            (tmp_path / "continue").touch()
            eventually(lambda: (tmp_path / "logged-offline").exists())
            eventually(proxy["failures"].is_set)
            assert process.poll() is None, (tmp_path / "training.log").read_text()
            assert list(tmp_path.glob("wandb/run-*/*.wandb"))
            proxy["lost_ack"].set()
            proxy["down"].clear()
            eventually(lambda: not proxy["lost_ack"].is_set())
            (tmp_path / "finish").touch()
            process.wait(60)
            assert process.returncode == 0, (tmp_path / "training.log").read_text()
            assert run_points(server, "online-outage") == [[s, s] for s in range(20)]
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


@pytest.mark.integration
def test_watch_offline_start_live_reconnect_and_exit(server, proxy, tmp_path):
    env = dict(
        server["env"],
        WANDB_BASE_URL=proxy["url"],
        WANDB_DIR=str(tmp_path),
        WANDB_MODE="offline",
        WANDB_X_GRAPHQL_RETRY_WAIT_MIN_SECONDS=".1",
        WANDB_X_GRAPHQL_RETRY_WAIT_MAX_SECONDS="1",
    )
    proxy["down"].set()
    code = """
from pathlib import Path
import time
import wandb
with wandb.init(project="network", id="watched-offline", settings=wandb.Settings(x_disable_stats=True)) as run:
    for s in range(5): run.log({"loss":s}, step=s, commit=True)
    # The official offline writer flushes full log blocks, not on every log().
    # Force enough real record data for the preceding metrics to reach disk.
    run.config.update({"test_padding":"x" * 70000})
    Path("ready").touch()
    while not Path("finish").exists(): time.sleep(.1)
    for s in range(5,10): run.log({"loss":s}, step=s, commit=True)
"""
    with (tmp_path / "processes.log").open("w+") as output:
        training = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        watcher = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "open_train.cli",
                "sync-watch",
                str(tmp_path),
                "--interval",
                ".2",
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            eventually(lambda: (tmp_path / "ready").exists())
            eventually(proxy["failures"].is_set)
            proxy["down"].clear()
            eventually(
                lambda: len(run_points(server, "watched-offline")) == 5, timeout=60
            )
            assert training.poll() is None  # Live data delivered before exit.
            proxy["down"].set()
            proxy["failures"].clear()
            # Restart the uploader after a partial upload. Its durable SDK
            # cursor must survive; replay must not duplicate accepted rows.
            watcher.terminate()
            watcher.wait(15)
            (tmp_path / "finish").touch()
            training.wait(30)
            assert training.returncode == 0, (tmp_path / "processes.log").read_text()
            watcher = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "open_train.cli",
                    "sync-watch",
                    str(tmp_path),
                    "--interval",
                    ".2",
                ],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            eventually(proxy["failures"].is_set)
            # Watcher survives training exit and reconnects without manual sync.
            proxy["down"].clear()
            eventually(
                lambda: bool(list(tmp_path.glob("wandb/*/*.wandb.synced"))), timeout=60
            )
            assert run_points(server, "watched-offline") == [[s, s] for s in range(10)]
        finally:
            for process in (training, watcher):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def test_spool_discovery_and_lock(tmp_path):
    from open_train.sync import pending_runs, spool_lock

    for name in (
        "offline-run-20260912_010000-a",
        "offline-run-20260912_020000-a",
        "run-20260912_030000-b",
    ):
        directory = tmp_path / "wandb" / name
        directory.mkdir(parents=True)
        (directory / f"run-{name[-1]}.wandb").touch()
    pending = pending_runs(tmp_path)
    assert list(pending) == ["run-a"]
    assert len(pending["run-a"]) == 2
    Path(str(pending["run-a"][0]) + ".synced").touch()
    assert len(pending_runs(tmp_path)["run-a"]) == 1
    with spool_lock(tmp_path), pytest.raises(RuntimeError, match="already owns"):
        with spool_lock(tmp_path):
            pass


def test_once_requires_delivery_receipts(tmp_path, monkeypatch):
    from open_train import sync

    run_dir = tmp_path / "offline-run-20260912_010000-test"
    run_dir.mkdir()
    source = run_dir / "run-test.wandb"
    source.touch()
    monkeypatch.setenv("WANDB_BASE_URL", "http://localhost:1")
    monkeypatch.setenv("WANDB_API_KEY", "test-key")
    popen = subprocess.Popen

    def unsuccessful_cli(command, **kwargs):
        # Reproduce beta CLI returning zero without uploading the run.
        return popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(sync.subprocess, "Popen", unsuccessful_cli)
    assert sync.watch(str(tmp_path), once=True, interval=0.01) == 1
    assert source.is_file()
    assert not Path(str(source) + ".synced").exists()


@pytest.mark.integration
def test_offline_fragments_and_once(server, tmp_path):
    env = dict(server["env"], WANDB_DIR=str(tmp_path), WANDB_MODE="offline")
    for attempt in range(2):
        directory = tmp_path / str(attempt)
        directory.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"""
import wandb
for name in ("chain", "separate"):
    with wandb.init(project="network", id="offline-"+name, dir="{directory}",
                    settings=wandb.Settings(x_disable_stats=True)) as run:
        for s in range({attempt * 5}, {attempt * 5 + 5}):
            run.log({{"loss":s, "global_step":s}}, step=s, commit=True)
""",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert result.returncode == 0, result.stderr
    for _ in range(2):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "open_train.cli",
                "sync-watch",
                str(tmp_path),
                "--once",
                "--interval",
                ".1",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            result.stdout
            + result.stderr
            + (tmp_path / ".open-train-sync.log").read_text()
        )
    for name in ("chain", "separate"):
        assert run_points(server, "offline-" + name) == [[s, s] for s in range(10)]
    assert len(list(tmp_path.rglob("*.wandb.synced"))) == 4


@pytest.mark.integration
def test_offline_sync(server, tmp_path):
    env = dict(server["env"], WANDB_DIR=str(tmp_path), WANDB_MODE="offline")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from pathlib import Path
import wandb
from PIL import Image
with wandb.init(project="network", id="offline-test", config={"lr":.1},
                settings=wandb.Settings(x_disable_stats=True)) as run:
    for step in range(12):
        run.log({"loss":12-step}, step=step)
    run.log({"table":wandb.Table(columns=["score"], data=[[.9]]),
             "image":wandb.Image(Image.new("RGB", (8,8), "red"))})
    Path("weights.txt").write_text("offline weights")
    run.save("weights.txt", policy="now")
    artifact = wandb.Artifact("offline-weights", type="model")
    artifact.add_file("weights.txt")
    run.log_artifact(artifact)
""",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
    env["WANDB_MODE"] = "online"
    for _ in range(2):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "wandb",
                "beta",
                "sync",
                "--yes",
                str(tmp_path / "wandb"),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stderr
    runs = httpx.get(server["url"] + "/api/runs").json()["runs"]
    run = next(r for r in runs if r["name"] == "offline-test")
    assert run["config"]["lr"] == 0.1
    points = httpx.get(
        f"{server['url']}/api/runs/{run['uid']}/series", params={"key": "loss"}
    ).json()["points"]
    assert points == [[step, 12 - step] for step in range(12)]
    detail = httpx.get(f"{server['url']}/api/runs/{run['uid']}").json()
    names = [f["name"] for f in detail["files"]]
    assert "weights.txt" in names
    assert any(n.endswith(".png") for n in names)
    assert any(n.endswith(".table.json") for n in names)
    assert list(tmp_path.glob("wandb/*/*.wandb.synced"))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from pathlib import Path
import wandb
artifact = wandb.Api().artifact("local/network/offline-weights:latest")
path = artifact.download(root="download", skip_cache=True)
assert (Path(path) / "weights.txt").read_text() == "offline weights"
""",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
