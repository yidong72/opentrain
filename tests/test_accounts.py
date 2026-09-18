import secrets
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from open_train.app import create_app


@pytest.fixture
def accounts_app(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_TRAIN_AUTH_MODE", "accounts")
    app = create_app(tmp_path, public_url="http://testserver")
    return app


def identity(app, subject):
    account = app.state.accounts.login_identity(
        "github", subject, f"{subject}@example.com", subject
    )
    key = app.state.accounts.issue(account["id"], "api_key", "training", 1)["key"]
    return account, {"Authorization": f"Bearer {key}"}


def test_identity_isolation_revocation_and_sessions(accounts_app):
    app = accounts_app
    alice, headers = identity(app, "alice")
    bob, other = identity(app, "bob")
    client = TestClient(app)
    assert client.get("/api/runs").status_code == 401
    query = 'mutation($e:String!){upsertBucket(input:{entityName:$e,modelName:"private",name:"run1"}){bucket{id}}}'
    response = client.post(
        "/graphql",
        headers=headers,
        json={"query": query, "variables": {"e": alice["username"]}},
    ).json()
    uid = response["data"]["upsertBucket"]["bucket"]["id"]
    assert client.get(f"/api/runs/{uid}", headers=other).status_code == 403
    assert client.get(f"/api/runs/{uid}/sessions", headers=other).status_code == 403
    assert client.get(f"/api/runs/{uid}/plots", headers=other).status_code == 403
    assert (
        client.get(f"/api/runs/{uid}/sessions?compact=true", headers=other).status_code
        == 403
    )
    assert (
        client.post(
            "/api/series", headers=other, json={"runs": [uid], "keys": ["loss"]}
        ).status_code
        == 403
    )
    assert client.get("/api/runs", headers=other).json()["runs"] == []
    denied = client.post(
        "/graphql",
        headers=other,
        json={"query": query, "variables": {"e": alice["username"]}},
    ).json()
    assert denied["errors"]
    assert (
        client.post(
            "/auth/keys", headers=headers, json={"name": "escalation"}
        ).status_code
        == 403
    )
    session = app.state.accounts.issue(alice["id"], "session", "browser", 1)["key"]
    client.cookies.set("open_train_session", session)
    assert client.post("/auth/keys", json={"name": "csrf"}).status_code == 403
    created = client.post(
        "/auth/keys",
        headers={"Origin": "http://testserver"},
        json={"name": "training2"},
    ).json()
    assert len(created["key"]) == 40
    assert client.get("/auth/keys").json()["keys"][0].get("key") is None
    assert (
        client.delete(
            "/auth/keys/" + created["id"], headers={"Origin": "http://testserver"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/runs", headers={"Authorization": "Bearer " + created["key"]}
        ).status_code
        == 401
    )
    assert (
        client.post("/auth/logout", headers={"Origin": "http://testserver"}).status_code
        == 200
    )
    assert client.get("/auth/me").status_code == 401
    # Matching email addresses across providers must not merge identities.
    google = app.state.accounts.login_identity(
        "google", "google-sub", alice["email"], "Alice"
    )
    assert google["id"] != alice["id"]


def test_reader_cannot_write(accounts_app):
    alice, headers = identity(accounts_app, "owner")
    bob, other = identity(accounts_app, "reader")
    client = TestClient(accounts_app)
    session = accounts_app.state.accounts.issue(alice["id"], "session", "browser", 1)[
        "key"
    ]
    client.cookies.set("open_train_session", session)
    response = client.post(
        f"/auth/workspaces/{alice['username']}/members",
        headers={"Origin": "http://testserver"},
        json={"username": bob["username"], "role": "reader"},
    )
    assert response.status_code == 200
    roster_url = f"/auth/workspaces/{alice['username']}/members"
    roster = client.get(roster_url).json()
    assert roster == {
        "entity": alice["username"],
        "members": [
            {"username": alice["username"], "name": alice["name"], "role": "owner"},
            {"username": bob["username"], "name": bob["name"], "role": "reader"},
        ],
    }
    assert client.get(roster_url, headers=headers).json() == roster
    assert client.get(roster_url, headers=other).status_code == 403
    stranger, stranger_headers = identity(accounts_app, "outsider")
    assert client.get(roster_url, headers=stranger_headers).status_code == 403
    assert TestClient(accounts_app).get(roster_url).status_code == 401
    assert (
        client.get(
            "/api/runs", headers=other, params={"entity": alice["username"]}
        ).status_code
        == 200
    )
    query = 'mutation($e:String!){upsertBucket(input:{entityName:$e,name:"forbidden"}){bucket{id}}}'
    assert client.post(
        "/graphql",
        headers=other,
        json={"query": query, "variables": {"e": alice["username"]}},
    ).json()["errors"]
    assert not accounts_app.state.store.list_runs(project="uncategorized")
    create = client.post(
        "/graphql",
        headers=headers,
        json={"query": query, "variables": {"e": alice["username"]}},
    ).json()
    uid = create["data"]["upsertBucket"]["bucket"]["id"]
    assert (
        client.post(
            "/api/series", headers=other, json={"runs": [uid], "keys": ["loss"]}
        ).status_code
        == 200
    )


def test_oauth_github_pkce_and_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_TRAIN_GITHUB_CLIENT_ID", "test-client")
    monkeypatch.setenv("OPEN_TRAIN_GITHUB_CLIENT_SECRET", secrets.token_hex(20))
    app = create_app(tmp_path, public_url="http://testserver")
    client = TestClient(app)
    response = client.get("/auth/login/github", follow_redirects=False)
    assert response.status_code == 302
    assert "code_challenge=" in response.headers["location"]
    assert "state=" in response.headers["location"]
    response = client.get("/auth/callback/github?state=wrong&code=stolen")
    assert response.status_code == 400
    assert "open_train_session" not in client.cookies


@pytest.mark.parametrize("provider", ["google", "github"])
def test_verified_oauth_callback_creates_opaque_session(
    tmp_path, monkeypatch, provider
):
    monkeypatch.setenv(f"OPEN_TRAIN_{provider.upper()}_CLIENT_ID", "test-client")
    monkeypatch.setenv(f"OPEN_TRAIN_{provider.upper()}_CLIENT_SECRET", "test-secret")
    app = create_app(tmp_path, public_url="https://testserver")
    oauth = app.state.oauth.create_client(provider)
    # Provider exchange is mocked; Authlib's state/PKCE path is tested separately.
    oauth.authorize_access_token = AsyncMock(
        return_value={
            "userinfo": {
                "sub": "stable-sub",
                "email": "verified@example.com",
                "email_verified": True,
                "name": "Verified User",
            }
        }
    )
    oauth.get = AsyncMock(
        side_effect=[
            httpx.Response(
                200,
                json={"id": 42, "login": "verified", "name": "Verified User"},
                request=httpx.Request("GET", "https://api.github.com/user"),
            ),
            httpx.Response(
                200,
                json=[
                    {"email": "verified@example.com", "verified": True, "primary": True}
                ],
                request=httpx.Request("GET", "https://api.github.com/user/emails"),
            ),
        ]
    )
    client = TestClient(app, base_url="https://testserver")
    response = client.get(
        f"/auth/callback/{provider}?code=test", follow_redirects=False
    )
    assert response.status_code == 303
    assert (
        "HttpOnly" in response.headers["set-cookie"]
        and "Secure" in response.headers["set-cookie"]
    )
    me = client.get("/auth/me").json()
    assert me["name"] == "Verified User"
    token = client.cookies.get("open_train_session")
    with app.state.store.connect() as db:
        assert db.execute("SELECT hash FROM credentials").fetchone()[0] != token


@pytest.mark.integration
@pytest.mark.parametrize("server", ["accounts"], indirect=True)
def test_official_sdk_with_per_user_key(server, sdk):
    sdk("""
import os, wandb
with wandb.init(project="private-training",settings=wandb.Settings(init_timeout=20,x_disable_stats=True)) as run:
    assert run.entity==os.environ["WANDB_ENTITY"]
    run.log({"loss":.1})
    path=f"{run.entity}/{run.project}/{run.id}"
api=wandb.Api()
assert api.viewer.username==os.environ["WANDB_ENTITY"]
assert api.run(path).summary["loss"]==.1
""")
    assert httpx.get(server["url"] + "/api/runs").status_code == 401
