"""Export only readable history records from a stopped, damaged SDK journal.

Validates W&B journal framing and uses the installed SDK protobuf schema.
Never modifies the source, repairs bytes,
marks a run synced, executes log text, or uploads automatically.
"""

import hashlib
import json
import math
import os
import struct
import zlib
from pathlib import Path


class JournalReader:
    """Read-only W&B v0 journal framing (32 KiB blocks, CRC32 records).

    The SDK removed its Python DataStore parser in 0.30. Keeping framing here
    avoids depending on that private API; protobuf decoding remains SDK-owned.
    Reject partial frames and fragment chains instead of silently accepting EOF.
    """

    def __init__(self, path):
        self._fp = path.open("rb")

    def _read_header(self):
        if self._fp.read(7) != struct.pack("<4sHB", b":W&B", 0xBEE1, 0):
            raise ValueError("Unsupported journal header")

    def get_offset(self):
        return self._fp.tell()

    def scan_data(self):
        fragments = None
        while True:
            space = 32768 - self.get_offset() % 32768
            if space < 7:
                padding = self._fp.read(space)
                if not padding and fragments is None:
                    return None
                if padding != b"\0" * space:
                    raise ValueError("Incomplete block padding")
                space = 32768
            header = self._fp.read(7)
            if not header and fragments is None:
                return None
            if len(header) != 7:
                raise ValueError("Incomplete record header or fragment chain")
            checksum, size, kind = struct.unpack("<IHB", header)
            if kind not in (1, 2, 3, 4) or size > space - 7:
                raise ValueError("Invalid record framing")
            data = self._fp.read(size)
            if (
                len(data) != size
                or zlib.crc32(bytes([kind]) + data) & 0xFFFFFFFF != checksum
            ):
                raise ValueError("Invalid record checksum or incomplete payload")
            if kind == 1:
                if fragments is not None:
                    raise ValueError("Unfinished fragment chain")
                return data
            if kind == 2:
                if fragments is not None:
                    raise ValueError("Nested fragment chain")
                fragments = bytearray(data)
            else:
                if fragments is None:
                    raise ValueError("Missing first fragment")
                fragments.extend(data)
            if len(fragments) > 64 * 1024 * 1024:
                raise ValueError("Record exceeds recovery limit")
            if kind == 4:
                return bytes(fragments)


def recover(source, output):
    from wandb.proto import wandb_internal_pb2

    from .store import clean, dumps

    source, output = Path(source).resolve(), Path(output).absolute()
    if not source.is_file() or source == output:
        raise ValueError(
            "Use an existing .wandb source and a different, new output file"
        )
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; recovery never overwrites files")
    initial = source.stat()
    parser = JournalReader(source)
    rows, records, skipped, exit_seen, valid_end, error = 0, 0, 0, False, 0, None
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser._fp.close()
        raise
    try:
        with os.fdopen(fd, "w") as stream:
            try:
                parser._read_header()
                valid_end = parser.get_offset()
                while True:
                    data = parser.scan_data()
                    if data is None:
                        # scan_data can silently hit EOF midway through a
                        # fragmented record. A finish record is also required.
                        if parser.get_offset() > valid_end:
                            error = "incomplete_tail_or_padding"
                        break
                    record = wandb_internal_pb2.Record()
                    record.ParseFromString(data)
                    records += 1
                    valid_end = parser.get_offset()
                    if record.HasField("exit"):
                        exit_seen = True
                    if not record.HasField("history"):
                        continue
                    row = {}
                    for item in record.history.item:
                        value = json.loads(item.value_json)
                        path = list(item.nested_key) or [item.key]
                        target = row
                        for part in path[:-1]:
                            target = target.setdefault(part, {})
                        target[path[-1]] = value
                    if "_step" not in row and record.history.HasField("step"):
                        row["_step"] = record.history.step.num
                    step = row.get("_step")
                    if (
                        isinstance(step, bool)
                        or not isinstance(step, (int, float))
                        or not math.isfinite(step)
                    ):
                        skipped += 1
                        continue
                    stream.write(dumps(clean(row)) + "\n")
                    rows += 1
            except Exception:
                # Parser diagnostics can contain journal contents. Return a
                # bounded classification, never the raw message or data.
                error = "truncated_corrupt_or_unsupported_record"
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        parser._fp.close()
    final = source.stat()
    changed = (initial.st_size, initial.st_mtime_ns) != (
        final.st_size,
        final.st_mtime_ns,
    )
    with output.open("rb") as saved:
        sha256 = hashlib.file_digest(saved, "sha256").hexdigest()
    with source.open("rb") as original:
        source_sha256 = hashlib.file_digest(original, "sha256").hexdigest()
    return {
        "output": str(output),
        "sha256": sha256,
        "source_sha256": source_sha256,
        "history_rows": rows,
        "readable_records": records,
        "skipped_history_without_step": skipped,
        "valid_prefix_bytes": valid_end,
        "source_bytes": initial.st_size,
        "exit_record_seen": exit_seen,
        "source_changed": changed,
        "status": "partial"
        if error or not exit_seen or skipped or changed
        else "complete",
        "error": error,
        "source_unchanged": not changed,
        "note": "History-only export, not a repaired .wandb file or a successful sync. No missing bytes, media, artifacts, config or summary records are reconstructed. Use a separate import batch with this recovery status and original-source checksum.",
    }
