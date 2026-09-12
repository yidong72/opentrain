"""Independent, transactional SDK stream cursors for distributed shared runs."""

import json
import time

from .store import clean, decode, dumps


class SharedStreams:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS shared_writers (
                run TEXT NOT NULL REFERENCES runs(uid), writer TEXT NOT NULL,
                updated REAL NOT NULL, finished INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(run,writer)
            );
            CREATE TABLE IF NOT EXISTS shared_lines (
                run TEXT NOT NULL REFERENCES runs(uid), writer TEXT NOT NULL, file TEXT NOT NULL,
                offset INTEGER NOT NULL, content TEXT NOT NULL, global_offset INTEGER,
                PRIMARY KEY(run,writer,file,offset)
            );
            CREATE INDEX IF NOT EXISTS shared_global ON shared_lines(run,file,global_offset);
            """)

    def stream(self, uid, writer, payload):
        self.store.assert_run(uid, True)
        if not writer or len(writer) > 256:
            raise ValueError("Shared streams require a valid X-WANDB-ASYNC-CLIENT-ID")
        with self.store.connect(write=True) as db:
            run = self.store.get(uid=uid, db=db)
            known = db.execute(
                "SELECT * FROM shared_writers WHERE run=? AND writer=?", (uid, writer)
            ).fetchone()
            db.execute(
                "INSERT INTO shared_writers VALUES (?,?,?,0) ON CONFLICT(run,writer) DO UPDATE SET updated=excluded.updated",
                (uid, writer, time.time()),
            )
            summary = decode(run["summary"])
            for name, chunk in payload.get("files", {}).items():
                start, lines = chunk["offset"], chunk["content"]
                if (
                    not isinstance(start, int)
                    or start < 0
                    or not isinstance(lines, list)
                ):
                    raise ValueError("Invalid stream chunk")
                immutable = name in ("wandb-history.jsonl", "wandb-events.jsonl")
                next_local = db.execute(
                    "SELECT COALESCE(MAX(offset)+1,0) FROM shared_lines WHERE run=? AND writer=? AND file=?",
                    (uid, writer, name),
                ).fetchone()[0]
                if immutable and start > next_local:
                    raise ValueError("Gap in shared writer stream")
                for offset, content in enumerate(lines, start):
                    if not isinstance(content, str):
                        raise ValueError("Invalid stream line")
                    old = db.execute(
                        "SELECT * FROM shared_lines WHERE run=? AND writer=? AND file=? AND offset=?",
                        (uid, writer, name, offset),
                    ).fetchone()
                    if old and old["content"] == content:
                        continue
                    if old and immutable:
                        raise ValueError("Conflicting shared writer retry")
                    if known and known["finished"] and immutable:
                        raise ValueError(
                            "Writer already finished; restart with a new SDK client ID"
                        )
                    global_offset = (
                        old["global_offset"]
                        if old
                        else db.execute(
                            "SELECT COALESCE(MAX(offset)+1,0) FROM lines WHERE run=? AND file=?",
                            (uid, name),
                        ).fetchone()[0]
                    )
                    normalized = content
                    if immutable:
                        row = clean(json.loads(content))
                        if "_step" in row:
                            row["_source_step"] = row["_step"]
                        row["_step"], row["_writer"] = global_offset, writer
                        self.store.records.put(
                            db,
                            uid,
                            "history" if name == "wandb-history.jsonl" else "system",
                            f"sdk:{global_offset}",
                            row,
                            global_offset,
                            "sdk_shared",
                            writer,
                        )
                        self.store.add_metrics(
                            db,
                            uid,
                            "history" if name == "wandb-history.jsonl" else "system",
                            row,
                            global_offset,
                        )
                        normalized = dumps(row)
                        if name == "wandb-history.jsonl":
                            summary.update(row)
                    elif name == "wandb-summary.json":
                        # Each worker sends a partial view. Merge its keys, never replace another worker's summary.
                        update = clean(decode(content))
                        update.pop("_step", None)
                        summary.update(update)
                    db.execute(
                        "INSERT INTO shared_lines VALUES (?,?,?,?,?,?) ON CONFLICT(run,writer,file,offset) DO UPDATE SET content=excluded.content",
                        (uid, writer, name, offset, content, global_offset),
                    )
                    db.execute(
                        "INSERT INTO lines VALUES (?,?,?,?) ON CONFLICT(run,file,offset) DO UPDATE SET content=excluded.content",
                        (uid, name, global_offset, normalized),
                    )
            state = run["state"]
            if payload.get("complete") and not (known and known["finished"]):
                # The SDK's x_update_finish_state=False suppresses complete on secondary workers.
                state = "finished" if payload.get("exitcode", 0) == 0 else "failed"
                db.execute(
                    "UPDATE shared_writers SET finished=1 WHERE run=? AND writer=?",
                    (uid, writer),
                )
            db.execute(
                "UPDATE runs SET summary=?,state=?,updated=? WHERE uid=?",
                (dumps(summary), state, time.time(), uid),
            )

    def writers(self, uid):
        self.store.assert_run(uid)
        with self.store.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT w.*, COUNT(l.offset) AS history_rows FROM shared_writers w LEFT JOIN shared_lines l ON w.run=l.run AND w.writer=l.writer AND l.file='wandb-history.jsonl' WHERE w.run=? GROUP BY w.run,w.writer ORDER BY w.writer",
                    (uid,),
                )
            ]
