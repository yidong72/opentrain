"""OAuth identities, hashed credentials, and entity-level authorization."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import time
import uuid
from contextvars import ContextVar

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

principal = ContextVar("principal", default=None)
writing = ContextVar("writing", default=False)


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def default_entity():
    user = principal.get()
    return user["username"] if user and user.get("username") else "local"


class Accounts:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT NOT NULL,
                name TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identities (
                provider TEXT NOT NULL, subject TEXT NOT NULL, user TEXT NOT NULL REFERENCES users(id),
                PRIMARY KEY(provider, subject)
            );
            CREATE TABLE IF NOT EXISTS memberships (
                entity TEXT NOT NULL, user TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL,
                PRIMARY KEY(entity, user)
            );
            CREATE TABLE IF NOT EXISTS credentials (
                id TEXT PRIMARY KEY, user TEXT NOT NULL REFERENCES users(id), hash TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL, name TEXT NOT NULL, prefix TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, revoked REAL, last_used REAL
            );
            CREATE TABLE IF NOT EXISTS server_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_owners (run TEXT PRIMARY KEY REFERENCES runs(uid), user TEXT NOT NULL REFERENCES users(id));
            """)
            db.execute(
                "INSERT OR IGNORE INTO server_settings VALUES ('signing_key',?)",
                (secrets.token_hex(32),),
            )
            self.signing_key = db.execute(
                "SELECT value FROM server_settings WHERE key='signing_key'"
            ).fetchone()[0]

    def login_identity(self, provider, subject, email, name, handle=None):
        # Identity comes from the provider's immutable subject, never automatic email linking.
        with self.store.connect(write=True) as db:
            found = db.execute(
                "SELECT u.* FROM identities i JOIN users u ON i.user=u.id WHERE i.provider=? AND i.subject=?",
                (provider, subject),
            ).fetchone()
            if found:
                return dict(found)
            uid = uuid.uuid4().hex
            base = (
                re.sub(
                    r"[^a-z0-9-]", "-", (handle or email.split("@")[0]).lower()
                ).strip("-")[:32]
                or "user"
            )
            username = f"{base}-{uid[:8]}"
            admin = email.casefold() in {
                e.strip().casefold()
                for e in os.environ.get("OPEN_TRAIN_ADMIN_EMAILS", "").split(",")
                if e.strip()
            }
            db.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,?)",
                (uid, username, email, name or username, int(admin), time.time()),
            )
            db.execute(
                "INSERT INTO identities VALUES (?,?,?)", (provider, subject, uid)
            )
            db.execute("INSERT INTO memberships VALUES (?,?, 'owner')", (username, uid))
            return dict(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def issue(self, user_id, kind, name, days):
        secret = (
            secrets.token_hex(20) if kind == "api_key" else secrets.token_urlsafe(48)
        )
        uid = uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                (
                    uid,
                    user_id,
                    digest(secret),
                    kind,
                    name,
                    secret[:6],
                    time.time(),
                    time.time() + days * 86400,
                ),
            )
        return {"id": uid, "key": secret, "prefix": secret[:6], "name": name}

    def run_user(self, run):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT u.* FROM run_owners o JOIN users u ON u.id=o.user WHERE o.run=?",
                (run,),
            ).fetchone()
        user = (
            dict(row)
            if row
            else {
                "id": "local",
                "username": "local",
                "name": "Local user",
                "email": "local@localhost",
                "admin": False,
            }
        )
        return {
            **user,
            "entity": user["username"],
            "flags": "{}",
            "teams": {"edges": []},
        }

    def authenticate(self, secret, kind=None):
        if not secret:
            return None
        with self.store.connect(write=True) as db:
            row = db.execute(
                """SELECT u.*, c.id AS credential_id,c.kind AS credential_kind FROM credentials c
                JOIN users u ON c.user=u.id WHERE c.hash=? AND c.revoked IS NULL AND c.expires>?""",
                (digest(secret), time.time()),
            ).fetchone()
            if not row or (kind and row["credential_kind"] != kind):
                return None
            db.execute(
                "UPDATE credentials SET last_used=? WHERE id=?",
                (time.time(), row["credential_id"]),
            )
            return dict(row)

    def authorize(self, entity, write=None):
        user = principal.get()
        if user is None or user.get("admin"):
            return
        if user.get("file_run"):
            row = self.store.get(uid=user["file_run"], check_access=False)
            if row and row["entity"] == entity:
                return
        with self.store.connect() as db:
            role = db.execute(
                "SELECT role FROM memberships WHERE entity=? AND user=?",
                (entity, user.get("id")),
            ).fetchone()
        if not role or (
            (writing.get() if write is None else write) and role[0] == "reader"
        ):
            raise PermissionError("Access denied to this workspace")

    def entities(self):
        user = principal.get()
        if user is None or user.get("admin"):
            return None
        with self.store.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT entity FROM memberships WHERE user=?", (user["id"],)
                )
            ]


def bearer(request):
    value = request.headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:]
    if value.lower().startswith("basic "):
        try:
            return base64.b64decode(value[6:], validate=True).decode().split(":", 1)[1]
        except (ValueError, IndexError, UnicodeDecodeError):
            pass
    return ""


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int = Field(default=365, ge=1, le=3650)


class MemberRequest(BaseModel):
    username: str
    role: str = Field(pattern="^(owner|writer|reader)$")


def install_accounts(app, accounts, public_url, enabled):
    router = APIRouter(prefix="/auth")
    oauth = OAuth()
    providers = []
    for name in ("google", "github"):
        client_id = os.environ.get(f"OPEN_TRAIN_{name.upper()}_CLIENT_ID")
        client_secret = os.environ.get(f"OPEN_TRAIN_{name.upper()}_CLIENT_SECRET")
        if not (client_id and client_secret):
            continue
        kwargs = dict(
            client_id=client_id,
            client_secret=client_secret,
            client_kwargs={
                "scope": "openid email profile"
                if name == "google"
                else "read:user user:email",
                "code_challenge_method": "S256",
            },
        )
        if name == "google":
            kwargs["server_metadata_url"] = (
                "https://accounts.google.com/.well-known/openid-configuration"
            )
        else:
            kwargs.update(
                authorize_url="https://github.com/login/oauth/authorize",
                access_token_url="https://github.com/login/oauth/access_token",
                api_base_url="https://api.github.com/",
            )
        oauth.register(name, **kwargs)
        providers.append(name)
    app.state.oauth = oauth

    def user(request, session_only=False):
        current = getattr(request.state, "user", None)
        if not current or not current.get("id") or current.get("legacy"):
            raise HTTPException(401, "Sign in to manage your account")
        if session_only and current.get("credential_kind") != "session":
            raise HTTPException(403, "Use a browser session to manage credentials")
        return current

    @router.get("/providers")
    def available():
        return {"providers": providers, "accounts_enabled": enabled}

    @router.get("/login/{provider}")
    async def login(provider: str, request: Request):
        if provider not in providers:
            raise HTTPException(404, "Provider is not configured")
        if not public_url:
            raise HTTPException(
                503, "Set OPEN_TRAIN_PUBLIC_URL to the registered OAuth origin"
            )
        callback = public_url.rstrip("/") + f"/auth/callback/{provider}"
        return await oauth.create_client(provider).authorize_redirect(request, callback)

    @router.get("/callback/{provider}")
    async def callback(provider: str, request: Request):
        if provider not in providers:
            raise HTTPException(404, "Provider is not configured")
        client = oauth.create_client(provider)
        try:
            token = await client.authorize_access_token(request)
            if provider == "google":
                # Authlib verifies OIDC signature, issuer, audience, expiry and nonce.
                profile = token["userinfo"]
                if profile.get("email_verified") is not True:
                    raise HTTPException(403, "A verified email is required")
                subject, email, name, handle = (
                    profile["sub"],
                    profile["email"],
                    profile.get("name"),
                    None,
                )
            else:
                response = await client.get("user", token=token)
                response.raise_for_status()
                profile = response.json()
                response = await client.get("user/emails", token=token)
                response.raise_for_status()
                emails = [e for e in response.json() if e.get("verified")]
                if not emails:
                    raise HTTPException(403, "A verified email is required")
                selected = next((e for e in emails if e.get("primary")), emails[0])
                subject, email, name, handle = (
                    str(profile["id"]),
                    selected["email"],
                    profile.get("name"),
                    profile["login"],
                )
            account = accounts.login_identity(provider, subject, email, name, handle)
        except OAuthError as error:
            raise HTTPException(
                400, "OAuth sign-in failed; please start again"
            ) from error
        session = accounts.issue(account["id"], "session", "Browser", 7)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "open_train_session",
            session["key"],
            httponly=True,
            secure=bool(public_url and public_url.startswith("https://")),
            samesite="lax",
            max_age=7 * 86400,
        )
        return response

    @router.get("/me")
    def me(request: Request):
        current = user(request)
        with accounts.store.connect() as db:
            memberships = [
                dict(r)
                for r in db.execute(
                    "SELECT entity,role FROM memberships WHERE user=?", (current["id"],)
                )
            ]
        return {k: current[k] for k in ("id", "username", "name", "email", "admin")} | {
            "memberships": memberships
        }

    @router.post("/logout")
    def logout(request: Request):
        current = user(request, True)
        with accounts.store.connect(write=True) as db:
            db.execute(
                "UPDATE credentials SET revoked=? WHERE id=?",
                (time.time(), current["credential_id"]),
            )
        response = JSONResponse({"ok": True})
        response.delete_cookie("open_train_session")
        return response

    @router.get("/keys")
    def keys(request: Request):
        current = user(request, True)
        with accounts.store.connect() as db:
            return {
                "keys": [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,name,prefix,created,expires,revoked,last_used FROM credentials WHERE user=? AND kind='api_key' ORDER BY created DESC",
                        (current["id"],),
                    )
                ]
            }

    @router.post("/keys")
    def issue_key(body: KeyRequest, request: Request):
        current = user(request, True)
        return accounts.issue(current["id"], "api_key", body.name, body.expires_in_days)

    @router.delete("/keys/{key_id}")
    def revoke_key(key_id: str, request: Request):
        current = user(request, True)
        with accounts.store.connect(write=True) as db:
            changed = db.execute(
                "UPDATE credentials SET revoked=? WHERE id=? AND user=? AND kind='api_key'",
                (time.time(), key_id, current["id"]),
            ).rowcount
        if not changed:
            raise HTTPException(404, "Key not found")
        return {"ok": True}

    @router.get("/workspaces/{entity}/members")
    def members(entity: str, request: Request):
        current = user(request)
        with accounts.store.connect() as db:
            role = db.execute(
                "SELECT role FROM memberships WHERE entity=? AND user=?",
                (entity, current["id"]),
            ).fetchone()
            if not current["admin"] and (not role or role[0] != "owner"):
                raise HTTPException(403, "Workspace owner required")
            roster = [
                dict(row)
                for row in db.execute(
                    "SELECT u.username,u.name,m.role FROM memberships m JOIN users u ON u.id=m.user WHERE m.entity=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'writer' THEN 1 ELSE 2 END,u.username",
                    (entity,),
                )
            ]
        return {"entity": entity, "members": roster}

    @router.post("/workspaces/{entity}/members")
    def member(entity: str, body: MemberRequest, request: Request):
        current = user(request, True)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", entity):
            raise HTTPException(422, "Invalid workspace name")
        with accounts.store.connect(write=True) as db:
            roles = db.execute(
                "SELECT user,role FROM memberships WHERE entity=?", (entity,)
            ).fetchall()
            if not roles:
                db.execute(
                    "INSERT INTO memberships VALUES (?,?, 'owner')",
                    (entity, current["id"]),
                )
            elif not current["admin"] and not any(
                r["user"] == current["id"] and r["role"] == "owner" for r in roles
            ):
                raise HTTPException(403, "Workspace owner required")
            target = db.execute(
                "SELECT id FROM users WHERE username=?", (body.username,)
            ).fetchone()
            if not target:
                raise HTTPException(404, "User not found; they must sign in first")
            if body.role != "owner" and target[0] == current["id"]:
                raise HTTPException(
                    409, "Do not demote yourself; transfer ownership first"
                )
            db.execute(
                "INSERT INTO memberships VALUES (?,?,?) ON CONFLICT(entity,user) DO UPDATE SET role=excluded.role",
                (entity, target[0], body.role),
            )
        return {"ok": True}

    from fastapi.responses import JSONResponse

    app.include_router(router)
