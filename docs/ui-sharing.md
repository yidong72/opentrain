# Projects, chart axes, and sharing

The **Project** switcher is always visible above the run list. Selecting a
project clears the previous selection; no runs are selected automatically.
Other run filters are reset. The project URL survives reloads;
**All projects** lets you select runs across projects for comparison.

The dashboard's **Default X axis** applies to plots set to **Use dashboard
default**. Each plot has its own **X axis** selector, including in maximized
view. Explicit choices override the default; **Auto (metric definition)** honors
the SDK's configured metric axis. An incompatible auto-axis comparison shows a
warning and lets you choose a common explicit axis. Values without a verified
axis pairing are omitted and counted. Axis and smoothing preferences are stored
locally per metric; zoom is tracked separately for each axis.

Switch the sidebar from **Runs** to **Metrics** to explore slash-separated
categories and their metric counts. Click a category name to focus its plots,
or its disclosure arrow to explore subcategories. Breadcrumbs take you back up.
Search accepts plain text or a regular expression such as `/loss|reward/i`.
The dashboard initially mounts at most 24 charts; **Show more metrics** reveals
the next batch. Only visible, open plots fetch live updates.

**Compact** layout uses three columns on wide screens. **Comfortable** uses
larger cards with controls open. In compact mode, open **Plot settings** for
independent X axis, linear/logarithmic Y scale, smoothing, zoom, share and PNG
controls. Nonpositive values are omitted with a warning on a log scale.
The always-visible maximize button opens a detailed plot and summary table.

Card values and expanded summaries are computed from all records in the selected session view with
valid axis pairings, not from sampled or smoothed curves. **Last** means the latest
timestamp (record order breaks ties), even if resumed training rewinds the X axis.
**Δ** compares that value with the preceding non-null record, not the previous
poll. The table also shows min, max, mean and count. Endpoint dots and labels
refer to the rightmost displayed curve point, which may be smoothed. Hover shows
the corresponding raw value. Summary statistics include the whole paired series,
not just the zoomed region.

## Overlapping resumed sessions

The **Sessions** selector beside the default X axis provides two views:

- **Latest trajectory** (default): a new session resumed at step 1,000 replaces
  its predecessors from step 1,000 onward, including any old tail beyond the
  new session's current progress. Repeated rewinds apply to all earlier sessions.
  Curves are solid. On a verified, increasing training axis, the retained
  trajectory is connected and EMA carries across sequential resumes. Ambiguous
  writers, unknown boundaries, overlapping/nonmonotone axes and missing values
  stay separate. Joining is a display interpolation, not recovered measurements.
- **All sessions**: retain overlapping active records and draw each session
  separately. Color distinguishes sessions; line patterns and A/B/C labels
  distinguish runs. Open **Session colors · run line styles** to see the grouped
  legend. Hover a run or session to highlight it, or click to pin the highlight;
  **Show all** restores every curve. Highlighting does not exclude records from
  statistics. Session numbers belong to each run: S2 in two runs does not mean
  a matched checkpoint. Hover
  at an overlapping step shows each session's value, label and timestamp.

This is a read-only view. Nothing is deleted, quarantined or changed in the
SDK resume/upload cursor. Existing TensorBoard purges and quarantine rules still
apply; All sessions does not restore records previously superseded or quarantined.
Statistics and PNG exports follow the chosen view; share links preserve it.
Older share links without this setting retain their original all-sessions view.
The visible **View** selector on each plot (including maximized plots) changes
the dashboard session view; choose **All sessions · colored** for the color legend.
All-sessions smoothing inherits EMA from the retained history strictly before
each resume boundary. It never inherits an abandoned tail: repeated rewinds
restore the appropriate earlier prefix. Each colored branch keeps all its own
observations; the short connecting segment is interpolation, not another record.
Ambiguous writers, unsupported axes and missing/nonpositive log values remain
independent. Latest-trajectory smoothing carries forward along the retained path.
EMA on sampled points is approximate; inferred boundaries are not exact checkpoints.
Auto Y limits follow the displayed smoothed values; statistics remain raw.
Logarithmic axes use readable ticks and omit nonpositive values with a warning.

For exact boundaries, log the checkpoint's training step once at the beginning
of **each resumed SDK process**, before its metrics, using the official client:

```python
run = wandb.init(project="training", id=checkpoint["wandb_run_id"], resume="must")
run.log({"_open_train/resume_step": checkpoint["global_step"]})
# Continue logging train/global_step and eval/global_step alongside their metrics.
```

The marker uses the same step units as the training counter (or the declared
custom step metric). Do not copy the marker onto every record. Without a marker,
Open Train labels boundaries **inferred** from the first observed training step;
setup-only counter rows are ignored, and training observations take precedence
over sparse evals. This cannot recover an unlogged checkpoint exactly.
Hover over the trajectory note for the per-session boundary and its source.

Filtering uses paired training coordinates even when viewing wall time or logging
steps. It recognizes the declared custom axis and conventional `train/global_step`,
`global_step`, and namespaced global-step counters; with no such counter, it can
only infer from logging/event steps. Records without a paired coordinate remain
visible with a warning. Shared distributed writers are **not** sequential
resumes; they remain separate, untrimmed curves. Ambiguous overlapping session
time ranges also disable trimming with an explanation.

REST callers can select `view=latest` or `view=all` on the run-series GET endpoint,
or set `view` in the `/api/series` POST body. The API defaults to `all` for backward
compatibility; the dashboard explicitly requests `latest`. Responses include
per-point `point_sessions`, `trajectory` boundary/warning metadata and `stats.scope`.

## Sharing

Use **Share view** for the current metric view, or **Share** on a plot for exactly
that metric. Copy the link in the dialog. It preserves selected run IDs, colors,
project, metric category/selection/search, group expansion, default and per-plot axes,
Y scale, smoothing, zoom, session view, and session-marker visibility. The recipient's old local plot
preferences do not override the shared settings. Links open the latest data,
not a frozen snapshot. Very large views must be narrowed to a single plot.

Sharing does **not** grant access. Teammates must sign in and already have access
to the workspace; owners can add an existing teammate as a **reader** in
**Account & access → Workspace membership**. The teammate must have signed in
at least once so their account exists. Unavailable or unauthorized selected runs
are omitted with a warning; no other runs are silently substituted. Sharing
cannot make a private run public, and no new public/share-token endpoints exist.

Links contain view metadata (run IDs, project and metric names), but no API keys,
metric values, or authentication tokens. Settings are in the URL fragment, which
is not sent as part of the HTTP request. Treat links as internal information.
Same-tab OAuth login preserves pending view settings through session storage;
if browser storage is disabled, reopen the shared link after signing in.

Use **PNG** on a plot to download a static image of its current axes, smoothing,
zoom, and selected runs. The image includes a legend, timestamp, sampling status,
and omitted-axis count; hover indicators are excluded. All-session exports include
session colors and run line-style legends. A pinned highlight is reflected in the
image and caption; highlights are local inspection aids, not shared-link settings.
Sending that image outside
Open Train discloses the plotted information and is not protected by Open Train
account permissions. Zooming/exporting does not fetch higher-resolution data;
download history via MCP when investigating downsampled points.

The browser acceptance test covers project reloads, independent axes, maximized
plots, PNG export, fresh-browser link restoration, reader and denied access,
OAuth-return state, malformed links, and mobile layout:

```bash
uv run playwright install chromium
OPEN_TRAIN_BROWSER_TESTS=1 uv run pytest tests/test_workspace_ui.py -q
```

### Agent-generated chart links

The `opentrain-mcp` tool **get_plot_link** builds the same version-1 share link.
Ask your agent to resolve run names using `list_runs`, check the exact metric key,
and request `run_uids`, `key`, and `axis="_timestamp"` for a wall-time comparison.
The result's `plot_url` selects those runs and that exact metric in the dashboard;
it also supports smoothing, linear/log scale and latest/all session views. It
does not grant access or put an API key in the URL. `_runtime` is logged elapsed
runtime, not absolute wall time, and can reset between sessions.

The cross-repository browser test requires the updated MCP checkout and Chromium:

```bash
OPEN_TRAIN_BROWSER_TESTS=1 OPENTRAIN_MCP_DIR=/absolute/path/to/opentrain-mcp \
  uv run pytest tests/test_mcp_plot_link_ui.py -q
```
