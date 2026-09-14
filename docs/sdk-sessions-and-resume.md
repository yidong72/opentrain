# SDK sessions and checkpoint resume

An OpenTrain run is a logical experiment; an SDK session is one writer process.
Resume the same run ID after loading a checkpoint. The official client does not
need a custom wrapper to report session metadata.

## Behavior

- The SDK's `_wandb.e[writerId]` environment record creates a durable session.
  Config/metric-definition upserts and repeated delivery of the same metadata
  do not create extra sessions. Late metadata reattributes previously received
  SDK records by process start time. Shared-mode writers keep independent,
  overlapping provenance instead of being treated as sequential jobs.
- Run details return `sessions`, `session_count`, and a provenance caveat.
  Sessions expose start, observed end, exit code when reported, host, program,
  SDK step range, custom-axis ranges, history/system record counts, and evidence.
  System-only sessions warn that no training history was received.
- Historical sessions without retained writer metadata are **inferred lower
  bounds**. A falling SDK step can identify a segment, but a continuously
  increasing counter cannot reveal every Slurm restart. An inferred/ended
  session is not proof that the training job failed.
- The UI shows these sessions in run details and comparison legends. Session
  markers work on declared/custom axes as well as SDK steps and wall time.
  Series include per-metric `session_starts`, computed before downsampling, so
  setup-only `global_step=0` records do not hide later session boundaries.
  REST series default to `auto`, honoring `wandb.define_metric` step definitions.
- W&B resume keeps `historyLineCount = MAX(raw file offset) + 1`. Exact replay,
  quarantine and recovered imports never rewind that upload cursor. The
  validated resume tail skips rows without a usable SDK step and cannot go below
  the previous SDK high-water step; the resume-query summary uses that same step.
  Resume diagnostics log run UID, line count, and last step, not credentials or
  training configuration.

## Why not renumber all SDK steps?

SDK `_step` values are not record identities. The record index already preserves
distinct events at repeated steps; exact event replays are deduplicated using a
content hash. Replacing all SDK steps with line ordinals would break legitimate
explicit/sparse steps. Counting only active indexed records would also corrupt
the file-stream resume cursor after replay or quarantine. Neither is necessary
to preserve multiple sessions.

The migration adds session attribution without rewriting raw history, SDK
payload steps, legacy metrics, upload offsets, or quarantine flags. Rehearse on
a private SQLite backup, not by modifying the live database:

```sh
uv run python scripts/verify_history_migration.py /private/backup.sqlite3 \
  --output-dir /private/new-rehearsal
```

## September 13 investigation

The supplied incident report correctly identified missing SDK session provenance,
but its proposed data-loss mechanism was not supported by the raw database.
For `abl_klauto_recipe_pp`, `abl_klauto_min_fb`, and
`abl_klauto_min_fb_anneal`, early training history was absent from **both** the raw
lines and record index. Their first-session W&B journals also contained zero
history records in the checksum-valid prefix: only setup, console output and
system telemetry. The journals ended with incomplete records after job termination.
A server re-index or syncing those prefixes cannot recover metrics they lack.

Preserved TensorBoard files contained complete first-session training steps:

| Run suffix | Training steps | Scalar events | Recovered train/eval records |
| --- | ---: | ---: | ---: |
| `recipe_pp` | 1–396 | 18,315 | 475 |
| `min_fb` | 1–392 | 17,656 | 470 |
| `min_fb_anneal` | 1–400 | 18,015 | 479 |

The inspected Molt TensorBoard logger passes its `global_step` directly to
`SummaryWriter.add_scalar`. Recovery groups only identical namespace/event-step
pairs, supplies the corresponding `train/global_step` or `eval/global_step`,
and retains original scalar values. Its timestamp is the latest scalar wall time
in that group. Checksum-addressed, idempotent import batches retain provenance;
checkpoint-overlap steps are kept, not silently overwritten.

Original per-session `wandb-metadata.json` files identify five `recipe_pp`, three
`min_fb`, and three `min_fb_anneal` writer processes at the time of inspection.
The claimed sixth `recipe_pp` session was not present in the inspected source
directory. The precise producer-side reason for the empty first-session W&B
journals remains undetermined; recovery and server improvements do not establish
that cause or change the running training code.

The production deployment restored all three verified early ranges and backfilled
the 5/3/3 writer sessions. Import receipts are complete and replaying the batches
added zero duplicates. A post-deployment comparison with the private pre-change
backup found zero changed/missing immutable SDK lines and zero changed original
record payloads, SDK steps, or quarantine flags. An authenticated official W&B
0.28.2 online smoke test resumed successfully with two sessions and eight rows.

## Regression coverage

`tests/test_sessions.py` covers repeated steps, repeated upserts, late metadata,
system-only sessions, lossless migration, inferred resets, shared writers,
quarantine/replay offsets, sparse resume steps, and declared REST axes.
The real SDK resume test checks session counts and per-process records. CI tests
W&B 0.28.2, 0.29.0 and 0.30.0, including checkpoint/outage integration tests.
Opt-in Playwright tests cover SDK session cards and custom-axis markers alongside
project selection, per-plot controls and sharing.
