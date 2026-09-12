"""Checksummed, resumable local multipart artifact uploads."""

import base64
import hashlib
import os
import tempfile
import time
import uuid

from .store import decode, dumps


class Multipart:
    def __init__(self, artifacts):
        self.artifacts, self.store = artifacts, artifacts.store
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS multipart_uploads (
                id TEXT PRIMARY KEY, artifact TEXT NOT NULL REFERENCES artifacts(uid),
                name TEXT NOT NULL, parts TEXT NOT NULL, created REAL NOT NULL,
                completed TEXT
            )""")

    def start(self, artifact, name, parts, info):
        numbers = [p["partNumber"] for p in parts]
        if sorted(numbers) != list(range(1, len(parts) + 1)) or len(parts) > 10000:
            raise ValueError(
                "Multipart parts must be contiguous, unique, and start at 1"
            )
        for part in parts:
            if len(part["hexMD5"]) != 32:
                raise ValueError("Invalid multipart MD5")
            bytes.fromhex(part["hexMD5"])
        uid = uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO multipart_uploads VALUES (?,?,?,?,?,NULL)",
                (uid, artifact["uid"], name, dumps(parts), time.time()),
            )
        return {
            "uploadID": uid,
            "uploadUrlParts": [
                {
                    "partNumber": p["partNumber"],
                    "uploadUrl": info.context["file_url"](
                        artifact["run"], f".multipart/{uid}/{p['partNumber']}", "PUT"
                    ),
                }
                for p in parts
            ],
        }

    def complete(self, _, info, input):
        artifact = self.artifacts.get(input["artifactID"])
        with self.store.connect() as db:
            upload = db.execute(
                "SELECT * FROM multipart_uploads WHERE id=? AND artifact=?",
                (input["uploadID"], artifact["uid"]),
            ).fetchone()
        if (
            not upload
            or input["storagePath"] != f"artifacts/{artifact['uid']}/{upload['name']}"
        ):
            raise ValueError("Multipart upload not found")
        expected = sorted(decode(upload["parts"], []), key=lambda p: p["partNumber"])
        if sorted(input["completedParts"], key=lambda p: p["partNumber"]) != expected:
            raise ValueError("Multipart completion does not match the requested parts")
        if upload["completed"]:
            return {"digest": upload["completed"]}
        files = {f["name"]: f for f in self.store.files(artifact["run"])}
        sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
        fd, temporary = tempfile.mkstemp(
            dir=self.store.root / "blobs", prefix="multipart-"
        )
        try:
            with os.fdopen(fd, "wb") as output:
                for part in expected:
                    row = files.get(f".multipart/{upload['id']}/{part['partNumber']}")
                    if (
                        not row
                        or base64.b64decode(row["md5"]).hex() != part["hexMD5"].lower()
                    ):
                        raise ValueError("Multipart part missing or checksum mismatch")
                    with (self.store.root / "blobs" / row["digest"]).open(
                        "rb"
                    ) as source:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
                            sha.update(chunk)
                            md5.update(chunk)
                            size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            digest = sha.hexdigest()
            os.replace(temporary, self.store.root / "blobs" / digest)
            encoded = base64.b64encode(md5.digest()).decode()
            self.store.save_file(
                artifact["run"], input["storagePath"], digest, encoded, size
            )
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE multipart_uploads SET completed=? WHERE id=?",
                    (encoded, upload["id"]),
                )
            return {"digest": encoded}
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
