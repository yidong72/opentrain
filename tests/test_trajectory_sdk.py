import httpx
import pytest


@pytest.mark.integration
def test_official_sdk_checkpoint_metadata_and_overlap_views(server, sdk):
    sdk("""
import wandb
from pathlib import Path
settings = wandb.Settings(init_timeout=20, x_disable_stats=True)
with wandb.init(project="trajectory-sdk", id="overlap", settings=settings) as run:
    run.define_metric("train/*", step_metric="train/global_step")
    run.define_metric("eval/*", step_metric="eval/global_step")
    for step in [0, 40, 50, 60, 100]:
        run.log({"train/global_step": step, "train/loss": step+1, "eval/global_step": step, "eval/score": step+2})
Path("resumed").mkdir()
with wandb.init(project="trajectory-sdk", id="overlap", resume="must", dir="resumed", settings=settings) as run:
    assert run.resumed
    run.log({"_open_train/resume_step": 50})
    run.log({"train/global_step": 55, "train/loss": 550})
    run.log({"eval/global_step": 60, "eval/score": 600})
""")
    runs = httpx.get(
        server["url"] + "/api/runs", params={"project": "trajectory-sdk"}
    ).json()["runs"]
    uid = runs[0]["uid"]
    with httpx.Client(base_url=server["url"]) as client:
        for key, expected in [
            ("train/loss", [[0, 1], [40, 41], [55, 550]]),
            ("eval/score", [[0, 2], [40, 42], [60, 600]]),
        ]:
            result = client.post(
                "/api/series", json={"runs": [uid], "keys": [key], "view": "latest"}
            ).json()["series"][uid][key]
            assert result["points"] == expected
            assert (
                result["trajectory"]["boundaries"][1]["source"] == "checkpoint_metadata"
            )
            assert result["trajectory"]["warning"] is None
            all_rows = client.get(
                f"/api/runs/{uid}/series", params={"key": key, "view": "all"}
            ).json()
            assert all_rows["total"] == 6
            assert len(set(all_rows["point_sessions"])) == 2
        assert client.get(f"/api/runs/{uid}/history").json()["total"] == 8
