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
