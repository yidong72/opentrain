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
    s = $("#state-filter").value;
  return state.runs.filter(
    (r) =>
      (!p || `${r.entity}/${r.project}` === p) &&
      (!s || r.state === s) &&
      `${r.display_name} ${r.name} ${r.group_name} ${r.tags.join(" ")}`
        .toLowerCase()
        .includes(q),
  );
}
function switchView(view) {
  state.view = view;
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
      const data = await api(`/api/runs?limit=500&offset=${offset}`);
      runs.push(...data.runs);
      if (!data.has_more) break;
      offset += 500;
    }
    state.runs = runs;
    state.details.clear();
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
      const metricRuns = runs.filter((r) =>
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
    renderRuns();
    await renderCharts();
  } catch (e) {
    $("#connection").textContent = "Disconnected";
    showError(e);
  } finally {
    state.loading = false;
  }
}
function renderRuns() {
  const rows = visible();
  $("#empty").hidden = state.runs.length > 0;
  $("#data-view").hidden = !state.runs.length;
  $("#run-count").textContent = rows.length;
  $("#runs-body").replaceChildren();
  for (const r of rows) {
    const tr = el("tr"),
      check = el("input");
    check.type = "checkbox";
    check.checked = state.selected.has(r.uid);
    check.setAttribute("aria-label", `Compare ${r.display_name}`);
    check.onchange = () => {
      check.checked ? state.selected.add(r.uid) : state.selected.delete(r.uid);
      renderCharts().catch(showError);
    };
    const td = el("td");
    td.append(check);
    tr.append(td);
    const name = el("td"),
      button = el("button", undefined, "run-name"),
      dot = el("span", undefined, "run-color");
    dot.style.background = color(r.uid);
    button.append(dot, document.createTextNode(r.display_name));
    button.onclick = () => openDetail(r.uid).catch(showError);
    name.append(button, el("small", r.name, "run-id"));
    tr.append(name);
    const status = el("td");
    status.append(el("span", r.state, `badge ${r.state}`));
    tr.append(
      status,
      el("td", r.project),
      el("td", format(r.summary._step)),
      el("td", r.source === "tensorboard" ? "TensorBoard" : "W&B SDK"),
      el("td", age(r.updated)),
    );
    $("#runs-body").append(tr);
  }
  $("#select-all").checked =
    rows.length > 0 && rows.every((r) => state.selected.has(r.uid));
  $("#select-all").indeterminate =
    rows.some((r) => state.selected.has(r.uid)) && !$("#select-all").checked;
}
async function detail(uid) {
  if (!state.details.has(uid)) state.details.set(uid, api(`/api/runs/${uid}`));
  return state.details.get(uid);
}
async function renderCharts() {
  const generation = ++state.generation;
  const selected = visible().filter((r) => state.selected.has(r.uid));
  $("#selection-count").textContent = `${selected.length} runs selected`;
  $("#chart-grid").hidden = state.tab !== "charts";
  if (state.tab !== "charts") return;
  const container = document.createDocumentFragment();
  if (!selected.length) {
    container.append(
      el("div", "Select runs to compare their metrics.", "chart-placeholder"),
    );
    $("#chart-grid").replaceChildren(container);
    return;
  }
  const displayed = selected.slice(0, 12);
  const details = await Promise.all(displayed.map((r) => detail(r.uid)));
  const keys = [
    ...new Set(
      details.flatMap((d) =>
        d.keys
          .filter((k) => k.stream === "history" && !k.key.startsWith("_"))
          .map((k) => k.key),
      ),
    ),
  ].sort();
  const axis = $("#x-axis").value;
  const available = [
    ...new Set(
      details.flatMap((d) =>
        d.keys.filter((k) => k.stream === "history").map((k) => k.key),
      ),
    ),
  ];
  for (const key of available)
    if (![...$("#x-axis").options].some((o) => o.value === key))
      $("#x-axis").add(new Option(key, key));
  if (!keys.length)
    container.append(
      el(
        "div",
        "Waiting for scalar metrics. Uploaded tables and media are available in run details.",
        "chart-placeholder",
      ),
    );
  for (const key of keys.slice(0, 12)) {
    const series = await Promise.all(
      displayed.map(async (r) => ({
        run: r,
        ...(await api(
          `/api/runs/${r.uid}/series?key=${encodeURIComponent(key)}&x=${encodeURIComponent(axis)}`,
        )),
      })),
    );
    if (generation !== state.generation) return;
    container.append(chart(key, series, axis));
  }
  if (keys.length > 12 || selected.length > 12)
    container.append(
      el(
        "p",
        "Overview displays the first 12 metrics and up to 12 selected runs. Filter runs to narrow the comparison.",
        "muted",
      ),
    );
  if (generation === state.generation)
    $("#chart-grid").replaceChildren(container);
}
function chart(key, series, axis) {
  const card = el("article", undefined, "chart"),
    heading = el("div", undefined, "chart-heading");
  heading.append(
    el("span", key),
    el(
      "small",
      series.some((s) => s.sampled) ? "Sampled · min/max" : "All points",
    ),
  );
  card.append(heading);
  const w = 530,
    h = 235,
    pad = { l: 47, r: 15, t: 10, b: 34 };
  let points = series.flatMap((s) => s.points).filter((p) => p[1] !== null);
  if (!points.length) {
    card.append(el("div", "No values for this axis.", "chart-placeholder"));
    return card;
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
  const smoothing = Number($("#smoothing").value);
  for (const s of series) {
    let last = null,
      pen = false,
      d = "";
    for (const [x, y] of s.points) {
      if (y === null) {
        pen = false;
        last = null;
        continue;
      }
      let sy = last === null ? y : last * smoothing + y * (1 - smoothing);
      last = sy;
      d += `${pen ? "L" : "M"}${X(x).toFixed(2)},${Y(sy).toFixed(2)} `;
      pen = true;
    }
    node("path", {
      d,
      fill: "none",
      stroke: color(s.run.uid),
      "stroke-width": 2,
      "stroke-linejoin": "round",
      "stroke-linecap": "round",
    });
    if (s.points.length === 1 && s.points[0][1] !== null)
      node("circle", {
        cx: X(s.points[0][0]),
        cy: Y(s.points[0][1]),
        r: 3,
        fill: color(s.run.uid),
      });
  }
  card.append(svg);
  const legend = el("div", undefined, "chart-legend");
  for (const s of series) {
    const item = el("span", undefined, "legend-item"),
      line = el("i", undefined, "legend-line");
    line.style.background = color(s.run.uid);
    item.title = s.run.display_name;
    item.append(line, document.createTextNode(s.run.display_name));
    legend.append(item);
  }
  card.append(legend);
  const tooltip = el("div", undefined, "tooltip");
  tooltip.hidden = true;
  card.append(tooltip);
  svg.onpointermove = (e) => {
    const rect = svg.getBoundingClientRect(),
      x =
        xmin +
        ((((e.clientX - rect.left) / rect.width) * w - pad.l) /
          (w - pad.l - pad.r)) *
          (xmax - xmin);
    const text = [];
    for (const s of series) {
      let best;
      for (const p of s.points)
        if (!best || Math.abs(p[0] - x) < Math.abs(best[0] - x)) best = p;
      if (best)
        text.push(
          `${s.run.display_name}: ${format(best[1])} · ${format(best[0])}`,
        );
    }
    tooltip.textContent = text.join("\n");
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(0, Math.min(e.clientX - card.getBoundingClientRect().left + 12, card.clientWidth - 220))}px`;
    tooltip.style.top = `${e.clientY - card.getBoundingClientRect().top + 12}px`;
  };
  svg.onpointerleave = () => (tooltip.hidden = true);
  return card;
}
async function openDetail(uid) {
  state.detailUid = uid;
  state.detailTab = "summary";
  const r = await detail(uid);
  $("#detail-name").textContent = r.display_name;
  $("#detail-path").textContent = `${r.entity} / ${r.project} / ${r.name}`;
  if (!$("#detail").open) $("#detail").showModal();
  await renderDetail();
}
async function renderDetail() {
  $$("[data-detail]").forEach((b) =>
    b.classList.toggle("active", b.dataset.detail === state.detailTab),
  );
  const r = await detail(state.detailUid);
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
  } else if (state.detailTab === "logs") {
    const logs = await api(`/api/runs/${r.uid}/logs`);
    content.append(
      el("pre", logs.lines.join("\n") || "No console output recorded."),
    );
  } else if (state.detailTab === "writers") {
    const data = await api(`/api/runs/${r.uid}/writers`);
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
for (const id of ["search", "project-filter", "state-filter"])
  $(`#${id}`).addEventListener(id === "search" ? "input" : "change", () => {
    renderRuns();
    renderCharts().catch(showError);
  });
$("#select-all").onchange = (e) => {
  for (const r of visible())
    e.target.checked ? state.selected.add(r.uid) : state.selected.delete(r.uid);
  renderRuns();
  renderCharts().catch(showError);
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
$("#smoothing").oninput = () => {
  $("#smoothing-value").value = $("#smoothing").value;
  renderCharts().catch(showError);
};
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
refresh();
setInterval(() => {
  if (!document.hidden && !$("#auth").open && !$("#detail").open) refresh();
}, 5000);
