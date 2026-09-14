"""Record-identity history index, reversible quarantine, and import receipts.

SDK offsets remain immutable delivery cursors. They are never renumbered by
cleanup. Only TensorBoard events in an explicitly shared session/step merge.
The old metrics table remains a rollback-compatible projection, not an axis join.
"""

import fnmatch
import hashlib
import json
import math
import time
import uuid

from .store import clean, decode, dumps


class Records:
    @staticmethod
    def sdk_identity(row, fallback):
        if isinstance(row, dict) and "_step" in row and "_timestamp" in row:
            return (
                "sdk-event:"
                + hashlib.sha256(
                    json.dumps(
                        clean(row),
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
            )
        return fallback

    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS history_records (
                id INTEGER PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(uid),
                stream TEXT NOT NULL, identity TEXT NOT NULL, step REAL NOT NULL,
                timestamp REAL NOT NULL, source TEXT NOT NULL, session TEXT NOT NULL,
                batch TEXT, content TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                superseded INTEGER NOT NULL DEFAULT 0,
                UNIQUE(run,stream,identity)
            );
            CREATE INDEX IF NOT EXISTS records_run ON history_records(run,stream,active,superseded,id);
            CREATE TABLE IF NOT EXISTS record_values (
                record INTEGER NOT NULL REFERENCES history_records(id), key TEXT NOT NULL,
                value REAL, timestamp REAL NOT NULL, PRIMARY KEY(record,key)
            );
            CREATE INDEX IF NOT EXISTS values_key ON record_values(key,record);
            CREATE TABLE IF NOT EXISTS record_migrations (run TEXT PRIMARY KEY REFERENCES runs(uid));
            CREATE TABLE IF NOT EXISTS import_batches (
                run TEXT NOT NULL REFERENCES runs(uid), id TEXT NOT NULL, source TEXT NOT NULL,
                session TEXT NOT NULL, status TEXT NOT NULL, metadata TEXT NOT NULL,
                expected INTEGER, created REAL NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(run,id)
            );
            CREATE TABLE IF NOT EXISTS history_audit (
                id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(uid), action TEXT NOT NULL,
                reason TEXT NOT NULL, actor TEXT NOT NULL, records TEXT NOT NULL, created REAL NOT NULL
            );
            """)
        # Transactional, per-run migration is restartable. Original lines/metrics
        # and blob data are never removed or rewritten.
        with store.connect() as db:
            pending = [
                r[0]
                for r in db.execute(
                    "SELECT uid FROM runs WHERE uid NOT IN (SELECT run FROM record_migrations)"
                )
            ]
        for uid in pending:
            with store.connect(write=True) as db:
                if db.execute(
                    "SELECT 1 FROM record_migrations WHERE run=?", (uid,)
                ).fetchone():
                    continue
                for stream, filename in (
                    ("history", "wandb-history.jsonl"),
                    ("system", "wandb-events.jsonl"),
                ):
                    found = False
                    for line in db.execute(
                        "SELECT offset,content FROM lines WHERE run=? AND file=? ORDER BY offset",
                        (uid, filename),
                    ).fetchall():
                        found = True
                        row = decode(line["content"])
                        self.put(
                            db,
                            uid,
                            stream,
                            f"sdk:{line['offset']}",
                            row,
                            line["offset"],
                            "sdk",
                            str(row.get("_writer", "legacy-sdk")),
                        )
                    if not found:
                        # Legacy TensorBoard metrics were already collapsed by
                        # step. Preserve that projection, and disclose uncertainty.
                        rows = db.execute(
                            "SELECT * FROM metrics WHERE run=? AND stream=? ORDER BY step,key",
                            (uid, stream),
                        ).fetchall()
                        current, values, timestamps = None, {}, {}
                        for point in rows:
                            if current is not None and point["step"] != current:
                                self.legacy(
                                    db, uid, stream, current, values, timestamps
                                )
                                values, timestamps = {}, {}
                            current = point["step"]
                            values[point["key"]] = point["value"]
                            timestamps[point["key"]] = point["timestamp"]
                        if values:
                            self.legacy(db, uid, stream, current, values, timestamps)
                db.execute("INSERT INTO record_migrations VALUES (?)", (uid,))

    def legacy(self, db, uid, stream, step, values, timestamps):
        values.setdefault("_step", step)
        values.setdefault("_timestamp", max(timestamps.values()))
        rid = self.put(
            db,
            uid,
            stream,
            f"legacy:{step}",
            values,
            step,
            "legacy_tensorboard",
            "unknown",
        )
        db.executemany(
            "UPDATE record_values SET timestamp=? WHERE record=? AND key=?",
            [(timestamp, rid, key) for key, timestamp in timestamps.items()],
        )

    def put(
        self,
        db,
        uid,
        stream,
        identity,
        row,
        fallback=0,
        source="sdk",
        session="",
        batch=None,
        merge=False,
    ):
        if not isinstance(row, dict):
            raise ValueError("History record must be an object")
        row = clean(row)
        if source == "sdk" and "_step" in row and "_timestamp" in row:
            # Reconnecting offline sync can replay the same event at a NEW
            # filestream offset, with JSON keys reordered. Preserve raw delivery
            # lines/cursors, but index that exact event once. Different values,
            # timestamps, steps or writer identities remain distinct records.
            identity = self.sdk_identity(row, identity)
        step, timestamp = row.get("_step", fallback), row.get("_timestamp", time.time())
        if (
            isinstance(step, bool)
            or not isinstance(step, (float, int))
            or not math.isfinite(step)
        ):
            raise ValueError("Metric step must be finite")
        if not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp):
            timestamp = time.time()
        old = db.execute(
            "SELECT * FROM history_records WHERE run=? AND stream=? AND identity=?",
            (uid, stream, identity),
        ).fetchone()
        if old and not merge:
            if decode(old["content"]) != row:
                raise ValueError("Conflicting record identity; use a new import batch")
            return old["id"]
        if old:
            value_times = {
                r["key"]: r["timestamp"]
                for r in db.execute(
                    "SELECT key,timestamp FROM record_values WHERE record=?",
                    (old["id"],),
                )
            }
            accepted = {
                k: v
                for k, v in row.items()
                if timestamp >= value_times.get(k, -math.inf)
            }
            combined = decode(old["content"])
            combined.update(accepted)
            row_for_index = accepted
            timestamp_for_record = max(timestamp, old["timestamp"])
            db.execute(
                "UPDATE history_records SET content=?,timestamp=? WHERE id=?",
                (dumps(combined), timestamp_for_record, old["id"]),
            )
            rid = old["id"]
        else:
            rid = db.execute(
                "INSERT INTO history_records(run,stream,identity,step,timestamp,source,session,batch,content) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    stream,
                    identity,
                    step,
                    timestamp,
                    source,
                    session,
                    batch,
                    dumps(row),
                ),
            ).lastrowid
            row_for_index = row
        for key, value in row_for_index.items():
            if (
                value is None
                or isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                db.execute(
                    "INSERT INTO record_values VALUES (?,?,?,?) ON CONFLICT(record,key) DO UPDATE SET value=excluded.value,timestamp=excluded.timestamp",
                    (rid, key, value, timestamp),
                )
        return rid

    def rows(
        self, uid, stream="history", minimum=None, maximum=None, offset=0, limit=None
    ):
        self.store.assert_run(uid)
        where, args = "run=? AND stream=? AND active=1 AND superseded=0", [uid, stream]
        for condition, value in (("step>=?", minimum), ("step<?", maximum)):
            if value is not None:
                where += " AND " + condition
                args.append(value)
        with self.store.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM history_records WHERE " + where, args
            ).fetchone()[0]
            # SDK delivery order preserves duplicates; native imports use step order.
            order = "CASE WHEN source IN ('tensorboard','legacy_tensorboard') THEN step ELSE id END,id"
            records = db.execute(
                "SELECT content FROM history_records WHERE "
                + where
                + " ORDER BY "
                + order
                + " LIMIT ? OFFSET ?",
                args + [limit if limit is not None else -1, offset],
            ).fetchall()
        return {"rows": [decode(r[0]) for r in records], "total": total}

    def points(self, uid, key, stream, axis):
        with self.store.connect() as db:
            where = (
                "r.run=? AND r.stream=? AND r.active=1 AND r.superseded=0 AND a.key=?"
            )
            args = [uid, stream, key]
            count = db.execute(
                "SELECT COUNT(*) FROM history_records r JOIN record_values a ON a.record=r.id WHERE "
                + where,
                args,
            ).fetchone()[0]
            if axis == "_step":
                rows = db.execute(
                    "SELECT r.step AS x,a.value,a.timestamp,r.id,r.source,r.session FROM history_records r JOIN record_values a ON a.record=r.id WHERE "
                    + where
                    + " ORDER BY r.step,r.id",
                    args,
                ).fetchall()
            else:
                legacy_guard = (
                    ""
                    if axis == "_timestamp"
                    else " AND (r.source!='legacy_tensorboard' OR a.timestamp=b.timestamp)"
                )
                axis_expression = (
                    "CASE WHEN r.source='legacy_tensorboard' THEN a.timestamp ELSE b.value END"
                    if axis == "_timestamp"
                    else "b.value"
                )
                rows = db.execute(
                    "SELECT "
                    + axis_expression
                    + " AS x,a.value,a.timestamp,r.id,r.source,r.session FROM history_records r JOIN record_values a ON a.record=r.id JOIN record_values b ON b.record=r.id AND b.key=? WHERE "
                    + where
                    + " AND b.value IS NOT NULL"
                    + legacy_guard
                    + " ORDER BY x,r.id",
                    [axis] + args,
                ).fetchall()
        return rows, count - len(rows)

    def keys(self, uid):
        self.store.assert_run(uid)
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT r.stream,v.key,COUNT(*) AS count,MAX(r.step) AS last_step FROM history_records r JOIN record_values v ON v.record=r.id WHERE r.run=? AND r.active=1 AND r.superseded=0 GROUP BY r.stream,v.key ORDER BY v.key",
                    (uid,),
                )
            ]

    def rebuild_summary(self, db, uid):
        summary = {}
        for r in db.execute(
            "SELECT content FROM history_records WHERE run=? AND stream='history' AND active=1 AND superseded=0 ORDER BY timestamp,id",
            (uid,),
        ):
            summary.update(decode(r[0]))
        db.execute(
            "UPDATE runs SET summary=?,updated=? WHERE uid=?",
            (dumps(summary), time.time(), uid),
        )

    def catalog(
        self,
        uid,
        offset=0,
        limit=100,
        missing_axis=None,
        batch=None,
        include_inactive=False,
    ):
        self.store.assert_run(uid)
        where, args = "r.run=? AND r.stream='history'", [uid]
        if not include_inactive:
            where += " AND r.active=1 AND r.superseded=0"
        if batch is not None:
            where += " AND r.batch=?"
            args.append(batch)
        if missing_axis:
            where += " AND NOT EXISTS (SELECT 1 FROM record_values v WHERE v.record=r.id AND v.key=? AND v.value IS NOT NULL)"
            args.append(missing_axis)
        with self.store.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM history_records r WHERE " + where, args
            ).fetchone()[0]
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT r.* FROM history_records r WHERE "
                    + where
                    + " ORDER BY id LIMIT ? OFFSET ?",
                    args + [limit, offset],
                )
            ]
        for row in rows:
            row["data"] = decode(row.pop("content"))
        return {"records": rows, "total": total, "offset": offset}

    def quarantine(self, uid, ids, batch, active, reason, dry_run=True):
        from .accounts import principal

        self.store.assert_run(uid, True)
        if bool(ids) == (batch is not None):
            raise ValueError("Specify record_ids or batch_id, not both")
        with self.store.connect(write=True) as db:
            if batch is not None:
                rows = db.execute(
                    "SELECT id,active,superseded FROM history_records WHERE run=? AND batch=?",
                    (uid, batch),
                ).fetchall()
            else:
                ids = list(set(ids))
                rows = db.execute(
                    "SELECT id,active,superseded FROM history_records WHERE run=? AND id IN ("
                    + ",".join("?" for _ in ids)
                    + ")",
                    [uid] + ids,
                ).fetchall()
                if len(rows) != len(ids):
                    raise ValueError("One or more record IDs do not belong to this run")
            selected = [r["id"] for r in rows if r["active"] != int(active)]
            audit = None
            if not dry_run and selected:
                audit = uuid.uuid4().hex
                db.executemany(
                    "UPDATE history_records SET active=? WHERE id=?",
                    [(int(active), rid) for rid in selected],
                )
                db.execute(
                    "INSERT INTO history_audit VALUES (?,?,?,?,?,?,?)",
                    (
                        audit,
                        uid,
                        "restore" if active else "quarantine",
                        reason,
                        str((principal.get() or {}).get("id", "legacy-admin")),
                        dumps(selected),
                        time.time(),
                    ),
                )
                self.rebuild_summary(db, uid)
            return {
                "dry_run": dry_run,
                "matched": len(rows),
                "changed": len(selected),
                "audit_id": audit,
                "active": active,
                "sdk_offsets_unchanged": True,
                "note": "Checkpoint-superseded records remain excluded even when unquarantined. Summaries are recomputed from active history, not custom SDK summary aggregations.",
            }

    def imports(self, uid):
        self.store.assert_run(uid)
        with self.store.connect() as db:
            batches = [
                dict(r)
                for r in db.execute(
                    "SELECT b.*,COUNT(r.id) AS records,COALESCE(SUM(r.active=1 AND r.superseded=0),0) AS active_records FROM import_batches b LEFT JOIN history_records r ON r.run=b.run AND r.batch=b.id WHERE b.run=? GROUP BY b.run,b.id ORDER BY b.created",
                    (uid,),
                )
            ]
            sources = [
                dict(r)
                for r in db.execute(
                    "SELECT source,session,COUNT(*) AS records,SUM(active=1 AND superseded=0) AS active_records FROM history_records WHERE run=? GROUP BY source,session",
                    (uid,),
                )
            ]
        for batch in batches:
            batch["metadata"] = decode(batch["metadata"])
        return {
            "batches": batches,
            "sources": sources,
            "warning": "Legacy records lack verified import completeness/session provenance. Dashboard state is not job health.",
        }

    def import_rows(
        self,
        uid,
        batch_id,
        source,
        session,
        rows,
        status,
        expected,
        metadata,
        replace_batch=None,
    ):
        self.store.assert_run(uid, True)
        if status == "complete" and expected is None:
            raise ValueError(
                "Complete imports require expected_records; completion describes this batch, not the original training run"
            )
        with self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT * FROM import_batches WHERE run=? AND id=?", (uid, batch_id)
            ).fetchone()
            if old and (
                old["source"] != source
                or old["session"] != session
                or decode(old["metadata"]) != metadata
            ):
                raise ValueError("Batch provenance is immutable; use a new batch ID")
            if old and old["status"] == "complete" and status != "complete":
                raise ValueError("Completed batches cannot be reopened")
            added = 0
            for item in rows:
                identity = "import:" + dumps([batch_id, item["id"]])
                exists = db.execute(
                    "SELECT 1 FROM history_records WHERE run=? AND stream='history' AND identity=?",
                    (uid, identity),
                ).fetchone()
                if old and old["status"] == "complete" and not exists:
                    raise ValueError("Completed batches cannot accept new records")
                if "_step" not in item["data"]:
                    raise ValueError(
                        "Imported history requires an explicit _step; never infer training steps from row numbers"
                    )
                self.put(
                    db,
                    uid,
                    "history",
                    identity,
                    item["data"],
                    source=source,
                    session=session,
                    batch=batch_id,
                )
                added += not bool(exists)
            count = db.execute(
                "SELECT COUNT(*) FROM history_records WHERE run=? AND batch=?",
                (uid, batch_id),
            ).fetchone()[0]
            if expected is not None and (
                count > expected or status == "complete" and count != expected
            ):
                raise ValueError("Batch record count does not match expected_records")
            now = time.time()
            db.execute(
                "INSERT INTO import_batches VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run,id) DO UPDATE SET status=excluded.status,expected=excluded.expected,updated=excluded.updated",
                (
                    uid,
                    batch_id,
                    source,
                    session,
                    status,
                    dumps(metadata),
                    expected,
                    now,
                    now,
                ),
            )
            if replace_batch is not None:
                if (
                    status != "complete"
                    or replace_batch == batch_id
                    or not db.execute(
                        "SELECT 1 FROM import_batches WHERE run=? AND id=?",
                        (uid, replace_batch),
                    ).fetchone()
                ):
                    raise ValueError(
                        "Replace requires a different existing batch and a complete replacement"
                    )
                db.execute(
                    "UPDATE history_records SET active=0 WHERE run=? AND batch=?",
                    (uid, replace_batch),
                )
                from .accounts import principal

                db.execute(
                    "INSERT INTO history_audit VALUES (?,?,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        uid,
                        "replace_batch",
                        replace_batch,
                        str((principal.get() or {}).get("id", "legacy-admin")),
                        dumps({"replacement": batch_id}),
                        now,
                    ),
                )
            self.rebuild_summary(db, uid)
        return {
            "batch_id": batch_id,
            "records_added": added,
            "records": count,
            "status": status,
            "replaced_batch": replace_batch,
        }


def metric_axes(config):
    """Decode SDK protobuf-keyed metric definitions (_wandb.value.m)."""
    config = decode(config)
    wandb = config.get("_wandb", {})
    wandb = wandb.get("value", wandb) if isinstance(wandb, dict) else {}
    definitions = wandb.get("m", [])
    if not isinstance(definitions, list):
        return {}
    axes = {}
    for d in definitions:
        if not isinstance(d, dict):
            continue
        name = d.get("1") or d.get("name") or d.get("2") or d.get("glob_name")
        axis = d.get("4") or d.get("step_metric")
        index = d.get("5") or d.get("step_metric_index")
        if not axis and isinstance(index, int) and 0 < index <= len(definitions):
            axis = definitions[index - 1].get("1") or definitions[index - 1].get("name")
        if isinstance(name, str) and isinstance(axis, str):
            axes[name] = axis
    return axes


def axis_for(config, key):
    axes = metric_axes(config)
    return axes.get(key) or next(
        (axis for pattern, axis in axes.items() if fnmatch.fnmatchcase(key, pattern)),
        "_step",
    )
