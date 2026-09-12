"""Rehearse on a new private copy; never migrate the supplied database in place."""

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from open_train.store import Store


def fingerprint(db, table):
    result = hashlib.sha256()
    count = 0
    for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
        result.update(json.dumps(tuple(row), separators=(",", ":")).encode())
        count += 1
    return {"rows": count, "sha256": result.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    source = Path(args.database).resolve(strict=True)
    target = Path(args.output_dir).absolute()
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    path = target / "tracking.sqlite3"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db,
        sqlite3.connect(path) as backup,
    ):
        db.backup(backup)
    path.chmod(0o600)
    with sqlite3.connect(path) as db:
        before = {
            name: fingerprint(db, name)
            for name in ("runs", "metrics", "lines", "files")
        }
    started = time.monotonic()
    store = Store(target)
    elapsed = time.monotonic() - started
    with store.connect() as db:
        after = {name: fingerprint(db, name) for name in before}
        assert before == after, "Original data changed during index migration"
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        count = db.execute("SELECT COUNT(*) FROM history_records").fetchone()[0]
        points = db.execute("SELECT COUNT(*) FROM record_values").fetchone()[0]
    print(
        json.dumps(
            {
                "ok": True,
                "original_tables_unchanged": before,
                "migration_seconds": round(elapsed, 2),
                "records": count,
                "indexed_values": points,
                "copy": str(path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
