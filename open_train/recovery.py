"""Authenticated, explicit recovery operations. Reads never mutate history."""

from typing import Any, Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from .store import dumps


class ImportedRow(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    data: dict[str, Any]


class ImportBatch(BaseModel):
    batch_id: str = Field(min_length=1, max_length=256)
    source: Literal["slurm", "tensorboard", "offline_recovery", "jsonl"]
    session: str = Field(min_length=1, max_length=256)
    records: list[ImportedRow] = Field(max_length=1000)
    status: Literal["partial", "complete"] = "partial"
    expected_records: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    replace_batch_id: str | None = Field(default=None, min_length=1, max_length=256)


class Quarantine(BaseModel):
    record_ids: list[int] = Field(default_factory=list, max_length=5000)
    batch_id: str | None = Field(default=None, min_length=1, max_length=256)
    active: bool = False
    reason: str = Field(min_length=1, max_length=1000)
    dry_run: bool = True


class RestoreRun(BaseModel):
    confirm: bool = False


def install_recovery(app, store):
    def invoke(fn, *args):
        try:
            return fn(*args)
        except KeyError as error:
            raise HTTPException(404, "Run not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/runs/{uid}/records")
    def records(
        uid: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        missing_axis: str | None = None,
        batch_id: str | None = None,
        include_inactive: bool = False,
    ):
        return invoke(
            store.records.catalog,
            uid,
            offset,
            limit,
            missing_axis,
            batch_id,
            include_inactive,
        )

    @app.get("/api/runs/{uid}/imports")
    def imports(uid: str):
        return invoke(store.records.imports, uid)

    @app.post("/api/runs/{uid}/imports")
    def import_rows(uid: str, body: ImportBatch):
        try:
            size = len(dumps(body.model_dump()).encode())
        except ValueError as error:
            raise HTTPException(422, "Import values must be finite JSON") from error
        if size > 8 * 1024 * 1024:
            raise HTTPException(413, "Import request exceeds 8 MiB")
        return invoke(
            store.records.import_rows,
            uid,
            body.batch_id,
            body.source,
            body.session,
            [r.model_dump() for r in body.records],
            body.status,
            body.expected_records,
            body.metadata,
            body.replace_batch_id,
        )

    @app.post("/api/runs/{uid}/quarantine")
    def quarantine(uid: str, body: Quarantine):
        return invoke(
            store.records.quarantine,
            uid,
            body.record_ids,
            body.batch_id,
            body.active,
            body.reason,
            body.dry_run,
        )

    @app.get("/api/runs/{uid}/history-audit")
    def audit(uid: str):
        invoke(store.assert_run, uid)
        with store.connect() as db:
            return {
                "actions": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM history_audit WHERE run=? ORDER BY created DESC LIMIT 100",
                        (uid,),
                    )
                ]
            }

    @app.post("/api/runs/{uid}/restore")
    def restore(uid: str, body: RestoreRun):
        if not body.confirm:
            raise HTTPException(409, "Set confirm=true to restore this deleted run")
        return invoke(store.restore_run, uid)
