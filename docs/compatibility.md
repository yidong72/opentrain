# Compatibility contract

The acceptance target is the **unchanged official W&B Python SDK**, configured with `WANDB_BASE_URL`. Integration tests exercise **0.29.0 and 0.30.0**; the development lock uses `wandb==0.29.0` because of the 0.30 resume-counter limitations below. The server does not install a fake `wandb` module, forward to W&B Cloud, or accept unknown operations as successful.

| Feature | Current support |
|---|---|
| `wandb.init`, config, tags, names, groups, notes | Implemented |
| `wandb.log`, custom steps, summary, finish | Implemented; raw history and numeric metric index |
| `define_metric` and custom x axes | SDK config is stored; dashboard allows choosing numeric axes |
| Resume `must`, `allow`, `never` | Implemented; true SDK continuation tested |
| Resume `auto` | Standard resume lookup is supported; local ID discovery belongs to the SDK; not separately integration-tested |
| Checkpoint continuation across different runs | Separate run IDs, shared group, parent/checkpoint config; native `fork_from`/`resume_from` unsupported |
| Multiple concurrent runs | Transactional independent ingestion |
| Distributed shared-run writers | Two independent official SDK processes tested; per-writer offsets, global history steps, retry isolation, primary completion |
| Google/GitHub accounts | Configurable OAuth, verified identities, opaque sessions; mocked successful callbacks and invalid-state tests; live credentials required for deployment verification |
| Per-user API keys and workspace permissions | Hashed, expiring, revocable keys; owner/writer/reader memberships; official SDK authenticated ingestion tested |
| Public API run lookup/list/history/scan/files | Core paths integration-tested; equality filters and selected ordering fields only |
| `wandb.save`, public file download | Implemented, streamed uploads and signed download URLs |
| Console / system metrics | Stored; logs visible in details; numeric system-series endpoint available, no system-metrics dashboard yet |
| Grid/random sweeps with Python function agents | Implemented; durable, transactional allocation |
| Random distributions | Categorical, constant, uniform, integer uniform, log-uniform, log-uniform-values, normal |
| Bayesian/Hyperband/nested sweep parameters | Unsupported; rejected |
| CLI/subprocess sweep agents | Protocol compatible in principle; not integration-tested |
| Sweep editing/stopping/agent lease recovery | Unsupported |
| `wandb.Table` | Immutable/mutable/incremental logging; pagination, filtering, sorting and rich media cells in the inspector |
| Images/audio/video | Uploaded as files; supported formats have browser previews; images/audio SDK integration-tested |
| Joined and partitioned tables | SDK upload/read-back and viewer tests; full outer join preview, concatenated partitions |
| Image masks, 3D, histograms, interactive HTML/Plotly | Raw values/files preserved; specialized interactive viewers unsupported |
| Artifact lifecycle | Create/log/use/download, drafts, inherited files, reference entries, versions, aliases, metadata/description/tags/TTL, soft delete |
| Artifact collections | Portfolio link/unlink, source version listing, artifact types, source collection name/description updates |
| Artifact integrity | Immutable committed bytes, MD5 verification, SHA-256 physical dedup, latest-version digest dedup, replay-safe multipart completion |
| Artifact multipart | HTTP protocol tests; actual multi-gigabyte SDK transfer unverified |
| Full artifact API / model registry | Not complete: registries, collection deletion, distributed artifact finalization, patch manifests, XXH128, storage regions, retention/GC and full filtering semantics remain |
| TensorBoard scalar migration | Native event files, tensor scalars, multiple directories, repeat imports, watch mode, restart markers |
| TensorBoard media/plugins | Skipped and counted; scalar migration only |
| `sync_tensorboard=True`, offline `.wandb` sync | Not integration-tested; dedicated TensorBoard import is the supported migration path |
| W&B reports, Weave, Launch, automations, enterprise organization APIs | Unsupported; local workspace RBAC is implemented, not enterprise API parity |

## Resume details

The server implements the SDK's `RunResumeStatus` query, including raw history tails, line counts, config, summary, and started-run metadata. Resume tails carry the latest uploaded `_step`; file streams resume at persisted offsets. Zero-history runs remain resumable.

SDK 0.30.0 initializes its Python `run.step` query lazily: before the first `log` after resume it can report zero. `run.starting_step` contains the actual resume step, and persisted history continues correctly. Use the checkpoint's own training step for model restoration. Avoid reusing the exact same local W&B run directory for distinct attempts, because the SDK persists local synchronization state there.

Tests also observed SDK 0.30.0 emitting inflated `_runtime` values after resume. Its [run upserter](https://github.com/wandb/wandb/blob/v0.30.0/core/internal/runupserter/runupserter.go) multiplies an already-duration-valued resumed runtime by `time.Second`. The server preserves the SDK's emitted values; it does not rewrite telemetry to conceal this behavior. Use the pinned 0.29.0 client when resumed runtime counters matter. Scalar losses, custom training steps, and history continuation are verified on both versions.

History is append-only by offset. Retrying identical data does not duplicate it. A conflicting retry returns HTTP 409; it does not overwrite accepted training history. Stream updates to console lines and summary remain mutable as the SDK expects. Standard mode permits one writer per run; shared mode uses the SDK's async client ID to isolate each process's offsets and terminal state. Only the primary should send completion. Shared `_step` is server-assigned; preserve your checkpoint step in `global_step`. Completed writers cannot append new data under the same client identity; restarted processes need new SDK sessions. Shared mode does not provide distributed checkpoint coordination.

## Limits and follow-up work

The first release intentionally favors inspectable correctness over scale. SQLite supports many independent runs but serializes writes. Some read paths load the run's complete history before paging or sampling. The dashboard bounds plots to 12 metrics and 12 selected runs, with min/max sampling that preserves spikes; full data remains accessible through API pagination. TensorBoard watch mode rescans inputs.

Full W&B parity remains the target, **excluding Bayesian sweeps by request**, not a claim about this build. Remaining work includes unimplemented public/admin/registry operations, native run forks/rewinds, offline-sync verification, sweep controls and early termination, full table query semantics, richer visualization, artifact retention, incremental TensorBoard cursors, SQL-level history pagination, and production scaling/security hardening. OAuth credentials must be supplied and live sign-in checked before deploying accounts. The supported version matrix does not imply compatibility with every past or future SDK.

Table previews cap materialization at 64 MiB/200,000 rows and 16 reference levels. They do not fetch arbitrary URLs. SDK downloads can resolve external artifact references using credentials on the training machine; the server does not proxy those external storage systems. Artifact deletion is logical; bytes stay on disk. The server is single-node/local-disk, with no quotas, garbage collector, rate limiter, or HA failover. Do not expose unauthenticated mode publicly.

Protocol references: [shared-run settings](https://docs.wandb.ai/models/track/log/distributed-training), [table logging modes](https://docs.wandb.ai/models/tables/log_tables), [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect), [GitHub OAuth](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).
