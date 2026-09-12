import httpx
import pytest


@pytest.mark.integration
def test_sdk_metrics_resume_and_files(server, sdk):
    sdk("""
import wandb
from pathlib import Path
settings = wandb.Settings(init_timeout=20, x_disable_stats=True)
with wandb.init(project="compat", id="resume-test", name="Resumable training", config={"lr":0.01}, settings=settings) as run:
    for step in range(5): run.log({"loss":1/(step+1), "global_step":step})
    run.summary["best_loss"] = .2
    Path("checkpoint.txt").write_text("checkpoint payload")
    run.save("checkpoint.txt", policy="now")
Path("attempt-2").mkdir()
with wandb.init(project="compat", id="resume-test", resume="must", dir="attempt-2", settings=settings) as run:
    assert run.resumed
    assert run.starting_step == 5, run.starting_step
    assert run.config.lr == .01
    for step in range(5,8): run.log({"loss":1/(step+1), "global_step":step})
api = wandb.Api()
run = api.run("local/compat/resume-test")
assert run.state == "finished", run.state
assert run.config["lr"] == .01
history = run.history(pandas=False)
assert [r["_step"] for r in history] == list(range(8)), history
assert len(list(run.scan_history(page_size=3))) == 8
assert len(list(api.runs("local/compat", per_page=1))) >= 1
f = run.file("checkpoint.txt").download(root="restored", replace=True)
assert Path(f.name).read_text() == "checkpoint payload"
""")
    runs = httpx.get(server["url"] + "/api/runs").json()["runs"]
    run = next(r for r in runs if r["name"] == "resume-test")
    points = httpx.get(
        f"{server['url']}/api/runs/{run['uid']}/series", params={"key": "loss"}
    ).json()["points"]
    assert len(points) == 8


@pytest.mark.integration
def test_sdk_resume_guards(server, sdk):
    sdk("""
import wandb
settings=wandb.Settings(init_timeout=15,x_disable_stats=True)
try:
    wandb.init(project="compat", id="does-not-exist", resume="must",settings=settings)
    raise AssertionError("must accepted a missing run")
except wandb.errors.UsageError:
    pass
with wandb.init(project="compat",id="never-test",resume="allow",settings=wandb.Settings(init_timeout=15,x_disable_stats=True)) as run:
    run.log({"value":1})
try:
    wandb.init(project="compat",id="never-test",resume="never",settings=settings)
    raise AssertionError("never accepted an existing run")
except wandb.errors.UsageError:
    pass
""")


@pytest.mark.integration
def test_sdk_tables_media(server, sdk):
    sdk(
        """
import wandb
import wave
from PIL import Image
with wandb.init(project="compat",id="media-test",settings=wandb.Settings(init_timeout=20,x_disable_stats=True)) as run:
    with wave.open("sample.wav", "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(2000))
    run.log({"image":wandb.Image(Image.new("RGB",(32,32),(60,130,90))),
             "audio":wandb.Audio("sample.wav"),
             "predictions":wandb.Table(columns=["input","score"],data=[["hello",.95],["world",.82]]),
             "loss":.3})
""",
        timeout=120,
    )
    runs = httpx.get(server["url"] + "/api/runs").json()["runs"]
    run = next(r for r in runs if r["name"] == "media-test")
    detail = httpx.get(f"{server['url']}/api/runs/{run['uid']}").json()
    names = [f["name"] for f in detail["files"]]
    assert any(n.endswith(".png") for n in names), names
    assert any(n.endswith(".wav") for n in names), names
    table = next(f for f in detail["files"] if f["name"].endswith(".table.json"))
    assert httpx.get(table["url"]).json()["data"] == [["hello", 0.95], ["world", 0.82]]


@pytest.mark.integration
@pytest.mark.parametrize("method", ["grid", "random"])
def test_sdk_sweep(server, sdk, method):
    sdk(
        """
import wandb
sweep = wandb.sweep({"method":"METHOD", "run_cap":2, "parameters":{"lr":{"values":[.01,.1]}}},project="sweep-METHOD")
def train():
    with wandb.init(settings=wandb.Settings(init_timeout=20,x_disable_stats=True)) as run:
        run.log({"loss":run.config.lr*2})
wandb.agent(sweep, function=train, project="sweep-METHOD")
""".replace("METHOD", method),
        timeout=150,
    )
    runs = httpx.get(
        server["url"] + "/api/runs", params={"project": f"sweep-{method}"}
    ).json()["runs"]
    assert len(runs) == 2
    if method == "grid":
        assert sorted(r["config"]["lr"] for r in runs) == [0.01, 0.1]
    else:
        assert all(r["config"]["lr"] in (0.01, 0.1) for r in runs)
    assert all(r["state"] == "finished" and r["sweep"] for r in runs)


@pytest.mark.integration
def test_multiple_active_sdk_runs_and_resumes(server, sdk):
    sdk("""
import wandb
from pathlib import Path
settings = wandb.Settings(init_timeout=20, x_disable_stats=True)
a = wandb.init(project="parallel", id="run-a", reinit="create_new", settings=settings)
b = wandb.init(project="parallel", id="run-b", reinit="create_new", settings=settings)
for i in range(10):
    a.log({"value": i})
    b.log({"value": i+100})
a.finish()
b.finish()
for name, value in [("run-a",10),("run-b",110)]:
    Path(name).mkdir()
    with wandb.init(project="parallel", id=name, resume="must", dir=name, settings=settings) as run:
        assert run.resumed and run.starting_step == 10
        run.log({"value": value})
""")
    runs = httpx.get(
        server["url"] + "/api/runs", params={"project": "parallel"}
    ).json()["runs"]
    assert len(runs) == 2
    for run in runs:
        points = httpx.get(
            f"{server['url']}/api/runs/{run['uid']}/series", params={"key": "value"}
        ).json()["points"]
        expected = 0 if run["name"] == "run-a" else 100
        assert points == [[i, expected + i] for i in range(11)]
