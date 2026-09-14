from __future__ import annotations

import base64
import fnmatch
import hashlib
import logging
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from graphql import GraphQLError, build_schema, get_operation_ast, graphql_sync, parse

from .accounts import default_entity, principal, writing
from .store import clean, decode, dumps
from .sweeps import Sweeps

log = logging.getLogger(__name__)


def iso(timestamp):
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def page(items, first=None, after=None):
    start = int(base64.b64decode(after).decode()) if after else 0
    size = min(first or 50, 1000)
    if start < 0 or size < 1:
        raise ValueError("Invalid pagination")
    edges = [
        {"node": node, "cursor": base64.b64encode(str(i + 1).encode()).decode()}
        for i, node in enumerate(items[start : start + size], start)
    ]
    return {
        "edges": edges,
        "totalCount": len(items),
        "pageInfo": {
            "endCursor": edges[-1]["cursor"] if edges else None,
            "hasNextPage": start + size < len(items),
        },
    }


def sample(rows, count=None):
    if not count or len(rows) <= count:
        return rows
    if count < 0:
        raise ValueError("samples must be positive")
    if count == 1:
        return rows[-1:]
    return [rows[round(i * (len(rows) - 1) / (count - 1))] for i in range(count)]


class Protocol:
    def __init__(self, store):
        self.store = store
        self.sweeps = Sweeps(store)
        self.schema = build_schema(
            Path(__file__).with_name("schema.graphql").read_text()
        )
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    uid TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(uid),
                    digest TEXT NOT NULL, state TEXT NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manifests (
                    uid TEXT PRIMARY KEY, artifact TEXT NOT NULL REFERENCES artifacts(uid), name TEXT NOT NULL
                );
            """)
        self.bind("Query", "viewer", lambda *_: self.viewer())
        # SDKs/integrations query server metadata at either location. Keep the
        # response identical rather than adding a nullable compatibility stub.
        for typename in ("Query", "User"):
            self.bind(typename, "serverInfo", lambda *_: self.server_info())
        for field in ("project", "model"):
            self.bind(
                "Query",
                field,
                lambda _, info, name=None, entityName=None: self.project(
                    entityName, name
                ),
            )
        self.bind("Query", "entity", lambda _, info, name=None: self.entity(name))
        self.bind(
            "Query",
            "organization",
            lambda _, info, name: {"name": name, "featureFlags": []},
        )
        self.bind("Organization", "featureFlags", lambda *_args, **_kwargs: [])
        for field in ("bucket", "run"):
            self.bind(
                "Project",
                field,
                lambda p, info, name, **_: self.run(
                    self.store.get(p["entity"]["name"], p["name"], name)
                ),
            )
        self.bind("Project", "runs", self.runs)
        self.bind(
            "Project",
            "runCount",
            lambda p, info, filters=None: len(self.filtered_runs(p, filters)),
        )
        self.bind(
            "Project",
            "sweep",
            lambda p, info, sweepName: self.sweep(
                self.sweeps.get(p["entity"]["name"], p["name"], sweepName)
            ),
        )
        self.bind("Project", "artifactType", lambda *_args, **_kwargs: None)
        self.bind("Run", "history", self.history)
        self.bind(
            "Run",
            "events",
            lambda r, info, samples=None: [
                dumps(v) for v in sample(self.store.history(r["id"], "system"), samples)
            ],
        )
        self.bind("Run", "sampledHistory", self.sampled_history)
        self.bind(
            "Run",
            "parquetHistory",
            lambda r, info, **_: {"parquetUrls": [], "liveData": []},
        )
        for field, filename in (
            ("historyLineCount", "wandb-history.jsonl"),
            ("eventsLineCount", "wandb-events.jsonl"),
            ("logLineCount", "output.log"),
        ):
            self.bind(
                "Run",
                field,
                lambda r, info, f=filename: (
                    self.resume_status(r, info)["line_count"]
                    if f == "wandb-history.jsonl"
                    else self.store.count(r["id"], f)
                ),
            )
        self.bind(
            "Run",
            "historyTail",
            lambda r, info: self.resume_status(r, info)["tail"],
        )
        self.bind("Run", "summaryMetrics", self.resume_summary)
        self.bind(
            "Run",
            "eventsTail",
            lambda r, info: dumps(self.store.lines(r["id"], "wandb-events.jsonl", 1)),
        )
        self.bind("Run", "historyKeys", lambda r, info: self.history_keys(r["id"]))
        self.bind(
            "Run",
            "wandbConfig",
            lambda r, info, **_: dumps(
                {
                    "t": decode(r["config"])
                    .get("_wandb", {})
                    .get("value", {})
                    .get("t", {})
                }
            ),
        )
        self.bind("Run", "files", self.run_files)
        self.bind("Run", "fileCount", lambda r, info: len(self.store.files(r["id"])))
        self.bind("Mutation", "upsertBucket", self.upsert)
        self.bind("Mutation", "deleteRun", self.delete_run)
        self.bind(
            "Mutation",
            "upsertModel",
            lambda _, info, input: {
                "project": self.project(input["entityName"], input["name"]),
                "model": self.project(input["entityName"], input["name"]),
            },
        )
        self.bind("Mutation", "createRunFiles", self.create_files)
        self.bind(
            "Mutation",
            "upsertSweep",
            lambda _, info, input: {
                "sweep": self.sweep(self.sweeps.upsert(input)),
                "configValidationWarnings": [],
            },
        )
        self.bind(
            "Mutation",
            "createAgent",
            lambda _, info, input: self.sweeps.register(input),
        )
        self.bind(
            "Mutation",
            "agentHeartbeat",
            lambda _, info, input: self.sweeps.heartbeat(input),
        )
        from .artifacts import ArtifactProtocol

        self.artifacts = ArtifactProtocol(self)
        self.bind("User", "admin", lambda r, info: bool(r.get("admin", False)))
        self.bind("User", "apiKeys", self.user_keys)

    @staticmethod
    def server_info():
        return {
            "cliVersionInfo": {
                "min_cli_version": "0.18.0",
                "max_cli_version": "0.30.0",
            },
            "latestLocalVersionInfo": {
                "outOfDate": False,
                "latestVersionString": "0.40.0",
                "versionOnThisInstanceString": "0.40.0",
            },
            "features": [{"name": "TOTAL_COUNT_IN_FILE_CONNECTION", "isEnabled": True}],
        }

    def user_keys(self, user, info):
        current = principal.get() or {}
        if current.get("id") != user["id"]:
            return {"edges": []}
        with self.store.connect() as db:
            keys = [
                dict(r)
                for r in db.execute(
                    "SELECT id,name FROM credentials WHERE user=? AND kind='api_key' AND revoked IS NULL",
                    (user["id"],),
                )
            ]
        return {"edges": [{"node": k} for k in keys]}

    def bind(self, typename, field, resolver):
        self.schema.type_map[typename].fields[field].resolve = resolver

    def execute(self, payload, file_url):
        try:
            operation = get_operation_ast(
                parse(payload.get("query", "")), payload.get("operationName") or None
            )
        except GraphQLError as error:
            return {"data": None, "errors": [error.formatted]}
        access_token = writing.set(
            bool(operation and operation.operation.value == "mutation")
        )
        try:
            return self._execute(payload, file_url)
        finally:
            writing.reset(access_token)

    def _execute(self, payload, file_url):
        result = graphql_sync(
            self.schema,
            payload.get("query", ""),
            variable_values=payload.get("variables"),
            operation_name=payload.get("operationName") or None,
            context_value={"file_url": file_url},
        )
        response = {"data": clean(result.data)}
        if result.errors:
            response["errors"] = [e.formatted for e in result.errors]
            # Avoid logging variables/configs, which can contain training secrets.
            log.warning("GraphQL errors: %s", [e.message for e in result.errors])
        return response

    def viewer(self):
        current = principal.get()
        if current and current.get("username"):
            entities = self.store.accounts.entities() if self.store.accounts else []
            return {
                "id": current["id"],
                "entity": current["username"],
                "username": current["username"],
                "name": current["name"],
                "email": current["email"],
                "flags": "{}",
                "teams": {
                    "edges": [
                        {
                            "node": {
                                "id": e,
                                "name": e,
                                "isTeam": e != current["username"],
                            }
                        }
                        for e in (entities or [current["username"]])
                    ]
                },
            }
        return {
            "id": "local",
            "entity": "local",
            "username": "local",
            "name": "Local user",
            "email": "local@localhost",
            "flags": "{}",
            "teams": {"edges": [{"node": self.entity("local")}]},
        }

    def entity(self, name=None):
        name = name or default_entity()
        self.store.authorize(name)
        return {
            "id": name or "local",
            "name": name or "local",
            "isTeam": False,
            "organization": None,
        }

    def project(self, entity=None, name=None):
        entity, name = entity or default_entity(), name or "uncategorized"
        self.store.authorize(entity)
        internal_id = str(
            int(hashlib.sha256(f"{entity}/{name}".encode()).hexdigest()[:12], 16)
        )
        return {
            "id": f"{entity}/{name}",
            "internalId": internal_id,
            "name": name,
            "entity": self.entity(entity),
            "readOnly": False,
        }

    def run(self, row):
        if not row:
            return None
        state = row["state"]
        if state == "running" and time.time() - row["updated"] > 300:
            state = "crashed"
        return {
            "id": row["uid"],
            "name": row["name"],
            "displayName": row["display_name"],
            "project": self.project(row["entity"], row["project"]),
            "projectId": self.project(row["entity"], row["project"])["internalId"],
            "config": row["config"],
            "summaryMetrics": row["summary"],
            "systemMetrics": "{}",
            "tags": decode(row["tags"]),
            "group": row["group_name"],
            "jobType": row["job_type"],
            "notes": row["notes"],
            "description": row["notes"],
            "sweepName": row["sweep"],
            "state": state,
            "commit": None,
            "readOnly": False,
            "user": self.store.accounts.run_user(row["uid"])
            if self.store.accounts
            else self.viewer(),
            "createdAt": iso(row["created"]),
            "heartbeatAt": iso(row["updated"]),
            "updatedAt": iso(row["updated"]),
            "stopped": False,
            "shouldStop": False,
            "failed": state == "failed",
            "running": state == "running",
            "exitcode": row["exitcode"],
        }

    def upsert(self, _, info, input):
        run, inserted = self.store.upsert(input)
        return {"bucket": self.run(run), "inserted": inserted}

    def resume_status(self, run, info):
        cache = info.context.setdefault("resume_status", {})
        if run["id"] not in cache:
            cache[run["id"]] = self.store.sessions.resume(run["id"])
            status = cache[run["id"]]
            log.info(
                "RunResumeStatus run=%s historyLineCount=%s last_step=%s",
                run["id"],
                status["line_count"],
                status["last_step"],
            )
        return cache[run["id"]]

    def resume_summary(self, run, info):
        summary = run["summaryMetrics"]
        if info.operation.name and "resume" in info.operation.name.value.lower():
            last = self.resume_status(run, info)["last_step"]
            if last is not None:
                summary = dumps({**decode(summary), "_step": last})
        return summary

    def filtered_runs(self, project, filters=None):
        rows = self.store.list_runs(
            project["entity"]["name"], project["name"], limit=1_000_000
        )
        filters = decode(filters)
        for key, expected in filters.items():
            mapping = {
                "name": "name",
                "displayName": "display_name",
                "state": "state",
                "group": "group_name",
                "sweep": "sweep",
            }
            if key not in mapping and not key.startswith("config."):
                raise ValueError(f"Unsupported run filter: {key}")
            if isinstance(expected, dict):
                raise ValueError("Only equality filters are implemented")

            def value(row, key=key, mapping=mapping):
                if key.startswith("config."):
                    return decode(row["config"]).get(key[7:], {}).get("value")
                return row[mapping[key]]

            rows = [row for row in rows if value(row) == expected]
        return rows

    def runs(self, p, info, first=None, after=None, order=None, filters=None):
        rows = self.filtered_runs(p, filters)
        if order:
            mapping = {
                "created_at": "created",
                "createdAt": "created",
                "name": "name",
                "displayName": "display_name",
                "updated_at": "updated",
            }
            key = order.lstrip("+-")
            if key not in mapping:
                raise ValueError(f"Unsupported run ordering: {order}")
            rows.sort(key=lambda r: r[mapping[key]], reverse=order.startswith("-"))
        return page([self.run(r) for r in rows], first, after)

    def delete_run(self, _, info, input):
        self.store.delete_run(input["id"], input.get("deleteArtifacts", False))
        return {"clientMutationId": input.get("clientMutationId")}

    def history(self, r, info, samples=None, minStep=None, maxStep=None):
        return [
            dumps(v)
            for v in sample(
                self.store.history(r["id"], minimum=minStep, maximum=maxStep), samples
            )
        ]

    def sampled_history(self, r, info, specs):
        result = []
        for raw in specs:
            spec = decode(raw)
            rows = self.store.history(
                r["id"], minimum=spec.get("minStep"), maximum=spec.get("maxStep")
            )
            keys = spec.get("keys")
            if keys:
                rows = [
                    {k: row[k] for k in keys}
                    for row in rows
                    if all(k in row for k in keys)
                ]
            result.append(sample(rows, spec.get("samples")))
        return result

    def history_keys(self, uid):
        keys = [k for k in self.store.keys(uid) if k["stream"] == "history"]
        return {
            "lastStep": int(max((k["last_step"] for k in keys), default=-1)),
            "keys": {
                k["key"]: {"typeCounts": [{"type": "number", "count": k["count"]}]}
                for k in keys
            },
        }

    def file(self, uid, name, info, row=None):
        row = row or {}
        url = info.context["file_url"](uid, name, "GET")
        return {
            "id": f"{uid}/{name}",
            "name": name,
            "url": url,
            "displayName": name,
            "digest": row.get("md5", ""),
            "directUrl": url,
            "uploadUrl": info.context["file_url"](uid, name, "PUT"),
            "uploadHeaders": [],
            "sizeBytes": row.get("size", 0),
            "mimetype": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "updatedAt": iso(row.get("updated", time.time())),
            "md5": row.get("md5", ""),
        }

    def create_files(self, _, info, input):
        row = self.store.get(
            input["entityName"], input["projectName"], input["runName"]
        )
        if not row:
            raise ValueError("Run not found")
        return {
            "runID": row["uid"],
            "uploadHeaders": [],
            "files": [self.file(row["uid"], f, info) for f in input["files"]],
        }

    def run_files(self, r, info, names=None, first=None, after=None, pattern=None):
        files = self.store.files(r["id"])
        if names:
            files = [f for f in files if f["name"] in names]
        if pattern:
            files = [
                f
                for f in files
                if fnmatch.fnmatchcase(
                    f["name"], pattern.replace("%", "*").replace("_", "?")
                )
            ]
        return page(
            [self.file(r["id"], f["name"], info, f) for f in files], first, after
        )

    def sweep(self, row):
        if not row:
            return None
        runs = [
            self.run(r)
            for r in self.store.list_runs(
                row["entity"], row["project"], limit=1_000_000
            )
            if r["sweep"] == row["name"]
        ]
        return {
            "id": row["uid"],
            "name": row["name"],
            "project": self.project(row["entity"], row["project"]),
            "config": row["config"],
            "method": yaml.safe_load(row["config"]).get("method", "grid"),
            "state": row["state"],
            "createdAt": iso(row["created"]),
            "heartbeatAt": iso(row["created"]),
            "updatedAt": iso(row["created"]),
            "earlyStopJobRunning": False,
            "controller": "{}",
            "scheduler": "{}",
            "runs": page(runs, 1000),
        }
