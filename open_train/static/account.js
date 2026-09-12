function makeDialog(title) {
  const dialog = el("dialog"),
    head = el("div", undefined, "detail-header"),
    close = el("button", "×", "icon-button");
  close.setAttribute("aria-label", "Close");
  close.onclick = () => dialog.close();
  head.append(el("h2", title), close);
  dialog.append(head);
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

async function tableInspector(uid, reference, title) {
  const dialog = makeDialog(title.split("/").pop()),
    toolbar = el("form", undefined, "dialog-actions"),
    search = el("input"),
    submit = el("button", "Filter"),
    status = el("p", undefined, "muted"),
    wrap = el("div", undefined, "table-wrap"),
    pager = el("div", undefined, "dialog-actions");
  dialog.classList.add("table-inspector");
  search.placeholder = "Search cells…";
  search.setAttribute("aria-label", "Search table");
  toolbar.append(search, submit);
  dialog.append(toolbar, status, wrap, pager);
  let offset = 0,
    sort = null,
    descending = false;
  async function load() {
    const params = new URLSearchParams({
      ...reference,
      offset,
      limit: 50,
      search: search.value,
      descending,
    });
    if (sort !== null) params.set("sort", sort);
    try {
      const data = await api(`/api/runs/${uid}/table?${params}`);
      status.textContent = `${data.total.toLocaleString()} rows · ${data.columns.length} columns${data.join ? ` · ${data.join} join` : ""} · click a column to sort`;
      const table = el("table", undefined, "data-table"),
        head = el("thead"),
        tr = el("tr"),
        body = el("tbody");
      data.columns.forEach((name, i) => {
        const th = el("th"),
          b = el(
            "button",
            `${name}${sort === i ? (descending ? " ↓" : " ↑") : ""}`,
          );
        b.onclick = () => {
          descending = sort === i ? !descending : false;
          sort = i;
          offset = 0;
          load();
        };
        th.append(b);
        tr.append(th);
      });
      head.append(tr);
      table.append(head, body);
      for (const row of data.data) {
        const tr = el("tr");
        for (const value of row) {
          const td = el("td");
          if (
            value &&
            typeof value === "object" &&
            value.url &&
            value._type === "image-file"
          ) {
            const img = el("img");
            img.src = value.url;
            img.alt = value.caption || "Table image";
            img.loading = "lazy";
            td.append(img);
          } else if (
            value &&
            value.url &&
            ["audio-file", "video-file"].includes(value._type)
          ) {
            const media = el(value._type === "audio-file" ? "audio" : "video");
            media.src = value.url;
            media.controls = true;
            media.preload = "none";
            td.append(media);
          } else td.textContent = format(value);
          tr.append(td);
        }
        body.append(tr);
      }
      wrap.replaceChildren(table);
      pager.replaceChildren();
      const previous = el("button", "← Previous"),
        next = el("button", "Next →");
      previous.disabled = offset === 0;
      next.disabled = offset + 50 >= data.total;
      previous.onclick = () => {
        offset = Math.max(0, offset - 50);
        load();
      };
      next.onclick = () => {
        offset += 50;
        load();
      };
      pager.append(
        previous,
        el(
          "span",
          data.total
            ? `${offset + 1}–${Math.min(offset + 50, data.total)}`
            : "0 rows",
        ),
        next,
      );
    } catch (error) {
      status.textContent = error.message;
    }
  }
  toolbar.onsubmit = (e) => {
    e.preventDefault();
    offset = 0;
    load();
  };
  await load();
}

async function accountSettings(me) {
  const dialog = makeDialog("Account & access"),
    content = el("div");
  dialog.append(content);
  content.append(
    el("p", `${me.name} · ${me.email}`),
    el("p", `Entity: ${me.username}`),
    el("h3", "API keys"),
  );
  const keylist = el("div"),
    form = el("form", undefined, "dialog-actions"),
    name = el("input"),
    expires = el("input"),
    create = el("button", "Create key", "primary"),
    secret = el("pre");
  name.placeholder = "Key name";
  name.required = true;
  name.maxLength = 100;
  name.setAttribute("aria-label", "Key name");
  expires.type = "number";
  expires.value = "90";
  expires.min = "1";
  expires.max = "3650";
  expires.setAttribute("aria-label", "Key expiration in days");
  secret.hidden = true;
  form.append(name, expires, create);
  content.append(keylist, form, secret);
  async function loadKeys() {
    const data = await api("/auth/keys");
    keylist.replaceChildren();
    for (const key of data.keys) {
      const row = el("div", undefined, "kv"),
        revoke = el("button", key.revoked ? "Revoked" : "Revoke");
      revoke.disabled = !!key.revoked;
      row.append(el("span", `${key.name} · ${key.prefix}…`), revoke);
      keylist.append(row);
      revoke.onclick = async () => {
        try {
          await api(`/auth/keys/${key.id}`, { method: "DELETE" });
          await loadKeys();
        } catch (e) {
          secret.hidden = false;
          secret.textContent = e.message;
        }
      };
    }
  }
  form.onsubmit = async (e) => {
    e.preventDefault();
    create.disabled = true;
    try {
      const result = await api("/auth/keys", {
        method: "POST",
        body: JSON.stringify({
          name: name.value,
          expires_in_days: Number(expires.value),
        }),
      });
      secret.hidden = false;
      secret.textContent = `Copy now — this key will not be shown again.\nWANDB_API_KEY=${result.key}`;
      await loadKeys();
    } catch (e) {
      secret.hidden = false;
      secret.textContent = e.message;
    } finally {
      create.disabled = false;
    }
  };
  content.append(
    el("h3", "Workspace membership"),
    el(
      "p",
      "Add an existing user's entity name. A new workspace makes you its owner.",
    ),
  );
  for (const membership of me.memberships)
    content.append(el("p", `${membership.entity} · ${membership.role}`));
  const members = el("form", undefined, "dialog-actions"),
    entity = el("input"),
    username = el("input"),
    role = el("select"),
    add = el("button", "Save membership"),
    message = el("p");
  entity.placeholder = "Workspace";
  entity.required = true;
  entity.setAttribute("aria-label", "Workspace");
  username.placeholder = "User entity name";
  username.required = true;
  username.setAttribute("aria-label", "User entity name");
  role.setAttribute("aria-label", "Role");
  for (const value of ["reader", "writer", "owner"]) {
    const option = el("option", value);
    option.value = value;
    role.append(option);
  }
  members.append(entity, username, role, add);
  content.append(members, message);
  members.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api(
        `/auth/workspaces/${encodeURIComponent(entity.value)}/members`,
        {
          method: "POST",
          body: JSON.stringify({ username: username.value, role: role.value }),
        },
      );
      message.textContent = "Membership saved.";
    } catch (e) {
      message.textContent = e.message;
    }
  };
  const logout = el("button", "Sign out");
  logout.onclick = async () => {
    await api("/auth/logout", { method: "POST" });
    sessionStorage.removeItem("open-train-key");
    location.reload();
  };
  content.append(logout);
  try {
    await loadKeys();
  } catch (e) {
    secret.hidden = false;
    secret.textContent = e.message;
  }
}

(async () => {
  const providers = await (await fetch("/auth/providers")).json();
  for (const provider of providers.providers) {
    const link = el(
      "a",
      `Continue with ${provider === "google" ? "Google" : "GitHub"}`,
      "primary oauth-link",
    );
    link.href = `/auth/login/${provider}`;
    $("#oauth-providers").append(link);
  }
  if (providers.accounts_enabled) {
    const response = await fetch("/auth/me");
    if (response.ok) {
      const me = await response.json();
      state.entity = me.username;
      state.key = "";
      sessionStorage.removeItem("open-train-key");
      $("#auth").close();
      $(".workspace div").replaceChildren(
        el("span", me.username),
        el("small", "Your workspace"),
      );
      $(".avatar").textContent = me.name[0].toUpperCase();
      $("#auth-button").title = "Account & access";
      $("#auth-button").setAttribute("aria-label", "Account & access");
      $("#auth-button").onclick = () => accountSettings(me).catch(showError);
      refresh();
    }
  }
})().catch(showError);
