// Namespace navigation, bounded progressive loading, and revision-aware series caching.
const metricView = {
  cache: new Map(),
  open: new Map(),
  allOpen: null,
  signature: null,
  observer: null,
  controller: null,
  details: new Map(),
  category: "",
  limit: 24,
};

async function plotDetail(uid, signal) {
  const revision = state.runs.find((r) => r.uid === uid)?.updated;
  let cached = metricView.details.get(uid);
  if (!cached || cached.revision !== revision || cached.signal?.aborted) {
    cached = { revision, signal };
    cached.promise = api(`/api/runs/${uid}/plots`, { signal }).catch(
      (error) => {
        if (metricView.details.get(uid) === cached)
          metricView.details.delete(uid);
        throw error;
      },
    );
    metricView.details.set(uid, cached);
    while (metricView.details.size > 64)
      metricView.details.delete(metricView.details.keys().next().value);
  }
  return cached.promise;
}

function runSessions(run) {
  const sessions = run.sessions ?? run.config?.tensorboard_import?.sessions;
  return (Array.isArray(sessions) ? sessions : []).map((session, i) => ({
    ...session,
    label: `S${i + 1}`,
    index: i,
  }));
}

function sessionStart(session, axis, series) {
  const boundary = series?.trajectory?.boundaries?.find(
    (b) => b.id === session.id,
  );
  if (
    Number.isFinite(boundary?.step) &&
    (axis === boundary.axis ||
      (axis.endsWith("global_step") && boundary.axis.endsWith("global_step")))
  )
    return boundary.step;
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

function sessionCurves(series) {
  const sessions = runSessions(series.run),
    groups = new Map();
  series.points.forEach((point, i) => {
    const id = series.point_sessions?.[i];
    const session =
      id !== undefined
        ? sessions.find(
            (s) => s.id === id || s.directory === id || s.session === id,
          ) || { id, label: id ? `Session ${id}` : "Unattributed", index: null }
        : sessionForPoint(series.run, series.timestamps?.[i], sessions);
    const groupKey = session?.id || session?.label || "unknown";
    if (!groups.has(groupKey))
      groups.set(groupKey, { ...series, session, points: [], timestamps: [] });
    groups.get(groupKey).points.push(point);
    groups.get(groupKey).timestamps.push(series.timestamps?.[i]);
  });
  return [...groups.values()];
}

function displayCurves(series, axis, smoothing, logarithmic) {
  let curves = sessionCurves(series);
  const info = series.trajectory;
  const trainingAxis =
    axis === info?.coordinate_axis ||
    (axis.endsWith("global_step") &&
      info?.coordinate_axis?.endsWith("global_step"));
  if (info?.stitch_sessions && trainingAxis && curves.length > 1) {
    const order = new Map(info.boundaries.map((b, i) => [b.id, i]));
    const ordered = [...curves].sort(
      (a, b) => order.get(a.session?.id) - order.get(b.session?.id),
    );
    // Custom/nonmonotone axes, equal-X branches and uncertain provenance stay
    // separate. Never draw a line backwards through a checkpoint rewind.
    if (
      ordered.every(
        (s, i) =>
          order.has(s.session?.id) &&
          s.points.every((p, j) => !j || p[0] >= s.points[j - 1][0]) &&
          (!i || s.points[0][0] > ordered[i - 1].points.at(-1)[0]),
      )
    ) {
      curves = [
        {
          ...series,
          session: null,
          continuous: true,
          points: ordered.flatMap((s) => s.points),
          timestamps: ordered.flatMap((s) => s.timestamps),
          sessions: ordered.flatMap((s) => s.points.map(() => s.session)),
        },
      ];
    }
  }
  function smooth(s, anchor = null) {
    let last = anchor?.y ?? null;
    const displayPoints = s.points.map(([x, raw], i) => {
      const valid = Number.isFinite(raw) && (!logarithmic || raw > 0);
      last = valid
        ? last === null
          ? raw
          : last * smoothing + raw * (1 - smoothing)
        : null;
      return {
        x,
        y: last,
        raw,
        session: s.sessions?.[i] || s.session,
        timestamp: s.timestamps?.[i],
      };
    });
    return { ...s, displayPoints, smoothingAnchor: anchor };
  }
  if (
    info?.view === "all" &&
    info.resume_smoothing &&
    trainingAxis &&
    smoothing > 0
  ) {
    const boundaries = new Map(info.boundaries.map((b) => [b.id, b]));
    const valid = curves.every(
      (s) =>
        boundaries.has(s.session?.id) &&
        s.points.every(
          (p, i) =>
            (!i || p[0] >= s.points[i - 1][0]) &&
            (boundaries.get(s.session.id).step == null ||
              p[0] >= boundaries.get(s.session.id).step),
        ),
    );
    if (valid) {
      const byId = new Map(curves.map((s) => [s.session.id, s]));
      const results = new Map();
      let prefix = [];
      for (const boundary of info.boundaries) {
        // Rewind the smoothing lineage, not the displayed data. This discards
        // every abandoned tail from the seed, including on repeated rewinds.
        if (boundary.step != null) {
          let lo = 0,
            hi = prefix.length;
          while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (prefix[mid].x < boundary.step) lo = mid + 1;
            else hi = mid;
          }
          prefix.length = lo;
        }
        const curve = byId.get(boundary.id);
        if (!curve) continue;
        const previous = prefix.at(-1);
        const anchor =
          Number.isFinite(previous?.y) && previous.x < curve.points[0][0]
            ? previous
            : null;
        const result = smooth(curve, anchor);
        results.set(boundary.id, result);
        prefix.push(...result.displayPoints);
      }
      return curves.map((s) => results.get(s.session.id));
    }
  }
  return curves.map((s) => smooth(s));
}

function axisTicks(min, max, logarithmic = false) {
  const niceLinear = (lo, hi) => {
    const rough = (hi - lo) / 4;
    const power = 10 ** Math.floor(Math.log10(rough));
    const step = [1, 2, 2.5, 5, 10].find((v) => v * power >= rough) * power;
    if (!(step > 0) || !Number.isFinite(step)) return [];
    const start = Math.ceil(lo / step - 1e-10);
    return Array.from(
      {
        length: Math.min(
          20,
          Math.max(0, Math.floor(hi / step + 1e-10) - start + 1),
        ),
      },
      (_, i) => Number(((start + i) * step).toPrecision(12)),
    );
  };
  if (!logarithmic) return niceLinear(min, max);
  const low = 10 ** min,
    high = 10 ** max;
  // Narrow ranges still use a true log transform, with readable numeric ticks.
  if (max - min < 1) return niceLinear(low, high).filter((v) => v > 0);
  const ticks = [];
  const stride = Math.max(1, Math.ceil((max - min) / 6));
  for (let e = Math.floor(min); e <= Math.ceil(max); e += stride)
    for (const m of max - min <= 2 ? [1, 2, 5] : [1]) {
      const value = m * 10 ** e;
      if (value >= low && value <= high) ticks.push(value);
    }
  return ticks;
}

// Session ordinals share a palette across runs; line style and A/B labels
// identify the run. Session S2 in two runs does not imply the same checkpoint.
function sessionInk(session) {
  let index = session?.index;
  if (!Number.isInteger(index)) {
    index = 0;
    for (const c of session?.id || "unattributed")
      index = (index * 31 + c.charCodeAt(0)) >>> 0;
  }
  const hue = (210 + index * 137.508) % 360;
  return `hsl(${hue.toFixed(2)} 68% ${hue >= 35 && hue < 180 ? 35 : 44}%)`;
}

function curveAppearance(run, session, series) {
  const index = Math.max(
    0,
    series.findIndex((s) => s.run.uid === run.uid),
  );
  const all = series[0]?.trajectory?.view === "all";
  const patterns = [
    "none",
    "8 3",
    "2 3",
    "10 3 2 3",
    "12 4",
    "4 3",
    "8 2 2 2",
    "1 3",
    "14 3 3 3",
    "6 2 6 5",
    "3 2 1 2",
    "12 3 1 3 1 3",
  ];
  return {
    color: all ? sessionInk(session) : color(run.uid),
    dash: all ? patterns[index % patterns.length] : "none",
    runLabel: String.fromCharCode(65 + index),
  };
}

function sessionLegendEntries(series) {
  return series
    .flatMap((s) =>
      sessionCurves(s).map((curve) => ({
        run: s.run,
        session: curve.session,
        ...curveAppearance(s.run, curve.session, series),
      })),
    )
    .sort(
      (a, b) =>
        a.runLabel.localeCompare(b.runLabel) ||
        (a.session?.index ?? Infinity) - (b.session?.index ?? Infinity),
    );
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
  initNavigator();
  $("#session-view").onchange = () => renderCharts().catch(showError);
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
    metricView.limit = 24;
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
  const search = $("#metric-search").value.trim();
  const axis = $("#x-axis").value;
  const signature = JSON.stringify([
    selected.map((r) => r.uid),
    state.tab,
    search,
    axis,
    state.sharedMetric,
    metricView.category,
    metricView.limit,
    $("#show-sessions").checked,
    $("#session-view").value,
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
  $("#more-metrics").hidden = true;
  $("#metric-search-error").hidden = true;
  if (!selected.length) {
    renderMetricNavigation([]);
    return;
  }
  const displayed = selected.slice(0, 12);
  let details;
  try {
    details = await Promise.all(
      displayed.map((r) => plotDetail(r.uid, signal)),
    );
  } catch (error) {
    if (error.name === "AbortError" || generation !== state.generation) return;
    metricView.signature = null;
    throw error;
  }
  if (generation !== state.generation) return;
  const runs = displayed.map((r, i) => ({
    ...r,
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
  ].sort((a, b) => {
    const rank = (k) =>
      k.startsWith("train/") ? 0 : k.startsWith("eval/") ? 1 : 2;
    return rank(a) - rank(b) || a.localeCompare(b);
  });
  metricView.catalogEmpty = keys.length === 0;
  for (const key of new Set(
    details.flatMap((d) =>
      d.keys.filter((k) => k.stream === "history").map((k) => k.key),
    ),
  ))
    if (![...$("#x-axis").options].some((o) => o.value === key))
      $("#x-axis").add(new Option(key, key));
  renderMetricNavigation(keys);
  let matches;
  try {
    matches = state.sharedMetric
      ? keys.filter((k) => k === state.sharedMetric)
      : await filterMetricKeys(
          keys.filter(
            (k) =>
              !metricView.category || k.startsWith(metricView.category + "/"),
          ),
          search,
          signal,
        );
  } catch (error) {
    if (error.name === "AbortError" || generation !== state.generation) return;
    $("#metric-search-error").textContent = error.message;
    $("#metric-search-error").hidden = false;
    $("#chart-grid").replaceChildren(
      el("p", "Adjust the search to explore metrics.", "chart-placeholder"),
    );
    return;
  }
  if (generation !== state.generation) return;
  const filtered = matches.slice(0, metricView.limit);
  $("#metric-count").textContent =
    `${filtered.length} of ${matches.length} metrics · ${keys.length} available`;
  $("#more-metrics").hidden = filtered.length >= matches.length;
  $("#more-metrics").textContent =
    `Show ${Math.min(24, matches.length - filtered.length)} more metrics (${matches.length - filtered.length} remaining)`;
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
      $("#session-view").value,
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
          view: $("#session-view").value,
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
      if (
        metricView.category === id ||
        metricView.category.startsWith(id + "/")
      ) {
        fragment.append(build(child, next));
        continue;
      }
      group.dataset.group = id;
      group.open =
        Boolean(search || metricView.category || state.sharedMetric) ||
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
