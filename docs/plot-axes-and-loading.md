# Training steps, evaluation steps and chart loading

W&B `_step` counts committed history rows, not optimizer updates. Logging metrics,
tables or evaluation separately can advance it more than once per training step.
Use `train/global_step` for training charts and `eval/global_step` for evaluation
charts when those counters are recorded with the corresponding metric. Evaluation
can be less frequent, so its latest training step can legitimately lag training.

For example, `q38_rpga_rghard_1n_gb` contained an evaluation at logging step 2545
whose recorded `eval/global_step` was 1180. Training reached 1195. Showing 2545
next to 1195 compared two different counters; showing 1180 and 1195 is correct.

The run sidebar also prefers the latest recorded `train/global_step`, then
`global_step`. If neither is available it explicitly labels the SDK counter
**Log step**. Hover over the step label to see its source and the logging counter.

The home page and project pages start with **no runs selected**, including when
switching projects. No chart metadata or series are fetched until you select a
run. Explicit run URLs and shared view links still restore their selected runs.

In **Account & access → Workspace membership**, workspace owners can see the
current member list and each member's role. The list reloads when opening the
dialog, after saving a membership, or using **Refresh members**. It does not expose
email addresses or credentials; readers, writers and outsiders cannot fetch the
owner-only roster endpoint. Existing access grants are unchanged.

## Automatic axes

1. Explicit per-plot axes always win, including **Logging step** (`_step`).
2. Auto honors the SDK metric definition (including wildcard definitions).
3. If there is no saved axis definition, Auto checks for a namespaced
   `global_step` paired with the metric in the same history record, starting with
   the closest namespace. The chart labels this fallback `Auto: eval/global_step`
   (or the corresponding counter). Unpaired records are omitted with a warning,
   never joined by offset, interpolated or silently renumbered.
4. Without a verified paired counter, Auto uses `_step`.

Config updates now preserve metric definitions omitted by a later resumed SDK
process. Incoming SDK indexes retain their original meaning; older retained
definitions have their indexes resolved to names before merging. A new definition
for the same metric replaces the old one. Previously lost definitions can use
the paired-counter fallback without modifying stored history or resume cursors.

Recommended client setup remains:

```python
run.define_metric("train/global_step")
run.define_metric("train/*", step_metric="train/global_step")
run.define_metric("eval/global_step")
run.define_metric("eval/*", step_metric="eval/global_step")
run.log({"train/global_step": step, "train/loss": loss})
if step % 20 == 0:
    run.log({"eval/global_step": step, "eval/score": score})
```

## Loading performance

Charts use authenticated `GET /api/runs/{uid}/plots`, returning only history
metric keys and compact session provenance. They no longer wait for the full
run-detail endpoint, which includes file links, config, import receipts and the
system-metric catalog. Full details remain available when explicitly opened.

Session statistics are aggregated once per run instead of rescanning the history
for every resumed job. Chart polling requests `sessions?compact=true`, omitting
all-metric axis ranges it does not use. SDK session markers still use the
per-series paired `session_starts`; full session details and MCP retain all axes.

Chart startup requests are cancellable when switching selections. Existing
on-demand series loading, stable live updates, per-plot zoom/smoothing and sharing
remain in place. These changes require no database migration or data rewrite.
