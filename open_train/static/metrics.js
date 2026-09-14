// Namespace navigation, bounded progressive loading, and revision-aware series caching.
const metricView = {
  cache: new Map(),
  open: new Map(),
  allOpen: null,
  signature: null,
  observer: null,
  controller: null,
};

function runSessions(run) {
  const sessions = run.sessions ?? run.config?.tensorboard_import?.sessions;
  return (Array.isArray(sessions) ? sessions : []).map((session, i) => ({
    ...session,
    label: `S${i + 1}`,
    index: i,
  }));
}

function sessionStart(session, axis, series) {
  if (series?.session_starts && session.source?.startsWith("sdk"))
    return series.session_starts[session.id]?.x;
  if (axis === "_timestamp") return session.first_wall_time ?? session.started;
  if (axis === "_step") return session.first_step;
  return session.axes?.[axis]?.first;
}

function sessionDescription(s) {
  const start = s.started ?? s.first_wall_time;
  const end = s.ended ?? s.last_wall_time;
  return `${s.label} · ${s.source || "tensorboard"}${s.inferred ? " (inferred)" : ""} · ${s.state || "imported"}\n${Number.isFinite(start) ? new Date(start * 1000).toLocaleString() : "Unknown start"} — ${Number.isFinite(end) ? new Date(end * 1000).toLocaleString() : "end not recorded"}\nSDK/event steps ${format(s.first_step)}–${format(s.last_step)} · ${s.records ?? s.events ?? 0} records${s.exitcode != null ? ` · exit ${s.exitcode}` : ""}`;
}

function sessionForPoint(run, timestamp, sessions = runSessions(run)) {
  if (!Number.isFinite(timestamp)) return null;
  const matches = sessions.filter(
    (s) =>
      Number.isFinite(s.first_wall_time) &&
      Number.isFinite(s.last_wall_time) &&
      timestamp >= s.first_wall_time &&
      timestamp <= s.last_wall_time,
  );
  // Never guess in an overlapping time range or when provenance is absent.
  return matches.length === 1 ? matches[0] : null;
}

function renderSessions(content, run) {
  const sessions = runSessions(run);
  content.append(el("p", `Run ID: ${run.name}`, "muted"));
  if (run.ingestion) {
    content.append(el("h3", "Data provenance & recovery"));
    for (const source of run.ingestion.sources || [])
      content.append(
        el(
          "p",
          `${source.source} · session ${source.session || "unknown"} · ${source.active_records}/${source.records} active records`,
        ),
      );
    for (const batch of run.ingestion.batches || [])
      content.append(
        el(
          "p",
          `Batch ${batch.id}: ${batch.status} · ${batch.records}${batch.expected === null ? "" : `/${batch.expected}`} received · ${batch.active_records} active · ${batch.source}`,
        ),
      );
    content.append(el("p", run.ingestion.warning, "muted"));
  }
  if (!sessions.length) {
    content.append(
      el(
        "p",
        "No session provenance was recorded for this run. Resuming with the same W&B run ID keeps one logical run; separate IDs remain separate runs.",
      ),
    );
    return;
  }
  content.append(
    el(
      "p",
      `${sessions.length} recorded source sessions. ${run.session_caveat || "Inferred segments are not a complete count of training jobs."} Dashed chart markers indicate the first recorded point on the chosen axis.`,
      "session-note",
    ),
  );
  for (const s of sessions) {
    const item = el("article", undefined, "session-card");
    item.append(
      el(
        "strong",
        `${s.label} · ${s.host || s.directory?.split("/").pop() || s.id || "Session"}`,
      ),
    );
    item.append(el("p", sessionDescription(s)));
    for (const [key, range] of Object.entries(s.axes || {}))
      if (key.endsWith("global_step"))
        item.append(
          el("p", `${key}: ${format(range.first)}–${format(range.last)}`),
        );
    if (s.warning) item.append(el("p", s.warning, "session-note"));
    if (Number.isFinite(s.first_wall_time))
      item.append(
        el(
          "small",
          `${new Date(s.first_wall_time * 1000).toLocaleString()} — ${new Date(s.last_wall_time * 1000).toLocaleString()}`,
        ),
      );
    content.append(item);
  }
}

function initMetrics() {
  try {
    const saved = JSON.parse(
      localStorage.getItem("open-train-metric-groups") || "[]",
    );
    metricView.open = new Map(saved);
  } catch {
    /* A corrupt local preference must not prevent chart loading. */
  }
  let timer;
  $("#metric-search").oninput = () => {
    state.sharedMetric = null;
    clearTimeout(timer);
    timer = setTimeout(() => renderCharts().catch(showError), 150);
  };
  for (const [id, open] of [
    ["expand-metrics", true],
    ["collapse-metrics", false],
  ])
    $("#" + id).onclick = () => {
      metricView.allOpen = open;
      metricView.open.clear();
      metricView.signature = null;
      renderCharts().catch(showError);
    };
  $("#show-sessions").onchange = () => renderCharts().catch(showError);
}

async function renderCharts() {
  // Filtering/pagination only navigate the run list; selection spans pages.
  const selected = [...state.selected]
    .map((uid) => state.runs.find((r) => r.uid === uid))
    .filter(Boolean);
  $("#selection-count").textContent = `${selected.length} runs selected`;
  for (const id of ["chart-grid", "metric-toolbar", "comparison-legend"])
    $("#" + id).hidden = state.tab !== "charts";
  const search = $("#metric-search").value.trim().toLowerCase();
  const axis = $("#x-axis").value;
  const signature = JSON.stringify([
    selected.map((r) => r.uid),
    state.tab,
    search,
    axis,
    state.sharedMetric,
    $("#show-sessions").checked,
  ]);
  if (
    signature === metricView.signature &&
    !(metricView.catalogEmpty && !search && selected.some((r) => r.has_metrics))
  ) {
    await refreshOpenPlots();
    return;
  }
  metricView.signature = signature;
  const generation = ++state.generation;
  cancelLivePlots();
  metricView.observer?.disconnect();
  metricView.controller?.abort();
  metricView.controller = new AbortController();
  const signal = metricView.controller.signal;
  if (state.tab !== "charts") return;
  $("#comparison-legend").replaceChildren();
  $("#chart-grid").replaceChildren(
    el(
      "p",
      selected.length
        ? "Loading metric groups…"
        : "Select runs to compare their metrics.",
      "chart-placeholder",
    ),
  );
  $("#metric-count").textContent = "";
  if (!selected.length) return;
  const displayed = selected.slice(0, 12);
  let details;
  try {
    details = await Promise.all(displayed.map((r) => detail(r.uid)));
  } catch (error) {
    if (generation === state.generation) metricView.signature = null;
    throw error;
  }
  if (generation !== state.generation) return;
  const runs = displayed.map((r, i) => ({
    ...r,
    config: details[i].config,
    sessions: details[i].sessions,
  }));
  const usedColors = new Set();
  let colorsChanged = false;
  for (const run of runs) {
    if (usedColors.has(color(run.uid))) {
      runColors.set(
        run.uid,
        colors.find((c) => !usedColors.has(c)),
      );
      colorsChanged = true;
    }
    usedColors.add(color(run.uid));
  }
  if (colorsChanged) renderRuns();
  const keys = [
    ...new Set(
      details.flatMap((d) =>
        d.keys
          .filter((k) => k.stream === "history" && !k.key.startsWith("_"))
          .map((k) => k.key),
      ),
    ),
  ].sort();
  metricView.catalogEmpty = keys.length === 0;
  for (const key of new Set(
    details.flatMap((d) =>
      d.keys.filter((k) => k.stream === "history").map((k) => k.key),
    ),
  ))
    if (![...$("#x-axis").options].some((o) => o.value === key))
      $("#x-axis").add(new Option(key, key));
  const filtered = keys.filter((k) =>
    state.sharedMetric
      ? k === state.sharedMetric
      : k.toLowerCase().includes(search),
  );
  $("#metric-count").textContent =
    `${filtered.length} of ${keys.length} metrics · charts load on demand`;
  for (const run of runs) {
    const item = el("button", undefined, "comparison-item");
    item.dataset.uid = run.uid;
    const dot = el("i", undefined, "run-color");
    dot.style.background = color(run.uid);
    item.append(
      dot,
      el("span", run.display_name),
      el(
        "small",
        `${run.name} · ${runSessions(run).length || "unknown"} sessions`,
      ),
    );
    item.onclick = () => openDetail(run.uid, "sessions").catch(showError);
    $("#comparison-legend").append(item);
  }
  if (selected.length > 12)
    $("#comparison-legend").append(
      el(
        "p",
        "Comparing the first 12 selected runs. Narrow the selection to compare others.",
        "muted",
      ),
    );
  const root = { groups: new Map(), keys: [], count: 0 };
  for (const key of filtered) {
    let current = root;
    current.count++;
    const parts = key.split("/");
    const groups = parts.length > 1 ? parts.slice(0, -1) : ["Other"];
    for (const part of groups) {
      if (!current.groups.has(part))
        current.groups.set(part, { groups: new Map(), keys: [], count: 0 });
      current = current.groups.get(part);
      current.count++;
    }
    current.keys.push(key);
  }
  const pending = new Set();
  let timer,
    active = 0;
  const cacheKey = (run, key) =>
    JSON.stringify([
      run.uid,
      run.updated,
      key,
      plotPreference(key).axis || axis,
    ]);
  function paint(slot) {
    const key = slot.dataset.metric;
    const selectedAxis = plotPreference(key).axis || axis;
    const series = runs.map((run) => ({
      run,
      ...metricView.cache.get(cacheKey(run, key)),
    }));
    const resolved = [
      ...new Set(
        series
          .filter((s) => s.total || s.missing_axis)
          .map((s) => s.axis || selectedAxis),
      ),
    ];
    if (selectedAxis === "auto" && resolved.length > 1) {
      const card = chart(
        key,
        series.map((s) => ({ ...s, points: [] })),
        "auto",
      );
      card.append(
        el(
          "p",
          `Runs define different axes (${resolved.join(", ")}). Select an explicit X axis on this plot to compare them.`,
        ),
      );
      slot.replaceWith(card);
      return;
    }
    slot.replaceWith(
      chart(
        key,
        series,
        selectedAxis === "auto" ? resolved[0] || "_step" : selectedAxis,
      ),
    );
    scheduleLivePlots();
  }
  async function load(slots) {
    const missing = slots.filter((slot) =>
      runs.some(
        (run) => !metricView.cache.has(cacheKey(run, slot.dataset.metric)),
      ),
    );
    if (missing.length) {
      const keys = missing.map((slot) => slot.dataset.metric);
      const response = await api("/api/series", {
        method: "POST",
        signal,
        body: JSON.stringify({
          runs: runs.map((r) => r.uid),
          keys,
          x: plotPreference(keys[0]).axis || axis,
          limit: 800,
        }),
      });
      if (generation !== state.generation) return;
      for (const run of runs)
        for (const key of keys)
          metricView.cache.set(
            cacheKey(run, key),
            response.series[run.uid][key],
          );
    }
    if (generation !== state.generation) return;
    for (const slot of slots) if (slot.isConnected) paint(slot);
    while (metricView.cache.size > 512)
      metricView.cache.delete(metricView.cache.keys().next().value);
  }
  function pump() {
    if (generation !== state.generation) return;
    while (active < 3 && pending.size) {
      const first = pending.values().next().value;
      const selectedAxis = plotPreference(first.dataset.metric).axis || axis;
      const slots = [...pending]
        .filter(
          (slot) =>
            (plotPreference(slot.dataset.metric).axis || axis) === selectedAxis,
        )
        .slice(0, 6);
      slots.forEach((slot) => pending.delete(slot));
      active++;
      load(slots)
        .catch((error) => {
          if (error.name === "AbortError" || generation !== state.generation)
            return;
          for (const slot of slots) {
            const retry = el("button", "Retry loading chart");
            retry.onclick = () => {
              pending.add(slot);
              pump();
            };
            slot.replaceChildren(
              el("strong", slot.dataset.metric),
              el("p", error.message),
              retry,
            );
          }
        })
        .finally(() => {
          active--;
          pump();
        });
    }
  }
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries)
        if (entry.isIntersecting) {
          observer.unobserve(entry.target);
          pending.add(entry.target);
        }
      clearTimeout(timer);
      timer = setTimeout(pump, 0);
    },
    { rootMargin: "250px" },
  );
  metricView.observer = observer;
  const slots = [];
  function build(node, path = []) {
    const fragment = document.createDocumentFragment();
    if (node.keys.length) {
      const grid = el("div", undefined, "chart-grid");
      for (const key of node.keys) {
        const slot = el("article", undefined, "chart chart-loading");
        slot.dataset.metric = key;
        slot.append(el("strong", key), el("p", "Loading chart…", "muted"));
        slots.push(slot);
        grid.append(slot);
      }
      fragment.append(grid);
    }
    const order = (name) =>
      name === "train" ? "0" : name === "eval" ? "1" : "2" + name;
    for (const [name, child] of [...node.groups].sort((a, b) =>
      order(a[0]).localeCompare(order(b[0])),
    )) {
      const group = el("details", undefined, "metric-group"),
        next = [...path, name],
        id = next.join("/");
      group.dataset.group = id;
      group.open =
        Boolean(search) ||
        (metricView.open.get(id) ?? metricView.allOpen ?? path.length === 0);
      const heading = el("summary");
      heading.append(
        document.createTextNode(name),
        el("span", `${child.count} metrics`, "muted"),
      );
      group.append(heading, build(child, next));
      group.addEventListener("toggle", () => {
        if (group.open) scheduleLivePlots();
        if (!search && generation === state.generation) {
          metricView.open.set(id, group.open);
          try {
            localStorage.setItem(
              "open-train-metric-groups",
              JSON.stringify([...metricView.open]),
            );
          } catch {
            /* Storage may be disabled. */
          }
        }
      });
      fragment.append(group);
    }
    return fragment;
  }
  $("#chart-grid").replaceChildren(
    filtered.length
      ? build(root)
      : el(
          "p",
          keys.length
            ? "No metrics match your search."
            : "No scalar metrics recorded. Check Files & media for original data.",
          "chart-placeholder",
        ),
  );
  for (const slot of slots) observer.observe(slot);
}
