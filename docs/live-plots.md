# Live plots without dashboard refreshes

The five-second poll updates run status and visible plots in place. It does not
rebuild the metric groups, chart cards, controls or SVG roots when a run's update
timestamp changes. Existing runs retain their sidebar order.

- Only visible plots in open metric groups fetch fresh series. Scrolling to an
  existing plot or reopening its group brings it up to date.
- When a plot is maximized, background charts pause and only the maximized plot
  receives updates. Zoom, smoothing, axis choice, and the modal remain intact.
- While hovering a plot, dragging, or editing its controls, the latest response
  waits to be drawn. Leaving the plot applies it. Tooltip numbers and highlighted
  points therefore stay stable while being inspected.
- Failed requests keep the last successful plot and retry on the next poll.
  Updates replace per-run data snapshots rather than appending duplicate points.
  Selection/layout changes cancel old requests and ignore late results.
- Live updates fetch lightweight `/api/runs/{uid}/sessions` provenance instead
  of repeatedly downloading the full config and metric/file catalogs. The
  endpoint uses the same run permissions as run details.

The toolbar's refresh button deliberately reloads the metric list. Use it to
discover newly introduced metric names; routine telemetry does not rebuild that
list while you are reading. New runs with no metrics yet load their initial
metric groups when the first metrics become available.

Browser regression coverage: `OPEN_TRAIN_BROWSER_TESTS=1 uv run pytest
tests/test_live_ui.py tests/test_workspace_ui.py`. Tests verify mounted node
identity, scroll/zoom/smoothing preservation, visible-only requests, hover
stability, retry behavior, collapsed groups, and maximized plots.
