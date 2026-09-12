"""Versioned artifacts, manifests, aliases, collection links, and lineage."""

from __future__ import annotations

import base64
import json
import time
import uuid
from urllib.parse import unquote, urlsplit

from .accounts import writing
from .filters import matches
from .multipart import Multipart
from .store import decode, dumps


class ArtifactProtocol:
    def __init__(self, protocol):
        self.p, self.store = protocol, protocol.store
        from .protocol import iso, page

        self.iso, self.page = iso, page
        with self.store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS artifact_collections (
                id TEXT PRIMARY KEY, entity TEXT NOT NULL, project TEXT NOT NULL, name TEXT NOT NULL,
                type TEXT NOT NULL, portfolio INTEGER NOT NULL DEFAULT 0, description TEXT,
                created REAL NOT NULL, UNIQUE(entity,project,name)
            );
            CREATE TABLE IF NOT EXISTS artifact_versions (
                id TEXT PRIMARY KEY REFERENCES artifacts(uid), collection TEXT NOT NULL REFERENCES artifact_collections(id),
                version INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                description TEXT, metadata TEXT NOT NULL DEFAULT '{}', ttl INTEGER,
                tags TEXT NOT NULL DEFAULT '[]', UNIQUE(collection,version)
            );
            CREATE TABLE IF NOT EXISTS artifact_memberships (
                id TEXT PRIMARY KEY, collection TEXT NOT NULL REFERENCES artifact_collections(id),
                artifact TEXT NOT NULL REFERENCES artifacts(uid), version INTEGER NOT NULL,
                UNIQUE(collection,artifact), UNIQUE(collection,version)
            );
            CREATE TABLE IF NOT EXISTS artifact_aliases (
                collection TEXT NOT NULL REFERENCES artifact_collections(id), alias TEXT NOT NULL,
                artifact TEXT NOT NULL REFERENCES artifacts(uid), PRIMARY KEY(collection,alias)
            );
            CREATE TABLE IF NOT EXISTS artifact_usage (
                artifact TEXT NOT NULL REFERENCES artifacts(uid), run TEXT NOT NULL REFERENCES runs(uid),
                used_as TEXT NOT NULL DEFAULT '', PRIMARY KEY(artifact,run,used_as)
            );
            CREATE TABLE IF NOT EXISTS artifact_expected_files (
                artifact TEXT NOT NULL REFERENCES artifacts(uid), name TEXT NOT NULL, md5 TEXT NOT NULL,
                PRIMARY KEY(artifact,name)
            );
            CREATE TABLE IF NOT EXISTS artifact_entries (
                artifact TEXT NOT NULL REFERENCES artifacts(uid), name TEXT NOT NULL,
                digest TEXT, size INTEGER NOT NULL, ref TEXT, PRIMARY KEY(artifact,name)
            );
            CREATE TABLE IF NOT EXISTS artifact_types (
                entity TEXT NOT NULL, project TEXT NOT NULL, name TEXT NOT NULL,
                description TEXT, created REAL NOT NULL, PRIMARY KEY(entity,project,name)
            );
            CREATE TABLE IF NOT EXISTS artifact_client_ids (client TEXT PRIMARY KEY, artifact TEXT NOT NULL REFERENCES artifacts(uid));
            CREATE TABLE IF NOT EXISTS artifact_producers (artifact TEXT NOT NULL REFERENCES artifacts(uid), run TEXT NOT NULL REFERENCES runs(uid), PRIMARY KEY(artifact,run));
            INSERT OR IGNORE INTO artifact_producers SELECT uid,run FROM artifacts;
            INSERT OR IGNORE INTO artifact_types SELECT entity,project,type,NULL,created FROM artifact_collections;
            """)
            legacy = db.execute(
                "SELECT * FROM artifacts WHERE uid NOT IN (SELECT id FROM artifact_versions)"
            ).fetchall()
            for row in legacy:
                self.version(db, dict(row), decode(row["metadata"]))
        p = self.p
        self.multipart = Multipart(self)
        p.bind("Mutation", "completeMultipartUploadArtifact", self.multipart.complete)
        p.bind(
            "ArtifactEdge", "version", lambda r, info: f"v{r['node']['versionIndex']}"
        )
        for name, method in {
            "createArtifact": self.create,
            "createArtifactManifest": self.create_manifest,
            "updateArtifactManifest": self.update_manifest,
            "createArtifactFiles": self.create_files,
            "commitArtifact": self.commit,
            "useArtifact": self.use,
            "updateArtifact": self.update,
            "deleteArtifact": self.delete,
            "addAliases": self.add_aliases,
            "deleteAliases": self.delete_aliases,
            "linkArtifact": self.link,
            "unlinkArtifact": self.unlink,
            "createArtifactType": self.create_type,
            "updateArtifactSequence": self.update_collection,
            "updateArtifactPortfolio": self.update_collection,
        }.items():
            p.bind("Mutation", name, method)
        p.bind("Query", "artifact", lambda _, info, id: self.result(self.get(id)))
        p.bind(
            "Query",
            "artifactCollection",
            lambda _, info, id: self.collection_result(self.collection(id)),
        )
        p.bind(
            "Query",
            "clientIDMapping",
            lambda _, info, clientID: {"serverID": self.get(clientID)["uid"]},
        )
        p.bind("Project", "artifact", self.by_name)
        p.bind(
            "Project",
            "artifactCollection",
            lambda r, info, name: self.collection_result(self.find_collection(r, name)),
        )
        p.bind("Project", "artifactCollectionMembership", self.membership_by_name)
        p.bind(
            "Project", "artifactType", lambda r, info, name: self.type_result(r, name)
        )
        p.bind("Project", "artifactTypes", self.types)
        p.bind("Project", "artifactCollections", self.collections)
        p.bind(
            "ArtifactType",
            "artifactCollection",
            lambda r, info, name: self.collection_result(
                self.find_collection(r["project"], name)
            ),
        )
        p.bind(
            "ArtifactType",
            "artifactCollections",
            lambda r, info, **args: self.collections(
                r["project"], info, type=r["name"], **args
            ),
        )
        for kind in ("ArtifactSequence", "ArtifactPortfolio"):
            p.bind(
                kind,
                "artifactMembership",
                lambda r, info, aliasName: self.membership(r["id"], aliasName),
            )
            p.bind(kind, "artifacts", self.versions)
        p.bind(
            "ArtifactSequence", "latestArtifact", lambda r, info: self.latest(r["id"])
        )
        p.bind(
            "ArtifactSequence",
            "aliases",
            lambda r, info, first=None, after=None: self.page(
                self.aliases(collection=r["id"]), first, after
            ),
        )
        p.bind(
            "Artifact",
            "currentManifest",
            lambda r, info: self.current_manifest(r["id"], info),
        )
        p.bind("Artifact", "files", self.files)
        p.bind(
            "Artifact",
            "filesByManifestEntries",
            lambda r, info, entries=None, **_: self.files(
                r,
                info,
                names=[e["name"] for e in entries] if entries else None,
                first=1000,
            ),
        )
        p.bind("Artifact", "usedBy", lambda r, info: self.lineage(r["id"]))
        p.bind(
            "Artifact",
            "createdBy",
            lambda r, info: self.p.run(self.store.get(uid=self.get(r["id"])["run"])),
        )
        p.bind(
            "Artifact",
            "artifactCollections",
            lambda r, info: self.linked_collections(r["id"]),
        )
        p.bind(
            "ArtifactCollectionMembership",
            "files",
            lambda r, info, **kwargs: self.files(r["artifact"], info, **kwargs),
        )
        p.bind(
            "Run",
            "outputArtifacts",
            lambda r, info, **args: self.run_artifacts(r["id"], False, **args),
        )
        p.bind(
            "Run",
            "inputArtifacts",
            lambda r, info, **args: self.run_artifacts(r["id"], True, **args),
        )

    def find_collection(self, project, name):
        self.store.authorize(project["entity"]["name"])
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM artifact_collections WHERE entity=? AND project=? AND name=?",
                (project["entity"]["name"], project["name"], name),
            ).fetchone()
        return dict(row) if row else None

    def collection(self, uid):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM artifact_collections WHERE id=?", (uid,)
            ).fetchone()
        if not row:
            raise ValueError("Artifact collection not found")
        self.store.authorize(row["entity"])
        return dict(row)

    def ensure_collection(self, db, entity, project, name, type, portfolio=False):
        self.store.authorize(entity, True)
        db.execute(
            "INSERT OR IGNORE INTO artifact_types VALUES (?,?,?,NULL,?)",
            (entity, project, type, time.time()),
        )
        row = db.execute(
            "SELECT * FROM artifact_collections WHERE entity=? AND project=? AND name=?",
            (entity, project, name),
        ).fetchone()
        if row:
            if row["type"] != type or bool(row["portfolio"]) != portfolio:
                raise ValueError("Collection already exists with a different type")
            return dict(row)
        uid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO artifact_collections VALUES (?,?,?,?,?,?,NULL,?)",
            (uid, entity, project, name, type, int(portfolio), time.time()),
        )
        return dict(
            db.execute(
                "SELECT * FROM artifact_collections WHERE id=?", (uid,)
            ).fetchone()
        )

    def version(self, db, row, data):
        run = self.store.get(uid=row["run"], db=db)
        collection = self.ensure_collection(
            db,
            run["entity"],
            run["project"],
            data.get("artifactCollectionName") or row["uid"],
            data.get("artifactTypeName") or "dataset",
        )
        index = db.execute(
            "SELECT COALESCE(MAX(version)+1,0) FROM artifact_versions WHERE collection=?",
            (collection["id"],),
        ).fetchone()[0]
        now = time.time()
        tags = [t.get("tagName", t.get("name")) for t in data.get("tags") or []]
        db.execute(
            "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["uid"],
                collection["id"],
                index,
                now,
                now,
                data.get("description"),
                data.get("metadata") or "{}",
                data.get("ttlDurationSeconds"),
                dumps(tags),
            ),
        )
        db.execute(
            "INSERT INTO artifact_memberships VALUES (?,?,?,?)",
            (uuid.uuid4().hex, collection["id"], row["uid"], index),
        )
        if row["state"] == "COMMITTED":
            self.set_aliases(
                db, row["uid"], collection["id"], data.get("aliases") or []
            )

    def get(self, uid, include_deleted=False):
        with self.store.connect() as db:
            mapped = db.execute(
                "SELECT artifact FROM artifact_client_ids WHERE client=?", (uid,)
            ).fetchone()
            if mapped:
                uid = mapped[0]
            row = db.execute(
                "SELECT a.*,v.collection,v.version,v.created,v.updated,v.description,v.metadata AS user_metadata,v.ttl,v.tags FROM artifacts a JOIN artifact_versions v ON a.uid=v.id WHERE a.uid=?",
                (uid,),
            ).fetchone()
        if not row or (row["state"] == "DELETED" and not include_deleted):
            raise ValueError("Artifact not found")
        self.store.assert_run(row["run"], include_deleted=True)
        if (
            not include_deleted
            and row["ttl"]
            and row["ttl"] > 0
            and row["created"] + row["ttl"] < time.time()
        ):
            raise ValueError("Artifact has expired")
        return dict(row)

    def collection_result(self, row):
        if not row:
            return None
        project = self.p.project(row["entity"], row["project"])
        return {
            "__typename": "ArtifactPortfolio"
            if row["portfolio"]
            else "ArtifactSequence",
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "project": project,
            "createdAt": self.iso(row["created"]),
            "updatedAt": self.iso(row["created"]),
            "defaultArtifactType": self.type_result(project, row["type"]),
            "tags": {"edges": []},
            "digestAlgorithm": "MANIFEST_MD5",
        }

    def type_result(self, project, name):
        self.store.authorize(project["entity"]["name"])
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM artifact_types WHERE entity=? AND project=? AND name=?",
                (project["entity"]["name"], project["name"], name),
            ).fetchone()
        if not row:
            return None
        return {
            "id": f"{project['id']}/{name}",
            "name": name,
            "project": project,
            "createdAt": self.iso(row["created"]),
            "description": row["description"],
        }

    def result(self, row):
        if not row:
            return None
        collection = self.collection(row["collection"])
        files = self.artifact_files(row)
        return {
            "id": row["uid"],
            "state": row["state"],
            "digest": row["digest"],
            "digestAlgorithm": "MANIFEST_MD5",
            "artifactSequence": self.collection_result(collection),
            "artifactType": self.type_result(
                self.p.project(collection["entity"], collection["project"]),
                collection["type"],
            ),
            "versionIndex": row["version"],
            "description": row["description"],
            "metadata": row["user_metadata"],
            "ttlDurationSeconds": row["ttl"] if row["ttl"] is not None else -1,
            "ttlIsInherited": row["ttl"] is None,
            "tags": [{"id": name, "name": name} for name in decode(row["tags"], [])],
            "historyStep": decode(row["metadata"]).get("historyStep"),
            "size": sum(f["size"] for f in files),
            "fileCount": len(files),
            "commitHash": row["digest"],
            "createdAt": self.iso(row["created"]),
            "updatedAt": self.iso(row["updated"]),
            "aliases": self.aliases(artifact=row["uid"]),
        }

    def aliases(self, artifact=None, collection=None):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM artifact_aliases WHERE "
                + ("artifact=?" if artifact else "collection=?"),
                (artifact or collection,),
            ).fetchall()
        return [
            {
                "id": f"{r['collection']}:{r['alias']}",
                "alias": r["alias"],
                "artifactCollection": self.collection_result(
                    self.collection(r["collection"])
                ),
            }
            for r in rows
        ]

    def set_aliases(self, db, artifact, collection, aliases):
        for alias in aliases:
            value = alias["alias"] if isinstance(alias, dict) else alias
            if not value or value.startswith("v") and value[1:].isdigit():
                raise ValueError("Version aliases are reserved")
            db.execute(
                "INSERT INTO artifact_aliases VALUES (?,?,?) ON CONFLICT(collection,alias) DO UPDATE SET artifact=excluded.artifact",
                (collection, value, artifact),
            )

    def membership(self, collection, alias):
        self.collection(collection)
        with self.store.connect() as db:
            if alias.startswith("v") and alias[1:].isdigit():
                row = db.execute(
                    "SELECT * FROM artifact_memberships WHERE collection=? AND version=?",
                    (collection, int(alias[1:])),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT m.* FROM artifact_memberships m JOIN artifact_aliases a ON m.collection=a.collection AND m.artifact=a.artifact WHERE m.collection=? AND a.alias=?",
                    (collection, alias),
                ).fetchone()
        if not row:
            return None
        artifact = self.get(row["artifact"])
        return {
            "id": row["id"],
            "versionIndex": row["version"],
            "createdAt": self.iso(artifact["created"]),
            "artifactCollection": self.collection_result(self.collection(collection)),
            "aliases": self.aliases(artifact=row["artifact"]),
            "artifact": self.result(artifact),
        }

    def membership_by_name(self, project, info, name):
        collection_name, _, alias = name.partition(":")
        collection = self.find_collection(project, collection_name)
        return (
            self.membership(collection["id"], alias or "latest") if collection else None
        )

    def by_name(self, project, info, name):
        membership = self.membership_by_name(project, info, name)
        return membership["artifact"] if membership else None

    def latest(self, collection):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT v.id FROM artifact_versions v JOIN artifacts a ON v.id=a.uid WHERE v.collection=? AND a.state='COMMITTED' ORDER BY v.version DESC LIMIT 1",
                (collection,),
            ).fetchone()
        return self.result(self.get(row[0])) if row else None

    def create(self, _, info, input):
        if input.get("distributedID"):
            raise ValueError("Distributed artifact finalization is not implemented")
        if input.get("digestAlgorithm") not in (None, "MANIFEST_MD5"):
            raise ValueError("Only MD5 artifact manifests are currently supported")
        if input.get("storageRegion") not in (None, "", "default"):
            raise ValueError("This server has a single local storage region")
        self.store.authorize(input["entityName"], True)
        run = self.store.get(
            input["entityName"], input["projectName"], input.get("runName")
        )
        if not run:
            run, _ = self.store.upsert(
                {
                    "entityName": input["entityName"],
                    "modelName": input["projectName"],
                    "name": input.get("runName") or "artifact-" + uuid.uuid4().hex,
                    "state": "finished",
                }
            )
        uid = input.get("clientID") or uuid.uuid4().hex
        client_id = uid
        with self.store.connect(write=True) as db:
            mapped = db.execute(
                "SELECT artifact FROM artifact_client_ids WHERE client=?", (uid,)
            ).fetchone()
            if mapped:
                uid = mapped[0]
            old = db.execute("SELECT * FROM artifacts WHERE uid=?", (uid,)).fetchone()
            if old:
                owner = self.store.get(uid=old["run"], db=db)
                original = decode(old["metadata"])
                if (
                    owner["entity"] != input["entityName"]
                    or owner["project"] != input["projectName"]
                    or original.get("artifactCollectionName")
                    != input.get("artifactCollectionName")
                ):
                    raise ValueError("Artifact client ID belongs to another collection")
                if old["digest"] != input["digest"]:
                    raise ValueError("Artifact client ID conflict")
            else:
                latest = db.execute(
                    """SELECT a.*,v.collection FROM artifacts a JOIN artifact_versions v ON a.uid=v.id
                    JOIN artifact_collections c ON c.id=v.collection WHERE c.entity=? AND c.project=? AND c.name=?
                    AND a.state='COMMITTED' ORDER BY v.version DESC LIMIT 1""",
                    (
                        input["entityName"],
                        input["projectName"],
                        input.get("artifactCollectionName"),
                    ),
                ).fetchone()
                if (
                    input.get("enableDigestDeduplication", True)
                    and latest
                    and latest["digest"] == input["digest"]
                ):
                    uid = latest["uid"]
                    self.set_aliases(
                        db, uid, latest["collection"], input.get("aliases") or []
                    )
                else:
                    db.execute(
                        "INSERT INTO artifacts VALUES (?,?,?,?,?)",
                        (uid, run["uid"], input["digest"], "PENDING", dumps(input)),
                    )
                    self.version(
                        db, {"uid": uid, "run": run["uid"], "state": "PENDING"}, input
                    )
            db.execute(
                "INSERT OR IGNORE INTO artifact_client_ids VALUES (?,?)",
                (client_id, uid),
            )
            db.execute(
                "INSERT OR IGNORE INTO artifact_producers VALUES (?,?)",
                (uid, run["uid"]),
            )
        return {"artifact": self.result(self.get(uid))}

    def create_manifest(self, _, info, input):
        artifact = self.get(input["artifactID"])
        if input["type"] == "PATCH":
            raise ValueError("Patch manifests are not yet supported; log a full draft")
        with self.store.connect(write=True) as db:
            old = db.execute(
                "SELECT uid FROM manifests WHERE artifact=? AND name=?",
                (input["artifactID"], input["name"]),
            ).fetchone()
            uid = old[0] if old else uuid.uuid4().hex
            if not old:
                if artifact["state"] != "PENDING":
                    raise ValueError("Committed manifests are immutable")
                db.execute(
                    "INSERT INTO manifests VALUES (?,?,?)",
                    (uid, input["artifactID"], input["name"]),
                )
        return {"artifactManifest": self.manifest(uid, info)}

    def manifest(self, uid, info):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM manifests WHERE uid=?", (uid,)).fetchone()
        if not row:
            raise ValueError("Manifest not found")
        artifact = self.get(row["artifact"])
        return {
            "id": uid,
            "file": self.p.file(
                artifact["run"], f"artifacts/{artifact['uid']}/{row['name']}", info
            ),
        }

    def current_manifest(self, uid, info):
        self.get(uid)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT uid FROM manifests WHERE artifact=? ORDER BY rowid DESC LIMIT 1",
                (uid,),
            ).fetchone()
        return self.manifest(row[0], info) if row else None

    def update_manifest(self, _, info, input):
        return {"artifactManifest": self.manifest(input["artifactManifestID"], info)}

    def create_files(self, _, info, input):
        files = []
        for spec in input["artifactFiles"]:
            artifact = self.get(spec["artifactID"])
            if artifact["state"] != "PENDING":
                existing = next(
                    (
                        f
                        for f in self.artifact_files(artifact)
                        if f["logical_name"] == spec["name"]
                    ),
                    None,
                )
                if not existing or existing["md5"] != spec["md5"]:
                    raise ValueError(
                        "Committed artifact file declarations are immutable"
                    )
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO artifact_expected_files VALUES (?,?,?) ON CONFLICT(artifact,name) DO UPDATE SET md5=excluded.md5",
                    (artifact["uid"], spec["name"], spec["md5"]),
                )
            name = f"artifacts/{artifact['uid']}/{spec['name']}"
            file = self.p.file(artifact["run"], name, info)
            file.update(storagePath=name, artifact={"id": artifact["uid"]})
            if spec.get("uploadPartsInput"):
                file["uploadMultipartUrls"] = self.multipart.start(
                    artifact, spec["name"], spec["uploadPartsInput"], info
                )
            files.append(file)
        return {"files": self.page(files, max(1, len(files)))}

    def artifact_files(self, artifact):
        prefix = f"artifacts/{artifact['uid']}/"
        with self.store.connect() as db:
            manifest_names = {
                r[0]
                for r in db.execute(
                    "SELECT name FROM manifests WHERE artifact=?", (artifact["uid"],)
                )
            }
        files = [
            {**f, "logical_name": f["name"][len(prefix) :]}
            for f in self.store.files(artifact["run"], include_deleted=True)
            if f["name"].startswith(prefix)
            and f["name"][len(prefix) :] not in manifest_names
        ]
        with self.store.connect() as db:
            refs = db.execute(
                "SELECT * FROM artifact_entries WHERE artifact=? AND ref IS NOT NULL",
                (artifact["uid"],),
            ).fetchall()
        files.extend(
            {
                "name": prefix + r["name"],
                "logical_name": r["name"],
                "md5": r["digest"] or "",
                "size": r["size"],
                "updated": artifact["updated"],
                "ref": r["ref"],
            }
            for r in refs
        )
        return files

    def reference_target(self, reference, depth=0):
        if depth > 16:
            raise ValueError("Artifact reference chain exceeds 16 levels")
        parsed = urlsplit(reference)
        if parsed.scheme not in ("wandb-artifact", "wandb-client-artifact"):
            return None
        uid = (
            base64.b64encode(bytes.fromhex(parsed.netloc)).decode()
            if parsed.scheme == "wandb-artifact"
            else parsed.netloc
        )
        artifact = self.get(uid)
        path = unquote(parsed.path).lstrip("/")
        file = next(
            (f for f in self.artifact_files(artifact) if f["logical_name"] == path),
            None,
        )
        if not file:
            raise ValueError("Referenced artifact entry not found")
        if file.get("ref"):
            return self.reference_target(file["ref"], depth + 1)
        return artifact["run"], file["name"]

    def files(self, r, info, names=None, first=None, after=None):
        artifact = self.get(r["id"])
        result = []
        for file in self.artifact_files(artifact):
            if names and file["logical_name"] not in names:
                continue
            node = self.p.file(artifact["run"], file["name"], info, file)
            if file.get("ref"):
                target = self.reference_target(file["ref"])
                url = (
                    info.context["file_url"](*target, "GET") if target else file["ref"]
                )
                node.update(url=url, directUrl=url)
            node.update(
                name=file["logical_name"],
                displayName=file["logical_name"],
                digest=file["md5"],
                storagePath=file["name"],
            )
            result.append(node)
        return self.page(result, first, after)

    def commit(self, _, info, input):
        artifact = self.get(input["artifactID"])
        if artifact["state"] == "COMMITTED":
            return {"artifact": self.result(artifact)}
        files = {f["logical_name"]: f for f in self.artifact_files(artifact)}
        with self.store.connect(write=True) as db:
            manifests = db.execute(
                "SELECT name FROM manifests WHERE artifact=?", (artifact["uid"],)
            ).fetchall()
            if not manifests:
                raise ValueError("Cannot commit an artifact without a manifest")
            for manifest in manifests:
                stored = db.execute(
                    "SELECT digest FROM files WHERE run=? AND name=?",
                    (artifact["run"], f"artifacts/{artifact['uid']}/{manifest[0]}"),
                ).fetchone()
                if not stored:
                    raise ValueError("Artifact manifest has not been uploaded")
                manifest_data = json.loads(
                    (self.store.root / "blobs" / stored[0]).read_text()
                )
                for name, entry in manifest_data.get("contents", {}).items():
                    db.execute(
                        "INSERT INTO artifact_entries VALUES (?,?,?,?,?) ON CONFLICT(artifact,name) DO UPDATE SET digest=excluded.digest,size=excluded.size,ref=excluded.ref",
                        (
                            artifact["uid"],
                            name,
                            entry.get("digest"),
                            entry.get("size") or 0,
                            entry.get("ref"),
                        ),
                    )
                    if entry.get("ref"):
                        continue
                    if name not in files:
                        owner = self.store.get(uid=artifact["run"], db=db)
                        inherited = db.execute(
                            """SELECT f.* FROM files f JOIN runs r ON f.run=r.uid
                            JOIN artifacts a ON f.name='artifacts/'||a.uid||'/'||?
                            WHERE r.entity=? AND f.md5=? AND a.state='COMMITTED'
                            ORDER BY f.updated DESC LIMIT 1""",
                            (name, owner["entity"], entry.get("digest")),
                        ).fetchone()
                        if not inherited:
                            raise ValueError(f"Artifact file not uploaded: {name}")
                        files[name] = dict(inherited)
                        db.execute(
                            "INSERT INTO files VALUES (?,?,?,?,?,?)",
                            (
                                artifact["run"],
                                f"artifacts/{artifact['uid']}/{name}",
                                inherited["digest"],
                                inherited["md5"],
                                inherited["size"],
                                time.time(),
                            ),
                        )
                        db.execute(
                            "INSERT OR IGNORE INTO artifact_expected_files VALUES (?,?,?)",
                            (artifact["uid"], name, inherited["md5"]),
                        )
                    if entry.get("digest") and entry["digest"] != files[name]["md5"]:
                        raise ValueError(f"Artifact checksum mismatch: {name}")
            self.set_aliases(
                db,
                artifact["uid"],
                artifact["collection"],
                decode(artifact["metadata"]).get("aliases") or [],
            )
            db.execute(
                "UPDATE artifacts SET state='COMMITTED' WHERE uid=?", (artifact["uid"],)
            )
            db.execute(
                "UPDATE artifact_versions SET updated=? WHERE id=?",
                (time.time(), artifact["uid"]),
            )
        return {"artifact": self.result(self.get(artifact["uid"]))}

    def use(self, _, info, input):
        token = writing.set(False)
        try:
            return self._use(_, info, input)
        finally:
            writing.reset(token)

    def _use(self, _, info, input):
        self.store.authorize(input["entityName"], True)
        artifact = self.get(input["artifactID"])
        if artifact["state"] != "COMMITTED":
            raise ValueError("Only committed artifacts may be used")
        run = self.store.get(
            input["entityName"], input["projectName"], input["runName"]
        )
        if not run:
            raise ValueError("Run not found")
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO artifact_usage VALUES (?,?,?)",
                (artifact["uid"], run["uid"], input.get("usedAs") or ""),
            )
        return {"artifact": self.result(artifact)}

    def update(self, _, info, input):
        artifact = self.get(input["artifactID"])
        with self.store.connect(write=True) as db:
            for incoming, column in (
                ("description", "description"),
                ("metadata", "metadata"),
                ("ttlDurationSeconds", "ttl"),
            ):
                if incoming in input:
                    db.execute(
                        f"UPDATE artifact_versions SET {column}=? WHERE id=?",
                        (input[incoming], artifact["uid"]),
                    )
            tags = set(decode(artifact["tags"], []))
            tags.update(
                t.get("tagName", t.get("name")) for t in input.get("tagsToAdd") or []
            )
            tags.difference_update(
                t.get("tagName", t.get("name")) for t in input.get("tagsToDelete") or []
            )
            db.execute(
                "UPDATE artifact_versions SET tags=?,updated=? WHERE id=?",
                (dumps(sorted(tags)), time.time(), artifact["uid"]),
            )
            if input.get("aliases") is not None:
                db.execute(
                    "DELETE FROM artifact_aliases WHERE collection=? AND artifact=?",
                    (artifact["collection"], artifact["uid"]),
                )
                self.set_aliases(
                    db, artifact["uid"], artifact["collection"], input["aliases"]
                )
        return {"artifact": self.result(self.get(artifact["uid"]))}

    def delete(self, _, info, input):
        artifact = self.get(input["artifactID"])
        result = self.result(artifact)
        with self.store.connect(write=True) as db:
            aliases = db.execute(
                "SELECT 1 FROM artifact_aliases WHERE artifact=?", (artifact["uid"],)
            ).fetchone()
            if aliases and not input.get("deleteAliases"):
                raise ValueError("Artifact has aliases; pass delete_aliases=True")
            db.execute(
                "DELETE FROM artifact_aliases WHERE artifact=?", (artifact["uid"],)
            )
            db.execute(
                "UPDATE artifacts SET state='DELETED' WHERE uid=?", (artifact["uid"],)
            )
        result["state"] = "DELETED"
        return {"artifact": result}

    def add_aliases(self, _, info, input):
        artifact = self.get(input["artifactID"])
        for alias in input["aliases"]:
            collection = self.find_collection(
                self.p.project(alias["entityName"], alias["projectName"]),
                alias["artifactCollectionName"],
            )
            if not collection:
                raise ValueError("Collection not found")
            with self.store.connect(write=True) as db:
                if not db.execute(
                    "SELECT 1 FROM artifact_memberships WHERE collection=? AND artifact=?",
                    (collection["id"], artifact["uid"]),
                ).fetchone():
                    raise ValueError(
                        "Link artifact to this collection before adding an alias"
                    )
                self.set_aliases(db, artifact["uid"], collection["id"], [alias])
        return {"success": True}

    def delete_aliases(self, _, info, input):
        artifact = self.get(input["artifactID"])
        for alias in input["aliases"]:
            collection = self.find_collection(
                self.p.project(alias["entityName"], alias["projectName"]),
                alias["artifactCollectionName"],
            )
            if collection:
                with self.store.connect(write=True) as db:
                    db.execute(
                        "DELETE FROM artifact_aliases WHERE collection=? AND alias=? AND artifact=?",
                        (collection["id"], alias["alias"], artifact["uid"]),
                    )
        return {"success": True}

    def link(self, _, info, input):
        token = writing.set(False)
        try:
            return self._link(_, info, input)
        finally:
            writing.reset(token)

    def _link(self, _, info, input):
        artifact = self.get(input.get("artifactID") or input["clientID"])
        source = self.collection(artifact["collection"])
        with self.store.connect(write=True) as db:
            collection = (
                self.collection(input["artifactPortfolioID"])
                if input.get("artifactPortfolioID")
                else self.ensure_collection(
                    db,
                    input.get("entityName") or source["entity"],
                    input.get("projectName") or source["project"],
                    input["artifactPortfolioName"],
                    source["type"],
                    True,
                )
            )
            self.store.authorize(collection["entity"], True)
            membership = db.execute(
                "SELECT * FROM artifact_memberships WHERE collection=? AND artifact=?",
                (collection["id"], artifact["uid"]),
            ).fetchone()
            index = (
                membership["version"]
                if membership
                else db.execute(
                    "SELECT COALESCE(MAX(version)+1,0) FROM artifact_memberships WHERE collection=?",
                    (collection["id"],),
                ).fetchone()[0]
            )
            if not membership:
                db.execute(
                    "INSERT INTO artifact_memberships VALUES (?,?,?,?)",
                    (uuid.uuid4().hex, collection["id"], artifact["uid"], index),
                )
            self.set_aliases(
                db, artifact["uid"], collection["id"], input.get("aliases") or []
            )
        return {
            "versionIndex": index,
            "artifactMembership": self.membership(collection["id"], f"v{index}"),
        }

    def unlink(self, _, info, input):
        self.get(input["artifactID"])
        collection = self.collection(input["artifactPortfolioID"])
        if not collection["portfolio"]:
            raise ValueError("Cannot unlink from the source collection")
        with self.store.connect(write=True) as db:
            db.execute(
                "DELETE FROM artifact_aliases WHERE collection=? AND artifact=?",
                (collection["id"], input["artifactID"]),
            )
            db.execute(
                "DELETE FROM artifact_memberships WHERE collection=? AND artifact=?",
                (collection["id"], input["artifactID"]),
            )
        return {"success": True}

    def linked_collections(self, uid):
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT collection FROM artifact_memberships WHERE artifact=?",
                    (uid,),
                )
            ]
        return [self.collection_result(self.collection(i)) for i in ids]

    def lineage(self, uid):
        self.get(uid)
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT run FROM artifact_usage WHERE artifact=?", (uid,)
                )
            ]
        return [self.p.run(row) for i in ids if (row := self.store.get(uid=i))]

    def run_artifacts(self, uid, inputs, first=None, after=None):
        self.store.assert_run(uid)
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT a.uid FROM artifacts a JOIN artifact_usage u ON a.uid=u.artifact WHERE u.run=? AND a.state='COMMITTED'"
                    if inputs
                    else "SELECT a.uid FROM artifacts a JOIN artifact_producers p ON a.uid=p.artifact WHERE p.run=? AND a.state='COMMITTED'",
                    (uid,),
                )
            ]
        return self.page([self.result(self.get(i)) for i in ids], first, after)

    def types(self, project, info, first=None, after=None):
        with self.store.connect() as db:
            names = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM artifact_types WHERE entity=? AND project=? ORDER BY name",
                    (project["entity"]["name"], project["name"]),
                )
            ]
        return self.page([self.type_result(project, n) for n in names], first, after)

    def collections(
        self, project, info, first=None, after=None, type=None, filters=None, order=None
    ):
        if decode(filters):
            raise ValueError("Artifact collection filters are not implemented")
        with self.store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM artifact_collections WHERE entity=? AND project=? ORDER BY created DESC",
                    (project["entity"]["name"], project["name"]),
                )
                if type is None or r["type"] == type
            ]
        return self.page([self.collection_result(r) for r in rows], first, after)

    def versions(
        self, collection, info, first=None, after=None, order=None, filters=None
    ):
        self.collection(collection["id"])
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT m.artifact FROM artifact_memberships m JOIN artifacts a ON m.artifact=a.uid WHERE m.collection=? AND a.state='COMMITTED' ORDER BY m.version DESC",
                    (collection["id"],),
                )
            ]
        rows = [self.result(self.get(i)) for i in ids]
        rows = [
            r
            for r in rows
            if matches(
                {
                    **r,
                    "metadata": decode(r["metadata"]),
                    "tags": [t["name"] for t in r["tags"]],
                },
                decode(filters),
            )
        ]
        if order:
            key = order.lstrip("+-")
            if key not in ("createdAt", "updatedAt", "versionIndex", "size", "id"):
                raise ValueError(f"Unsupported artifact order: {key}")
            rows.sort(key=lambda r: r[key], reverse=order.startswith("-"))
        return self.page(rows, first, after)

    def create_type(self, _, info, input):
        project = self.p.project(input["entityName"], input["projectName"])
        self.store.authorize(input["entityName"], True)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO artifact_types VALUES (?,?,?,?,?)",
                (
                    input["entityName"],
                    input["projectName"],
                    input["name"],
                    input.get("description"),
                    time.time(),
                ),
            )
        return {"artifactType": self.type_result(project, input["name"])}

    def update_collection(self, _, info, input):
        uid = input.get("artifactSequenceID") or input["artifactPortfolioID"]
        row = self.collection(uid)
        with self.store.connect(write=True) as db:
            for key in ("name", "description"):
                if input.get(key) is not None:
                    db.execute(
                        f"UPDATE artifact_collections SET {key}=? WHERE id=?",
                        (input[key], uid),
                    )
        return {
            "artifactPortfolio"
            if row["portfolio"]
            else "artifactSequence": self.collection_result(self.collection(uid))
        }
