"""Bounded, explicit JSON filters; unsupported operators fail closed."""

import operator


def matches(record, query):
    for key, expected in (query or {}).items():
        if key in ("$and", "$or", "$nor"):
            tests = [matches(record, part) for part in expected]
            if not (
                {"$and": all(tests), "$or": any(tests), "$nor": not any(tests)}[key]
            ):
                return False
            continue
        value = record
        for component in key.split("."):
            value = value.get(component) if isinstance(value, dict) else None
        for op, target in (
            expected if isinstance(expected, dict) else {"$eq": expected}
        ).items():
            if op == "$eq":
                ok = target in value if isinstance(value, list) else value == target
            elif op == "$ne":
                ok = target not in value if isinstance(value, list) else value != target
            elif op in ("$in", "$nin"):
                ok = (
                    any(v in target for v in value)
                    if isinstance(value, list)
                    else value in target
                )
                if op == "$nin":
                    ok = not ok
            elif op == "$exists":
                ok = (value is not None) == bool(target)
            elif op in ("$gt", "$gte", "$lt", "$lte"):
                try:
                    ok = {
                        "$gt": operator.gt,
                        "$gte": operator.ge,
                        "$lt": operator.lt,
                        "$lte": operator.le,
                    }[op](value, target)
                except TypeError:
                    ok = False
            else:
                raise ValueError(f"Unsupported filter operator: {op}")
            if not ok:
                return False
    return True
