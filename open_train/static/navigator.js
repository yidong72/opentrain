// Category navigation is independent of run selection and live plot updates.
const navigatorView = { tab: "runs", expanded: new Set(), signature: null };

function setNavigator(tab) {
  navigatorView.tab = tab;
  for (const name of ["runs", "metrics"]) {
    $("#" + name + "-panel").hidden = tab !== name;
    $("#" + name + "-tab").setAttribute("aria-selected", String(tab === name));
    $("#" + name + "-tab").tabIndex = tab === name ? 0 : -1;
  }
}

function initNavigator() {
  setNavigator("runs");
  for (const name of ["runs", "metrics"]) {
    const button = $("#" + name + "-tab");
    button.onclick = () => setNavigator(name);
    button.onkeydown = (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const next = name === "runs" ? "metrics" : "runs";
      setNavigator(next);
      $("#" + next + "-tab").focus();
    };
  }
  let density = "compact";
  try {
    density = localStorage.getItem("open-train-density") || density;
  } catch {
    /* optional */
  }
  $("#plot-density").value = density === "comfortable" ? density : "compact";
  document.body.dataset.density = $("#plot-density").value;
  $("#plot-density").onchange = () => {
    const value = $("#plot-density").value;
    document.body.dataset.density = value;
    try {
      localStorage.setItem("open-train-density", value);
    } catch {
      /* optional */
    }
    for (const settings of $$("#chart-grid .plot-settings"))
      settings.open = value === "comfortable";
    scheduleLivePlots();
  };
  $("#more-metrics").onclick = () => {
    metricView.limit += 24;
    renderCharts().catch(showError);
  };
}

function chooseMetricCategory(path) {
  metricView.category = path;
  metricView.limit = 24;
  state.sharedMetric = null;
  $("#metric-search").value = "";
  renderCharts().catch(showError);
}

function renderMetricNavigation(keys) {
  const signature = JSON.stringify([keys, metricView.category]);
  if (signature !== navigatorView.signature) {
    navigatorView.signature = signature;
    const host = $("#metric-tree");
    host.replaceChildren();
    if (!keys.length) {
      host.append(el("p", "Select runs to explore their metrics.", "muted"));
    } else {
      const root = { children: new Map(), count: keys.length };
      for (const key of keys) {
        let node = root;
        for (const part of key.split("/").slice(0, -1)) {
          if (!node.children.has(part))
            node.children.set(part, { children: new Map(), count: 0 });
          node = node.children.get(part);
          node.count++;
        }
      }
      function button(name, path, count) {
        const b = el("button", undefined, "metric-category");
        b.dataset.category = path;
        b.setAttribute("aria-label", `Show ${path || "all"} metrics`);
        if (metricView.category === path)
          b.setAttribute("aria-current", "page");
        b.append(el("span", name), el("small", String(count), "metric-count"));
        b.onclick = (event) => {
          event.preventDefault();
          chooseMetricCategory(path);
        };
        return b;
      }
      host.append(button("All metrics", "", keys.length));
      function branches(parent, node, prefix = "") {
        for (const [name, child] of [...node.children].sort(([a], [b]) =>
          a.localeCompare(b),
        )) {
          const path = prefix ? `${prefix}/${name}` : name;
          if (!child.children.size) {
            parent.append(button(name, path, child.count));
            continue;
          }
          const group = el("details", undefined, "metric-tree-branch"),
            summary = el("summary");
          summary.append(button(name, path, child.count));
          group.append(summary);
          group.open =
            navigatorView.expanded.has(path) ||
            metricView.category === path ||
            metricView.category.startsWith(path + "/");
          let built = false;
          function populate() {
            if (!group.open || built) return;
            const nested = el("div", undefined, "metric-tree-children");
            branches(nested, child, path);
            group.append(nested);
            built = true;
          }
          group.addEventListener("toggle", () => {
            if (!group.isConnected) return;
            group.open
              ? navigatorView.expanded.add(path)
              : navigatorView.expanded.delete(path);
            populate();
          });
          populate();
          parent.append(group);
        }
      }
      branches(host, root);
    }
  }
  const crumbs = $("#metric-breadcrumbs");
  crumbs.replaceChildren();
  const all = el("button", "All metrics");
  all.onclick = () => {
    setNavigator("metrics");
    chooseMetricCategory("");
  };
  crumbs.append(all);
  const parts = metricView.category.split("/").filter(Boolean);
  parts.forEach((part, i) => {
    const b = el("button", part);
    b.onclick = () => chooseMetricCategory(parts.slice(0, i + 1).join("/"));
    crumbs.append(el("span", "/", "muted"), b);
  });
}

function filterMetricKeys(keys, query, signal) {
  if (!query.startsWith("/"))
    return Promise.resolve(
      keys.filter((k) => k.toLowerCase().includes(query.toLowerCase())),
    );
  // Regex from a pasted/shared view must not block the UI thread indefinitely.
  return new Promise((resolve, reject) => {
    const worker = new Worker("/static/metric-filter.js?v=20260919-studio1");
    const finish = (error, matches) => {
      worker.terminate();
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      error ? reject(error) : resolve(matches);
    };
    const abort = () => finish(new DOMException("Cancelled", "AbortError"));
    const timer = setTimeout(
      () => finish(Error("Regex took too long. Try a simpler expression.")),
      1000,
    );
    worker.onmessage = ({ data }) =>
      finish(data.error ? Error(data.error) : null, data.keys);
    worker.onerror = () =>
      finish(Error("Could not evaluate regex. Try plain text search."));
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) {
      abort();
      return;
    }
    worker.postMessage({ keys, query });
  });
}
