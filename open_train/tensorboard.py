"""Import real TensorBoard event files without TensorFlow or a GPU."""

import hashlib
import math
import time
from pathlib import Path

import httpx
from tensorboard.backend.event_processing.event_file_loader import EventFileLoader
from tensorboard.compat.proto import event_pb2
from tensorboard.util import tensor_util


def event_records(directory):
    events = []
    skipped = 0
    for path in sorted(Path(directory).glob("*tfevents*")):
        if not path.is_file():
            continue
        for event in EventFileLoader(str(path)).Load():
            if (
                event.HasField("session_log")
                and event.session_log.status == event_pb2.SessionLog.START
            ):
                events.append(
                    {
                        "step": event.step,
                        "wall_time": event.wall_time,
                        "values": {},
                        "restart": True,
                    }
                )
            values = {}
            for value in event.summary.value:
                if value.HasField("simple_value"):
                    scalar = value.simple_value
                elif value.HasField("tensor"):
                    array = tensor_util.make_ndarray(value.tensor)
                    if array.size != 1 or array.dtype.kind not in "fiu":
                        skipped += 1
                        continue
                    scalar = float(array.reshape(-1)[0])
                else:
                    skipped += 1
                    continue
                values[value.tag] = scalar if math.isfinite(scalar) else None
            if values:
                events.append(
                    {
                        "step": event.step,
                        "wall_time": event.wall_time,
                        "values": values,
                        "restart": False,
                    }
                )
    # Stable chronology handles multiple event files from checkpoint restarts.
    events.sort(key=lambda e: (e["wall_time"], not e["restart"], e["step"]))
    return events, skipped


def import_once(
    logdir, project, base_url, entity="local", run_id=None, source=None, api_key=None
):
    root = Path(logdir).resolve()
    if not root.is_dir():
        raise ValueError(f"TensorBoard directory does not exist: {root}")
    directories = sorted({p.parent for p in root.rglob("*tfevents*") if p.is_file()})
    if not directories:
        raise ValueError(f"No TensorBoard event files found under {root}")
    if run_id and len(directories) != 1:
        raise ValueError(
            "--run-id requires exactly one event directory; point --logdir at that run"
        )
    results = []
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(
        base_url=base_url.rstrip("/"), headers=headers, timeout=60
    ) as client:
        for directory in directories:
            relative = directory.relative_to(root).as_posix()
            identity = f"{source or root.as_posix()}:{relative}"
            name = directory.name if relative == "." else relative
            identifier = (
                run_id or "tb-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
            )
            events, skipped = event_records(directory)
            added, uid = 0, None
            for start in range(0, max(1, len(events)), 1000):
                payload = {
                    "entity": entity,
                    "project": project,
                    "run_id": identifier,
                    "name": name,
                    "events": events[start : start + 1000],
                }
                for attempt in range(4):
                    try:
                        response = client.post("/api/import/tensorboard", json=payload)
                        response.raise_for_status()
                        break
                    except (httpx.TransportError, httpx.HTTPStatusError) as error:
                        if (
                            isinstance(error, httpx.HTTPStatusError)
                            and error.response.status_code < 500
                        ):
                            raise
                        if attempt == 3:
                            raise
                        time.sleep(2**attempt)
                result = response.json()
                added += result["events_added"]
                uid = result["uid"]
            results.append(
                {
                    "run_id": identifier,
                    "uid": uid,
                    "directory": str(directory),
                    "events_added": added,
                    "non_scalar_values_skipped": skipped,
                }
            )
    return results
