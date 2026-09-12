from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import os
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .accounts import Accounts, bearer, install_accounts, principal, writing
from .protocol import Protocol
from .shared import SharedStreams
from .store import Store, decode
from .tables import Tables


class TBEvent(BaseModel):
    step: int = Field(ge=0)
    wall_time: float = Field(allow_inf_nan=False)
    values: dict[str, float | None] = Field(default_factory=dict)
    restart: bool = False


class TBImport(BaseModel):
    entity: str = "local"
    project: str
    run_id: str
    name: str | None = None
    events: list[TBEvent] = Field(max_length=5000)


class SeriesBatch(BaseModel):
    runs: list[str] = Field(min_length=1, max_length=12)
    keys: list[str] = Field(min_length=1, max_length=12)
    x: str = "_step"
    limit: int = Field(default=800, ge=10, le=1500)


def create_app(data_dir=None, token=None, public_url=None):
    app = FastAPI(title="Open Train", version="0.1.0")
    store = Store(data_dir or os.environ.get("OPEN_TRAIN_DATA_DIR", "data"))
    accounts = Accounts(store)
    store.accounts = accounts
    protocol = Protocol(store)
    app.state.store = store
    shared = SharedStreams(store)
    token = token if token is not None else os.environ.get("OPEN_TRAIN_API_KEY", "")
    public_url = public_url or os.environ.get("OPEN_TRAIN_PUBLIC_URL")
    accounts_enabled = os.environ.get("OPEN_TRAIN_AUTH_MODE") == "accounts" or bool(
        os.environ.get("OPEN_TRAIN_GOOGLE_CLIENT_ID")
        or os.environ.get("OPEN_TRAIN_GITHUB_CLIENT_ID")
    )
    signing_key = accounts.signing_key.encode()
    app.state.accounts = accounts

    def signature(uid, name, method, expires):
        return hmac.new(
            signing_key, f"{method}\n{uid}\n{name}\n{expires}".encode(), hashlib.sha256
        ).hexdigest()

    def file_url(request, uid, name, method="GET"):
        expires = int(time.time()) + 3600
        base = (public_url or str(request.base_url)).rstrip("/")
        path = f"/files/{quote(uid, safe='')}/{quote(name, safe='/')}"
        return f"{base}{path}?expires={expires}&signature={signature(uid, name, method, expires)}"

    @app.middleware("http")
    async def authenticate(request, call_next):
        path = request.url.path
        supplied = bearer(request)
        current = (
            accounts.authenticate(supplied, "api_key")
            if supplied
            else accounts.authenticate(
                request.cookies.get("open_train_session"), "session"
            )
        )
        if (
            current is None
            and token
            and supplied
            and secrets.compare_digest(token, supplied)
        ):
            current = {"admin": True, "legacy": True}
        if current is None and not accounts_enabled and not token:
            current = {"admin": True, "legacy": True}
        protected = (
            path == "/graphql"
            or path.startswith(("/api/", "/files/", "/artifacts/", "/artifactsV2/"))
            or path in ("/auth/me", "/auth/keys", "/auth/logout")
            or path.startswith(("/auth/keys/", "/auth/workspaces/"))
        )
        if protected and current is None:
            valid_link = False
            if path.startswith("/files/"):
                parts = path.split("/", 3)
                try:
                    expires = int(request.query_params.get("expires", "0"))
                    supplied = request.query_params.get("signature", "")
                    valid_link = expires >= time.time() and secrets.compare_digest(
                        supplied, signature(parts[2], parts[3], request.method, expires)
                    )
                except (ValueError, IndexError):
                    pass
            if not valid_link:
                return JSONResponse({"detail": "API key required"}, status_code=401)
            current = {"file_run": parts[2], "file_name": parts[3]}
        if (
            current
            and current.get("credential_kind") == "session"
            and request.method not in ("GET", "HEAD", "OPTIONS")
        ):
            origin = request.headers.get("origin")
            expected = (public_url or str(request.base_url)).rstrip("/")
            if (origin and origin != expected) or (
                not origin and request.headers.get("x-open-train-csrf") != "1"
            ):
                return JSONResponse(
                    {"detail": "Invalid request origin"}, status_code=403
                )
        request.state.user = current
        context_token = principal.set(current)
        write_token = writing.set(
            request.method not in ("GET", "HEAD", "OPTIONS") and path != "/graphql"
        )
        try:
            response = await call_next(request)
        except PermissionError:
            response = JSONResponse({"detail": "Access denied"}, status_code=403)
        finally:
            principal.reset(context_token)
            writing.reset(write_token)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if (
            path.startswith(
                ("/api/", "/files/", "/artifacts/", "/artifactsV2/", "/auth/")
            )
            or path == "/graphql"
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    install_accounts(app, accounts, public_url, accounts_enabled)
    app.add_middleware(
        SessionMiddleware,
        secret_key=accounts.signing_key,
        session_cookie="open_train_oauth",
        max_age=600,
        same_site="lax",
        https_only=bool(public_url and public_url.startswith("https://")),
    )

    @app.get("/healthz")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/graphql")
    async def graphql(request: Request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(400, "Expected a GraphQL request object")
        return await run_in_threadpool(
            protocol.execute,
            payload,
            lambda uid, name, method: file_url(request, uid, name, method),
        )

    @app.post("/sdk/otel/v1/{signal}")
    async def sdk_diagnostics(signal: str, request: Request):
        # The SDK's own OpenTelemetry diagnostics are optional, not training data.
        # Acknowledge locally without forwarding them to a third party.
        return Response(content=b"", media_type="application/x-protobuf")

    @app.post("/files/{entity}/{project}/{run}/file_stream")
    async def stream(entity: str, project: str, run: str, request: Request):
        row = await run_in_threadpool(store.get, entity, project, run)
        if not row:
            raise HTTPException(404, "Run not found")
        payload = await request.json()
        try:
            if (
                request.headers.get("x-wandb-use-async-filestream", "").lower()
                == "true"
            ):
                await run_in_threadpool(
                    shared.stream,
                    row["uid"],
                    request.headers.get("x-wandb-async-client-id", ""),
                    payload,
                )
            else:
                await run_in_threadpool(store.stream, row["uid"], payload)
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(409, str(error)) from error
        return {
            "limits": {},
            "exitcode": payload.get("exitcode"),
            "uploaded": payload.get("uploaded", []),
        }

    @app.put("/files/{uid}/{name:path}")
    async def upload(uid: str, name: str, request: Request):
        if not store.get(uid=uid):
            raise HTTPException(404, "Run not found")
        if not name or ".." in Path(name).parts or name.startswith("/"):
            raise HTTPException(400, "Invalid file name")
        sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
        fd, temporary = tempfile.mkstemp(dir=store.root / "blobs", prefix="upload-")
        try:
            with os.fdopen(fd, "wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    sha.update(chunk)
                    md5.update(chunk)
                    await run_in_threadpool(output.write, chunk)
                output.flush()
                os.fsync(output.fileno())
            digest = sha.hexdigest()
            os.replace(temporary, store.root / "blobs" / digest)
            try:
                await run_in_threadpool(
                    store.save_file,
                    uid,
                    name,
                    digest,
                    base64.b64encode(md5.digest()).decode(),
                    size,
                )
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return Response(status_code=200, headers={"ETag": f'"{md5.hexdigest()}"'})

    @app.get("/files/{uid}/{name:path}")
    def download(uid: str, name: str):
        if name.startswith("artifacts/"):
            try:
                artifact = protocol.artifacts.get(name.split("/", 2)[1])
                if artifact["run"] != uid:
                    raise ValueError("Artifact does not belong to this run")
            except ValueError as error:
                raise HTTPException(404, str(error)) from error
        rows = [f for f in store.files(uid) if f["name"] == name]
        if not rows:
            lines = store.lines(uid, name)
            if lines:
                return Response("\n".join(lines) + "\n", media_type="text/plain")
            raise HTTPException(404, "File not found")
        row = rows[0]
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        # Uploaded HTML/SVG are downloads, never executable content on the app origin.
        safe_inline = media_type in (
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "audio/wav",
            "audio/mpeg",
            "video/mp4",
            "video/webm",
            "application/json",
        )
        return FileResponse(
            store.root / "blobs" / row["digest"],
            media_type=media_type,
            filename=Path(name).name,
            content_disposition_type="inline" if safe_inline else "attachment",
            headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
        )

    def artifact_blob(entity, digest, project=None, collection=None, birth=None):
        store.authorize(entity, False)
        try:
            checksum = base64.b64encode(bytes.fromhex(digest)).decode()
        except ValueError as error:
            raise HTTPException(404, "Artifact file not found") from error
        with store.connect() as db:
            rows = db.execute(
                """SELECT f.*,a.uid AS artifact,c.project,c.name AS collection
                FROM files f JOIN artifacts a ON a.run=f.run
                JOIN artifact_versions v ON v.id=a.uid JOIN artifact_collections c ON c.id=v.collection
                WHERE c.entity=? AND f.md5=? AND a.state='COMMITTED'
                AND substr(f.name,1,length('artifacts/'||a.uid||'/'))='artifacts/'||a.uid||'/'""",
                (entity, checksum),
            ).fetchall()
        for row in rows:
            if (
                project
                and row["project"] != project
                or collection
                and row["collection"] != collection
                or birth
                and row["artifact"] != birth
            ):
                continue
            protocol.artifacts.get(row["artifact"])
            return download(row["run"], row["name"])
        raise HTTPException(404, "Artifact file not found")

    @app.get("/artifacts/{entity}/{digest}")
    def artifact_v1(entity: str, digest: str):
        return artifact_blob(entity, digest)

    @app.get(
        "/artifactsV2/{region}/{entity}/{project}/{collection}/{birth}/{digest}/{name:path}"
    )
    def artifact_v2(
        region: str,
        entity: str,
        project: str,
        collection: str,
        birth: str,
        digest: str,
        name: str,
    ):
        return artifact_blob(entity, digest, project, collection, birth)

    def run_json(row):
        return {
            **row,
            "state": protocol.run(row)["state"],
            "config": {
                k: v.get("value") if isinstance(v, dict) and "value" in v else v
                for k, v in decode(row["config"]).items()
                if not k.startswith("_")
            },
            "summary": decode(row["summary"]),
            "tags": decode(row["tags"]),
        }

    @app.get("/api/runs")
    def runs(
        entity: str | None = None,
        project: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
        offset: int = Query(0, ge=0),
        compact: bool = False,
    ):
        rows = store.list_runs(entity, project, limit + 1, offset)
        result = [run_json(r) for r in rows[:limit]]
        if compact:
            for run in result:
                provenance = run["config"].get("tensorboard_import")
                sessions = (
                    provenance.get("sessions") if isinstance(provenance, dict) else None
                )
                run["session_count"] = (
                    len(sessions) if isinstance(sessions, list) else 0
                )
                run["has_metrics"] = any(
                    not key.startswith("_") and isinstance(value, (int, float))
                    for key, value in run["summary"].items()
                )
                run["summary"] = {"_step": run["summary"].get("_step")}
                run.pop("config")
                run.pop("notes")
        return {
            "runs": result,
            "has_more": len(rows) > limit,
        }

    @app.post("/api/series")
    def series_batch(body: SeriesBatch):
        # A POST is used only to bound the query size. It is a read operation:
        # preserve workspace reader access and authorize every run before returning data.
        read_token = writing.set(False)
        try:
            for uid in body.runs:
                if not store.get(uid=uid):
                    raise HTTPException(404, "Run not found")
            return {
                "series": {
                    uid: {
                        key: store.series(
                            uid,
                            key,
                            "history",
                            body.limit,
                            body.x,
                            include_timestamps=True,
                        )
                        for key in dict.fromkeys(body.keys)
                    }
                    for uid in dict.fromkeys(body.runs)
                }
            }
        finally:
            writing.reset(read_token)

    @app.get("/api/runs/{uid}")
    def run(uid: str, request: Request):
        row = store.get(uid=uid)
        if not row:
            raise HTTPException(404, "Run not found")
        return {
            **run_json(row),
            "keys": store.keys(uid),
            "files": [
                {**f, "url": file_url(request, uid, f["name"])}
                for f in store.files(uid)
            ],
        }

    @app.get("/api/runs/{uid}/series")
    def series(
        uid: str,
        key: str,
        stream: str = "history",
        x: str = "_step",
        limit: int = Query(1500, ge=10, le=10000),
    ):
        if not store.get(uid=uid):
            raise HTTPException(404, "Run not found")
        return store.series(uid, key, stream, limit, x)

    @app.get("/api/runs/{uid}/history")
    def history(
        uid: str, offset: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=10000)
    ):
        if not store.get(uid=uid):
            raise HTTPException(404, "Run not found")
        rows = store.history(uid)
        return {"rows": rows[offset : offset + limit], "total": len(rows)}

    @app.get("/api/runs/{uid}/writers")
    def writers(uid: str):
        return {"writers": shared.writers(uid)}

    @app.get("/api/runs/{uid}/table")
    def table(
        uid: str,
        request: Request,
        path: str | None = None,
        key: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        search: str = Query("", max_length=500),
        sort: int | None = None,
        descending: bool = False,
    ):
        row = store.get(uid=uid)
        if not row:
            raise HTTPException(404, "Run not found")
        reference = decode(row["summary"]).get(key) if key else path
        if not reference:
            raise HTTPException(404, "Table not found")
        try:
            return Tables(
                protocol.artifacts, lambda r, n, m: file_url(request, r, n, m)
            ).query(uid, reference, offset, limit, search, sort, descending)
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/runs/{uid}/artifacts")
    def run_artifacts(uid: str, request: Request):
        store.assert_run(uid)
        with store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT a.uid FROM artifacts a JOIN artifact_producers p ON a.uid=p.artifact WHERE p.run=? AND a.state='COMMITTED' UNION SELECT a.uid FROM artifacts a JOIN artifact_usage u ON a.uid=u.artifact WHERE u.run=? AND a.state='COMMITTED'",
                    (uid, uid),
                )
            ]
        results = []
        for artifact_id in ids:
            artifact = protocol.artifacts.get(artifact_id)
            result = protocol.artifacts.result(artifact)
            result["metadata"] = decode(result["metadata"])
            result["files"] = []
            for file in protocol.artifacts.artifact_files(artifact):
                target = (
                    protocol.artifacts.reference_target(file["ref"])
                    if file.get("ref")
                    else (artifact["run"], file["name"])
                )
                if target:
                    result["files"].append(
                        {
                            "name": file["logical_name"],
                            "size": file["size"],
                            "path": target[1],
                            "run": target[0],
                            "url": file_url(request, *target),
                        }
                    )
                else:
                    result.setdefault("external_references", []).append(
                        {"name": file["logical_name"], "reference": file["ref"]}
                    )
            result["producer"] = store.get(uid=artifact["run"])["name"]
            result["consumers"] = [
                r["name"] for r in protocol.artifacts.lineage(artifact_id)
            ]
            results.append(result)
        return {"artifacts": results}

    @app.get("/api/runs/{uid}/logs")
    def logs(uid: str):
        return {"lines": store.lines(uid, "output.log", 500)}

    @app.post("/api/import/tensorboard")
    def import_tensorboard(body: TBImport):
        row = store.get(body.entity, body.project, body.run_id)
        if not row:
            row, _ = store.upsert(
                {
                    "entityName": body.entity,
                    "modelName": body.project,
                    "name": body.run_id,
                    "displayName": body.name or body.run_id,
                }
            )
        try:
            added = store.import_events(
                row["uid"], [e.model_dump() for e in body.events]
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"uid": row["uid"], "run_id": row["name"], "events_added": added}

    @app.get("/api/sweeps")
    def sweeps():
        with store.connect() as db:
            allowed = accounts.entities()
            return {
                "sweeps": [
                    dict(r)
                    for r in db.execute("SELECT * FROM sweeps ORDER BY created DESC")
                    if allowed is None or r["entity"] in allowed
                ]
            }

    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    @app.get("/{entity}/{project}")
    @app.get("/{entity}/{project}/runs/{run}")
    @app.get("/{entity}/{project}/sweeps/{sweep}")
    def index():
        return FileResponse(static / "index.html")

    return app
