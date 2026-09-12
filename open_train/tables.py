"""Local, permission-checked table resolution with bounded materialization."""

import base64
import json
from collections import defaultdict
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .store import dumps


class Tables:
    MAX_BYTES = 64 * 1024 * 1024
    MAX_ROWS = 200_000

    def __init__(self, artifacts, file_url):
        self.artifacts, self.store, self.file_url = artifacts, artifacts.store, file_url
        self.bytes = 0

    def locate(self, run, reference, prefix=""):
        url = urlsplit(reference)
        if url.scheme in ("wandb-artifact", "wandb-client-artifact"):
            uid = (
                base64.b64encode(bytes.fromhex(url.netloc)).decode()
                if url.scheme == "wandb-artifact"
                else url.netloc
            )
            artifact = self.artifacts.get(uid)
            run, prefix = artifact["run"], f"artifacts/{artifact['uid']}/"
            reference = unquote(url.path).lstrip("/")
        elif url.scheme or url.netloc:
            raise ValueError("External references are not fetched by the table viewer")
        if ".." in PurePosixPath(reference).parts or reference.startswith("/"):
            raise ValueError("Invalid table path")
        name = prefix + reference
        if name.startswith("artifacts/"):
            self.artifacts.get(name.split("/", 2)[1])
        self.store.assert_run(run)
        return run, name

    def read(self, run, name, depth):
        if depth > 16:
            raise ValueError("Table reference nesting exceeds 16 levels")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT digest,size FROM files WHERE run=? AND name=?", (run, name)
            ).fetchone()
        if not row:
            # Artifact entries can reference files in earlier artifacts.
            if name.startswith("artifacts/"):
                _, uid, entry = name.split("/", 2)
                with self.store.connect() as db:
                    manifest = db.execute(
                        "SELECT f.digest FROM manifests m JOIN files f ON f.run=? AND f.name='artifacts/'||m.artifact||'/'||m.name WHERE m.artifact=? ORDER BY m.rowid DESC LIMIT 1",
                        (run, uid),
                    ).fetchone()
                if manifest:
                    path = self.store.root / "blobs" / manifest[0]
                    self.budget(path.stat().st_size)
                    ref = (
                        json.loads(path.read_text())
                        .get("contents", {})
                        .get(entry, {})
                        .get("ref")
                    )
                    if ref:
                        return self.load(run, ref, depth=depth + 1)
            raise ValueError(f"Table file not found: {name}")
        self.budget(row["size"])
        value = json.loads((self.store.root / "blobs" / row["digest"]).read_text())
        prefix = (
            "/".join(name.split("/")[:2]) + "/" if name.startswith("artifacts/") else ""
        )
        return self.materialize(run, value, prefix, depth)

    def budget(self, size):
        self.bytes += size
        if self.bytes > self.MAX_BYTES:
            raise ValueError(
                "Table preview exceeds the 64 MiB materialization limit; download through the SDK"
            )

    def load(self, run, reference, prefix="", depth=0):
        if isinstance(reference, dict):
            return self.materialize(run, reference, prefix, depth)
        run, name = self.locate(run, reference, prefix)
        return self.read(run, name, depth)

    def concatenate(self, tables):
        columns, rows = None, []
        for table in tables:
            if columns is not None and table["columns"] != columns:
                raise ValueError("Table partitions have incompatible columns")
            columns = table["columns"]
            rows.extend(table["data"])
            self.check_rows(rows)
        return {"columns": columns or [], "data": rows}

    def check_rows(self, rows):
        if len(rows) > self.MAX_ROWS:
            raise ValueError(
                "Table preview exceeds 200,000 rows; download through the SDK"
            )

    def materialize(self, run, value, prefix, depth):
        if depth > 16:
            raise ValueError("Table reference nesting exceeds 16 levels")
        kind = value.get("_type", "table")
        if kind == "incremental-table-file":
            refs = list(
                dict.fromkeys(
                    value.get("previous_increments_paths", [])
                    + [value.get("artifact_path") or value["path"]]
                )
            )
            return self.concatenate(
                self.load(run, ref, prefix, depth + 1) for ref in refs
            )
        if "artifact_path" in value or "path" in value and "columns" not in value:
            return self.load(
                run, value.get("artifact_path") or value["path"], prefix, depth + 1
            )
        if kind == "partitioned-table":
            part_prefix = prefix + value["parts_path"].rstrip("/") + "/"
            names = sorted(
                f["name"]
                for f in self.store.files(run)
                if f["name"].startswith(part_prefix)
                and f["name"].endswith(".table.json")
            )
            return self.concatenate(
                self.load(run, name, depth=depth + 1) for name in names
            )
        if kind == "joined-table":
            left, right = [
                self.load(run, value[key], prefix, depth + 1)
                for key in ("table1", "table2")
            ]
            keys = value["join_key"]
            keys = [keys, keys] if isinstance(keys, str) else keys
            li, ri = left["columns"].index(keys[0]), right["columns"].index(keys[1])
            index, used, rows = defaultdict(list), set(), []
            for i, row in enumerate(right["data"]):
                if row[ri] is not None:
                    index[dumps(row[ri])].append((i, row))
            for row in left["data"]:
                matches = index.get(dumps(row[li]), []) if row[li] is not None else []
                for i, other in matches:
                    rows.append(row + other)
                    used.add(i)
                    self.check_rows(rows)
                if not matches:
                    rows.append(row + [None] * len(right["columns"]))
            rows.extend(
                [None] * len(left["columns"]) + row
                for i, row in enumerate(right["data"])
                if i not in used
            )
            self.check_rows(rows)
            return {
                "columns": [f"left.{c}" for c in left["columns"]]
                + [f"right.{c}" for c in right["columns"]],
                "data": rows,
                "join": "full outer",
            }
        if not isinstance(value.get("columns"), list) or not isinstance(
            value.get("data"), list
        ):
            raise ValueError("Not a supported table")
        self.check_rows(value["data"])
        return {
            "columns": value["columns"],
            "data": [[self.cell(run, c, prefix) for c in row] for row in value["data"]],
            "column_types": value.get("column_types"),
        }

    def cell(self, run, value, prefix):
        if isinstance(value, dict) and value.get("path"):
            try:
                media_run, name = self.locate(run, value["path"], prefix)
                return {**value, "url": self.file_url(media_run, name, "GET")}
            except ValueError:
                return value
        return value

    def query(
        self, run, reference, offset=0, limit=50, search="", sort=None, descending=False
    ):
        table = self.load(run, reference)
        rows = table["data"]
        if search:
            rows = [r for r in rows if search.casefold() in dumps(r).casefold()]
        if sort is not None:
            if not 0 <= sort < len(table["columns"]):
                raise ValueError("Sort column does not exist")

            def key(row):
                value = row[sort]
                return (
                    value is None,
                    0 if isinstance(value, (int, float)) else 1,
                    value if isinstance(value, (int, float)) else dumps(value),
                )

            rows.sort(key=key, reverse=descending)
        return {
            **table,
            "data": rows[offset : offset + limit],
            "total": len(rows),
            "offset": offset,
            "limit": limit,
        }
