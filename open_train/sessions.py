"""SDK process provenance without changing SDK steps or upload cursors.

Writer metadata is evidence of a process start; upsertBucket alone is not (the
SDK also upserts config and metric definitions many times within one process).
Old history segments are explicitly inferred, never claimed to be a job census.
"""

import math
import time
from datetime import datetime

from .store import decode, dumps


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


class Sessions:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sdk_sessions (
                    run TEXT NOT NULL REFERENCES runs(uid), id TEXT NOT NULL,
                    started REAL NOT NULL, ended REAL, last_seen REAL,
                    exitcode INTEGER, host TEXT, program TEXT, client TEXT,
                    evidence TEXT NOT NULL, end_reason TEXT,
                    PRIMARY KEY(run,id)
                );
                CREATE INDEX IF NOT EXISTS sdk_sessions_time ON sdk_sessions(run,started);
                CREATE TABLE IF NOT EXISTS sdk_session_migrations (
                    run TEXT PRIMARY KEY REFERENCES runs(uid)
                );
                CREATE TABLE IF NOT EXISTS sdk_deliveries (
                    run TEXT NOT NULL REFERENCES runs(uid), stream TEXT NOT NULL,
                    offset INTEGER NOT NULL, record INTEGER NOT NULL REFERENCES history_records(id),
                    PRIMARY KEY(run,stream,offset)
                );
                CREATE INDEX IF NOT EXISTS deliveries_record ON sdk_deliveries(record,offset);
            """)
            pending = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM runs WHERE uid NOT IN (SELECT run FROM sdk_session_migrations)"
                )
            ]
        for run in pending:
            with store.connect(write=True) as db:
                if db.execute(
                    "SELECT 1 FROM sdk_session_migrations WHERE run=?", (run["uid"],)
                ).fetchone():
                    continue
                self.capture(db, run["uid"], run["config"])
                for stream, filename in (
                    ("history", "wandb-history.jsonl"),
                    ("system", "wandb-events.jsonl"),
                ):
                    for line in db.execute(
                        "SELECT offset,content FROM lines WHERE run=? AND file=? ORDER BY offset",
                        (run["uid"], filename),
                    ).fetchall():
                        row = decode(line["content"])
                        identity = self.store.records.sdk_identity(
                            row, f"sdk:{line['offset']}"
                        )
                        record = db.execute(
                            "SELECT id FROM history_records WHERE run=? AND stream=? AND identity=?",
                            (run["uid"], stream, identity),
                        ).fetchone()
                        # Shared streams already carry their own writer identity.
                        if record:
                            self.attach(
                                db,
                                run["uid"],
                                stream,
                                line["offset"],
                                record["id"],
                                row,
                            )
                if run["state"] in ("finished", "failed"):
                    self.finish(db, run["uid"], run["exitcode"], run["updated"])
                db.execute(
                    "INSERT INTO sdk_session_migrations VALUES (?)", (run["uid"],)
                )

    def capture(self, db, uid, config):
        config = decode(config)
        value = config.get("_wandb", {})
        value = value.get("value", value) if isinstance(value, dict) else {}
        environments = value.get("e", {})
        if not isinstance(environments, dict):
            return
        for writer, metadata in environments.items():
            if not isinstance(metadata, dict):
                continue
            try:
                started = datetime.fromisoformat(
                    metadata["startedAt"].replace("Z", "+00:00")
                ).timestamp()
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                continue
            if not finite(started):
                continue
            sid = "sdk:" + str(metadata.get("writerId") or writer)
            inserted = db.execute(
                "INSERT OR IGNORE INTO sdk_sessions(run,id,started,host,program,client,evidence) VALUES (?,?,?,?,?,?,?)",
                (
                    uid,
                    sid,
                    started,
                    str(metadata.get("host", ""))[:256],
                    str(metadata.get("program", ""))[:2048],
                    str(value.get("cli_version", ""))[:64],
                    "sdk_writer_metadata",
                ),
            ).rowcount
            if inserted:
                db.execute(
                    "UPDATE sdk_sessions SET ended=COALESCE(last_seen,started),end_reason='next_session' WHERE run=? AND id!=? AND started<? AND ended IS NULL",
                    (uid, sid, started),
                )
                # Metadata may arrive after history (or be recovered later).
                # Reattribute this sequential SDK process's interval, including
                # records provisionally attached to its predecessor. Explicit
                # shared-writer records and imported provenance are untouched.
                later = db.execute(
                    "SELECT MIN(started) FROM sdk_sessions WHERE run=? AND started>? AND evidence='sdk_writer_metadata'",
                    (uid, started),
                ).fetchone()[0]
                db.execute(
                    "UPDATE history_records SET session=? WHERE run=? AND source='sdk' AND timestamp>=? AND (? IS NULL OR timestamp<?)",
                    (sid, uid, started, later, later),
                )
                db.execute(
                    "DELETE FROM sdk_sessions WHERE run=? AND evidence='inferred' AND NOT EXISTS (SELECT 1 FROM history_records r WHERE r.run=sdk_sessions.run AND r.session=sdk_sessions.id)",
                    (uid,),
                )
                db.execute(
                    "UPDATE sdk_sessions SET last_seen=(SELECT MAX(timestamp) FROM history_records r WHERE r.run=sdk_sessions.run AND r.session=sdk_sessions.id) WHERE run=?",
                    (uid,),
                )
                db.execute(
                    "UPDATE sdk_sessions SET ended=MAX(started,COALESCE(last_seen,started)),end_reason='next_session' WHERE run=? AND (ended IS NULL OR end_reason='next_session') AND started<(SELECT MAX(started) FROM sdk_sessions WHERE run=?)",
                    (uid, uid),
                )

    def attach(self, db, uid, stream, offset, rid, row):
        db.execute(
            "INSERT OR IGNORE INTO sdk_deliveries VALUES (?,?,?,?)",
            (uid, stream, offset, rid),
        )
        record = db.execute(
            "SELECT session,source,timestamp FROM history_records WHERE id=?", (rid,)
        ).fetchone()
        if record["source"] != "sdk":
            return
        # Exact replay at a new offset keeps the original process attribution.
        if db.execute(
            "SELECT 1 FROM sdk_sessions WHERE run=? AND id=?", (uid, record["session"])
        ).fetchone():
            return
        timestamp = record["timestamp"]
        session = db.execute(
            "SELECT * FROM sdk_sessions WHERE run=? AND started<=? ORDER BY started DESC LIMIT 1",
            (uid, timestamp),
        ).fetchone()
        if session is None:
            earliest = db.execute(
                "SELECT * FROM sdk_sessions WHERE run=? ORDER BY started LIMIT 1",
                (uid,),
            ).fetchone()
            if earliest and earliest["evidence"] == "inferred":
                sid = earliest["id"]
                db.execute(
                    "UPDATE sdk_sessions SET started=MIN(started,?) WHERE run=? AND id=?",
                    (timestamp, uid, sid),
                )
            else:
                sid = "inferred:" + str(rid)
                db.execute(
                    "INSERT INTO sdk_sessions(run,id,started,last_seen,evidence) VALUES (?,?,?,?,'inferred')",
                    (uid, sid, timestamp, timestamp),
                )
        else:
            sid = session["id"]
            if stream == "history" and session["evidence"] == "inferred":
                previous = db.execute(
                    "SELECT r.content FROM sdk_deliveries d JOIN history_records r ON r.id=d.record WHERE d.run=? AND d.stream='history' AND d.offset<? ORDER BY d.offset DESC LIMIT 1",
                    (uid, offset),
                ).fetchone()
                last = decode(previous[0]) if previous else {}
                if (
                    finite(row.get("_step"))
                    and finite(last.get("_step"))
                    and row["_step"] < last["_step"]
                ):
                    self.finish(
                        db, uid, None, session["last_seen"], sid, "inferred_step_reset"
                    )
                    sid = "inferred:" + str(rid)
                    db.execute(
                        "INSERT OR IGNORE INTO sdk_sessions(run,id,started,last_seen,evidence) VALUES (?,?,?,?,'inferred')",
                        (uid, sid, timestamp, timestamp),
                    )
        db.execute("UPDATE history_records SET session=? WHERE id=?", (sid, rid))
        db.execute(
            "UPDATE sdk_sessions SET last_seen=MAX(COALESCE(last_seen,started),?) WHERE run=? AND id=?",
            (timestamp, uid, sid),
        )
        db.execute(
            "UPDATE sdk_sessions SET ended=MAX(started,last_seen) WHERE run=? AND id=? AND end_reason='next_session'",
            (uid, sid),
        )

    def finish(self, db, uid, exitcode, ended=None, sid=None, reason="complete"):
        if sid is None:
            latest = db.execute(
                "SELECT id FROM sdk_sessions WHERE run=? ORDER BY started DESC LIMIT 1",
                (uid,),
            ).fetchone()
            if not latest:
                return
            sid = latest["id"]
        db.execute(
            "UPDATE sdk_sessions SET ended=MAX(started,?),exitcode=?,end_reason=? WHERE run=? AND id=? AND ended IS NULL",
            (ended or time.time(), exitcode, reason, uid, sid),
        )

    def count(self, uid, config):
        with self.store.connect() as db:
            shared = db.execute(
                "SELECT COUNT(DISTINCT session) FROM history_records WHERE run=? AND source='sdk_shared'",
                (uid,),
            ).fetchone()[0]
            sdk = db.execute(
                "SELECT COUNT(*) FROM sdk_sessions WHERE run=?", (uid,)
            ).fetchone()[0]
        return (shared or sdk) + len(self.tensorboard(config))

    @staticmethod
    def tensorboard(config):
        config = decode(config)
        imported = config.get("tensorboard_import", {})
        imported = imported.get("value", imported) if isinstance(imported, dict) else {}
        return (
            imported.get("sessions", [])
            if isinstance(imported.get("sessions", []), list)
            else []
        )

    def list(self, uid, compact=False):
        run = self.store.assert_run(uid)
        with self.store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM sdk_sessions WHERE run=? ORDER BY started,id", (uid,)
                )
            ]
            shared = [
                dict(r)
                for r in db.execute(
                    "SELECT session id,MIN(timestamp) started,MAX(timestamp) last_seen FROM history_records WHERE run=? AND source='sdk_shared' GROUP BY session",
                    (uid,),
                )
            ]
            if shared:
                # Shared writers overlap; never manufacture sequential job ends.
                rows = [
                    {
                        **r,
                        "ended": None,
                        "exitcode": None,
                        "end_reason": None,
                        "evidence": "shared_writer",
                        "host": None,
                        "program": None,
                    }
                    for r in shared
                ]
            # Aggregate once per run, not once per resumed job. System rows can
            # greatly outnumber history rows and must not join the value table.
            stats = {
                r["session"]: dict(r)
                for r in db.execute(
                    "SELECT session,COUNT(*) records,MIN(step) first_step,MAX(step) last_step,MIN(timestamp) first_wall_time,MAX(timestamp) last_wall_time,SUM(active=1 AND superseded=0) active_records FROM history_records WHERE run=? AND stream='history' GROUP BY session",
                    (uid,),
                )
            }
            system_counts = dict(
                db.execute(
                    "SELECT session,COUNT(*) FROM history_records WHERE run=? AND stream='system' GROUP BY session",
                    (uid,),
                )
            )
            axes = {}
            if not compact and rows:
                for r in db.execute(
                    "SELECT r.session,v.key,MIN(v.value) first,MAX(v.value) last FROM history_records r JOIN record_values v ON v.record=r.id WHERE r.run=? AND r.stream='history' AND r.active=1 AND r.superseded=0 GROUP BY r.session,v.key",
                    (uid,),
                ):
                    axes.setdefault(r["session"], {})[r["key"]] = {
                        "first": r["first"],
                        "last": r["last"],
                    }
            for s in rows:
                s.update(
                    stats.get(
                        s["id"],
                        dict(
                            records=0,
                            first_step=None,
                            last_step=None,
                            first_wall_time=None,
                            last_wall_time=None,
                            active_records=0,
                        ),
                    )
                )
                s.pop("session", None)
                s["source"] = "sdk_shared" if shared else "sdk"
                s["system_records"] = system_counts.get(s["id"], 0)
                s["warning"] = (
                    "System telemetry received, but no training history was received for this session."
                    if not s["records"] and s["system_records"]
                    else None
                )
                s["inferred"] = s["evidence"] == "inferred"
                s["state"] = (
                    ("finished" if s["exitcode"] == 0 else "failed")
                    if s["end_reason"] == "complete" and s["exitcode"] is not None
                    else ("ended" if s["ended"] is not None else "open")
                )
                s["axes"] = axes.get(s["id"], {})
                s.pop("run", None)
        rows.extend(
            {
                **s,
                "id": "tensorboard:" + str(i),
                "source": "tensorboard",
                "started": s.get("first_wall_time"),
                "ended": s.get("last_wall_time"),
                "state": "imported",
                "records": s.get("events"),
                "axes": {},
            }
            for i, s in enumerate(self.tensorboard(run["config"]))
        )
        return sorted(rows, key=lambda s: s.get("started") or 0)

    def resume(self, uid):
        self.store.assert_run(uid)
        with self.store.connect() as db:
            count = db.execute(
                "SELECT COALESCE(MAX(offset)+1,0) FROM lines WHERE run=? AND file='wandb-history.jsonl'",
                (uid,),
            ).fetchone()[0]
            tail = None
            for row in db.execute(
                "SELECT content FROM lines WHERE run=? AND file='wandb-history.jsonl' ORDER BY offset DESC",
                (uid,),
            ):
                value = decode(row[0])
                if isinstance(value, dict) and finite(value.get("_step")):
                    tail = value
                    break
            # Preserve legitimate explicit SDK steps, including sparse steps.
            # Quarantine/imports never reduce the upload offset or resume cursor.
            highest = db.execute(
                "SELECT MAX(step) FROM history_records WHERE run=? AND stream='history' AND source IN ('sdk','sdk_shared')",
                (uid,),
            ).fetchone()[0]
            if tail and finite(highest):
                tail["_step"] = max(tail["_step"], highest)
            return {
                "line_count": count,
                "tail": dumps([dumps(tail)] if tail else []),
                "last_step": tail.get("_step") if tail else None,
            }
