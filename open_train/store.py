from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def dumps(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def decode(value, default=None):
    if value is None:
        return {} if default is None else default
    return json.loads(value) if isinstance(value, str) else value


class Store:
    def __init__(self, root: str | Path):
        self.accounts = None
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "blobs").mkdir(exist_ok=True)
        self.path = self.root / "tracking.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    uid TEXT PRIMARY KEY, entity TEXT NOT NULL, project TEXT NOT NULL,
                    name TEXT NOT NULL, display_name TEXT NOT NULL, state TEXT NOT NULL,
                    config TEXT NOT NULL DEFAULT '{}', summary TEXT NOT NULL DEFAULT '{}',
                    tags TEXT NOT NULL DEFAULT '[]', group_name TEXT NOT NULL DEFAULT '',
                    job_type TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                    sweep TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'wandb', exitcode INTEGER,
                    UNIQUE(entity, project, name)
                );
                CREATE INDEX IF NOT EXISTS runs_project ON runs(entity, project, updated);
                CREATE TABLE IF NOT EXISTS deleted_runs (
                    run TEXT PRIMARY KEY REFERENCES runs(uid), snapshot TEXT NOT NULL,
                    artifacts TEXT NOT NULL, deleted REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lines (
                    run TEXT NOT NULL REFERENCES runs(uid), file TEXT NOT NULL,
                    offset INTEGER NOT NULL, content TEXT NOT NULL,
                    PRIMARY KEY(run, file, offset)
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    run TEXT NOT NULL REFERENCES runs(uid), stream TEXT NOT NULL,
                    step REAL NOT NULL, key TEXT NOT NULL, value REAL,
                    timestamp REAL NOT NULL, PRIMARY KEY(run, stream, key, step)
                );
                CREATE TABLE IF NOT EXISTS files (
                    run TEXT NOT NULL REFERENCES runs(uid), name TEXT NOT NULL,
                    digest TEXT NOT NULL, md5 TEXT NOT NULL, size INTEGER NOT NULL,
                    updated REAL NOT NULL, PRIMARY KEY(run, name)
                );
                CREATE TABLE IF NOT EXISTS imports (
                    run TEXT NOT NULL REFERENCES runs(uid), event TEXT NOT NULL,
                    PRIMARY KEY(run, event)
                );
                CREATE TABLE IF NOT EXISTS restart_points (
                    run TEXT NOT NULL REFERENCES runs(uid), step INTEGER NOT NULL,
                    timestamp REAL NOT NULL, PRIMARY KEY(run, step, timestamp)
                );
                CREATE TABLE IF NOT EXISTS sweeps (
                    uid TEXT PRIMARY KEY, entity TEXT NOT NULL, project TEXT NOT NULL,
                    name TEXT NOT NULL, config TEXT NOT NULL, state TEXT NOT NULL,
                    next_index INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                    UNIQUE(entity, project, name)
                );
                CREATE TABLE IF NOT EXISTS agents (
                    uid TEXT PRIMARY KEY, sweep TEXT NOT NULL REFERENCES sweeps(uid),
                    assignment TEXT, updated REAL NOT NULL
                );
            """)
        from .records import Records

        self.records = Records(self)
        from .sessions import Sessions

        self.sessions = Sessions(self)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def authorize(self, entity, write=None):
        if self.accounts:
            self.accounts.authorize(entity, write)

    def assert_run(self, uid, write=None, include_deleted=False):
        row = self.get(uid=uid, check_access=False, include_deleted=include_deleted)
        if not row:
            raise KeyError("Run not found")
        self.authorize(row["entity"], write)
        return row

    def get(
        self,
        entity=None,
        project=None,
        name=None,
        uid=None,
        db=None,
        check_access=True,
        include_deleted=False,
    ):
        if db is None:
            with self.connect() as connection:
                return self.get(
                    entity,
                    project,
                    name,
                    uid,
                    connection,
                    check_access,
                    include_deleted,
                )
        query = "SELECT * FROM runs WHERE " + (
            "uid=?" if uid else "entity=? AND project=? AND name=?"
        )
        if not include_deleted:
            query += " AND uid NOT IN (SELECT run FROM deleted_runs)"
        row = db.execute(
            query,
            (uid,) if uid else (entity, project, name),
        ).fetchone()
        if check_access and (row or entity):
            self.authorize(row["entity"] if row else entity)
        return dict(row) if row else None

    def upsert(self, data):
        from .accounts import default_entity, principal

        entity = data.get("entityName") or default_entity()
        project = data.get("modelName") or "uncategorized"
        name = data.get("name") or uuid.uuid4().hex[:8]
        with self.connect(write=True) as db:
            run = self.get(entity, project, name, data.get("id"), db)
            self.authorize(run["entity"] if run else entity, True)
            inserted = run is None
            if run is None:
                run = dict(
                    uid=uuid.uuid4().hex,
                    entity=entity,
                    project=project,
                    name=name,
                    display_name=name,
                    state="running",
                    config="{}",
                    summary="{}",
                    tags="[]",
                    group_name="",
                    job_type="",
                    notes="",
                    sweep=None,
                    created=time.time(),
                    updated=time.time(),
                    source="wandb",
                    exitcode=None,
                )
            for incoming, column in {
                "displayName": "display_name",
                "state": "state",
                "groupName": "group_name",
                "jobType": "job_type",
                "notes": "notes",
                "sweep": "sweep",
            }.items():
                if data.get(incoming) is not None:
                    run[column] = data[incoming]
            if data.get("config") is not None:
                config = decode(run["config"])
                incoming = decode(data["config"])
                config.update(incoming)
                run["config"] = dumps(clean(config))
            if data.get("summaryMetrics") is not None:
                run["summary"] = dumps(clean(decode(data["summaryMetrics"])))
            if data.get("tags") is not None:
                run["tags"] = dumps(data["tags"])
            run["updated"] = time.time()
            if inserted:
                columns = ",".join(run)
                placeholders = ",".join("?" for _ in run)
                db.execute(
                    f"INSERT INTO runs ({columns}) VALUES ({placeholders})",
                    tuple(run.values()),
                )
                db.execute("INSERT INTO record_migrations VALUES (?)", (run["uid"],))
                current = principal.get() or {}
                if self.accounts and current.get("id"):
                    db.execute(
                        "INSERT INTO run_owners VALUES (?,?)",
                        (run["uid"], current["id"]),
                    )
            else:
                columns = [k for k in run if k != "uid"]
                db.execute(
                    "UPDATE runs SET "
                    + ",".join(f"{k}=?" for k in columns)
                    + " WHERE uid=?",
                    [run[k] for k in columns] + [run["uid"]],
                )
            self.sessions.capture(db, run["uid"], run["config"])
            db.execute(
                "INSERT OR IGNORE INTO sdk_session_migrations VALUES (?)", (run["uid"],)
            )
            return run, inserted

    def list_runs(self, entity=None, project=None, limit=500, offset=0):
        where, args = ["uid NOT IN (SELECT run FROM deleted_runs)"], []
        if entity:
            self.authorize(entity)
        allowed = self.accounts.entities() if self.accounts else None
        if allowed is not None:
            if not allowed:
                return []
            where.append("entity IN (" + ",".join("?" for _ in allowed) + ")")
            args.extend(allowed)
        for column, value in (("entity", entity), ("project", project)):
            if value:
                where.append(f"{column}=?")
                args.append(value)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM runs"
                    + clause
                    + " ORDER BY created DESC, uid LIMIT ? OFFSET ?",
                    args + [limit, offset],
                )
            ]

    def delete_run(self, uid, delete_artifacts=False):
        self.assert_run(uid, True, include_deleted=True)
        with self.connect(write=True) as db:
            old = db.execute(
                "SELECT 1 FROM deleted_runs WHERE run=?", (uid,)
            ).fetchone()
            if old:
                return {"deleted": True, "uid": uid, "recoverable": True}
            run = self.get(uid=uid, db=db)
            artifacts = []
            if (
                delete_artifacts
                and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='artifacts'"
                ).fetchone()
            ):
                for row in db.execute(
                    "SELECT uid,state FROM artifacts WHERE run=? AND state!='DELETED'",
                    (uid,),
                ).fetchall():
                    aliases = [
                        dict(r)
                        for r in db.execute(
                            "SELECT * FROM artifact_aliases WHERE artifact=?",
                            (row["uid"],),
                        )
                    ]
                    artifacts.append({**dict(row), "aliases": aliases})
                    db.execute(
                        "DELETE FROM artifact_aliases WHERE artifact=?", (row["uid"],)
                    )
                    db.execute(
                        "UPDATE artifacts SET state='DELETED' WHERE uid=?",
                        (row["uid"],),
                    )
            db.execute(
                "INSERT INTO deleted_runs VALUES (?,?,?,?)",
                (uid, dumps(run), dumps(artifacts), time.time()),
            )
            db.execute(
                "UPDATE runs SET name=?,state='deleted',updated=? WHERE uid=?",
                (f"__deleted__{uid}", time.time(), uid),
            )
        return {"deleted": True, "uid": uid, "recoverable": True}

    def restore_run(self, uid):
        self.assert_run(uid, True, include_deleted=True)
        with self.connect(write=True) as db:
            deleted = db.execute(
                "SELECT * FROM deleted_runs WHERE run=?", (uid,)
            ).fetchone()
            if not deleted:
                raise ValueError("Run is not deleted")
            original = decode(deleted["snapshot"])
            if db.execute(
                "SELECT 1 FROM runs WHERE entity=? AND project=? AND name=?",
                (original["entity"], original["project"], original["name"]),
            ).fetchone():
                raise ValueError("Run name has been reused; restore would conflict")
            for artifact in decode(deleted["artifacts"], []):
                for alias in artifact["aliases"]:
                    if db.execute(
                        "SELECT 1 FROM artifact_aliases WHERE collection=? AND alias=?",
                        (alias["collection"], alias["alias"]),
                    ).fetchone():
                        raise ValueError(
                            "An artifact alias has been reused; restore would conflict"
                        )
                    db.execute(
                        "INSERT INTO artifact_aliases VALUES (?,?,?)",
                        (alias["collection"], alias["alias"], alias["artifact"]),
                    )
                db.execute(
                    "UPDATE artifacts SET state=? WHERE uid=?",
                    (artifact["state"], artifact["uid"]),
                )
            db.execute(
                "UPDATE runs SET name=?,state=?,updated=? WHERE uid=?",
                (original["name"], original["state"], time.time(), uid),
            )
            db.execute("DELETE FROM deleted_runs WHERE run=?", (uid,))
        return {"restored": True, "uid": uid}

    def count(self, uid, file):
        self.assert_run(uid)
        with self.connect() as db:
            return db.execute(
                "SELECT COALESCE(MAX(offset)+1,0) FROM lines WHERE run=? AND file=?",
                (uid, file),
            ).fetchone()[0]

    def lines(self, uid, file, tail=None):
        self.assert_run(uid)
        with self.connect() as db:
            if tail:
                rows = db.execute(
                    "SELECT content FROM lines WHERE run=? AND file=? ORDER BY offset DESC LIMIT ?",
                    (uid, file, tail),
                ).fetchall()
                return [r[0] for r in reversed(rows)]
            return [
                r[0]
                for r in db.execute(
                    "SELECT content FROM lines WHERE run=? AND file=? ORDER BY offset",
                    (uid, file),
                )
            ]

    def add_metrics(self, db, uid, stream, row, fallback_step=0):
        step = row.get("_step", fallback_step)
        timestamp = row.get("_timestamp", time.time())
        if not isinstance(step, (float, int)) or not math.isfinite(step):
            raise ValueError("Metric step must be finite")
        if not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp):
            timestamp = time.time()
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                db.execute(
                    """INSERT INTO metrics VALUES (?,?,?,?,?,?)
                    ON CONFLICT(run,stream,key,step) DO UPDATE SET
                    value=excluded.value, timestamp=excluded.timestamp
                    WHERE excluded.timestamp >= metrics.timestamp""",
                    (uid, stream, step, key, clean(value), timestamp),
                )

    def stream(self, uid, payload):
        self.assert_run(uid, True)
        with self.connect(write=True) as db:
            run = self.get(uid=uid, db=db)
            if not run:
                raise KeyError("Run not found")
            summary = decode(run["summary"])
            for filename, chunk in payload.get("files", {}).items():
                offset, content = chunk["offset"], chunk["content"]
                if (
                    not isinstance(offset, int)
                    or offset < 0
                    or not isinstance(content, list)
                ):
                    raise ValueError("Invalid stream chunk")
                immutable = filename in ("wandb-history.jsonl", "wandb-events.jsonl")
                next_offset = db.execute(
                    "SELECT COALESCE(MAX(offset)+1,0) FROM lines WHERE run=? AND file=?",
                    (uid, filename),
                ).fetchone()[0]
                if immutable and offset > next_offset:
                    raise ValueError(
                        f"Gap in {filename}: expected offset {next_offset}, got {offset}"
                    )
                for index, line in enumerate(content, offset):
                    if not isinstance(line, str):
                        raise ValueError("Stream content must contain strings")
                    old = db.execute(
                        "SELECT content FROM lines WHERE run=? AND file=? AND offset=?",
                        (uid, filename, index),
                    ).fetchone()
                    if old and immutable:
                        if old[0] != line:
                            raise ValueError(
                                "Conflicting stream write; only one writer per run ID is supported"
                            )
                        continue
                    db.execute(
                        "INSERT INTO lines VALUES (?,?,?,?) ON CONFLICT(run,file,offset) DO UPDATE SET content=excluded.content",
                        (uid, filename, index, line),
                    )
                    if immutable:
                        row = decode(line)
                        rid = self.records.put(
                            db,
                            uid,
                            "history" if "history" in filename else "system",
                            f"sdk:{index}",
                            row,
                            index,
                            "sdk",
                            str(row.get("_writer", "sdk")),
                        )
                        self.sessions.attach(
                            db,
                            uid,
                            "history" if "history" in filename else "system",
                            index,
                            rid,
                            row,
                        )
                        self.add_metrics(
                            db,
                            uid,
                            "history" if "history" in filename else "system",
                            row,
                            index,
                        )
                        if "history" in filename:
                            summary.update(clean(row))
                    elif filename == "wandb-summary.json":
                        summary = clean(decode(line))
            state = run["state"]
            if payload.get("preempting"):
                state = "preempting"
            if payload.get("complete"):
                state = "finished" if payload.get("exitcode", 0) == 0 else "failed"
                self.sessions.finish(db, uid, payload.get("exitcode", 0))
            db.execute(
                "UPDATE runs SET summary=?,state=?,updated=?,exitcode=? WHERE uid=?",
                (
                    dumps(summary),
                    state,
                    time.time(),
                    payload.get("exitcode", run["exitcode"]),
                    uid,
                ),
            )

    def history(self, uid, stream="history", minimum=None, maximum=None):
        return self.records.rows(uid, stream, minimum, maximum)["rows"]

    def series(
        self,
        uid,
        key,
        stream="history",
        limit=1500,
        x="auto",
        include_timestamps=False,
    ):
        run = self.assert_run(uid)
        from .records import axis_for

        if x == "auto":
            x = axis_for(run["config"], key)
        rows, missing_axis = self.records.points(uid, key, stream, x)
        # A session may log a setup-only global_step=0 without this metric.
        # Use paired records for this plot, before sampling, not run-wide minima.
        session_starts = {}
        for row in rows:
            sid = row["session"]
            if (
                sid
                and row["value"] is not None
                and (
                    sid not in session_starts
                    or row["timestamp"] < session_starts[sid]["timestamp"]
                )
            ):
                session_starts[sid] = {"x": row["x"], "timestamp": row["timestamp"]}
        points = [[r["x"], r["value"], r["timestamp"]] for r in rows]
        # Min/max buckets preserve spikes as well as endpoints while bounding response size.
        if len(points) > limit:
            result = [points[0]]
            width = math.ceil((len(points) - 2) / max(1, (limit - 2) // 2))
            for start in range(1, len(points) - 1, width):
                bucket = points[start : min(start + width, len(points) - 1)]
                valid = [(i, p) for i, p in enumerate(bucket) if p[1] is not None]
                if valid:
                    selected = {
                        min(valid, key=lambda p: p[1][1])[0],
                        max(valid, key=lambda p: p[1][1])[0],
                    }
                    result.extend(bucket[i] for i in sorted(selected))
            result.append(points[-1])
            points = result
        result = {
            "points": [p[:2] for p in points],
            "total": len(rows),
            "sampled": len(points) < len(rows),
            "axis": x,
            "session_starts": session_starts,
            "missing_axis": missing_axis,
            "join": "record_identity",
            "legacy_projection_points": sum(
                r["source"] == "legacy_tensorboard" for r in rows
            ),
            "axis_warning": "Legacy TensorBoard records were already collapsed by step. Custom axes require matching original value timestamps; unmatched/unknown pairs are omitted."
            if any(r["source"] == "legacy_tensorboard" for r in rows)
            else None,
        }
        if include_timestamps:
            result["timestamps"] = [p[2] for p in points]
        return result

    def keys(self, uid):
        return self.records.keys(uid)

    def files(self, uid, include_deleted=False):
        self.assert_run(uid, include_deleted=include_deleted)
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM files WHERE run=? ORDER BY name", (uid,)
                )
            ]

    def save_file(self, uid, name, digest, md5, size):
        self.assert_run(uid, True)
        with self.connect(write=True) as db:
            if name.startswith("artifacts/"):
                parts = name.split("/", 2)
                if len(parts) != 3:
                    raise ValueError("Invalid artifact file path")
                artifact = db.execute(
                    "SELECT * FROM artifacts WHERE uid=? AND run=?", (parts[1], uid)
                ).fetchone()
                if not artifact:
                    raise ValueError("Artifact not found in this run")
                old = db.execute(
                    "SELECT digest FROM files WHERE run=? AND name=?", (uid, name)
                ).fetchone()
                if artifact["state"] != "PENDING" and (not old or old[0] != digest):
                    raise ValueError("Committed artifact content is immutable")
                expected = db.execute(
                    "SELECT md5 FROM artifact_expected_files WHERE artifact=? AND name=?",
                    (parts[1], parts[2]),
                ).fetchone()
                if expected and expected[0] != md5:
                    raise ValueError("Artifact checksum mismatch")
                if (
                    not expected
                    and not db.execute(
                        "SELECT 1 FROM manifests WHERE artifact=? AND name=?",
                        (parts[1], parts[2]),
                    ).fetchone()
                ):
                    raise ValueError("Artifact file was not declared")
            db.execute(
                "INSERT INTO files VALUES (?,?,?,?,?,?) ON CONFLICT(run,name) DO UPDATE SET digest=excluded.digest,md5=excluded.md5,size=excluded.size,updated=excluded.updated",
                (uid, name, digest, md5, size, time.time()),
            )

    def import_events(self, uid, events):
        self.assert_run(uid, True)
        added = 0
        with self.connect(write=True) as db:
            run = self.get(uid=uid, db=db)
            if (
                run["source"] != "tensorboard"
                and db.execute(
                    "SELECT 1 FROM lines WHERE run=? LIMIT 1", (uid,)
                ).fetchone()
            ):
                raise ValueError(
                    "Import into a dedicated TensorBoard run, not an existing SDK run"
                )
            for event in events:
                # Content identity makes retries safe, including checkpoint reset markers.
                legacy_event = {k: v for k, v in event.items() if k != "session"}
                legacy_id = hashlib.sha256(dumps(legacy_event).encode()).hexdigest()
                if db.execute(
                    "SELECT 1 FROM imports WHERE run=? AND event=?", (uid, legacy_id)
                ).fetchone():
                    continue
                event_id = hashlib.sha256(
                    dumps(
                        event
                        if event.get("session", "legacy-default") != "legacy-default"
                        else legacy_event
                    ).encode()
                ).hexdigest()
                if not db.execute(
                    "INSERT OR IGNORE INTO imports VALUES (?,?)", (uid, event_id)
                ).rowcount:
                    continue
                step, timestamp = event["step"], event["wall_time"]
                if event.get("restart"):
                    db.execute(
                        "INSERT OR IGNORE INTO restart_points VALUES (?,?,?)",
                        (uid, step, timestamp),
                    )
                    db.execute(
                        "DELETE FROM metrics WHERE run=? AND step>=? AND timestamp<=?",
                        (uid, step, timestamp),
                    )
                    db.execute(
                        "UPDATE history_records SET superseded=1 WHERE run=? AND source IN ('tensorboard','legacy_tensorboard') AND step>=? AND timestamp<=?",
                        (uid, step, timestamp),
                    )
                elif db.execute(
                    "SELECT 1 FROM restart_points WHERE run=? AND step<=? AND timestamp>? LIMIT 1",
                    (uid, step, timestamp),
                ).fetchone():
                    # A late-arriving older file must not resurrect a purged checkpoint tail.
                    added += 1
                    continue
                self.add_metrics(
                    db,
                    uid,
                    "history",
                    {**event.get("values", {}), "_step": step, "_timestamp": timestamp},
                )
                if event.get("values"):
                    session = event.get("session", "legacy-default")
                    epoch = db.execute(
                        "SELECT COALESCE(MAX(timestamp),0) FROM restart_points WHERE run=? AND step<=? AND timestamp<=?",
                        (uid, step, timestamp),
                    ).fetchone()[0]
                    identity = "tb:" + dumps([session, epoch, step])
                    self.records.put(
                        db,
                        uid,
                        "history",
                        identity,
                        {**event["values"], "_step": step, "_timestamp": timestamp},
                        step,
                        "tensorboard",
                        session,
                        merge=True,
                    )
                    # A new post-restart event replaces a superseded projection
                    # only within its explicitly identified session/step.
                    db.execute(
                        "UPDATE history_records SET superseded=0 WHERE run=? AND stream='history' AND identity=?",
                        (uid, identity),
                    )
                added += 1
            self.records.rebuild_summary(db, uid)
            db.execute(
                "UPDATE runs SET source='tensorboard',state='imported',updated=? WHERE uid=?",
                (time.time(), uid),
            )
        return added
