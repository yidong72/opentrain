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
  // Membership can change while the page is open; never reuse login-time data.
  me = await api("/auth/me");
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
      "Use Edit role or Remove beside a teammate to manage access. Reader: view only. Writer: log and edit runs. Owner: also manage members. Removing membership does not delete their account or training data.",
    ),
  );
  const membershipList = el("div", undefined, "workspace-memberships");
  const refreshMembers = el("button", "Refresh members");
  content.append(membershipList, refreshMembers);
  async function loadMemberships() {
    const current = await api("/auth/me");
    membershipList.replaceChildren();
    await Promise.all(
      current.memberships.map(async (membership) => {
        const section = el("section", undefined, "workspace-membership");
        section.dataset.workspace = membership.entity;
        section.append(el("h4", `${membership.entity} · ${membership.role}`));
        membershipList.append(section);
        if (membership.role !== "owner" && !current.admin) {
          section.append(
            el(
              "p",
              "Ask a workspace owner to view or manage its member list.",
              "muted",
            ),
          );
          return;
        }
        const status = el("p", "Loading members…", "muted");
        section.append(status);
        try {
          const data = await api(
            `/auth/workspaces/${encodeURIComponent(membership.entity)}/members`,
          );
          status.textContent = `${data.members.length} members`;
          const list = el("ul", undefined, "workspace-member-list");
          for (const member of data.members) {
            const item = el("li", undefined, "workspace-member"),
              label = el("span", `${member.username} · ${member.role}`);
            item.dataset.username = member.username;
            item.title = member.name;
            item.append(label);
            if (member.username === current.username) {
              const own = el(
                "small",
                "You · cannot demote or remove yourself",
                "muted",
              );
              item.append(own);
            } else {
              const edit = el("button", "Edit role");
              edit.type = "button";
              edit.setAttribute(
                "aria-label",
                `Edit role for ${member.username}`,
              );
              const remove = el("button", "Remove"),
                removalError = el("p", undefined, "member-role-error");
              remove.type = "button";
              remove.setAttribute(
                "aria-label",
                `Remove ${member.username} from ${membership.entity}`,
              );
              removalError.setAttribute("role", "status");
              removalError.hidden = true;
              item.append(edit, remove, removalError);
              remove.onclick = async () => {
                if (
                  !window.confirm(
                    `Remove ${member.username} from workspace ${membership.entity}? This revokes their membership but does not delete their account or training data.`,
                  )
                )
                  return;
                edit.disabled = remove.disabled = true;
                removalError.hidden = true;
                try {
                  await api(
                    `/auth/workspaces/${encodeURIComponent(membership.entity)}/members/${encodeURIComponent(member.username)}`,
                    { method: "DELETE" },
                  );
                  await loadMemberships();
                  message.textContent = `Removed ${member.username} from ${membership.entity}. Their account and training data were not deleted. You can add them again below.`;
                } catch (failure) {
                  removalError.textContent = `Could not remove member: ${failure.message}`;
                  removalError.hidden = false;
                } finally {
                  edit.disabled = remove.disabled = false;
                }
              };
              edit.onclick = () => {
                edit.hidden = remove.hidden = true;
                removalError.hidden = true;
                const editor = el("form", undefined, "member-role-editor"),
                  select = el("select"),
                  save = el("button", "Save", "primary"),
                  cancel = el("button", "Cancel"),
                  error = el("p", undefined, "member-role-error");
                select.setAttribute(
                  "aria-label",
                  `Role for ${member.username}`,
                );
                for (const value of ["reader", "writer", "owner"])
                  select.add(new Option(value, value));
                select.value = member.role;
                cancel.type = "button";
                error.setAttribute("role", "status");
                error.hidden = true;
                editor.append(select, save, cancel, error);
                item.append(editor);
                select.focus();
                cancel.onclick = () => {
                  editor.remove();
                  edit.hidden = remove.hidden = false;
                  edit.focus();
                };
                editor.onsubmit = async (event) => {
                  event.preventDefault();
                  select.disabled = save.disabled = cancel.disabled = true;
                  error.hidden = true;
                  try {
                    await api(
                      `/auth/workspaces/${encodeURIComponent(membership.entity)}/members`,
                      {
                        method: "POST",
                        body: JSON.stringify({
                          username: member.username,
                          role: select.value,
                        }),
                      },
                    );
                    await loadMemberships();
                    message.textContent = `Updated ${member.username} to ${select.value}.`;
                  } catch (failure) {
                    error.textContent = `Could not save role: ${failure.message}`;
                    error.hidden = false;
                  } finally {
                    select.disabled = save.disabled = cancel.disabled = false;
                  }
                };
              };
            }
            list.append(item);
          }
          section.append(list);
        } catch (error) {
          status.textContent = `Could not load members: ${error.message}`;
        }
      }),
    );
  }
  refreshMembers.onclick = () => loadMemberships().catch(showError);
  const members = el("form", undefined, "dialog-actions"),
    entity = el("input"),
    username = el("input"),
    role = el("select"),
    add = el("button", "Save membership"),
    message = el("p");
  entity.placeholder = "Workspace";
  entity.required = true;
  entity.setAttribute("aria-label", "Workspace");
  entity.value = me.memberships.find((m) => m.role === "owner")?.entity || "";
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
      await loadMemberships();
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
  await loadMemberships();
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
