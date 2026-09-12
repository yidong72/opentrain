"""Snapshot SSH TensorBoard trees and import one run per cluster/root/tag.

Only event files are read from remote hosts. Credentials stay on the local machine.
Put --workspace under data/ so private manifests and telemetry stay gitignored.
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from open_train.tensorboard import event_records

REMOTE_SNAPSHOT = r"""
import json, pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
if not root.is_dir():
    raise SystemExit('Source root missing')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:
    for tag in sorted(root.iterdir()):
        tb = tag / 'tb'
        if not tb.is_dir():
            continue
        for path in sorted(tb.rglob('events.out.tfevents.*')):
            if not path.is_file() or path.is_symlink():
                continue
            with path.open('rb') as stream:
                info = archive.gettarinfo(fileobj=stream, arcname=str(path.relative_to(root)))
                archive.addfile(info, stream)
"""


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def snapshot(spec, workspace):
    host, root = spec.split("=", 1)
    if not root.startswith("/") or host.startswith("-"):
        raise ValueError("Expected --root SSH_ALIAS=/absolute/outputs")
    identity = hashlib.sha256(spec.encode()).hexdigest()[:12]
    snapshots = workspace / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    local = Path(tempfile.mkdtemp(prefix=host + "-" + identity + "-", dir=snapshots))
    command = "python3 -c " + shlex.quote(REMOTE_SNAPSHOT) + " " + shlex.quote(root)
    process = subprocess.Popen(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=15",
            host,
            command,
        ],
        stdout=subprocess.PIPE,
    )
    groups = {}
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|gz") as archive:
            for member in archive:
                relative = Path(member.name)
                if (
                    not member.isfile()
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or len(relative.parts) < 3
                    or relative.parts[1] != "tb"
                    or not relative.name.startswith("events.out.tfevents.")
                ):
                    raise ValueError("Unexpected archive member")
                target = local / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".partial")
                with (
                    archive.extractfile(member) as source,
                    temporary.open("wb") as output,
                ):
                    shutil.copyfileobj(source, output)
                temporary.replace(target)
                with target.open("rb") as saved:
                    digest = hashlib.file_digest(saved, "sha256").hexdigest()
                groups.setdefault(relative.parts[0], []).append(
                    {
                        "relative": member.name,
                        "bytes": member.size,
                        "mtime": member.mtime,
                        "sha256": digest,
                    }
                )
        if process.wait() != 0:
            raise RuntimeError("SSH snapshot failed: " + host)
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            process.wait()
    result = []
    tree = Path(root).parent.name
    for tag, files in groups.items():
        source = host + ":" + root.rstrip("/") + "/" + tag
        result.append(
            {
                "cluster": host,
                "root": root,
                "tree": tree,
                "tag": tag,
                "name": "/".join((host, tree, tag)),
                "source": source,
                "run_id": "tb-" + hashlib.sha256(source.encode()).hexdigest()[:16],
                "local": str(local),
                "files": files,
            }
        )
    print(
        json.dumps(
            {
                "snapshot": host + ":" + root,
                "runs": len(result),
                "files": sum(len(r["files"]) for r in result),
                "bytes": sum(f["bytes"] for r in result for f in r["files"]),
            }
        ),
        flush=True,
    )
    return result


def analyze(run, workspace):
    events, skipped, sessions = [], 0, []
    directories = sorted({str(Path(f["relative"]).parent) for f in run["files"]})
    for directory in directories:
        records, ignored = event_records(Path(run["local"]) / directory)
        steps = [e["step"] for e in records if e["values"]]
        sessions.append(
            {
                "directory": directory,
                "events": len(records),
                "first_step": min(steps) if steps else None,
                "last_step": max(steps) if steps else None,
                "first_wall_time": min((e["wall_time"] for e in records), default=None),
                "last_wall_time": max((e["wall_time"] for e in records), default=None),
            }
        )
        events.extend(records)
        skipped += ignored
    events.sort(key=lambda e: (e["wall_time"], not e["restart"], e["step"]))
    metrics = {}
    restarts = 0
    for event in events:
        step, timestamp = event["step"], event["wall_time"]
        if event["restart"]:
            restarts += 1
            metrics = {
                k: v
                for k, v in metrics.items()
                if not (k[1] >= step and v[0] <= timestamp)
            }
        for key, value in event["values"].items():
            # The current server does not index null/non-finite scalar values.
            if value is not None:
                metrics[key, step] = (timestamp, value)
    expected = {}
    for (key, step), (_, value) in sorted(metrics.items()):
        expected.setdefault(key, []).append([step, value])
    expected_keys = {
        key: {"count": len(points), "last_step": points[-1][0]}
        for key, points in expected.items()
    }
    sample_keys = []
    for prefix in ("train/", "eval/"):
        candidates = [k for k in expected if k.startswith(prefix)]
        if candidates:
            sample_keys.append(max(candidates, key=lambda k: len(expected[k])))
    if not sample_keys and expected:
        sample_keys.append(next(iter(expected)))
    stream_path = workspace / (run["run_id"] + ".jsonl.gz")
    with gzip.open(stream_path, "wt") as output:
        for event in events:
            output.write(json.dumps(event, separators=(",", ":")) + "\n")
    run.update(
        events=len(events),
        non_scalar_values_skipped=skipped,
        restart_markers=restarts,
        sessions=sessions,
        expected_keys=expected_keys,
        check_series={key: expected[key] for key in sample_keys},
        event_stream=str(stream_path),
        scalar_points=len(metrics),
    )
    print(
        json.dumps(
            {
                "analyzed": run["name"],
                "events": len(events),
                "scalar_points": len(metrics),
                "sessions": len(sessions),
                "restarts": restarts,
                "non_scalar_skipped": skipped,
            }
        ),
        flush=True,
    )
    return run


def request(client, method, path, **kwargs):
    for attempt in range(5):
        try:
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            if isinstance(error, httpx.HTTPStatusError):
                if (
                    error.response.status_code < 500
                    and error.response.status_code != 429
                ):
                    raise RuntimeError(
                        f"{path} returned HTTP {error.response.status_code}"
                    ) from None
            if attempt == 4:
                raise RuntimeError("Request failed: " + path) from None
            time.sleep(2**attempt)


def upload_events(client, run, uid, existing):
    """Retain original non-scalar data as checksum-verified downloadable files."""
    names = {}
    for file in run["files"]:
        name = (
            "tensorboard/"
            + Path(file["relative"]).relative_to(Path(run["tag"]) / "tb").as_posix()
        )
        names[name] = file
        if existing.get(name, {}).get("digest") == file["sha256"]:
            continue
        path = "/files/" + uid + "/" + quote(name, safe="/")
        for attempt in range(5):
            try:
                with (Path(run["local"]) / file["relative"]).open("rb") as source:
                    response = client.put(
                        path,
                        content=source,
                        headers={"Content-Length": str(file["bytes"])},
                    )
                response.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if isinstance(error, httpx.HTTPStatusError):
                    if (
                        error.response.status_code < 500
                        and error.response.status_code != 429
                    ):
                        raise RuntimeError(
                            f"Event upload returned HTTP {error.response.status_code}"
                        ) from None
                if attempt == 4:
                    raise RuntimeError(
                        "Event upload failed for " + run["name"]
                    ) from None
                time.sleep(2**attempt)
    detail = request(client, "GET", "/api/runs/" + uid)
    actual = {f["name"]: f for f in detail["files"]}
    for name, expected in names.items():
        saved = actual.get(name, {})
        if (
            saved.get("digest") != expected["sha256"]
            or saved.get("size") != expected["bytes"]
        ):
            raise RuntimeError("Event file checksum differs for " + run["name"])
    return len(names)


def import_run(run, args, key, entity):
    with httpx.Client(
        base_url=args.base_url,
        timeout=120,
        headers={"Authorization": "Bearer " + key, "User-Agent": "Mozilla/5.0"},
    ) as client:
        metadata = {
            k: run[k]
            for k in (
                "cluster",
                "root",
                "tree",
                "tag",
                "source",
                "files",
                "sessions",
                "non_scalar_values_skipped",
                "restart_markers",
            )
        }
        result = request(
            client,
            "POST",
            "/graphql",
            json={
                "query": "mutation($input: UpsertBucketInput!) { upsertBucket(input: $input) { bucket { id } } }",
                "variables": {
                    "input": {
                        "entityName": entity,
                        "modelName": args.project,
                        "name": run["run_id"],
                        "displayName": run["name"],
                        "groupName": run["cluster"],
                        "tags": ["tensorboard", run["cluster"], run["tree"]],
                        "config": json.dumps(
                            {"tensorboard_import": {"value": metadata}}
                        ),
                    }
                },
            },
        )
        if result.get("errors"):
            raise RuntimeError("Run metadata mutation failed for " + run["name"])
        uid = result["data"]["upsertBucket"]["bucket"]["id"]
        added = 0
        batch = []

        def send(events):
            payload = {
                "entity": entity,
                "project": args.project,
                "run_id": run["run_id"],
                "name": run["name"],
                "events": events,
            }
            return request(client, "POST", "/api/import/tensorboard", json=payload)[
                "events_added"
            ]

        with gzip.open(run["event_stream"], "rt") as source:
            for line in source:
                batch.append(json.loads(line))
                if len(batch) == 5000:
                    added += send(batch)
                    batch = []
        if batch or not run["events"]:
            added += send(batch)
        detail = request(client, "GET", "/api/runs/" + uid)
        actual = {
            k["key"]: {"count": k["count"], "last_step": k["last_step"]}
            for k in detail["keys"]
            if k["stream"] == "history" and not k["key"].startswith("_")
        }
        if actual != run["expected_keys"]:
            raise RuntimeError("Metric counts or last steps differ for " + run["name"])
        for metric, expected in run["check_series"].items():
            series = request(
                client,
                "GET",
                "/api/runs/" + uid + "/series",
                params={"key": metric, "limit": 10000},
            )
            if series["total"] != len(expected):
                raise RuntimeError("Series total differs for " + run["name"])
            if not series["sampled"] and series["points"] != expected:
                raise RuntimeError("Series values differ for " + run["name"])
        raw_files = 0
        if args.upload_events:
            raw_files = upload_events(
                client, run, uid, {f["name"]: f for f in detail["files"]}
            )
        receipt = {
            "run_id": run["run_id"],
            "uid": uid,
            "name": run["name"],
            "events_added": added,
            "scalar_points": run["scalar_points"],
            "metrics": len(run["expected_keys"]),
            "sessions": len(run["sessions"]),
            "verified": True,
            "raw_event_files_verified": raw_files,
            "project": args.project,
            "entity": entity,
            "verified_at": time.time(),
        }
        save(args.workspace / (run["run_id"] + ".receipt.json"), receipt)
        print(json.dumps(receipt), flush=True)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["snapshot", "analyze", "import"])
    parser.add_argument(
        "--root", action="append", default=[], help="SSH_ALIAS=/absolute/outputs"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--project", default="rpga")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--upload-events",
        action="store_true",
        help="Also retain original event files on each imported run",
    )
    args = parser.parse_args()
    args.workspace = args.workspace.resolve()
    args.workspace.mkdir(parents=True, exist_ok=True)
    inventory = args.workspace / "inventory.json"
    if args.phase == "snapshot":
        if not args.root:
            parser.error("snapshot needs --root")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            groups = pool.map(lambda spec: snapshot(spec, args.workspace), args.root)
            runs = [run for group in groups for run in group]
        save(inventory, runs)
        print(
            json.dumps(
                {
                    "snapshot_runs": len(runs),
                    "event_files": sum(len(r["files"]) for r in runs),
                }
            )
        )
    elif args.phase == "analyze":
        runs = json.loads(inventory.read_text())
        analyzed = [analyze(run, args.workspace) for run in runs]
        save(args.workspace / "analysis.json", analyzed)
    else:
        if not args.key_file:
            parser.error("import needs --key-file")
        key = args.key_file.read_text().strip()
        if "=" in key:
            key = key.split("=", 1)[1].strip().strip("\"'")
        with httpx.Client(
            base_url=args.base_url,
            timeout=30,
            headers={"Authorization": "Bearer " + key, "User-Agent": "Mozilla/5.0"},
        ) as client:
            entity = request(client, "GET", "/auth/me")["username"]
        runs = json.loads((args.workspace / "analysis.json").read_text())
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            receipts = list(
                pool.map(lambda run: import_run(run, args, key, entity), runs)
            )
        save(args.workspace / "receipts.json", receipts)
        print(
            json.dumps(
                {
                    "verified_runs": len(receipts),
                    "events_added": sum(r["events_added"] for r in receipts),
                    "scalar_points": sum(r["scalar_points"] for r in receipts),
                }
            )
        )


if __name__ == "__main__":
    main()
