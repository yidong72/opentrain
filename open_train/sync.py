"""Supervise the official SDK's offline uploader; never replace its log format.

The spool and SDK cache must live on persistent storage. Only offline runs are
selected: replaying an online run while its writer is alive is unsafe.
"""

from __future__ import annotations

import fcntl
import importlib.util
import os
import random
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def pending_runs(spool: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for path in sorted(spool.rglob("run-*.wandb")):
        if not path.parent.name.startswith("offline-run-"):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(spool.resolve()):
            continue
        if path.is_file() and not Path(str(path) + ".synced").exists():
            # Conservatively serialize identical IDs, even across projects.
            groups.setdefault(path.stem, []).append(path)
    return groups


@contextmanager
def spool_lock(spool: Path):
    descriptor = os.open(spool / ".open-train-sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "Another sync watcher already owns this spool"
            ) from error
        yield
    finally:
        os.close(descriptor)


@dataclass
class Upload:
    process: subprocess.Popen
    paths: list[Path]


def stop_upload(upload: Upload):
    # SDK starts a background core. Kill the process group, not just the CLI.
    try:
        os.killpg(upload.process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        upload.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(upload.process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    upload.process.wait()


def watch(spool: str, *, interval: float = 5, workers: int = 4, once: bool = False):
    """Return nonzero if a one-shot recovery leaves unsynced files.

    Live SDK sync waits for producer exit records, so uncleanly killed producers
    must be recovered using --once after their writers have stopped. No local data
    is removed, even after successful upload.
    """
    if importlib.util.find_spec("wandb") is None:
        raise RuntimeError(
            "Install the uploader from the Open Train checkout: pip install '.[client]'"
        )
    if not os.environ.get("WANDB_BASE_URL") or not os.environ.get("WANDB_API_KEY"):
        raise RuntimeError("Set WANDB_BASE_URL and your personal WANDB_API_KEY")
    root = Path(spool).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("The spool directory must already exist")
    env = {k: v for k, v in os.environ.items() if k != "WANDB_SERVICE"}
    # The training process is offline; its independent uploader must be online.
    env.update(WANDB_MODE="online", WANDB_SILENT="false", WANDB_CONSOLE="off")
    active: dict[str, Upload] = {}
    retry: dict[str, tuple[int, float]] = {}
    attempted: set[str] = set()
    failed = False
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    with spool_lock(root):
        previous = {
            sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            while not stopping:
                for key, upload in list(active.items()):
                    if upload.process.poll() is None:
                        continue
                    # The beta CLI can exit zero with failed runs. Require SDK
                    # receipts, not exit code alone, before considering delivery.
                    delivered = all(
                        Path(str(p) + ".synced").is_file() for p in upload.paths
                    )
                    stop_upload(upload)
                    del active[key]
                    if delivered:
                        retry.pop(key, None)
                        print(f"Uploaded {key}; local cache retained", flush=True)
                    else:
                        failed = True
                        attempts = retry.get(key, (0, 0))[0] + 1
                        delay = min(300, interval * 2 ** min(attempts, 10))
                        retry[key] = (
                            attempts,
                            time.monotonic() + delay * random.uniform(0.8, 1.2),
                        )
                        print(
                            f"Upload incomplete for {key}; cache retained, retry in ~{delay:g}s",
                            flush=True,
                        )
                for key, paths in pending_runs(root).items():
                    if len(active) >= workers:
                        break
                    if key in active or (once and key in attempted):
                        continue
                    if time.monotonic() < retry.get(key, (0, 0))[1]:
                        continue
                    command = [
                        sys.executable,
                        "-m",
                        "wandb",
                        "beta",
                        "sync",
                        "--yes",
                        "-n",
                        "1",
                    ]
                    if not once:
                        command.append("--live")
                    command.extend(str(path) for path in paths)
                    # SDK diagnostics may contain signed URLs. Keep them in a
                    # private local log, never echo credentials to the terminal.
                    log_path = root / ".open-train-sync.log"
                    fd = os.open(
                        log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
                    )
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "ab") as output:
                        process = subprocess.Popen(
                            command,
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=output,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                    active[key] = Upload(process, paths)
                    attempted.add(key)
                    print(f"Uploading {key} ({len(paths)} fragment(s))", flush=True)
                if once and not active:
                    return int(failed or bool(pending_runs(root)))
                time.sleep(min(interval, 1))
        finally:
            for upload in active.values():
                stop_upload(upload)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return 130
