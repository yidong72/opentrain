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

Card values and expanded summaries are computed from all original records with
valid axis pairings, not from sampled or smoothed curves. **Last** means the latest
timestamp (record order breaks ties), even if resumed training rewinds the X axis.
**Δ** compares that value with the preceding non-null record, not the previous
poll. The table also shows min, max, mean and count. Endpoint dots and labels
refer to the rightmost displayed curve point, which may be smoothed. Hover shows
the corresponding raw value. Summary statistics include the whole paired series,
not just the zoomed region.

Use **Share view** for the current metric view, or **Share** on a plot for exactly
that metric. Copy the link in the dialog. It preserves selected run IDs, colors,
project, metric category/selection/search, group expansion, default and per-plot axes,
Y scale, smoothing, zoom, and session-marker visibility. The recipient's old local plot
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
and omitted-axis count; hover indicators are excluded. Sending that image outside
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
