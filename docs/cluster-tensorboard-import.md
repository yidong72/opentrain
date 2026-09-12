# Import TensorBoard experiments from SSH clusters

`scripts/import_cluster_tensorboard.py` handles trees shaped like:

```text
outputs/<experiment-tag>/tb/<session>/events.out.tfevents.*
```

It creates one run per **SSH alias + absolute outputs root + experiment tag**, combining sessions chronologically. Different clusters or trees remain separate. The display name is `cluster/tree/tag`, with source paths, session step ranges, and file checksums in the run configuration.

## Snapshot, analyze, import

Run from the repository with `uv sync` completed. This requires Python 3 on the SSH hosts but installs nothing there. Only event files are read; model checkpoints and other output files are not traversed or copied.

```bash
uv run python scripts/import_cluster_tensorboard.py snapshot \
  --workspace data/my-import \
  --root cluster-a=/path/to/tree/outputs \
  --root cluster-b=/path/to/another-tree/outputs

uv run python scripts/import_cluster_tensorboard.py analyze \
  --workspace data/my-import

uv run python scripts/import_cluster_tensorboard.py import \
  --workspace data/my-import \
  --base-url https://opentrain.example.com \
  --project my-training \
  --key-file /private/path/opentrain_key \
  --upload-events
```

The key file accepts a bare API key or `WANDB_API_KEY=...`. Protect it with owner-only permissions. Credentials are read locally, never sent to the clusters, and only used with the destination server. The destination entity is the key owner's username.

Keep the workspace under gitignored `data/`: it contains private telemetry, source paths, analysis, and import receipts. Do not publish it to GitHub. Use trusted SSH aliases and roots only.

`--upload-events` also attaches the original event files under each run's **Files & media**, verifying stored size and SHA-256. Non-scalar TensorBoard records remain in those downloadable files but are not rendered as charts. Without this flag, only scalar metrics are uploaded. Per-request hosting limits apply to the original files.

## Resume semantics and checks

- Training step numbers are preserved, without invented offsets.
- At overlapping metric/step pairs, the later wall-time value wins.
- Explicit TensorBoard restart markers purge obsolete tails. Without markers, unmatched old tail points are retained; session directory boundaries alone do not prove which points to discard.
- Repeated import of the same snapshot uses stable run IDs and deduplicated events. Existing raw files with matching checksums are not uploaded again.
- Every run is checked against locally computed per-metric point counts and last steps. Representative train/eval series are compared point-for-point when they fit the API's 10,000-point limit.
- Runs with event-file headers but no scalars are retained as empty runs, with original files when requested.

This is a **snapshot**, not ongoing synchronization. A live file is copied up to its size at the time it is read; an incomplete trailing event is not charted. Snapshot and analyze again, then import with the same aliases, roots, and project to bring in newer data. Do not run multiple imports into the same project/run simultaneously. Imported state means a snapshot was ingested, not that the training process has finished.

Individual `*.receipt.json` files record completed runs; `receipts.json` is written when the entire import succeeds. If interrupted, rerun the import phase. Keep the original snapshot to audit overlaps and non-scalar data.
