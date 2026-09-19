// Share view state, never credentials or access grants. All data still uses the
// authenticated API. URL fragments are not sent in HTTP requests/referrers.
function validateSharedView(value) {
  const text = (s, max = 500) => typeof s === "string" && s.length <= max;
  if (
    !value ||
    value.version !== 1 ||
    !Array.isArray(value.runs) ||
    value.runs.length > 12 ||
    !value.runs.every((s) => text(s, 200) && /^[\w-]+$/.test(s)) ||
    !text(value.project) ||
    !text(value.axis) ||
    !text(value.search) ||
    !(value.category === undefined || text(value.category)) ||
    !(value.metric === null || text(value.metric)) ||
    !Array.isArray(value.plots) ||
    value.plots.length > 512 ||
    !Array.isArray(value.groups) ||
    value.groups.length > 1000
  )
    throw Error("Invalid or unsupported shared view.");
  const plots = value.plots.map((p) => {
    if (
      !Array.isArray(p) ||
      p.length !== 2 ||
      !text(p[0]) ||
      !p[1] ||
      !text(p[1].axis) ||
      ![undefined, "linear", "log"].includes(p[1].scale) ||
      !Number.isFinite(p[1].smoothing) ||
      p[1].smoothing < 0 ||
      p[1].smoothing > 0.95 ||
      !Array.isArray(p[1].domains) ||
      p[1].domains.length > 100
    )
      throw Error("Invalid shared plot settings.");
    const domains = p[1].domains.map((d) => {
      if (
        !Array.isArray(d) ||
        d.length !== 2 ||
        !text(d[0]) ||
        !Array.isArray(d[1]) ||
        d[1].length !== 4 ||
        !d[1].every(Number.isFinite) ||
        d[1][0] >= d[1][1] ||
        d[1][2] >= d[1][3]
      )
        throw Error("Invalid shared zoom bounds.");
      return [d[0], [...d[1]]];
    });
    return [
      p[0],
      {
        axis: p[1].axis,
        smoothing: p[1].smoothing,
        scale: p[1].scale || "linear",
        domains,
      },
    ];
  });
  const groups = value.groups.map((g) => {
    if (
      !Array.isArray(g) ||
      g.length !== 2 ||
      !text(g[0]) ||
      typeof g[1] !== "boolean"
    )
      throw Error("Invalid shared metric groups.");
    return [g[0], g[1]];
  });
  const sharedColors =
    Array.isArray(value.colors) && value.colors.length <= 12
      ? value.colors.filter(
          (c) =>
            Array.isArray(c) &&
            c.length === 2 &&
            value.runs.includes(c[0]) &&
            colors.includes(c[1]),
        )
      : [];
  return {
    version: 1,
    runs: [...new Set(value.runs)],
    project: value.project,
    axis: value.axis,
    search: value.search,
    category: value.category || "",
    metric: value.metric,
    plots,
    groups,
    colors: sharedColors,
    sessions: value.sessions !== false,
    allOpen: typeof value.allOpen === "boolean" ? value.allOpen : null,
  };
}

function viewNotice(message) {
  $("#shared-view-notice").textContent = message;
  $("#shared-view-notice").hidden = false;
}

function initWorkspace() {
  $("#project-filter").onchange = () => {
    const project = $("#project-filter").value;
    state.selected.clear();
    metricView.category = "";
    metricView.limit = 24;
    state.sharedMetric = null;
    $("#metric-search").value = "";
    $("#shared-view-notice").hidden = true;
    state.runPage = 0;
    for (const id of ["search", "state-filter", "source-filter"])
      $("#" + id).value = "";
    $("#selected-filter").checked = false;
    const slash = project.indexOf("/");
    history.replaceState(
      null,
      "",
      project
        ? `/${encodeURIComponent(project.slice(0, slash))}/${encodeURIComponent(project.slice(slash + 1))}`
        : "/",
    );
    renderRuns();
    renderCharts().catch(showError);
  };
  $("#share-view").onclick = () => shareView();
  try {
    let encoded = location.hash.startsWith("#view=")
      ? location.hash.slice(6)
      : null;
    if (encoded === null)
      encoded = sessionStorage.getItem("open-train-pending-view");
    if (encoded !== null) {
      if (encoded.length > 64000) throw Error("Shared view is too large.");
      state.sharedView = validateSharedView(
        JSON.parse(decodeURIComponent(encoded)),
      );
      state.sharedOverrides = true;
      // OAuth redirects to /; retain this tab's view until authenticated loading.
      try {
        sessionStorage.setItem(
          "open-train-pending-view",
          encodeURIComponent(JSON.stringify(state.sharedView)),
        );
      } catch {
        /* The current-page view still works without optional storage. */
      }
    }
  } catch (error) {
    state.sharedView = null;
    try {
      sessionStorage.removeItem("open-train-pending-view");
    } catch {
      /* Optional storage. */
    }
    viewNotice(error.message);
  }
  window.addEventListener("hashchange", () => {
    if (location.hash.startsWith("#view=")) location.reload();
  });
}

function restoreSharedView() {
  const view = state.sharedView;
  const available = new Set(state.runs.map((r) => r.uid));
  state.selected = new Set(view.runs.filter((id) => available.has(id)));
  state.sharedMetric = view.metric;
  $("#metric-search").value = view.metric || view.search;
  if (![...$("#x-axis").options].some((o) => o.value === view.axis))
    $("#x-axis").add(new Option(view.axis, view.axis));
  $("#x-axis").value = view.axis;
  $("#show-sessions").checked = view.sessions;
  $("#project-filter").value = view.project;
  metricView.open = new Map(view.groups);
  metricView.category = view.category;
  metricView.limit = 24;
  metricView.allOpen = view.allOpen;
  runColors.clear();
  for (const [id, color] of view.colors) runColors.set(id, color);
  plotPreferences.clear();
  for (const [key, p] of view.plots)
    plotPreferences.set(key, { ...p, domains: new Map(p.domains) });
  const missing = view.runs.length - state.selected.size;
  viewNotice(
    missing
      ? `Shared view: ${missing} selected run(s) are unavailable or you do not have access. Showing only accessible runs; ask the workspace owner for access.`
      : "Shared view restored. This is live data, not a frozen snapshot. Access permissions are unchanged.",
  );
  try {
    sessionStorage.removeItem("open-train-pending-view");
  } catch {
    /* Optional storage. */
  }
}

function shareView(metric = null) {
  try {
    if (!state.selected.size)
      throw Error("Select at least one run before sharing.");
    const view = validateSharedView({
      version: 1,
      runs: [...state.selected],
      project: $("#project-filter").value,
      axis: $("#x-axis").value,
      search: $("#metric-search").value,
      category: metricView.category,
      metric: metric || state.sharedMetric || null,
      sessions: $("#show-sessions").checked,
      colors: [...state.selected].map((id) => [id, color(id)]),
      plots: [...plotPreferences]
        .filter(([key]) => !metric || key === metric)
        .map(([key, p]) => [
          key,
          {
            axis: p.axis,
            smoothing: p.smoothing,
            scale: p.scale || "linear",
            domains: [...p.domains],
          },
        ]),
      groups: [...metricView.open],
      allOpen: metricView.allOpen,
    });
    const fragment = encodeURIComponent(JSON.stringify(view));
    if (fragment.length > 64000)
      throw Error("This view is too large for a link. Share one plot instead.");
    const link = `${location.origin}/#view=${fragment}`;
    const dialog = makeDialog(metric ? "Share plot" : "Share view");
    dialog.classList.add("share-dialog");
    dialog.append(
      el(
        "p",
        "Only teammates who already have access can open the data. This link does not grant access or include your API key. Add teammates as readers in Account & access → Workspace membership.",
      ),
    );
    dialog.append(
      el(
        "p",
        "The link preserves run selection, metrics, axes, smoothing and zoom. Data stays live; download a PNG for a static snapshot. Links contain run IDs and metric names, so treat them as internal information.",
      ),
    );
    const input = el("textarea");
    input.readOnly = true;
    input.value = link;
    input.setAttribute("aria-label", "Share link");
    const copy = el("button", "Copy link", "primary");
    const status = el("p", "", "muted");
    status.setAttribute("role", "status");
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(link);
        status.textContent = "Link copied.";
      } catch {
        input.focus();
        input.select();
        status.textContent = "Copy the selected link manually (Ctrl/Cmd+C).";
      }
    };
    dialog.append(input, copy, status);
  } catch (error) {
    showError(error);
  }
}

async function loadPlot(key) {
  const selected = [...state.selected]
    .map((uid) => state.runs.find((r) => r.uid === uid))
    .filter(Boolean)
    .slice(0, 12);
  const axis = plotPreference(key).axis || $("#x-axis").value;
  const response = await api("/api/series", {
    method: "POST",
    body: JSON.stringify({
      runs: selected.map((r) => r.uid),
      keys: [key],
      x: axis,
      limit: 800,
    }),
  });
  const details = await Promise.all(selected.map((r) => plotDetail(r.uid)));
  const series = selected.map((r, i) => ({
    run: { ...r, sessions: details[i].sessions },
    ...response.series[r.uid][key],
  }));
  const axes = [
    ...new Set(
      series
        .filter((s) => s.total || s.missing_axis)
        .map((s) => s.axis || axis),
    ),
  ];
  if (axis === "auto" && axes.length > 1) {
    const card = chart(
      key,
      series.map((s) => ({ ...s, points: [] })),
      axis,
      true,
    );
    card.append(
      el(
        "p",
        "Runs define different axes. Select an explicit X axis on this plot.",
      ),
    );
    return card;
  }
  return chart(key, series, axis === "auto" ? axes[0] || "_step" : axis, true);
}

async function exportPlot(card, key, axis, series) {
  const smoothing = plotPreference(key).smoothing;
  const scale = plotPreference(key).scale || "linear";
  const original = $("svg", card);
  if (!original) throw Error("There are no plotted values to export.");
  const svg = original.cloneNode(true);
  svg.setAttribute("font-family", "Arial, sans-serif");
  for (const hover of svg.querySelectorAll(
    ".hover-point, .hover-guide, .zoom-brush",
  ))
    hover.remove();
  const box = original.viewBox.baseVal;
  const width = 1280,
    plotHeight = Math.round((box.height * (width - 80)) / box.width);
  svg.setAttribute("width", width - 80);
  svg.setAttribute("height", plotHeight);
  const url = URL.createObjectURL(
    new Blob([new XMLSerializer().serializeToString(svg)], {
      type: "image/svg+xml",
    }),
  );
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = plotHeight + 160 + series.length * 26;
    const context = canvas.getContext("2d");
    context.fillStyle = "white";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#172033";
    context.font = "bold 22px Arial";
    context.fillText(key, 40, 32, width - 80);
    context.font = "14px Arial";
    context.fillText(
      `X: ${axis} · Y: ${scale} · EMA ${smoothing} · ${series.some((s) => s.sampled) ? "Sampled (min/max)" : "All returned points"} · ${new Date().toISOString()}`,
      40,
      58,
      width - 80,
    );
    const omitted = series.reduce((n, s) => n + (s.missing_axis || 0), 0);
    context.fillText(
      `${omitted} records omitted for missing/unverified axis pairing.${scale === "log" ? " Nonpositive values omitted on log scale." : ""} Export reflects current zoom.`,
      40,
      80,
    );
    context.drawImage(image, 40, 95);
    series.forEach((s, i) => {
      context.fillStyle = color(s.run.uid);
      context.fillRect(40, plotHeight + 122 + i * 26, 22, 4);
      context.fillStyle = "#172033";
      context.fillText(
        `${s.run.display_name} (${s.points.length}/${s.total} returned points)`,
        72,
        plotHeight + 130 + i * 26,
        width - 110,
      );
    });
    const blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/png"),
    );
    if (!blob) throw Error("PNG export failed.");
    const download = URL.createObjectURL(blob);
    const anchor = el("a");
    anchor.href = download;
    anchor.download = `${key.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 100) || "plot"}.png`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(download), 1000);
  } finally {
    URL.revokeObjectURL(url);
  }
}
