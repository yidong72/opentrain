const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const colors = [
  "#328971",
  "#9380c6",
  "#da9b5b",
  "#669dc0",
  "#c77d91",
  "#a2a35c",
  "#687dcc",
  "#ae8265",
  "#b95e4d",
  "#4978a5",
  "#846b39",
  "#925981",
];
const runColors = new Map();
const state = {
  runs: [],
  selected: new Set(),
  details: new Map(),
  tab: "charts",
  view: "experiments",
  detailTab: "summary",
  generation: 0,
  key: sessionStorage.getItem("open-train-key") || "",
  initialized: false,
  loading: false,
  runPage: 0,
  pageSize: 20,
};
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function color(uid) {
  if (!runColors.has(uid))
    runColors.set(uid, colors[runColors.size % colors.length]);
  return runColors.get(uid);
}
function showError(error) {
  $("#error").textContent = error.message || String(error);
  $("#error").hidden = false;
}
async function api(path, options = {}) {
  const r = await fetch(path, {
    ...options,
    headers: {
      "x-open-train-csrf": "1",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(state.key ? { Authorization: `Bearer ${state.key}` } : {}),
    },
  });
  if (r.status === 401) {
    if (!$("#auth").open) $("#auth").showModal();
    throw Error("Enter your server API key to connect.");
  }
  if (!r.ok) throw Error(`${r.status}: ${await r.text()}`);
  return r.json();
}
function format(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (Number.isInteger(v) && Math.abs(v) < 1e6) return v.toLocaleString();
    return Math.abs(v) >= 10000 || (Math.abs(v) < 0.001 && v !== 0)
      ? v.toExponential(2)
      : Number(v.toFixed(4)).toString();
  }
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}
function age(ts) {
  let seconds = Math.max(0, Date.now() / 1000 - ts);
  return seconds < 60
    ? "just now"
    : seconds < 3600
      ? `${Math.floor(seconds / 60)}m ago`
      : seconds < 86400
        ? `${Math.floor(seconds / 3600)}h ago`
        : `${Math.floor(seconds / 86400)}d ago`;
}
function visible() {
  const q = $("#search").value.toLowerCase(),
    p = $("#project-filter").value,
    s = $("#state-filter").value,
    source = $("#source-filter").value;
  return state.runs.filter(
    (r) =>
      (!p || `${r.entity}/${r.project}` === p) &&
      (!s || r.state === s) &&
      (!source ||
        (source === "tensorboard"
          ? r.source === "tensorboard"
          : r.source !== "tensorboard")) &&
      (!$("#selected-filter").checked || state.selected.has(r.uid)) &&
      `${r.display_name} ${r.name} ${r.group_name} ${r.tags.join(" ")}`
        .toLowerCase()
        .includes(q),
  );
}
function switchView(view) {
  state.view = view;
  $("#run-sidebar").hidden = view !== "experiments";
  for (const name of ["experiments", "sweeps", "connect"])
    $(`#${name}-view`).hidden = name !== view;
  $$(".nav").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view),
  );
  $("#breadcrumb").textContent = {
    experiments: "Experiments",
    sweeps: "Sweeps",
    connect: "Connect a client",
  }[view];
  if (view === "sweeps") loadSweeps().catch(showError);
}
function connect() {
  switchView("connect");
  $("#connect-code").textContent =
    `export WANDB_BASE_URL=${location.origin}\nexport WANDB_API_KEY=${state.entity ? "YOUR_API_KEY" : "local00000000000000000000000000000000000"}\n# With accounts, create a key in Account & access.\nexport WANDB_ENTITY=${state.entity || "local"}`;
}
async function refresh() {
  if (state.loading) return;
  state.loading = true;
  try {
    const runs = [];
    let offset = 0;
    while (true) {
      const data = await api(
        `/api/runs?limit=500&offset=${offset}&compact=true`,
      );
      runs.push(...data.runs);
      if (!data.has_more) break;
      offset += 500;
    }
    const changed =
      JSON.stringify(runs.map((r) => [r.uid, r.updated, r.state])) !==
      JSON.stringify(state.runs.map((r) => [r.uid, r.updated, r.state]));
    state.runs = runs;
    const available = new Set(runs.map((r) => r.uid));
    for (const uid of state.selected)
      if (!available.has(uid)) state.selected.delete(uid);
    $("#error").hidden = true;
    $("#connection").textContent = "Live · refreshes every 5s";
    const project = $("#project-filter").value;
    $("#project-filter").replaceChildren(new Option("All projects", ""));
    for (const p of [
      ...new Set(runs.map((r) => `${r.entity}/${r.project}`)),
    ].sort())
      $("#project-filter").add(new Option(p, p));
    $("#project-filter").value = project;
    $("#total").textContent = runs.length;
    $("#nav-count").textContent = runs.length;
    $("#active").textContent = runs.filter((r) => r.state === "running").length;
    $("#completed").textContent = runs.filter(
      (r) => r.state === "finished",
    ).length;
    $("#projects-count").textContent = new Set(
      runs.map((r) => `${r.entity}/${r.project}`),
    ).size;
    if (!state.initialized && runs.length) {
      let path = location.pathname.split("/");
      let target = runs.find(
        (r) =>
          path[3] === "runs" &&
          r.entity === path[1] &&
          r.project === path[2] &&
          r.name === path[4],
      );
      const metricRuns = runs.filter(
        (r) =>
          r.has_metrics ||
          Object.entries(r.summary).some(
            ([key, value]) => !key.startsWith("_") && typeof value === "number",
          ),
      );
      for (const r of target
        ? [target]
        : (metricRuns.length ? metricRuns : runs).slice(0, 3))
        state.selected.add(r.uid);
      state.initialized = true;
      if (target) openDetail(target.uid);
    }
    if (changed) renderRuns();
    await renderCharts();
  } catch (e) {
    $("#connection").textContent = "Disconnected";
    showError(e);
  } finally {
    state.loading = false;
  }
}
function renderRuns() {
  const filtered = visible();
  const pages = Math.max(1, Math.ceil(filtered.length / state.pageSize));
  state.runPage = Math.min(state.runPage, pages - 1);
  const start = state.runPage * state.pageSize;
  const rows = filtered.slice(start, start + state.pageSize);
  $("#empty").hidden = state.runs.length > 0;
  $("#data-view").hidden = !state.runs.length;
  $("#run-count").textContent = filtered.length;
  $("#run-range").textContent = filtered.length
    ? `${start + 1}–${start + rows.length} of ${filtered.length} runs`
    : "No matching runs";
  $("#run-page").textContent = `${state.runPage + 1} / ${pages}`;
  $("#runs-prev").disabled = state.runPage === 0;
  $("#runs-next").disabled = state.runPage >= pages - 1;
  $("#filter-count").textContent =
    [
      $("#project-filter").value,
      $("#state-filter").value,
      $("#source-filter").value,
      $("#selected-filter").checked,
    ].filter(Boolean).length || "";
  $("#runs-body").replaceChildren();
  if (!rows.length)
    $("#runs-body").append(
      el("p", "No runs match these filters.", "run-empty"),
    );
  for (const r of rows) {
    const tr = el("article", undefined, "run-row"),
      check = el("input");
    tr.setAttribute("role", "listitem");
    tr.dataset.uid = r.uid;
    tr.classList.toggle("selected", state.selected.has(r.uid));
    check.type = "checkbox";
    check.checked = state.selected.has(r.uid);
    check.disabled = !check.checked && state.selected.size >= 12;
    check.setAttribute("aria-label", `Compare ${r.display_name}`);
    check.onchange = () => {
      check.checked ? state.selected.add(r.uid) : state.selected.delete(r.uid);
      renderRuns();
      renderCharts().catch(showError);
    };
    tr.append(check);
    const name = el("div", undefined, "run-row-content"),
      button = el("button", undefined, "run-name"),
      dot = el("span", undefined, "run-color");
    dot.style.background = color(r.uid);
    button.append(dot, document.createTextNode(r.display_name));
    button.onclick = () => openDetail(r.uid).catch(showError);
    button.title = r.display_name;
    name.append(button, el("small", r.name, "run-id"));
    if (r.session_count > 1) {
      const sessions = el(
        "button",
        `${r.session_count} sessions`,
        "session-badge",
      );
      sessions.onclick = () => openDetail(r.uid, "sessions").catch(showError);
      name.append(sessions);
    }
    const meta = el("div", undefined, "run-meta");
    meta.append(
      el("span", r.state, `badge ${r.state}`),
      el("span", `Step ${format(r.summary._step)}`),
    );
    name.append(
      meta,
      el(
        "small",
        `${r.project} · ${r.source === "tensorboard" ? "TensorBoard" : "W&B SDK"} · ${age(r.updated)}`,
        "run-meta",
      ),
    );
    tr.append(name);
    $("#runs-body").append(tr);
  }
  $("#select-all").checked =
    rows.length > 0 && rows.every((r) => state.selected.has(r.uid));
  $("#select-all").indeterminate =
    rows.some((r) => state.selected.has(r.uid)) && !$("#select-all").checked;
  $("#select-all").disabled =
    !rows.length ||
    (state.selected.size >= 12 && !rows.some((r) => state.selected.has(r.uid)));
}
async function detail(uid) {
  const revision = state.runs.find((r) => r.uid === uid)?.updated;
  let cached = state.details.get(uid);
  if (!cached || cached.revision !== revision || cached.expires < Date.now()) {
    cached = { revision, expires: Date.now() + 300000 };
    cached.promise = api(`/api/runs/${uid}`).catch((error) => {
      if (state.details.get(uid) === cached) state.details.delete(uid);
      throw error;
    });
    state.details.set(uid, cached);
  }
  return cached.promise;
}
const plotPreferences = new Map();
let plotClipSequence = 0;
function plotPreference(key, axis) {
  if (!plotPreferences.has(key)) {
    let smoothing = 0;
    try {
      const saved = Number(localStorage.getItem(`open-train-smoothing:${key}`));
      if (Number.isFinite(saved))
        smoothing = Math.max(0, Math.min(0.95, saved));
    } catch {
      /* Preferences are optional when browser storage is unavailable. */
    }
    plotPreferences.set(key, { smoothing, domains: new Map() });
  }
  return plotPreferences.get(key);
}
function chart(key, series, axis, expanded = false) {
  const preference = plotPreference(key, axis);
  const card = el("article", undefined, "chart"),
    heading = el("div", undefined, "chart-heading");
  card.dataset.metric = key;
  heading.append(
    el("span", key),
    el(
      "small",
      series.some((s) => s.sampled) ? "Sampled · min/max" : "All points",
    ),
  );
  card.append(heading);
  const missingAxis = series.reduce((n, s) => n + (s.missing_axis || 0), 0);
  if (missingAxis)
    card.append(
      el(
        "p",
        `${missingAxis} metric records omitted: missing or unverified ${axis} pairing.`,
        "muted",
      ),
    );
  const controls = el("div", undefined, "plot-controls");
  const smoothingLabel = el("label", "Smoothing ");
  const smoothingInput = el("input");
  smoothingInput.type = "range";
  smoothingInput.min = "0";
  smoothingInput.max = ".95";
  smoothingInput.step = ".05";
  smoothingInput.value = preference.smoothing;
  smoothingInput.className = "plot-smoothing";
  smoothingInput.setAttribute("aria-label", `Smoothing for ${key}`);
  const smoothingValue = el("output", String(preference.smoothing));
  smoothingLabel.append(smoothingInput, smoothingValue);
  controls.append(smoothingLabel);
  function action(label, text, cls) {
    const button = el("button", text, cls);
    button.title = label;
    button.setAttribute("aria-label", `${label}: ${key}`);
    controls.append(button);
    return button;
  }
  const zoomIn = action("Zoom in", "+", "plot-zoom-in");
  const zoomOut = action("Zoom out", "−", "plot-zoom-out");
  const reset = action("Reset zoom", "Reset", "plot-reset");
  if (!expanded) {
    action("Maximize plot", "⛶", "plot-maximize").onclick = () => {
      const dialog = el("dialog", undefined, "plot-dialog");
      dialog.setAttribute("aria-label", `${key} expanded plot`);
      const header = el("div", undefined, "detail-header");
      const close = el("button", "×", "icon-button");
      close.setAttribute("aria-label", "Close expanded plot");
      close.onclick = () => dialog.close();
      header.append(el("h2", key), close);
      dialog.append(header, chart(key, series, axis, true));
      dialog.addEventListener("close", () => {
        // A refresh already in flight may have replaced the original card.
        $$("#chart-grid .chart")
          .find((c) => c.dataset.metric === key)
          ?.redraw?.();
        dialog.remove();
      });
      document.body.append(dialog);
      dialog.showModal();
      close.focus();
    };
  }
  card.append(controls);
  const body = el("div", undefined, "plot-body");
  card.append(body);
  smoothingInput.oninput = () => {
    preference.smoothing = Number(smoothingInput.value);
    smoothingValue.value = smoothingInput.value;
    try {
      localStorage.setItem(`open-train-smoothing:${key}`, smoothingInput.value);
    } catch {
      /* Optional preference. */
    }
    draw();
  };
  card.redraw = () => {
    smoothingInput.value = preference.smoothing;
    smoothingValue.value = preference.smoothing;
    draw();
  };
  function draw() {
    const restoreFocus = document.activeElement === $("svg", body);
    body.replaceChildren();
    const w = expanded ? 1000 : 530,
      h = expanded ? 520 : 235,
      pad = { l: 47, r: 15, t: 10, b: 34 };
    let points = series.flatMap((s) => s.points).filter((p) => p[1] !== null);
    if (!points.length) {
      body.append(el("div", "No values for this axis.", "chart-placeholder"));
      zoomIn.disabled = zoomOut.disabled = reset.disabled = true;
      return;
    }
    let xmin = Infinity,
      xmax = -Infinity,
      ymin = Infinity,
      ymax = -Infinity;
    for (const [x, y] of points) {
      xmin = Math.min(xmin, x);
      xmax = Math.max(xmax, x);
      ymin = Math.min(ymin, y);
      ymax = Math.max(ymax, y);
    }
    if (xmin === xmax) xmax = xmin + 1;
    let yrange = ymax - ymin || Math.max(Math.abs(ymax) * 0.1, 0.1);
    ymin -= yrange * 0.08;
    ymax += yrange * 0.08;
    const full = [xmin, xmax, ymin, ymax];
    const domain = preference.domains.get(axis);
    if (domain) [xmin, xmax, ymin, ymax] = domain;
    card.dataset.domain = JSON.stringify([xmin, xmax, ymin, ymax]);
    reset.disabled = zoomOut.disabled = !domain;
    function setDomain(next) {
      if (next.every((v, i) => Math.abs(v - full[i]) < 1e-10))
        preference.domains.delete(axis);
      else preference.domains.set(axis, next);
      draw();
    }
    function zoom(factor, cx = (xmin + xmax) / 2, cy = (ymin + ymax) / 2) {
      const next = [
        cx + (xmin - cx) * factor,
        cx + (xmax - cx) * factor,
        cy + (ymin - cy) * factor,
        cy + (ymax - cy) * factor,
      ];
      for (const i of [0, 2]) {
        const span = next[i + 1] - next[i];
        if (span >= full[i + 1] - full[i])
          [next[i], next[i + 1]] = [full[i], full[i + 1]];
        else {
          next[i] = Math.max(full[i], Math.min(next[i], full[i + 1] - span));
          next[i + 1] = next[i] + span;
        }
      }
      if (
        next[1] - next[0] > Math.max(1e-12, Math.abs(cx) * 1e-13) &&
        next[3] - next[2] > Math.max(1e-12, Math.abs(cy) * 1e-13)
      )
        setDomain(next);
    }
    zoomIn.onclick = () => zoom(0.5);
    zoomOut.onclick = () => zoom(2);
    reset.onclick = () => {
      preference.domains.delete(axis);
      draw();
    };
    const X = (x) => pad.l + ((x - xmin) / (xmax - xmin)) * (w - pad.l - pad.r),
      Y = (y) => h - pad.b - ((y - ymin) / (ymax - ymin)) * (h - pad.t - pad.b);
    const ns = "http://www.w3.org/2000/svg",
      svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("role", "img");
    svg.setAttribute(
      "aria-label",
      `${key} by ${axis} across ${series.length} runs`,
    );
    function node(tag, attrs, text) {
      const n = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
      if (text !== undefined) n.textContent = text;
      svg.append(n);
      return n;
    }
    const clipID = `plot-clip-${++plotClipSequence}`;
    const clip = node("clipPath", { id: clipID });
    const clipRect = document.createElementNS(ns, "rect");
    for (const [k, v] of Object.entries({
      x: pad.l,
      y: pad.t,
      width: w - pad.l - pad.r,
      height: h - pad.t - pad.b,
    }))
      clipRect.setAttribute(k, v);
    clip.append(clipRect);
    for (let i = 0; i <= 4; i++) {
      const y = ymin + ((ymax - ymin) * i) / 4;
      node("line", {
        x1: pad.l,
        y1: Y(y),
        x2: w - pad.r,
        y2: Y(y),
        stroke: "#edf1ee",
        "stroke-dasharray": "3 3",
      });
      node(
        "text",
        {
          x: pad.l - 9,
          y: Y(y) + 3,
          "text-anchor": "end",
          fill: "#a2aea6",
          "font-size": 9,
        },
        format(y),
      );
      const x = xmin + ((xmax - xmin) * i) / 4;
      node(
        "text",
        {
          x: X(x),
          y: h - 14,
          "text-anchor": "middle",
          fill: "#a2aea6",
          "font-size": 9,
        },
        axis === "_timestamp"
          ? new Date(x * 1000).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
            })
          : format(x),
      );
    }
    const smoothing = preference.smoothing;
    const drawn = [];
    if ($("#show-sessions").checked && ["_step", "_timestamp"].includes(axis)) {
      for (const s of series) {
        const starts = new Map();
        for (const session of runSessions(s.run).slice(1)) {
          const x =
            axis === "_step" ? session.first_step : session.first_wall_time;
          if (!Number.isFinite(x) || x < xmin || x > xmax) continue;
          if (!starts.has(x)) starts.set(x, []);
          starts.get(x).push(session.label);
        }
        for (const [x, labels] of starts) {
          const marker = node("line", {
            x1: X(x),
            x2: X(x),
            y1: pad.t,
            y2: h - pad.b,
            stroke: color(s.run.uid),
            "stroke-dasharray": "3 5",
            opacity: 0.4,
            class: "session-start",
            "pointer-events": "none",
          });
          const title = document.createElementNS(ns, "title");
          title.textContent = `${s.run.display_name} · ${labels.join(", ")} start at ${format(x)}`;
          marker.append(title);
          node(
            "text",
            {
              x: X(x) + 3,
              y: pad.t + 10 + series.indexOf(s) * 11,
              fill: color(s.run.uid),
              "font-size": 9,
              "pointer-events": "none",
            },
            labels.join("/"),
          );
        }
      }
    }
    for (const s of series) {
      let last = null,
        pen = false,
        d = "",
        previousSession = null;
      const plotted = [];
      const sessions = runSessions(s.run);
      function flush() {
        if (!d) return;
        node("path", {
          d,
          fill: "none",
          stroke: color(s.run.uid),
          "stroke-width": 2,
          "stroke-linejoin": "round",
          "stroke-linecap": "round",
          class: "metric-line",
          "clip-path": `url(#${clipID})`,
          "stroke-dasharray":
            previousSession && previousSession.index % 2 ? "6 3" : "none",
        });
        d = "";
      }
      for (let index = 0; index < s.points.length; index++) {
        const [x, y] = s.points[index];
        if (y === null) {
          pen = false;
          last = null;
          continue;
        }
        let sy = last === null ? y : last * smoothing + y * (1 - smoothing);
        last = sy;
        const session = sessionForPoint(s.run, s.timestamps?.[index], sessions);
        if (session?.label !== previousSession?.label && pen) {
          flush();
          const before = plotted[plotted.length - 1];
          d = `M${X(before.x).toFixed(2)},${Y(before.y).toFixed(2)} `;
        }
        previousSession = session;
        d += `${pen ? "L" : "M"}${X(x).toFixed(2)},${Y(sy).toFixed(2)} `;
        pen = true;
        plotted.push({ x, y: sy, raw: y, session });
      }
      flush();
      drawn.push({ run: s.run, points: plotted });
      if (s.points.length === 1 && s.points[0][1] !== null)
        node("circle", {
          cx: X(s.points[0][0]),
          cy: Y(s.points[0][1]),
          r: 3,
          fill: color(s.run.uid),
          "clip-path": `url(#${clipID})`,
        });
    }
    body.append(svg);
    svg.style.touchAction = "none";
    svg.setAttribute("tabindex", "0");
    svg.setAttribute(
      "aria-label",
      `${key} by ${axis}. Drag a rectangle to zoom. Use plus, minus, or zero keys to zoom and reset.`,
    );
    const legend = el("div", undefined, "chart-legend");
    for (const s of series) {
      const item = el("span", undefined, "legend-item"),
        line = el("i", undefined, "legend-line");
      line.style.background = color(s.run.uid);
      item.title = s.run.display_name;
      item.append(line, document.createTextNode(s.run.display_name));
      legend.append(item);
    }
    body.append(legend);
    body.append(
      el(
        "small",
        "Drag to zoom · Ctrl/⌘ + scroll to zoom · double-click to reset",
        "plot-hint",
      ),
    );
    const tooltip = el("div", undefined, "tooltip");
    tooltip.hidden = true;
    body.append(tooltip);
    const guide = node("line", {
      y1: pad.t,
      y2: h - pad.b,
      stroke: "#536c60",
      "stroke-dasharray": "3 3",
      visibility: "hidden",
      class: "hover-guide",
      "pointer-events": "none",
    });
    const markers = drawn.map((s) =>
      node("circle", {
        r: 5,
        fill: color(s.run.uid),
        stroke: "white",
        "stroke-width": 2,
        visibility: "hidden",
        class: "hover-point",
        "pointer-events": "none",
        "clip-path": `url(#${clipID})`,
      }),
    );
    function coordinates(e) {
      const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(
        svg.getScreenCTM().inverse(),
      );
      return {
        x: Math.max(pad.l, Math.min(w - pad.r, p.x)),
        y: Math.max(pad.t, Math.min(h - pad.b, p.y)),
      };
    }
    const unX = (x) =>
      xmin + ((x - pad.l) / (w - pad.l - pad.r)) * (xmax - xmin);
    const unY = (y) =>
      ymax - ((y - pad.t) / (h - pad.t - pad.b)) * (ymax - ymin);
    let drag = null;
    const brush = node("rect", {
      class: "zoom-brush",
      fill: "#167b6226",
      stroke: "#167b62",
      visibility: "hidden",
      "pointer-events": "none",
    });
    svg.onpointerdown = (e) => {
      if (e.button !== 0) return;
      drag = coordinates(e);
      svg.setPointerCapture(e.pointerId);
      svg.onpointerleave();
      e.preventDefault();
    };
    svg.onpointerup = (e) => {
      if (!drag) return;
      const start = drag,
        end = coordinates(e);
      drag = null;
      brush.setAttribute("visibility", "hidden");
      if (svg.hasPointerCapture(e.pointerId))
        svg.releasePointerCapture(e.pointerId);
      if (Math.abs(start.x - end.x) > 8 && Math.abs(start.y - end.y) > 8)
        setDomain([
          unX(Math.min(start.x, end.x)),
          unX(Math.max(start.x, end.x)),
          unY(Math.max(start.y, end.y)),
          unY(Math.min(start.y, end.y)),
        ]);
    };
    svg.onpointercancel = () => {
      drag = null;
      brush.setAttribute("visibility", "hidden");
    };
    svg.ondblclick = () => reset.onclick();
    svg.onkeydown = (e) => {
      if (["+", "=", "-", "0"].includes(e.key)) {
        e.preventDefault();
        if (e.key === "0") reset.onclick();
        else zoom(e.key === "-" ? 2 : 0.5);
      }
    };
    svg.addEventListener(
      "wheel",
      (e) => {
        if (!e.ctrlKey && !e.metaKey) return;
        e.preventDefault();
        const p = coordinates(e);
        zoom(e.deltaY > 0 ? 1.25 : 0.8, unX(p.x), unY(p.y));
      },
      { passive: false },
    );
    svg.onpointermove = (e) => {
      const p = coordinates(e),
        x = unX(p.x);
      if (drag) {
        for (const [k, v] of Object.entries({
          x: Math.min(drag.x, p.x),
          y: Math.min(drag.y, p.y),
          width: Math.abs(drag.x - p.x),
          height: Math.abs(drag.y - p.y),
          visibility: "visible",
        }))
          brush.setAttribute(k, v);
        return;
      }
      const text = [];
      const clamped = Math.max(xmin, Math.min(xmax, x));
      guide.setAttribute("x1", X(clamped));
      guide.setAttribute("x2", X(clamped));
      guide.setAttribute("visibility", "visible");
      for (const [index, s] of drawn.entries()) {
        // Binary search keeps hover work logarithmic in the number of plotted points.
        let lo = 0,
          hi = s.points.length;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (s.points[mid].x < x) lo = mid + 1;
          else hi = mid;
        }
        const best = [s.points[lo - 1], s.points[lo]]
          .filter(Boolean)
          .sort((a, b) => Math.abs(a.x - x) - Math.abs(b.x - x))[0];
        markers[index].setAttribute("visibility", "hidden");
        if (
          best &&
          best.x >= xmin &&
          best.x <= xmax &&
          best.y >= ymin &&
          best.y <= ymax
        ) {
          const marker = markers[index];
          marker.setAttribute("cx", X(best.x));
          marker.setAttribute("cy", Y(best.y));
          marker.setAttribute("visibility", "visible");
          marker.dataset.x = best.x;
          marker.dataset.value = best.y;
          text.push(
            `${s.run.display_name}${best.session ? " · " + best.session.label : ""}\n${axis}: ${format(best.x)} · ${smoothing ? "smoothed" : "value"}: ${format(best.y)}${smoothing ? " · raw: " + format(best.raw) : ""}`,
          );
        }
      }
      tooltip.textContent = text.join("\n");
      tooltip.hidden = !text.length;
      tooltip.style.left = `${Math.max(0, Math.min(e.clientX - card.getBoundingClientRect().left + 12, card.clientWidth - 220))}px`;
      tooltip.style.top = `${e.clientY - card.getBoundingClientRect().top + 12}px`;
    };
    svg.onpointerleave = () => {
      tooltip.hidden = true;
      guide.setAttribute("visibility", "hidden");
      markers.forEach((marker) => marker.setAttribute("visibility", "hidden"));
    };
    if (restoreFocus) svg.focus({ preventScroll: true });
  }
  draw();
  return card;
}
async function openDetail(uid, tab = "summary") {
  state.detailUid = uid;
  state.detailTab = tab;
  const initial = state.runs.find((r) => r.uid === uid);
  $("#detail-name").textContent = initial?.display_name || "Run details";
  $("#detail-content").replaceChildren(
    el("p", "Loading run details…", "muted"),
  );
  if (!$("#detail").open) $("#detail").showModal();
  const r = await detail(uid);
  if (state.detailUid !== uid || !$("#detail").open) return;
  $("#detail-name").textContent = r.display_name;
  $("#detail-path").textContent = `${r.entity} / ${r.project} / ${r.name}`;
  if (!$("#detail").open) $("#detail").showModal();
  await renderDetail();
}
async function renderDetail() {
  const uid = state.detailUid,
    tab = state.detailTab;
  $$("[data-detail]").forEach((b) =>
    b.classList.toggle("active", b.dataset.detail === state.detailTab),
  );
  const r = await detail(state.detailUid);
  if (uid !== state.detailUid || tab !== state.detailTab) return;
  const content = $("#detail-content");
  content.replaceChildren();
  if (state.detailTab === "summary" || state.detailTab === "config") {
    const values = state.detailTab === "config" ? r.config : r.summary;
    for (const [k, v] of Object.entries(values).filter(
      ([k]) => !k.startsWith("_"),
    )) {
      const row = el("div", undefined, "kv");
      row.append(el("span", k), el("span", format(v)));
      if (v && typeof v === "object" && String(v._type).includes("table")) {
        const button = el("button", "Inspect table");
        button.onclick = () =>
          tableInspector(r.uid, { key: k }, k).catch(showError);
        row.replaceChildren(el("span", k), button);
      }
      content.append(row);
    }
    if (!content.children.length)
      content.append(el("p", "Nothing logged yet.", "muted"));
  } else if (state.detailTab === "sessions") {
    renderSessions(content, r);
  } else if (state.detailTab === "logs") {
    const logs = await api(`/api/runs/${r.uid}/logs`);
    if (uid !== state.detailUid || tab !== state.detailTab) return;
    content.append(
      el("pre", logs.lines.join("\n") || "No console output recorded."),
    );
  } else if (state.detailTab === "writers") {
    const data = await api(`/api/runs/${r.uid}/writers`);
    if (uid !== state.detailUid || tab !== state.detailTab) return;
    for (const writer of data.writers) {
      const row = el("div", undefined, "kv");
      row.append(
        el("span", writer.writer),
        el(
          "span",
          `${writer.history_rows} rows · ${writer.finished ? "finished" : "open"}`,
        ),
      );
      content.append(row);
    }
    if (!data.writers.length)
      content.append(el("p", "This run uses a single standard writer."));
  } else if (state.detailTab === "artifacts") {
    const data = await api(`/api/runs/${r.uid}/artifacts`);
    if (uid !== state.detailUid || tab !== state.detailTab) return;
    for (const artifact of data.artifacts) {
      const card = el("article", undefined, "file-item");
      card.append(
        el("h3", `${artifact.artifactSequence.name}:v${artifact.versionIndex}`),
        el(
          "p",
          `${artifact.artifactType.name} · ${artifact.aliases.map((a) => a.alias).join(", ")}`,
        ),
        el(
          "p",
          `Produced by ${artifact.producer} · Used by ${artifact.consumers.join(", ") || "no runs yet"}`,
        ),
        el("pre", JSON.stringify(artifact.metadata, null, 2)),
      );
      for (const file of artifact.files) {
        const link = el("a", file.name);
        link.href = file.url;
        link.target = "_blank";
        link.rel = "noopener";
        const row = el("div", undefined, "kv");
        row.append(link, el("small", `${format(file.size / 1024)} KB`));
        if (file.name.endsWith("table.json")) {
          const button = el("button", "Inspect table");
          button.onclick = () =>
            tableInspector(file.run, { path: file.path }, file.name).catch(
              showError,
            );
          row.append(button);
        }
        card.append(row);
      }
      content.append(card);
    }
    if (!data.artifacts.length)
      content.append(el("p", "No artifacts linked to this run."));
  } else {
    if (!r.files.length)
      content.append(el("p", "No files uploaded yet.", "muted"));
    for (const file of r.files) {
      const block = el("article", undefined, "file-item"),
        link = el("a", file.name);
      link.href = file.url;
      link.target = "_blank";
      link.rel = "noopener";
      block.append(link, el("small", `${format(file.size / 1024)} KB`));
      const ext = file.name.split(".").pop().toLowerCase();
      if (["png", "jpg", "jpeg", "gif", "webp"].includes(ext)) {
        const img = el("img");
        img.src = file.url;
        img.alt = file.name;
        img.loading = "lazy";
        block.append(img);
      } else if (["mp4", "webm", "wav", "mp3", "ogg"].includes(ext)) {
        const media = el(["mp4", "webm"].includes(ext) ? "video" : "audio");
        media.src = file.url;
        media.controls = true;
        media.preload = "none";
        block.append(media);
      } else if (file.name.endsWith("table.json")) {
        const button = el("button", "Preview table");
        button.style.marginTop = "12px";
        button.onclick = () =>
          tableInspector(r.uid, { path: file.name }, file.name).catch(
            showError,
          );
        block.append(button);
      }
      content.append(block);
    }
  }
}
async function loadSweeps() {
  const data = await api("/api/sweeps");
  $("#sweeps-list").replaceChildren();
  if (!data.sweeps.length)
    $("#sweeps-list").append(
      el(
        "p",
        "No sweeps yet. Create one with wandb.sweep(), then start wandb.agent().",
        "muted",
      ),
    );
  for (const s of data.sweeps) {
    const card = el("article", undefined, "sweep-card");
    card.append(
      el("h2", `${s.project} / ${s.name}`),
      el("p", `${s.next_index} trials allocated · ${s.state.toLowerCase()}`),
      el("pre", s.config),
    );
    $("#sweeps-list").append(card);
  }
}
$$("[data-view]").forEach(
  (b) =>
    (b.onclick = () =>
      b.dataset.view === "connect" ? connect() : switchView(b.dataset.view)),
);
for (const id of ["connect", "empty-connect"]) $(`#${id}`).onclick = connect;
$("#refresh").onclick = refresh;
for (const id of [
  "search",
  "project-filter",
  "state-filter",
  "source-filter",
  "selected-filter",
])
  $(`#${id}`).addEventListener(id === "search" ? "input" : "change", () => {
    state.runPage = 0;
    renderRuns();
  });
$("#select-all").onchange = (e) => {
  const rows = visible().slice(
    state.runPage * state.pageSize,
    (state.runPage + 1) * state.pageSize,
  );
  const clear =
    !e.target.checked ||
    (state.selected.size >= 12 && rows.some((r) => state.selected.has(r.uid)));
  for (const r of rows) {
    if (clear) state.selected.delete(r.uid);
    else if (state.selected.size < 12) state.selected.add(r.uid);
  }
  renderRuns();
  renderCharts().catch(showError);
};
$("#clear-selection").onclick = () => {
  state.selected.clear();
  renderRuns();
  renderCharts().catch(showError);
};
$("#clear-filters").onclick = () => {
  for (const id of [
    "search",
    "project-filter",
    "state-filter",
    "source-filter",
  ])
    $("#" + id).value = "";
  $("#selected-filter").checked = false;
  state.runPage = 0;
  renderRuns();
};
for (const [id, delta] of [
  ["runs-prev", -1],
  ["runs-next", 1],
])
  $("#" + id).onclick = () => {
    state.runPage += delta;
    renderRuns();
  };
$("#run-page-size").onchange = (e) => {
  state.pageSize = Number(e.target.value);
  state.runPage = 0;
  renderRuns();
};
$$("[data-tab]").forEach(
  (b) =>
    (b.onclick = () => {
      state.tab = b.dataset.tab;
      $$("[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
      renderCharts().catch(showError);
    }),
);
$("#x-axis").onchange = () => renderCharts().catch(showError);
$("#close-detail").onclick = () => $("#detail").close();
$$("[data-detail]").forEach(
  (b) =>
    (b.onclick = () => {
      state.detailTab = b.dataset.detail;
      renderDetail().catch(showError);
    }),
);
$("#auth-button").onclick = () => $("#auth").showModal();
$("#auth").addEventListener("close", () => {
  if ($("#auth").returnValue === "save") {
    state.key = $("#api-key").value;
    sessionStorage.setItem("open-train-key", state.key);
    $("#api-key").value = "";
    refresh();
  }
});
initMetrics();
refresh();
setInterval(() => {
  if (
    !document.hidden &&
    !$("#auth").open &&
    !$("#detail").open &&
    !$(".plot-dialog[open]")
  )
    refresh();
}, 5000);
