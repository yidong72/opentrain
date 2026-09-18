// Background data updates never rebuild the dashboard layout or its controls.
const livePlots = { flight: null, controller: null, timer: null };

function cancelLivePlots() {
  livePlots.controller?.abort();
}

function scheduleLivePlots() {
  clearTimeout(livePlots.timer);
  livePlots.timer = setTimeout(() => refreshOpenPlots().catch(showError), 150);
}

function openedPlot(card) {
  if (!card.isConnected || !card.updateSeries || !card.getClientRects().length)
    return false;
  for (let parent = card.parentElement; parent; parent = parent.parentElement)
    if (parent.hidden || (parent.tagName === "DETAILS" && !parent.open))
      return false;
  const modal = $(".plot-dialog[open]");
  if (modal && !modal.contains(card)) return false;
  if (!modal && (state.view !== "experiments" || state.tab !== "charts"))
    return false;
  const box = card.getBoundingClientRect();
  return (
    box.bottom > 0 &&
    box.top < window.innerHeight &&
    box.right > 0 &&
    box.left < window.innerWidth
  );
}

async function refreshOpenPlots() {
  if (livePlots.flight) return livePlots.flight;
  if (document.hidden || $("#auth").open || $("#detail").open) return;
  const cards = $$("#chart-grid .chart, .plot-dialog[open] .chart").filter(
    openedPlot,
  );
  const current = new Map(state.runs.map((r) => [r.uid, r]));
  const dirty = cards.filter((card) =>
    card.liveSeries.some(
      (s) => current.get(s.run.uid)?.updated !== s.run.updated,
    ),
  );
  if (!dirty.length) return;
  const generation = state.generation;
  const controller = new AbortController();
  livePlots.controller = controller;
  const valid = () =>
    !controller.signal.aborted && generation === state.generation;
  const run = async () => {
    // Fetch provenance only for changed runs used by an actually visible plot.
    const ids = [
      ...new Set(
        dirty.flatMap((c) =>
          c.liveSeries
            .filter((s) => current.get(s.run.uid)?.updated !== s.run.updated)
            .map((s) => s.run.uid),
        ),
      ),
    ];
    const details = new Map(
      await Promise.all(
        ids.map(async (uid) => [
          uid,
          await api(`/api/runs/${uid}/sessions?compact=true`, {
            signal: controller.signal,
          }),
        ]),
      ),
    );
    if (!valid()) return;
    for (const item of $$("#comparison-legend .comparison-item")) {
      const info = details.get(item.dataset.uid);
      if (info)
        $("small", item).textContent =
          `${current.get(item.dataset.uid).name} · ${info.sessions.length || "unknown"} sessions`;
    }
    const groups = new Map();
    for (const card of dirty.filter(openedPlot)) {
      const axis = card.requestedAxis;
      if (!groups.has(axis)) groups.set(axis, []);
      groups.get(axis).push(card);
    }
    const jobs = [];
    for (const [axis, group] of groups) {
      const keys = [...new Set(group.map((c) => c.dataset.metric))];
      for (let i = 0; i < keys.length; i += 6)
        jobs.push({ axis, keys: keys.slice(i, i + 6), group });
    }
    async function worker() {
      while (jobs.length && valid()) {
        const { axis, keys, group } = jobs.shift();
        const targets = group.filter(
          (c) => keys.includes(c.dataset.metric) && openedPlot(c),
        );
        const needed = [
          ...new Set(
            targets.flatMap((c) =>
              c.liveSeries
                .filter((s) => ids.includes(s.run.uid))
                .map((s) => s.run.uid),
            ),
          ),
        ];
        if (!needed.length) continue;
        const requestedKeys = [
          ...new Set(targets.map((c) => c.dataset.metric)),
        ];
        const response = await api("/api/series", {
          method: "POST",
          signal: controller.signal,
          body: JSON.stringify({
            runs: needed,
            keys: requestedKeys,
            x: axis,
            limit: 800,
          }),
        });
        if (!valid()) return;
        for (const card of targets) {
          if (!openedPlot(card) || card.requestedAxis !== axis) continue;
          const next = card.liveSeries.map((s) => {
            const update = response.series[s.run.uid]?.[card.dataset.metric];
            return update
              ? {
                  ...update,
                  run: {
                    ...current.get(s.run.uid),
                    config: s.run.config,
                    sessions: details.get(s.run.uid).sessions,
                  },
                }
              : s;
          });
          const axes = [
            ...new Set(
              next
                .filter((s) => s.total || s.missing_axis)
                .map((s) => s.axis || axis),
            ),
          ];
          const resolved =
            axis === "auto"
              ? axes.length > 1
                ? "auto"
                : axes[0] || "_step"
              : axis;
          card.updateSeries(
            resolved === "auto"
              ? next.map((s) => ({ ...s, points: [] }))
              : next,
            resolved,
          );
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(3, jobs.length) }, worker));
  };
  const flight = run()
    .catch((error) => {
      if (valid() && error.name !== "AbortError")
        $("#connection").textContent =
          "Live update delayed · keeping plots · retrying";
    })
    .finally(() => {
      if (livePlots.flight === flight) livePlots.flight = null;
    });
  livePlots.flight = flight;
  return flight;
}

document.addEventListener("scroll", scheduleLivePlots, {
  capture: true,
  passive: true,
});
document.addEventListener("visibilitychange", scheduleLivePlots);
window.addEventListener("resize", scheduleLivePlots);
