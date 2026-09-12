import json
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from open_train.app import create_app

QUERY = """
query viewer_server_info {
  viewer { id entity username serverInfo { ...Info } }
  serverInfo { ...Info }
}
fragment Info on ServerInfo {
  cliVersionInfo
  latestLocalVersionInfo {
    outOfDate latestVersionString versionOnThisInstanceString
  }
  features { name isEnabled }
}
"""


def test_nested_server_info_requires_auth_and_matches_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_TRAIN_AUTH_MODE", "accounts")
    app = create_app(tmp_path)
    user = app.state.accounts.login_identity(
        "github", "server-info", "info@example.com", "Info"
    )
    key = app.state.accounts.issue(user["id"], "api_key", "training", 1)["key"]
    with TestClient(app) as client:
        assert client.post("/graphql", json={"query": QUERY}).status_code == 401
        response = client.post(
            "/graphql",
            json={"query": QUERY},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 200
        result = response.json()
        assert "errors" not in result, result
        assert result["data"]["viewer"]["id"] == user["id"]
        assert result["data"]["viewer"]["serverInfo"] == result["data"]["serverInfo"]
        assert result["data"]["serverInfo"]["cliVersionInfo"]["max_cli_version"]


@pytest.mark.integration
@pytest.mark.parametrize("server", ["accounts"], indirect=True)
def test_sdk_server_info_during_online_logging(server, sdk):
    sdk("""
import wandb
import os
import httpx
try:
    from wandb.sdk.internal.internal_api import Api
except ModuleNotFoundError:
    Api = None  # SDK 0.30 removed this Python-internal API module.
if Api:
    viewer, info = Api().viewer_server_info()
    assert viewer['id'] and info['cliVersionInfo']['max_cli_version']
with wandb.init(project='server-info', id='online-info', settings=wandb.Settings(init_timeout=20, x_disable_stats=True)) as run:
    for step in range(5):
        result = httpx.post(os.environ['WANDB_BASE_URL']+'/graphql', headers={'Authorization':'Bearer '+os.environ['WANDB_API_KEY']}, json={'query':'query viewer_server_info { viewer { id serverInfo { cliVersionInfo latestLocalVersionInfo { outOfDate versionOnThisInstanceString } } } }'}).json()
        assert 'errors' not in result, result
        assert result['data']['viewer']['serverInfo']['cliVersionInfo']['max_cli_version']
        run.log({'loss':5-step}, step=step, commit=True)
""")
    with httpx.Client(
        base_url=server["url"],
        headers={"Authorization": "Bearer " + server["env"]["WANDB_API_KEY"]},
    ) as client:
        runs = client.get("/api/runs").json()["runs"]
        run = next(r for r in runs if r["name"] == "online-info")
        assert run["state"] == "finished"
        assert client.get(
            f"/api/runs/{run['uid']}/series", params={"key": "loss"}
        ).json()["points"] == [[s, 5 - s] for s in range(5)]


@pytest.mark.integration
@pytest.mark.parametrize("existing_history", [False, True])
def test_offline_sync_into_existing_run(server, tmp_path, existing_history):
    name = f"existing-offline-{int(existing_history)}"
    env = dict(server["env"], WANDB_DIR=str(tmp_path), WANDB_MODE="offline")
    with httpx.Client(base_url=server["url"]) as client:
        created = client.post(
            "/graphql",
            json={
                "query": 'mutation($name:String!){upsertBucket(input:{name:$name,entityName:"local",modelName:"server-info",state:"crashed"}){bucket{id}}}',
                "variables": {"name": name},
            },
        ).json()
        uid = created["data"]["upsertBucket"]["bucket"]["id"]
        if existing_history:
            r = client.post(
                f"/files/local/server-info/{name}/file_stream",
                json={
                    "files": {
                        "wandb-history.jsonl": {
                            "offset": 0,
                            "content": [
                                json.dumps(
                                    {"_step": 0, "_timestamp": 100, "old_loss": 99}
                                )
                            ],
                        }
                    }
                },
            )
            r.raise_for_status()
        code = f"""
import wandb
with wandb.init(project='server-info', id='{name}', settings=wandb.Settings(x_disable_stats=True)) as run:
    for step in range(3): run.log({{'loss':3-step}}, step=step, commit=True)
"""
        generated = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert generated.returncode == 0, generated.stderr
        env["WANDB_MODE"] = "online"
        for _ in range(2):
            synced = subprocess.run(
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
                text=True,
                capture_output=True,
                timeout=60,
            )
            assert synced.returncode == 0, synced.stderr
            assert list(tmp_path.glob("wandb/*/*.wandb.synced")), synced.stderr
        runs = client.get("/api/runs").json()["runs"]
        run = next(r for r in runs if r["name"] == name)
        assert run["uid"] == uid
        assert run["state"] == "finished"
        points = client.get(f"/api/runs/{uid}/series", params={"key": "loss"}).json()[
            "points"
        ]
        assert len(points) == 3, points
        history = client.post(
            "/graphql",
            json={
                "query": 'query($name:String!){model(name:"server-info",entityName:"local"){bucket(name:$name){history}}}',
                "variables": {"name": name},
            },
        ).json()
        assert "errors" not in history, history
        rows = [
            json.loads(row) for row in history["data"]["model"]["bucket"]["history"]
        ]
        assert len(rows) == 3 + int(existing_history)
        if existing_history:
            assert rows[0]["old_loss"] == 99
            assert client.get(
                f"/api/runs/{uid}/series", params={"key": "old_loss"}
            ).json()["points"] == [[0, 99]]
