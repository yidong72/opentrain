# Open Train

An open-source, self-hosted experiment tracker that accepts the **official `wandb` Python client**. Set `WANDB_BASE_URL` and keep your training instrumentation.

The server includes a live comparison dashboard, durable metrics/config/summary/file storage, checkpoint continuation, TensorBoard scalar import, grid/random sweeps, Google/GitHub accounts, per-user keys, shared-run ingestion, advanced table inspection, and versioned artifacts. Training data stays local; the dashboard needs no CDN. OAuth sign-in connects to the configured identity provider.

**Status: working alpha, not full W&B feature parity.** Compatibility is defined by real SDK integration tests, not a replacement Python module. See [the compatibility matrix](docs/compatibility.md) for supported operations and known gaps.

## Start locally

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Python 3.12 is used for development.

```bash
git clone https://github.com/yidong72/opentrain.git
cd opentrain
uv sync --python 3.12
uv run open-train serve
```

Open **http://127.0.0.1:8080**. The default data directory is `./data`.

Alternatively: `pip install -e .` and `open-train serve`.

In the training terminal:

```bash
export WANDB_BASE_URL=http://127.0.0.1:8080
export WANDB_API_KEY=local00000000000000000000000000000000000
export WANDB_ENTITY=local
uv run python examples/train.py
```

That example generates three explicitly labeled demo runs, including a resumed run, through the real SDK. Existing training environments only need their existing `wandb` dependency and the environment variables above. A local server without authentication ignores the placeholder key; the official SDK still expects a key.

```python
import wandb

with wandb.init(project="my-project", config={"lr": 0.001}) as run:
    run.log({"train/loss": 0.42, "eval/accuracy": 0.91})
    run.summary["best_accuracy"] = 0.91
```

Select runs to overlay charts. Search by name, run ID, group, or tag; filter by project/status. Click a run to inspect config, summary, files, media, tables, and console output. Choose a custom metric as the x axis to compare training steps across checkpoints.

## TensorBoard import

```bash
uv run open-train import-tensorboard ./logs --project my-project --source experiment-1
# Follow new event files and appended events:
uv run open-train import-tensorboard ./logs --project my-project --source experiment-1 --watch
```

Each event directory becomes a separate run. Multiple event files **in the same directory** are merged chronologically. TensorFlow tensor scalars and legacy scalar summaries are supported; TensorFlow itself is not required. Non-scalar TensorBoard records are reported as skipped, not imported as fake scalar values.

Imports are idempotent. Use the same `--source` and relative directory layout when moving logs to another machine. Without `--source`, identity includes the absolute input path. Use `--run-id existing-id` to target a particular TensorBoard run; the input must contain exactly one event directory. Do not import into a run concurrently written by the W&B SDK.

TensorBoard `SessionLog.START` markers (including those produced by `SummaryWriter(purge_step=...)`) purge obsolete points at or after the restart step. Overlapping scalar keys/steps use the event with the later wall time. Without a restart marker, old later points are preserved. `--watch` currently rescans event files and sends deduplicated events; use a longer interval for large logs.

## Continue from a checkpoint

Persist the W&B run ID alongside model weights, optimizer state, and your training step:

```python
# Include these in the checkpoint you already save:
checkpoint["wandb_run_id"] = run.id
checkpoint["global_step"] = global_step
```

On restart:

```python
run = wandb.init(
    project="my-project",
    id=checkpoint["wandb_run_id"],
    resume="must",  # "allow" also permits creation when the run is missing
)
run.define_metric("global_step")
run.define_metric("train/*", step_metric="global_step")
run.log({"global_step": checkpoint["global_step"], "train/loss": loss})
```

The W&B history `_step` continues after the last uploaded row. Your training `global_step` may rewind to an earlier checkpoint; log it as a custom axis instead of attempting to rewind W&B's internal step. Model/optimizer restoration remains the training program's responsibility.

For a new experiment derived from a checkpoint, use a **new run ID**, the same `group`, and config fields such as `parent_run_id`, `checkpoint_step`, and `checkpoint_path`. These remain inspectable provenance. Native W&B `fork_from` and `resume_from` are not implemented.

Separate runs can upload concurrently. Standard mode uses one writer per run ID. For multiple processes writing the same run, use the SDK's shared mode:

```python
run = wandb.init(
    project="distributed",
    id=shared_run_id,
    settings=wandb.Settings(
        mode="shared",
        x_label=f"rank{rank}",
        x_primary=rank == 0,
        x_update_finish_state=rank == 0,
    ),
)
run.log({"loss": loss, "global_step": training_step})
```

Each process must use its own SDK session/directory. Writer-local offsets and retries are tracked independently; the server assigns a global `_step`, preserves any supplied step as `_source_step`, and records `_writer`. Only the primary should finish the run. Use `global_step` as the training axis. The dashboard's Writers tab shows each writer's ingestion count. Transactions make batches atomic; conflicting retries return 409. This is tested with two independent SDK processes, not yet large-cluster load-tested.

`wandb.save()` and `wandb.Api().run(...).file(...).download()` transfer checkpoint files. See [examples/checkpoint.py](examples/checkpoint.py).

## Sweeps, tables, and media

```bash
uv run python examples/sweep.py
uv run python examples/media.py
```

`wandb.sweep()` and `wandb.agent(..., function=train)` work for grid and random searches. Trial allocation is transactional across agents. Random searches default to a maximum of 100 allocated trials unless `run_cap` is specified. Agents redeliver unacknowledged assignments, and run/config/assignment state survives server restarts. Bayesian search, Hyperband, distributed lease recovery, and sweep control/edit APIs are not implemented.

`wandb.Table` immutable, mutable, and incremental logging, `wandb.JoinedTable`, and partitioned tables are integration-tested. Open a run's Summary or Files & media tab and choose **Inspect table**. The inspector provides paging, text filtering, sortable columns, and image/audio/video cells. Joined previews use a full outer join with explicitly prefixed columns. Preview materialization is capped at 64 MiB and 200,000 rows; full artifacts remain downloadable through the SDK. External table references are never fetched by the server. Image masks, 3D, interactive Plotly/HTML, and W&B's complete table query language are not implemented.

## Artifacts

```python
artifact = wandb.Artifact("checkpoint", type="model", metadata={"epoch": 10})
artifact.add_file("checkpoint.pt")
run.log_artifact(artifact, aliases=["latest", "best"]).wait()
# In another run:
checkpoint = run.use_artifact("your-entity/my-project/checkpoint:best")
directory = checkpoint.download()
```

Implemented paths include immutable committed content, versions, aliases, metadata/description/tags/TTL updates, same-content latest-version deduplication, drafts with inherited files, reference entries, producer/consumer lineage, portfolio links/unlinks, pagination, and soft deletion. The SDK lifecycle tests explicitly bypass its cache when verifying downloads. Multipart upload and checksum validation have HTTP protocol tests; multi-gigabyte SDK transfers have not been exercised. Blobs are content-addressed; deletion and TTL do not yet reclaim disk space. The run's Artifacts tab shows files, versions, aliases, and lineage. This is not yet the entire W&B artifact/registry API; see the compatibility matrix.

## Google/GitHub accounts

Copy [.env.example](.env.example) to `.env` and configure one or both OAuth providers. Set `OPEN_TRAIN_AUTH_MODE=accounts` and `OPEN_TRAIN_PUBLIC_URL` to the externally reachable origin. Register these exact callback paths:

- Google: `https://your-host/auth/callback/google`
- GitHub: `https://your-host/auth/callback/github`

For a GitHub App (as opposed to an OAuth App), enable **Permissions & events → Account permissions → Email addresses → Read-only**. Existing users must approve the updated permission when signing in again. Keep wildcard callback matching disabled. Missing email permission prevents account creation even if GitHub authorization succeeds.

Start with `uv run --env-file .env open-train serve`, or Docker Compose. Sign in using the dashboard's key/account button, then open **Account & access** to create a named, expiring API key. Copy it immediately; only its hash is stored. Set `WANDB_API_KEY` to that key and `WANDB_ENTITY` to your account's displayed entity name. Keys can be revoked from the same screen. Workspace owners can grant existing users `reader`, `writer`, or `owner` membership.

OAuth uses provider-issued immutable identities, verified email, state and PKCE; Google ID tokens are verified by Authlib. Matching emails do not automatically merge Google and GitHub identities. Browser sessions use opaque, revocable HttpOnly cookies and origin checks on writes. Set `OPEN_TRAIN_ADMIN_EMAILS` before an administrator's first sign-in. Accounts mode denies anonymous data access. Live provider login requires your OAuth credentials and remains a deployment acceptance check; local tests mock successful provider exchanges and exercise invalid-state rejection.

## Deployment

For another machine, clone this repository and follow the [Docker deployment guide](docs/deployment.md). For Brev or any Linux Docker host with a dedicated Cloudflare Tunnel, see [the tunnel setup guide](docs/deployment-brev.md). Credentials and training data are not included in this repository.

```bash
# Generate a key and configure the server and clients with the same value.
export OPEN_TRAIN_API_KEY="$(python -c 'import secrets; print(secrets.token_hex(20))')"
export OPEN_TRAIN_PUBLIC_URL=http://your-server:8080
uv run open-train serve --host 0.0.0.0 --data-dir /path/to/persistent/data
```

This command demonstrates legacy single-key mode. The legacy key grants server-wide administration, including in accounts mode; leave it unset when using per-user keys. Protect remote traffic with HTTPS through your reverse proxy. Without accounts mode or a legacy key, the server is intentionally unauthenticated for localhost development. API keys entered manually in the dashboard are kept in browser session storage; OAuth sessions use HttpOnly cookies. Set `OPEN_TRAIN_PUBLIC_URL` when the externally reachable address differs from the server's request URL.

Docker:

```bash
docker compose up --build
```

Compose exposes only loopback by default and mounts a persistent volume. Configuration is in [compose.yaml](compose.yaml). The Docker app and Cloudflare overlay have been deployed on a Linux host. Back up the complete data directory while the server is stopped, or use SQLite's online backup API plus a consistent copy of `blobs/`. Do not copy a live SQLite database file alone: recent data can still be in its WAL.

This release targets a single server on a local disk. It is not yet an HA service or a proven replacement for W&B at large-cluster scale. No retention/garbage collection is enabled; old content-addressed blob versions remain on disk.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The development lock uses W&B 0.29.0; CI also tests 0.30.0. See [known 0.30 resume-counter limitations](docs/compatibility.md#resume-details). Tests cover the actual installed SDK over HTTP, artifact round trips, shared processes, advanced tables, accounts, scalar ingestion, files, resume guards, sweeps, retry conflicts, persistence, TensorBoard restarts, and signed URLs. Test the other SDK with `uv run --with wandb==0.30.0 pytest`. Browser checks: `uv run playwright install chromium`, then `uv run python scripts/check_ui.py --base-url http://127.0.0.1:8080` against a demo-seeded server.

Architecture: FastAPI HTTP endpoints → a validated GraphQL schema / file-stream adapter → SQLite WAL plus content-addressed local blobs → a dependency-free browser dashboard. Run histories are preserved as received and numeric metrics are separately indexed. Unknown GraphQL fields produce explicit errors.

`uv run python scripts/check_accounts_ui.py` checks account settings, key creation/revocation, and sign-out in Chromium using a disposable synthetic session. It does not substitute for live OAuth acceptance testing. The protocol suite currently contains 32 tests; dashboard and account browser checks are separate.

Apache-2.0 licensed. Independently implemented; not affiliated with Weights & Biases. Protocol references: [W&B open-source client](https://github.com/wandb/wandb), [resume documentation](https://docs.wandb.ai/models/runs/resuming), and [TensorBoard](https://github.com/tensorflow/tensorboard).
