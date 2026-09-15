const loginView = document.getElementById("login-view");
const appView = document.getElementById("app-view");
const alertsEl = document.getElementById("alerts");
const statusFilter = document.getElementById("status-filter");
let overviewCache = null;
let domainsCache = [];

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("未登录");
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = data && (data.detail || data.message);
    throw new Error(typeof detail === "string" ? detail : "请求失败");
  }
  return data;
}

function showLogin() {
  loginView.classList.remove("hidden");
  appView.classList.add("hidden");
}

function showApp() {
  loginView.classList.add("hidden");
  appView.classList.remove("hidden");
}

function statusLabel(status) {
  return { unused: "未使用", used: "已使用", error: "异常" }[status] || status;
}

function quotaText(used, max) {
  if (max === null || max === undefined) return `${used} / 不限`;
  return `${used} / ${max}`;
}

function accountButtons(kind, item) {
  return `
    <div class="row-actions">
      <button type="button" class="ghost" data-edit-${kind}="${item.id}">编辑</button>
      <button type="button" class="danger" data-del-${kind}="${item.id}">从本监控移除</button>
    </div>`;
}

function renderOverview(data) {
  overviewCache = data;
  document.getElementById("version-line").textContent = `版本 ${data.version} · 工作线程 ${data.worker.alive === "yes" ? "运行中" : "未运行"}`;
  document.getElementById("stat-unused").textContent = data.unused;
  document.getElementById("stat-used").textContent = data.used;
  document.getElementById("stat-error").textContent = data.error;
  document.getElementById("stat-worker").textContent = data.worker.alive === "yes" ? "正常" : "停止";
  alertsEl.innerHTML = (data.alerts || []).map((item) => `<div class="alert">${escapeHtml(item)}</div>`).join("");
  document.getElementById("yyds-list").innerHTML = data.yyds.map((item) => `
    <div class="account">
      <b>${escapeHtml(item.name)}</b>
      <div class="muted">${escapeHtml(item.username)} · 套餐 ${escapeHtml(item.plan_name || "未知")} · 顺序 ${item.sort_order}</div>
      <div>泛解析 ${quotaText(item.used_wildcard, item.max_wildcard)} ${item.wildcard_full ? "· 已满" : "· 有空位"}</div>
      <div>自定义域名 ${quotaText(item.used_domains, item.max_domains)}</div>
      <div>接收新域名：${item.receive_enabled ? "开" : "关"} · 登录：${item.login_error ? "失败" : "正常"}</div>
      ${item.login_error ? `<div class="error">${escapeHtml(item.login_error)}</div>` : ""}
      <div class="pills">${(item.domains || []).map((name) => `<span class="pill">${escapeHtml(name)}</span>`).join("") || '<span class="muted">当前没有绑定域名</span>'}</div>
      ${accountButtons("yyds", item)}
    </div>`).join("") || '<p class="muted">还没有 yyds 账号</p>';
  document.getElementById("aliyun-list").innerHTML = data.aliyun.map((item) => `
    <div class="account">
      <b>${escapeHtml(item.name)}</b>
      <div class="muted">${escapeHtml(item.access_key_id_masked)}</div>
      <div>启用：${item.enabled ? "是" : "否"} · 首次对账：${item.first_synced_at || "未开始"}</div>
      <div>上次成功：${item.last_success_at || "-"}</div>
      ${item.last_error ? `<div class="error">${escapeHtml(item.last_error)}</div>` : ""}
      ${accountButtons("aliyun", item)}
    </div>`).join("") || '<p class="muted">还没有阿里云账号</p>';
}

function renderDomains(rows) {
  domainsCache = rows;
  const filter = statusFilter.value;
  const body = document.getElementById("domain-rows");
  const filtered = rows.filter((item) => !filter || item.status === filter);
  body.innerHTML = filtered.map((item) => `
    <tr>
      <td>${escapeHtml(item.display_name)}</td>
      <td><span class="badge ${item.status}">${statusLabel(item.status)}</span>${item.filling ? " 补位中" : ""}</td>
      <td>${escapeHtml(item.registration_at || "-")}</td>
      <td>${escapeHtml(item.aliyun_account_name || "-")}</td>
      <td>${escapeHtml(item.yyds_account_name || "未绑定")}</td>
      <td>${escapeHtml(item.error_reason || (item.from_first_snapshot ? "首次对账" : ""))}</td>
      <td class="row-actions">
        <button type="button" class="ghost" data-status="${item.id}:unused">改未使用</button>
        <button type="button" class="ghost" data-status="${item.id}:used">改已使用</button>
      </td>
    </tr>`).join("") || '<tr><td colspan="7" class="muted">暂无域名</td></tr>';
}

function renderEvents(rows) {
  document.getElementById("event-list").innerHTML = rows.map((item) => `
    <div>
      <b>${escapeHtml(item.created_at || "")}</b>
      <span class="${item.level === "error" ? "error" : "muted"}"> ${escapeHtml(item.code)}</span>
      <div>${escapeHtml(item.message)}</div>
    </div>`).join("") || '<div class="muted">还没有日志</div>';
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

async function refresh() {
  const [overview, domains, events] = await Promise.all([
    api("/api/overview"),
    api("/api/domains"),
    api("/api/events"),
  ]);
  renderOverview(overview);
  renderDomains(domains);
  renderEvents(events);
}

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = new FormData(event.target).get("password");
  const errorEl = document.getElementById("login-error");
  errorEl.textContent = "";
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password }) });
    showApp();
    await refresh();
  } catch (err) {
    errorEl.textContent = err.message;
  }
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST", body: "{}" });
  showLogin();
});

document.getElementById("scan-btn").addEventListener("click", async () => {
  await api("/api/scan", { method: "POST", body: "{}" });
  setTimeout(refresh, 800);
});

statusFilter.addEventListener("change", () => renderDomains(domainsCache));

document.body.addEventListener("click", async (event) => {
  const openId = event.target.getAttribute("data-open");
  if (openId) {
    const dialog = document.getElementById(openId);
    dialog.querySelector("form").reset();
    dialog.querySelector("[name=id]").value = "";
    dialog.showModal();
    return;
  }
  const status = event.target.getAttribute("data-status");
  if (status) {
    const [id, value] = status.split(":");
    await api(`/api/domains/${id}`, { method: "PATCH", body: JSON.stringify({ status: value }) });
    await refresh();
    return;
  }
  const editYyds = event.target.getAttribute("data-edit-yyds");
  if (editYyds) {
    const item = overviewCache.yyds.find((row) => row.id === editYyds);
    fillForm("yyds-form", item);
    return;
  }
  const editAliyun = event.target.getAttribute("data-edit-aliyun");
  if (editAliyun) {
    const item = overviewCache.aliyun.find((row) => row.id === editAliyun);
    fillForm("aliyun-form", item);
    return;
  }
  const delYyds = event.target.getAttribute("data-del-yyds");
  if (delYyds && confirm("只从本监控移除这个 yyds 账号，不会删除 yyds 网站上的域名。确定？")) {
    await api(`/api/yyds-accounts/${delYyds}`, { method: "DELETE" });
    await refresh();
  }
  const delAliyun = event.target.getAttribute("data-del-aliyun");
  if (delAliyun && confirm("从本监控移除这个阿里云账号？")) {
    await api(`/api/aliyun-accounts/${delAliyun}`, { method: "DELETE" });
    await refresh();
  }
});

function fillForm(id, item) {
  const dialog = document.getElementById(id);
  const form = dialog.querySelector("form");
  form.reset();
  form.elements.id.value = item.id;
  form.elements.name.value = item.name;
  if (form.elements.username) form.elements.username.value = item.username;
  if (form.elements.access_key_id) form.elements.access_key_id.value = item.access_key_id;
  if (form.elements.sort_order) form.elements.sort_order.value = item.sort_order;
  if (form.elements.receive_enabled) form.elements.receive_enabled.checked = item.receive_enabled;
  form.elements.enabled.checked = item.enabled;
  dialog.showModal();
}

function bindDialog(id, endpoint) {
  const dialog = document.getElementById(id);
  dialog.querySelector("form").addEventListener("submit", async (event) => {
    if (event.submitter && event.submitter.value === "cancel") return;
    event.preventDefault();
    const form = event.target;
    const data = Object.fromEntries(new FormData(form).entries());
    const payload = {
      name: data.name,
      enabled: form.elements.enabled.checked,
    };
    if (form.elements.access_key_id) {
      payload.access_key_id = data.access_key_id;
      payload.access_key_secret = data.access_key_secret || null;
    }
    if (form.elements.username) {
      payload.username = data.username;
      payload.password = data.password || null;
      payload.sort_order = Number(data.sort_order || 100);
      payload.receive_enabled = form.elements.receive_enabled.checked;
    }
    const accountId = data.id;
    if (accountId) {
      await api(`${endpoint}/${accountId}`, { method: "PATCH", body: JSON.stringify(payload) });
    } else {
      await api(endpoint, { method: "POST", body: JSON.stringify(payload) });
    }
    dialog.close();
    await refresh();
  });
}

bindDialog("aliyun-form", "/api/aliyun-accounts");
bindDialog("yyds-form", "/api/yyds-accounts");

async function boot() {
  try {
    await api("/api/overview");
    showApp();
    await refresh();
    setInterval(() => { if (!appView.classList.contains("hidden")) refresh().catch(() => {}); }, 8000);
  } catch {
    showLogin();
  }
}

boot();
