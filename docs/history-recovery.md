# History integrity and recovery

Open Train separates SDK delivery offsets, logging `_step`, and user-defined training axes such as `train/global_step`. It never guesses training steps from event order.

## Identity, axes, and resumes

Scalar joins use a history record identity, not merely `(run, _step)`. A value lacking its chosen axis is omitted and counted in `missing_axis`; it cannot inherit that axis from another record at the same step. Different values or timestamps at the same logging step remain separate. Exact SDK events (same complete normalized JSON including timestamp, step and writer identity) replayed at a new upload offset are indexed once; the original raw SDK stream and delivery offsets are still retained unchanged.

Native TensorBoard ingestion uses `event.step`. Events merge only within the same run/session/restart epoch/step. New imports retain their session identity. Checkpoint restart markers supersede old tails without destroying their new history records. Legacy TensorBoard data already collapsed into the old metrics table cannot recover its original session identity from that table: it is labelled `legacy_tensorboard`. Its metric timestamps are preserved; custom-axis joins require matching original timestamps, otherwise the pair is omitted. Raw event-file re-import can recover richer provenance into a separate batch/run after verification.

The dashboard's default **Auto (metric definition)** respects `wandb.define_metric(..., step_metric=...)` metadata. Without a definition it falls back to **logging step**, not an inferred training step. Explicit axis selection overrides this. Comparisons with inconsistent auto axes are blocked until an explicit common axis is chosen. Series responses expose resolved `axis`, `missing_axis`, `legacy_projection_points`, and `axis_warning`.

`/api/runs/{uid}/history` and SDK history reads return active canonical records; HTTP history pages are selected in SQLite without rebuilding the entire history. Exact replay copies and quarantined/superseded rows are excluded. Original `wandb-history.jsonl` delivery streams, their resume offsets and tails remain unchanged for protocol compatibility and audit. Distinct resumed records sharing a step are not deduplicated.

## Reversible quarantine

All endpoints require normal workspace authorization. Reader accounts cannot mutate recovery state, even when requesting a dry run. Run IDs here are internal `uid` values.

1. Inspect candidates with `GET /api/runs/{uid}/records?missing_axis=train/global_step&limit=100&offset=0`. This is a discovery filter, **not proof that a record is invalid**; system/other logs can legitimately lack that key.
2. Preview `POST /api/runs/{uid}/quarantine` with `{"record_ids":[123,124],"reason":"verified incorrect import"}`. `dry_run` defaults to `true`.
3. After checking exact IDs and backing up, send the same request with `"dry_run":false`. Only those records become inactive. No SDK offsets, raw files, artifacts or other runs are deleted.
4. Undo with the same IDs, `"active":true`, a reason, and `"dry_run":false`. Checkpoint-superseded records remain excluded. A batch can be selected with `batch_id` instead of record IDs.

`GET /api/runs/{uid}/history-audit` shows recent actions. Summaries are recomputed from remaining active history after quarantine/restore/replacement. Custom SDK summary aggregations cannot be inferred from raw history and must be recomputed by the caller if needed. A live SDK may subsequently submit its own summary again.

No existing data is quarantined automatically during migration. In particular, the absence of `train/global_step` never triggers automatic deletion.

## Explicit import batches

`POST /api/runs/{uid}/imports` accepts an existing run and up to 1,000 records / 8 MiB per request:

```json
{
  "batch_id": "slurm-job-123-v1",
  "source": "slurm",
  "session": "job-123",
  "records": [
    {
      "id": "line-25",
      "data": { "_step": 100, "train/global_step": 100, "loss": 0.5 }
    }
  ],
  "status": "partial",
  "expected_records": 20,
  "metadata": {
    "source_sha256": "checksum-of-source-file",
    "recovery_status": "partial"
  }
}
```

Sources: `slurm`, `tensorboard`, `offline_recovery`, or `jsonl`. Each record requires an explicit `_step` and a stable caller-chosen ID. Never synthesize training steps by enumerating input rows. Retries with the same batch and record ID are idempotent; conflicting content is rejected transactionally. Session/source/metadata are immutable within a batch.

Send chunks with `status=partial`. Finalize with `status=complete` and `expected_records` only when that batch's declared records have all arrived. The count must match exactly. Completion means **this declared batch**, not that the original job or damaged source file is complete. Record the original recovery status/checksum separately in metadata. A completed batch cannot accept additional records; use another batch/session.

To replace an earlier import, finalize a new complete batch with `replace_batch_id` pointing to the old batch. Validation and deactivation of the old batch occur in one transaction. The old records remain available for audit/restore. Ordinary SDK uploads are never reset by this API.

`GET /api/runs/{uid}/imports` and the run detail `ingestion` field expose batches, completeness, sources and active/total counts. The dashboard displays these under **Sessions → Data provenance & recovery**. Historical records without a receipt are explicitly unverified, not retrospectively declared complete.

## Damaged offline journals

The server cannot reconstruct bytes missing from a cancelled job's `.wandb` file. Preserve the original and stop the writer before recovery. Install the `client` extra, then:

```bash
open-train recover-offline /path/to/run-id.wandb --output /private/path/recovered-history.jsonl
```

This validates journal checksums and fragment boundaries and decodes records with the installed official SDK protobuf schema. It supports SDK 0.29 and 0.30 without relying on the Python datastore parser removed in 0.30. It never changes the source, repairs its journal, marks it `.synced`, uploads automatically, or executes its content. It refuses existing output files. The JSON receipt includes SHA-256 checksums, readable-prefix length, exit-record detection, and partial/complete status. Exit code `2` means partial recovery; `0` means a complete history export under these checks. Missing/invalid-step records are skipped and disclosed, never numbered by event. Config, summaries, media and artifacts are not reconstructed by this history-only utility. Unsupported framing/schema or records exceeding 64 MiB produce a partial result; keep the original file.

Import recovered JSONL with a separate `offline_recovery` batch, retaining the receipt in metadata. Surviving TensorBoard data or trusted Slurm per-step dictionaries are alternative sources; do not evaluate arbitrary Python from a Slurm log. The existing sync watcher continues retaining failed uploads locally and requires SDK sync receipts before declaring delivery.

## W&B-compatible run deletion

`wandb.Api().run("entity/project/run").delete()` now calls the supported `deleteRun` GraphQL mutation. Deletion hides the run and frees its name for a new run, but retains an internal tombstone and stored data. By default artifacts remain usable. `delete(delete_artifacts=True)` also soft-deletes that run's owned artifacts and removes their aliases.

Restore by internal UID using `POST /api/runs/{uid}/restore` with `{"confirm":true}`. It restores the run and artifacts deleted by that operation, rejecting reused run names or artifact aliases rather than overwriting new work. Blob garbage collection is not performed. This is recoverable application deletion, not permanent erasure of sensitive data.

## Migration and rollback

Take a consistent SQLite backup and preserve blobs before deployment. Startup builds the new record index transactionally per run and leaves original run rows, raw lines, files, and the legacy metric projection intact. Existing SDK raw history is authoritative for its stream; legacy TensorBoard metrics are reconstructed only when no SDK stream exists. Migration receipts prevent re-running completed runs.

Rehearse on a new private database copy:

```bash
PYTHONPATH=. python scripts/verify_history_migration.py /private/backup/tracking.sqlite3 --output-dir /private/new-rehearsal
```

This verifies fingerprints of original tables before and after migration and SQLite integrity. Do not run an older server against new recovery writes: an old server ignores quarantine/tombstones/new import batches. Roll back code only before such writes, or restore a coordinated backup after accounting for all subsequent uploads. Never discard new training data merely to roll back a deployment.
