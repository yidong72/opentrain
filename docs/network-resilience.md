# Training with an unreliable connection

Open Train uses the **official W&B SDK's on-disk transaction log**. It does not
replace `wandb`, buffer training data in the browser, or forward to W&B Cloud.

## Existing online training

Keep `WANDB_BASE_URL` pointed at your Open Train server and use your personal API
key. The SDK writes locally and retries uploads in the background during an
interruption. Training can keep calling `wandb.log`. Open Train accepts repeated
identical history offsets without duplicating rows, including when the server
accepted a request but the acknowledgment was lost.

Online `wandb.init()` still needs a working connection; it can time out during
startup, and `run.finish()` can wait for pending uploads. For jobs that must start
and finish without network access, use offline-first mode below.

## Offline-first training with automatic live upload

Install the companion CLI **on the training machine**, in an environment with the
official SDK (tested 0.29.0 and 0.30.0):

```bash
pip install '.[client]'  # from an Open Train source checkout

# Use a persistent disk, not /tmp or an ephemeral container filesystem.
umask 077
export WANDB_DIR=/persistent/opentrain/runs
export WANDB_CACHE_DIR=/persistent/opentrain/cache
export WANDB_DATA_DIR=/persistent/opentrain/staging
mkdir -p "$WANDB_DIR" "$WANDB_CACHE_DIR" "$WANDB_DATA_DIR"

export WANDB_BASE_URL=https://opentrain.yihome.org
export WANDB_ENTITY=your-opentrain-username
# Set WANDB_API_KEY using your secret manager or a protected environment file.
# It is your personal Open Train key, not a server-wide key.
export WANDB_MODE=offline

# Terminal/service 1: leave this running independently of the training job.
open-train sync-watch "$WANDB_DIR"

# Terminal/job 2: same storage environment; existing W&B instrumentation.
python train.py
```

`sync-watch` discovers offline run files, uses `wandb beta sync --live`, and
uploads while the job runs. Network retries occur inside the SDK. If an uploader
exits without its SDK delivery receipts, the watcher retries with capped
exponential backoff and jitter. It checks receipts because the beta CLI can exit
zero even when some runs failed. Finished files are skipped after restarting the
watcher. It never deletes run files, media, artifacts, or SDK caches.

Live upload can only read records the SDK has flushed to disk. Small offline
runs can remain buffered until a log block fills or the run finishes, so offline
live mode does not guarantee a fixed chart-update latency or per-call fsync.

The watcher runs up to four run uploads concurrently (`--workers`). Fragments
sharing a run ID are serialized; independent runs can upload concurrently. One
live upload holds a slot until that run finishes; set `--workers` at least as high
as the number of simultaneous live runs if each needs live updates. One
watcher owns a spool via an OS lock. Do not start overlapping watchers on a parent
and child directory, or run a manual uploader on files that a watcher owns.
Online run directories are deliberately excluded to avoid racing their SDK
writers. A read-only API key cannot upload; use an owner/writer key. Expired or
revoked credentials require a new key and watcher restart.

Run the watcher under your job/service manager if it must survive logout or
reboots. The training process does not need the watcher to be running in order
to cache data. All previously queued files are discovered when it starts again.
Keep the watcher under the same OS user and with the same filesystem paths as
training: saved files may be symlinks, and artifact records may refer to staging
paths. `.open-train-sync.log` contains local SDK diagnostics (mode 0600); treat it
as sensitive and monitor its size. This release does not rotate that log.

## Checkpoints and multiple writers

Offline mode cannot query remote resume state; `resume="must"` is not an offline
resume guarantee. Save the run ID and global training step in the checkpoint,
use a **different local directory for each attempt**, and keep steps explicit.
For example, log `global_step` from the restored checkpoint. When sequential
offline fragments reuse an ID, the official uploader orders them by start time.
Have all older fragments available before syncing later attempts; a fragment
discovered late cannot be inserted before history that was already accepted.
Do not assume that offline `run.step` reflects the server's last step.

Offline-first mode is for independent runs and sequential fragments. Do **not**
use it as a replacement for SDK `mode="shared"`: setting offline mode changes
the shared-writer protocol. Existing online shared-mode runs retain the SDK's
network retries, but an offline shared-run recovery workflow is not verified.

## Stopped or crashed jobs

A normal offline finish writes completion records; the watcher can deliver them
after training has exited, once connectivity returns. Interrupting/restarting
the watcher also preserves the local cache and SDK upload cursor.

The official live uploader can wait indefinitely if a producer is killed without
completion records. In that case, stop the watcher, confirm **all writers in the
spool have stopped**, then recover with:

```bash
open-train sync-watch "$WANDB_DIR" --once
```

This uses non-live sync and exits nonzero if delivery receipts are missing. It
can still wait for the SDK's network retries; Ctrl-C leaves the queue intact.
For recovery of an **online** run, first stop its original writer and any live
uploaders, then use the official command explicitly:

```bash
WANDB_MODE=online wandb beta sync --yes --no-skip-online /path/to/stopped/run-directory
```

Do not use these non-live commands while a producer is still appending: the SDK
could mark a partial log as synchronized. Truncated/corrupt logs may require
manual recovery; no guarantee is made for records not flushed before a hard
crash, full disks, removed source files, lost disks, or unsupported SDK features.
Monitor free disk space: a long outage necessarily consumes local storage. No
cache quota or eviction policy is applied, since eviction would discard data.

## Verification

For existing TensorBoard writers, the event files themselves are the durable
cache. `open-train import-tensorboard ... --watch` now continues retrying after
connection errors, timeouts, HTTP 408/429, and server errors, with capped backoff.
Re-scanning accepted events is idempotent. Other HTTP 4xx errors (for example a
revoked key) stop the importer so its configuration can be corrected. Keep the
original event files until delivery is verified. This does not install a
continuous importer on a cluster automatically.

`tests/test_network.py` exercises actual SDK processes through a fault-injecting
local proxy: online loss/retry and a lost acknowledgment, startup with no server
connection, live offline upload after reconnect, uploader restart after partial
delivery, training exit during an outage, sequential fragments and concurrent
runs, repeat upload, and offline metrics/config/table/image/file/artifact replay.
These tests do not interrupt production networking.

References: [W&B network interruptions](https://docs.wandb.ai/support/models/articles/what-happens-if-internet-connection-is-l),
[official live sync CLI and crash caveat](https://docs.wandb.ai/models/ref/cli/wandb-beta/wandb-beta-sync).
