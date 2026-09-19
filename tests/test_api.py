import base64

import pytest
from fastapi.testclient import TestClient

from open_train.app import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path, token="secret"))


def test_auth_and_unknown_operations(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/runs").status_code == 401
    basic = base64.b64encode(b"api:secret").decode()
    assert (
        client.get("/api/runs", headers={"Authorization": f"Basic {basic}"}).status_code
        == 200
    )
    response = client.post(
        "/graphql",
        headers={"Authorization": "Bearer secret"},
        json={"query": "mutation { nonexistent }"},
    )
    assert response.json()["errors"]
    assert response.json()["data"] is None


def test_aliases_and_introspection(client):
    headers = {"Authorization": "Bearer secret"}
    response = client.post(
        "/graphql",
        headers=headers,
        json={
            "query": '{ who: viewer { entity } __type(name:"Run") { fields { name } } }',
            "operationName": "",
        },
    )
    assert response.json()["data"]["who"]["entity"] == "local"
    assert "historyTail" in [
        f["name"] for f in response.json()["data"]["__type"]["fields"]
    ]


def test_signed_files_and_content_safety(client):
    uid = client.app.state.store.upsert({"name": "file-test"})[0]["uid"]
    url = f"/files/{uid}/page.html"
    assert client.put(url, content=b"<script>alert(1)</script>").status_code == 401
    assert (
        client.put(
            url,
            content=b"<script>alert(1)</script>",
            headers={"Authorization": "Bearer secret"},
        ).status_code
        == 200
    )
    detail = client.get(
        f"/api/runs/{uid}", headers={"Authorization": "Bearer secret"}
    ).json()
    signed = detail["files"][0]["url"]
    response = client.get(signed)
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert client.put(signed, content=b"changed").status_code == 401
    assert client.get(signed.replace("page.html", "other.html")).status_code == 401


def test_empty_run_has_valid_resume_status(client):
    client.app.state.store.upsert({"name": "empty", "modelName": "p"})
    query = '{model(name:"p",entityName:"local"){bucket(name:"empty"){historyLineCount historyTail wandbConfig}}}'
    result = client.post(
        "/graphql", headers={"Authorization": "Bearer secret"}, json={"query": query}
    ).json()
    bucket = result["data"]["model"]["bucket"]
    assert bucket["historyLineCount"] == 0
    assert bucket["historyTail"] == "[]"
    assert '"t"' in bucket["wandbConfig"]


def test_lightweight_sessions_endpoint(client):
    uid = client.app.state.store.upsert({"name": "session-poll"})[0]["uid"]
    assert client.get(f"/api/runs/{uid}/sessions").status_code == 401
    headers = {"Authorization": "Bearer secret"}
    assert client.get(f"/api/runs/{uid}/sessions", headers=headers).json() == {
        "sessions": [],
        "session_count": 0,
    }
    assert client.get("/api/runs/missing/sessions", headers=headers).status_code == 404


def test_plot_catalog_skips_files_imports_and_system_values(client, monkeypatch):
    import json

    store = client.app.state.store
    uid = store.upsert({"name": "lightweight-plots"})[0]["uid"]
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {
                    "offset": 0,
                    "content": [json.dumps({"_step": 1, "train/loss": 0.5})],
                },
                "wandb-events.jsonl": {
                    "offset": 0,
                    "content": [json.dumps({"_step": 1, "system/gpu": 99})],
                },
            }
        },
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Chart startup must not load full run detail")

    monkeypatch.setattr(store, "files", forbidden)
    monkeypatch.setattr(store.records, "imports", forbidden)
    monkeypatch.setattr(store, "keys", forbidden)
    headers = {"Authorization": "Bearer secret"}
    assert client.get(f"/api/runs/{uid}/plots").status_code == 401
    response = client.get(f"/api/runs/{uid}/plots", headers=headers)
    assert response.status_code == 200
    assert set(response.json()) == {"keys", "sessions"}
    assert {k["key"] for k in response.json()["keys"]} == {"_step", "train/loss"}
    assert client.get("/api/runs/missing/plots", headers=headers).status_code == 404


def test_batched_series_and_compact_runs(client):
    import json

    store = client.app.state.store
    uid = store.upsert(
        {
            "name": "batch",
            "config": json.dumps(
                {
                    "tensorboard_import": {
                        "value": {
                            "sessions": [{"directory": "s1"}, {"directory": "s2"}]
                        }
                    }
                }
            ),
        }
    )[0]["uid"]
    store.import_events(
        uid,
        [
            {
                "step": i,
                "wall_time": 100 + i,
                "values": {"train/loss": i % 7, "global_step": i * 2},
                "restart": False,
            }
            for i in range(50)
        ],
    )
    body = {"runs": [uid], "keys": ["train/loss", "missing"], "limit": 10}
    assert client.post("/api/series", json=body).status_code == 401
    headers = {"Authorization": "Bearer secret"}
    response = client.post("/api/series", json=body, headers=headers)
    assert response.status_code == 200
    actual = response.json()["series"][uid]["train/loss"]
    old = client.get(
        f"/api/runs/{uid}/series?key=train/loss&limit=10", headers=headers
    ).json()
    assert actual["points"] == old["points"]
    assert actual["timestamps"] == [100 + x for x, _ in actual["points"]]
    assert actual["sampled"] and actual["total"] == 50
    assert actual["stats"] == store.series(uid, "train/loss", limit=1500)["stats"]
    assert actual["stats"]["count"] == 50
    assert actual["stats"]["last"] == 0
    assert actual["stats"]["delta"] == -6
    assert actual["stats"]["mean"] == pytest.approx(sum(i % 7 for i in range(50)) / 50)
    assert response.json()["series"][uid]["missing"]["points"] == []
    assert response.json()["series"][uid]["missing"]["stats"]["last"] is None
    body["x"] = "global_step"
    other = client.post("/api/series", json=body, headers=headers).json()["series"][
        uid
    ]["train/loss"]
    assert other["timestamps"] == [100 + x / 2 for x, _ in other["points"]]
    compact = client.get("/api/runs?compact=true", headers=headers).json()["runs"][0]
    assert compact["session_count"] == 2 and compact["has_metrics"]
    assert "config" not in compact and compact["summary"] == {
        "_step": 49,
        "global_step": 98,
    }
    assert (
        client.post(
            "/api/series", headers=headers, json={"runs": [uid], "keys": ["x"] * 13}
        ).status_code
        == 422
    )


def test_series_statistics_use_latest_record_not_largest_training_step(client):
    import json

    store = client.app.state.store
    uid = store.upsert({"name": "rewound-stats"})[0]["uid"]
    rows = [
        {"_step": 0, "_timestamp": 100, "train/global_step": 100, "train/loss": 8},
        {"_step": 1, "_timestamp": 101, "train/global_step": 50, "train/loss": 4},
        {"_step": 2, "_timestamp": 102, "train/global_step": 51, "train/loss": 0},
        {"_step": 3, "_timestamp": 103, "train/loss": 99},
        {"_step": 4, "_timestamp": 104, "train/global_step": 52, "train/loss": None},
    ]
    store.stream(
        uid,
        {
            "files": {
                "wandb-history.jsonl": {
                    "offset": 0,
                    "content": [json.dumps(row) for row in rows],
                }
            }
        },
    )
    result = store.series(uid, "train/loss", x="train/global_step")
    assert result["missing_axis"] == 1
    assert result["stats"] == {
        "count": 3,
        "last": 0,
        "last_x": 51,
        "last_timestamp": 102,
        "previous": 4,
        "delta": -4,
        "min": 0,
        "max": 8,
        "mean": 4,
        "nonpositive": 1,
        "scope": "all_paired_records",
    }
