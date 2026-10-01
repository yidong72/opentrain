"""Read-only checkpoint trajectories. Never modify ingestion or resume cursors."""

import math
from collections import defaultdict


def context(store, uid, key, declared=None):
    parts = key.split("/")[:-1]
    local_axes = [
        "/".join(parts[:i] + ["global_step"]) for i in range(len(parts), 0, -1)
    ]
    custom = (
        [declared]
        if declared
        and not declared.startswith("_")
        and not declared.endswith("global_step")
        else []
    )
    axes = list(
        dict.fromkeys([*custom, "train/global_step", "global_step", *local_axes])
    )
    marker = "_open_train/resume_step"
    names = axes + [marker]
    placeholders = ",".join("?" for _ in names)
    with store.connect() as db:
        records = db.execute(
            "SELECT r.id,r.session,r.source,r.timestamp,r.step,v.key,v.value,"
            "EXISTS(SELECT 1 FROM record_values m WHERE m.record=r.id AND m.value IS NOT NULL "
            "AND m.key!='global_step' AND substr(m.key,-12)!='/global_step' "
            f"AND substr(m.key,1,1)!='_' AND m.key NOT IN ({placeholders})) has_metric, "
            "EXISTS(SELECT 1 FROM record_values m WHERE m.record=r.id AND m.value IS NOT NULL "
            "AND substr(m.key,1,6)='train/' AND substr(m.key,-12)!='/global_step') has_train_metric "
            "FROM history_records r LEFT JOIN record_values v ON v.record=r.id "
            f"AND v.key IN ({placeholders}) WHERE r.run=? AND r.stream='history' "
            "AND r.active=1 AND r.superseded=0 ORDER BY r.timestamp,r.id",
            [*names, *names, uid],
        ).fetchall()
        starts = dict(
            db.execute("SELECT id,started FROM sdk_sessions WHERE run=?", (uid,))
        )
    by_id = {}
    for row in records:
        item = by_id.setdefault(row["id"], {**dict(row), "values": {}})
        if row["key"] and row["value"] is not None:
            item["values"][row["key"]] = row["value"]
    available = {k for r in by_id.values() for k in r["values"]}
    axis = next((k for k in axes if k in available), "_step")
    groups = defaultdict(list)
    coordinates = {}
    for rid, row in by_id.items():
        groups[row["session"]].append(row)
        # Prefer the coordinate paired with this metric's own namespace.
        coordinates[rid] = (
            row["step"]
            if axis == "_step"
            else next(
                (
                    row["values"][k]
                    for k in dict.fromkeys([*custom, *local_axes, axis, *axes])
                    if k in row["values"]
                ),
                None,
            )
        )
    ordered = sorted(
        groups, key=lambda sid: (starts.get(sid, groups[sid][0]["timestamp"]), sid)
    )
    boundaries = []
    for sid in ordered:
        rows = groups[sid]
        explicit = next(
            (r["values"][marker] for r in rows if marker in r["values"]), None
        )
        # Ignore setup-only global_step=0 rows. Prefer actual training records
        # over sparse evals, so eval frequency does not move the resume boundary.
        observed = next(
            (
                r["values"][axis]
                for r in rows
                if r[
                    "has_train_metric" if axis == "train/global_step" else "has_metric"
                ]
                and axis in r["values"]
            ),
            None,
        )
        if observed is None:
            observed = next(
                (
                    coordinates[r["id"]]
                    for r in rows
                    if r["has_metric"]
                    and coordinates[r["id"]] is not None
                    and (
                        axis != "train/global_step"
                        or r["has_train_metric"]
                        or axis not in r["values"]
                    )
                ),
                None,
            )
        boundaries.append(
            {
                "id": sid,
                "step": explicit if explicit is not None else observed,
                "axis": axis,
                "source": "checkpoint_metadata"
                if explicit is not None
                else "inferred_first_observation",
            }
        )
    warning = None
    if any(r["source"] == "sdk_shared" for r in by_id.values()):
        warning = "Shared distributed writers are not sequential resumes; no trajectory trimming was applied."
    elif axis == "_step" and any(
        marker in r["values"] and r["source"] == "sdk" for r in by_id.values()
    ):
        warning = "Checkpoint step supplied without a paired training counter; no trajectory trimming was applied."
    elif (
        any(
            not sid or sid in ("sdk", "legacy-sdk", "legacy-default") for sid in ordered
        )
        and len(ordered) > 1
    ):
        warning = "Some records have unknown session provenance; no trajectory trimming was applied."
    elif any(
        groups[a][-1]["timestamp"] >= groups[b][0]["timestamp"]
        for a, b in zip(ordered, ordered[1:], strict=False)
    ):
        warning = "Session time ranges overlap; resume order is ambiguous, so no trajectory trimming was applied."
    cutoffs = {}
    cutoff = math.inf
    for boundary in reversed(boundaries):
        cutoffs[boundary["id"]] = cutoff
        if boundary["step"] is not None:
            cutoff = min(cutoff, boundary["step"])
    return {
        "coordinates": coordinates,
        "cutoffs": cutoffs,
        "boundaries": boundaries,
        "warning": warning,
        "coordinate_axis": axis,
    }


def select(rows, info, view):
    kept = rows
    if view == "latest" and not info["warning"]:
        kept = [
            r
            for r in rows
            if info["coordinates"].get(r["id"]) is None
            or info["coordinates"][r["id"]]
            < info["cutoffs"].get(r["session"], math.inf)
        ]
    warnings = [info["warning"]] if info["warning"] else []
    if view == "latest" and len(info["boundaries"]) > 1 and not info["warning"]:
        if info["coordinate_axis"] == "_step":
            warnings.append(
                "No training counter available; boundaries use logging/event steps."
            )
        if any(b["source"].startswith("inferred") for b in info["boundaries"][1:]):
            warnings.append(
                "Resume boundaries inferred from first observed steps, not verified checkpoints."
            )
        if any(b["step"] is None for b in info["boundaries"][1:]):
            warnings.append(
                "Some resume boundaries are unknown; earlier tails may remain."
            )
        if any(info["coordinates"].get(r["id"]) is None for r in rows):
            warnings.append(
                "Records without a paired training coordinate were retained."
            )
    safe_resume = (
        not info["warning"]
        and all(b["step"] is not None for b in info["boundaries"][1:])
        and all(info["coordinates"].get(r["id"]) is not None for r in rows)
    )
    return kept, {
        "view": view,
        "hidden_points": len(rows) - len(kept),
        "boundaries": info["boundaries"],
        "coordinate_axis": info["coordinate_axis"],
        # The UI may join the retained trajectory, but never ambiguous writers
        # or sessions whose training coordinates could not be verified.
        "stitch_sessions": view == "latest" and safe_resume,
        # All-session curves can inherit EMA from the retained prefix before
        # their checkpoint without merging/deleting any overlapping branches.
        "resume_smoothing": safe_resume,
        "warning": " ".join(warnings) or None,
    }


def sample(rows, limit):
    """Allocate a bounded budget per session, never sample across its boundary."""
    if len(rows) <= limit:
        return rows, 0
    groups = defaultdict(list)
    for row in rows:
        groups[row["session"]].append(row)
    omitted = max(0, len(groups) - limit)
    if omitted:
        groups = dict(
            sorted(groups.items(), key=lambda p: max(r["timestamp"] for r in p[1]))[
                -limit:
            ]
        )
    budgets = {sid: 1 for sid in groups}
    remaining = limit - len(groups)
    # Give small sessions endpoints too; distribute remaining points by size.
    while remaining:
        candidates = [sid for sid in groups if budgets[sid] < len(groups[sid])]
        if not candidates:
            break
        for sid in sorted(
            candidates, key=lambda s: (budgets[s] > 1, budgets[s] / len(groups[s]))
        ):
            if not remaining:
                break
            budgets[sid] += 1
            remaining -= 1
    result = []
    for sid, points in groups.items():
        budget = budgets[sid]
        if len(points) <= budget:
            result.extend(points)
        elif budget == 1:
            result.append(points[-1])
        else:
            result.append(points[0])
            slots = budget - 2
            if slots:
                width = math.ceil((len(points) - 2) / max(1, slots // 2))
                for start in range(1, len(points) - 1, width):
                    bucket = points[start : min(start + width, len(points) - 1)]
                    valid = [r for r in bucket if r["value"] is not None]
                    chosen = {
                        r["id"]: r
                        for r in (
                            [
                                min(valid, key=lambda r: r["value"]),
                                max(valid, key=lambda r: r["value"]),
                            ]
                            if valid
                            else bucket[:1]
                        )
                    }
                    selected = sorted(chosen.values(), key=lambda r: (r["x"], r["id"]))[
                        :slots
                    ]
                    result.extend(selected)
                    slots -= len(selected)
                    if not slots:
                        break
            result.append(points[-1])
    return sorted(result, key=lambda r: (r["x"], r["id"])), omitted
