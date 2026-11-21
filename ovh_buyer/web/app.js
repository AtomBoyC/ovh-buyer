/* OVH Sniper Frontend Controller (Zero Dependencies) */

const state = {
  meta: null,
  status: null,
  accounts: [],
  tasks: [],
  radar: [],
  vps: [],
  orders: [],
  catalog: [],
  logs: [],
  logSource: null,
  activeTab: "dashboard",
  selectedDcs: [],
};

const TAB_TITLES = {
  dashboard: "总览仪表盘",
  catalog: "目录与实时查货",
  tasks: "抢购任务队列",
  radar: "黑五新品 & 配置雷达",
  vps: "VPS 库存监控",
  orders: "订单与历史记录",
  accounts: "多账号 API 管理",
  settings: "通知与系统设置",
  logs: "实时运行日志",
};

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, type = "info") {
  const box = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = `toast ${type === "ok" ? "ok" : type === "err" ? "err" : ""}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, opts = {}) {
  const init = {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    credentials: "same-origin",
  };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const res = await fetch(path, init);
  if (res.status === 401 && path !== "/api/auth/login") {
    showLoginOverlay();
    throw new Error("未授权，请登录");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function showLoginOverlay() {
  document.getElementById("login-overlay").classList.remove("hidden");
  document.getElementById("login-password").focus();
}

function hideLoginOverlay() {
  document.getElementById("login-overlay").classList.add("hidden");
}

function openModal(id) {
  document.getElementById(id).classList.remove("hidden");
}

function closeModal(id) {
  document.getElementById(id).classList.add("hidden");
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  document.getElementById("page-title").textContent = TAB_TITLES[tab] || tab;
  if (tab === "settings") loadSettings();
  if (tab === "logs") loadLogs();
}

function formatUptime(sec) {
  if (!sec || sec < 60) return `${sec || 0}s`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// ---------- Data Loaders ----------

async function refreshAll() {
  try {
    const [status, accs, tasks, radar, vps, orders] = await Promise.all([
      api("/api/status"),
      api("/api/accounts"),
      api("/api/tasks"),
      api("/api/radar"),
      api("/api/vps"),
      api("/api/orders?limit=100"),
    ]);
    state.status = status;
    state.accounts = accs.items || [];
    state.tasks = tasks.items || [];
    state.radar = radar.items || [];
    state.vps = vps.items || [];
    state.orders = orders.items || [];
    renderStatus();
    renderDashboard();
    renderTasks();
    renderRadar();
    renderVps();
    renderOrders();
    renderAccounts();
    populateAccountSelects();
  } catch (err) {
    if (!String(err.message).includes("未授权")) {
      console.error(err);
    }
  }
}

function renderStatus() {
  const st = state.status;
  if (!st) return;
  const s = st.stats || {};
  document.getElementById("kpi-running").textContent = s.tasks_running || 0;
  document.getElementById("kpi-tasks-sub").textContent = `总计 ${s.tasks_total || 0} 个任务 · 已完成 ${s.tasks_completed || 0}`;
  document.getElementById("kpi-orders").textContent = s.orders_success || 0;
  document.getElementById("kpi-dryrun-sub").textContent = `演练 ${s.orders_dry_run || 0} 次 · 失败 ${s.orders_failed || 0}`;
  document.getElementById("kpi-polls").textContent = `${st.total_polls || 0} / ${st.stock_hits || 0}`;
  document.getElementById("kpi-uptime-sub").textContent = `引擎已运行 ${formatUptime(st.uptime_seconds)}`;
  document.getElementById("kpi-accounts").textContent = `${s.accounts_count || 0} / ${s.active_radars || 0}`;
  document.getElementById("kpi-vps-sub").textContent = `VPS 监控 ${s.active_vps || 0} 个`;
  document.getElementById("nav-tasks-badge").textContent = s.tasks_running || 0;

  document.getElementById("toggle-sniper").checked = !!st.sniper_enabled;
  document.getElementById("toggle-radar").checked = !!st.radar_enabled;
  document.getElementById("toggle-vps").checked = !!st.vps_monitor_enabled;

  const pill = document.getElementById("sidebar-engine-pill");
  const txt = document.getElementById("sidebar-engine-text");
  if (st.running && st.sniper_enabled) {
    pill.classList.add("ok");
    txt.textContent = "引擎在线 · 抢购待命";
  } else if (st.running) {
    pill.classList.remove("ok");
    txt.textContent = "引擎在线 · 抢购已暂停";
  } else {
    pill.classList.remove("ok");
    txt.textContent = "引擎未启动";
  }
}

function renderDashboard() {
  const tBox = document.getElementById("dash-tasks-list");
  const running = state.tasks.filter((t) => t.status === "running").slice(0, 6);
  if (!running.length) {
    tBox.innerHTML = `<div class="muted">暂无运行中的抢购任务，点击右上角“+ 新建抢购任务”或从“目录与查货”一键添加。</div>`;
  } else {
    tBox.innerHTML = running
      .map((t) => {
        const dcs = t.datacenters.length ? t.datacenters.map((d) => d.toUpperCase()).join(", ") : "ALL";
        const dryBadge = t.dry_run ? `<span class="pill pill-blue">演练</span>` : "";
        return `<div class="list-row">
          <div>
            <div><b>${esc(t.name)}</b> <code class="mono">${esc(t.plan_code)}</code> ${dryBadge}</div>
            <div class="muted">机房: ${esc(dcs)} · ${esc(t.last_status_msg || "等待首次轮询...")}</div>
          </div>
          <span class="pill pill-green">${t.ordered_count}/${t.max_orders}</span>
        </div>`;
      })
      .join("");
  }

  const oBox = document.getElementById("dash-orders-list");
  const recent = state.orders.slice(0, 6);
  if (!recent.length) {
    oBox.innerHTML = `<div class="muted">暂无下单或演练记录。</div>`;
  } else {
    oBox.innerHTML = recent
      .map((o) => {
        const pill = statusPillForOrder(o.status);
        const pay = o.order_url ? `<a href="${esc(o.order_url)}" target="_blank" rel="noreferrer">去付款 ↗</a>` : "";
        return `<div class="list-row">
          <div>
            <div><code class="mono">${esc(o.plan_code)}</code> @ <b>${esc(o.datacenter.toUpperCase())}</b> ${pill}</div>
            <div class="muted">${esc(o.created_at)} · ${esc(o.order_id || o.error_message || "-")}</div>
          </div>
          <div>${pay}</div>
        </div>`;
      })
      .join("");
  }
}

function statusPillForOrder(st) {
  if (st === "success") return `<span class="pill pill-green">抢购成功</span>`;
  if (st === "dry_run") return `<span class="pill pill-blue">演练通过</span>`;
  if (st === "budget_exceeded") return `<span class="pill pill-amber">超预算取消</span>`;
  return `<span class="pill pill-red">失败</span>`;
}

// ---------- Catalog ----------

async function loadCatalog(force = false) {
  const ep = document.getElementById("cat-endpoint").value;
  const sub = document.getElementById("cat-subsidiary").value;
  const prod = document.getElementById("cat-product").value;
  const search = document.getElementById("cat-search").value.trim();
  const tbody = document.getElementById("catalog-tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="muted text-center">正在拉取 OVH 目录 (${esc(ep)}/${esc(sub)})...</td></tr>`;
  try {
    const q = new URLSearchParams({ endpoint: ep, subsidiary: sub, product_line: prod, search, refresh: force ? "1" : "0" });
    const data = await api(`/api/catalog?${q.toString()}`);
    state.catalog = data.items || [];
    document.getElementById("catalog-meta").textContent = `共加载 ${state.catalog.length} 个型号 (${ep} / ${sub} / ${prod})`;
    renderCatalog();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="error-text text-center">拉取失败: ${esc(err.message)}</td></tr>`;
    toast(`拉取目录失败: ${err.message}`, "err");
  }
}

function renderCatalog() {
  const tbody = document.getElementById("catalog-tbody");
  if (!state.catalog.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted text-center">未匹配到任何型号</td></tr>`;
    return;
  }
  const ep = document.getElementById("cat-endpoint").value;
  tbody.innerHTML = state.catalog
    .slice(0, 120)
    .map((p) => {
      const priceStr =
        p.price !== null && p.price !== undefined
          ? `<b>${p.price} ${esc(p.currency)}</b> / ${esc(p.defaultDuration)}`
          : `<span class="muted">查询确认</span>`;
      const setup = p.setupFee ? `<div class="muted">安装费 ${p.setupFee} ${esc(p.currency)}</div>` : "";
      return `<tr>
        <td>
          <div><code class="mono">${esc(p.planCode)}</code></div>
          <div class="muted">${esc(p.name)}</div>
        </td>
        <td>${esc(p.cpu)}</td>
        <td>
          <div>${esc(p.memory)}</div>
          <div class="muted">${esc(p.storage)}</div>
        </td>
        <td>${priceStr}${setup}</td>
        <td>
          <div class="toolbar">
            <button class="btn btn-ghost btn-sm" onclick="inspectAvailability('${esc(p.planCode)}','${esc(ep)}')">查库存</button>
            <button class="btn btn-primary btn-sm" onclick="openTaskFromCatalog('${esc(p.planCode)}')">+ 抢购</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

async function inspectAvailability(planCode, endpoint = "ovh-eu") {
  document.getElementById("avail-modal-title").textContent = `实时库存: ${planCode} (${endpoint})`;
  const body = document.getElementById("avail-modal-body");
  body.innerHTML = `<div class="muted">正在向 OVH API 查询实时库存...</div>`;
  openModal("avail-modal");
  try {
    const res = await api(`/api/availability/${encodeURIComponent(planCode)}?endpoint=${encodeURIComponent(endpoint)}`);
    body.innerHTML = renderAvailabilityCards(planCode, res.items || []);
  } catch (err) {
    body.innerHTML = `<div class="error-text">查询失败: ${esc(err.message)}</div>`;
  }
}

function renderAvailabilityCards(planCode, items) {
  if (!items.length) {
    return `<div class="muted">未查询到 <code>${esc(planCode)}</code> 的可用性数据（可能型号不存在或已下线）。</div>`;
  }
  return items
    .map((entry) => {
      const chips = (entry.datacenters || [])
        .map((dc) => {
          const cls = dc.orderable ? "pill-green" : "pill-gray";
          const btn = dc.orderable
            ? ` onclick="openTaskFromQuick('${esc(planCode)}','${esc(dc.datacenter)}','${esc(entry.memory)}','${esc(entry.storage)}')" style="cursor:pointer" title="点击添加该机房抢购任务"`
            : "";
          return `<span class="pill ${cls}"${btn}>${esc(dc.datacenter.toUpperCase())}: ${esc(dc.availability)}</span>`;
        })
        .join(" ");
      return `<div class="list-row" style="flex-direction:column;align-items:stretch;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div><code class="mono">${esc(entry.fqn)}</code></div>
          <div class="muted">${esc(entry.memory || "默认内存")} · ${esc(entry.storage || "默认存储")}</div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">${chips || '<span class="muted">无机房数据</span>'}</div>
      </div>`;
    })
    .join("");
}

function openTaskFromCatalog(planCode) {
  const plan = state.catalog.find((x) => x.planCode === planCode);
  openTaskModal({
    name: plan ? `${plan.planCode} (${plan.cpu.slice(0, 20)})` : planCode,
    plan_code: planCode,
    product_line: plan ? plan.productLine : "eco",
    duration: plan ? plan.defaultDuration : "auto",
    pricing_mode: plan ? plan.defaultPricingMode : "auto",
    dry_run: true,
  });
}

function openTaskFromQuick(planCode, dc, memory, storage) {
  closeModal("avail-modal");
  openTaskModal({
    name: `${planCode} @ ${dc.toUpperCase()}`,
    plan_code: planCode,
    datacenters: [dc],
    memory_filter: memory || "",
    storage_filter: storage || "",
    dry_run: false,
  });
}

// ---------- Tasks ----------

function renderTasks() {
  const tbody = document.getElementById("tasks-tbody");
  if (!state.tasks.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted text-center">暂无抢购任务，点击“+ 新建任务”开始配置</td></tr>`;
    return;
  }
  const accMap = Object.fromEntries(state.accounts.map((a) => [a.id, a]));
  tbody.innerHTML = state.tasks
    .map((t) => {
      const acc = accMap[t.account_id];
      const accLabel = acc ? `${acc.name} (${acc.subsidiary})` : "默认账号";
      const dcs = t.datacenters.length ? t.datacenters.map((d) => d.toUpperCase()).join(", ") : "全部机房";
      const hw = [t.memory_filter && `内存:${t.memory_filter}`, t.storage_filter && `盘:${t.storage_filter}`]
        .filter(Boolean)
        .join(" · ");
      const flags = [
        t.dry_run ? `<span class="pill pill-blue">演练</span>` : `<span class="pill pill-amber">实单</span>`,
        t.auto_pay ? `<span class="pill pill-green">自动付</span>` : "",
        t.mode === "notify_only" ? `<span class="pill pill-gray">仅通知</span>` : "",
        t.max_price > 0 ? `<span class="pill pill-gray">≤${t.max_price}</span>` : "",
      ].join(" ");
      const stPill =
        t.status === "running"
          ? `<span class="pill pill-green">运行中</span>`
          : t.status === "completed"
          ? `<span class="pill pill-blue">已完成</span>`
          : `<span class="pill pill-gray">已暂停</span>`;
      const toggleLabel = t.status === "running" ? "暂停" : "启动";
      const nextStatus = t.status === "running" ? "paused" : "running";
      return `<tr>
        <td>
          <div><b>${esc(t.name)}</b></div>
          <div><code class="mono">${esc(t.plan_code)}</code> <span class="muted">· ${esc(accLabel)}</span></div>
        </td>
        <td>
          <div class="mono">${esc(dcs)}</div>
          <div class="muted">${esc(hw || (t.options.length ? t.options.join(", ") : "默认/任意配置"))}</div>
        </td>
        <td>
          <div style="display:flex;gap:4px;flex-wrap:wrap">${flags}</div>
          <div class="muted mt-6">每 ${t.poll_interval}s · ${esc(t.duration)} / ${esc(t.pricing_mode)}</div>
        </td>
        <td>
          ${stPill}
          <div class="muted mt-6">已购 ${t.ordered_count}/${t.max_orders} · 轮询 ${t.attempts} 次</div>
        </td>
        <td style="max-width:220px">
          <div class="muted">${esc(t.last_status_msg || "尚未轮询")}</div>
          <div class="muted">${esc(t.last_check_at || "")}</div>
        </td>
        <td>
          <div class="toolbar">
            <button class="btn btn-ghost btn-sm" onclick="toggleTaskStatus('${t.id}','${nextStatus}')">${toggleLabel}</button>
            <button class="btn btn-success btn-sm" onclick="runTaskNow('${t.id}', true)" title="演练创建购物车与实时报价">演练</button>
            <button class="btn btn-ghost btn-sm" onclick="editTask('${t.id}')">编辑</button>
            <button class="btn btn-danger btn-sm" onclick="deleteTask('${t.id}')">删</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

function buildDcPicker(selected = []) {
  state.selectedDcs = selected.map((s) => s.toLowerCase());
  const dcs = (state.meta && state.meta.datacenters) || {};
  const picker = document.getElementById("dc-picker");
  picker.innerHTML = Object.entries(dcs)
    .map(([code, info]) => {
      const sel = state.selectedDcs.includes(code) ? "selected" : "";
      return `<span class="dc-chip ${sel}" data-dc="${code}" onclick="toggleDcChip('${code}')">${info.flag} ${code.toUpperCase()} ${info.name}</span>`;
    })
    .join("");
  document.getElementById("task-dcs-input").value = state.selectedDcs.join(",");
}

function toggleDcChip(code) {
  const idx = state.selectedDcs.indexOf(code);
  if (idx >= 0) state.selectedDcs.splice(idx, 1);
  else state.selectedDcs.push(code);
  buildDcPicker(state.selectedDcs);
}

function openTaskModal(prefill = {}) {
  const form = document.getElementById("task-form");
  form.reset();
  form.elements.id.value = prefill.id || "";
  document.getElementById("task-modal-title").textContent = prefill.id ? "编辑抢购任务" : "新建抢购任务";
  form.elements.name.value = prefill.name || "";
  form.elements.account_id.value = prefill.account_id || (state.accounts[0] ? state.accounts[0].id : "");
  form.elements.product_line.value = prefill.product_line || "eco";
  form.elements.plan_code.value = prefill.plan_code || "";
  form.elements.memory_filter.value = prefill.memory_filter || "";
  form.elements.storage_filter.value = prefill.storage_filter || "";
  form.elements.options.value = (prefill.options || []).join(",");
  form.elements.max_price.value = prefill.max_price ?? 0;
  form.elements.duration.value = prefill.duration || "auto";
  form.elements.pricing_mode.value = prefill.pricing_mode || "auto";
  form.elements.mode.value = prefill.mode || "buy";
  form.elements.max_orders.value = prefill.max_orders || 1;
  form.elements.poll_interval.value = prefill.poll_interval || 5;
  form.elements.os_code.value = prefill.os_code || "none_64.en";
  form.elements.coupons.value = (prefill.coupons || []).join(",");
  form.elements.dry_run.checked = !!prefill.dry_run;
  form.elements.auto_pay.checked = !!prefill.auto_pay;
  form.elements.only_instant.checked = !!prefill.only_instant;
  form.elements.price_includes_tax.checked = !!prefill.price_includes_tax;
  form.elements.waive_retractation.checked = prefill.waive_retractation !== false;
  buildDcPicker(prefill.datacenters || []);
  openModal("task-modal");
}

function editTask(id) {
  const t = state.tasks.find((x) => x.id === id);
  if (t) openTaskModal(t);
}

async function toggleTaskStatus(id, status) {
  try {
    await api(`/api/tasks/${id}`, { method: "PUT", body: { status } });
    toast(status === "running" ? "任务已启动" : "任务已暂停", "ok");
    refreshAll();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function runTaskNow(id, dryRun = true) {
  toast(dryRun ? "正在执行实测演练（创建临时购物车并校验价格）..." : "正在立即触发检测...", "info");
  try {
    const res = await api(`/api/tasks/${id}/run`, { method: "POST", body: { dry_run: dryRun } });
    if (res.success) {
      const priceText = res.price_without_tax != null ? `不含税 ${res.price_without_tax} / 含税 ${res.price_with_tax} ${res.currency}` : "";
      toast(`演练通过! ${priceText}`, "ok");
    } else {
      toast(`演练未通过: ${res.error}`, "err");
    }
    refreshAll();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function deleteTask(id) {
  if (!confirm("确定删除该抢购任务？")) return;
  try {
    await api(`/api/tasks/${id}`, { method: "DELETE" });
    toast("任务已删除", "ok");
    refreshAll();
  } catch (err) {
    toast(err.message, "err");
  }
}

// ---------- Radar ----------

function renderRadar() {
  const tbody = document.getElementById("radar-tbody");
  if (!state.radar.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted text-center">暂无黑五雷达规则，点击“+ 新建雷达规则”提前埋伏新品！</td></tr>`;
    return;
  }
  tbody.innerHTML = state.radar
    .map((r) => {
      const typeLabel = r.rule_type === "new_sku" ? `<span class="pill pill-amber">新品上新</span>` : `<span class="pill pill-blue">配置指纹</span>`;
      const conds = [
        r.keyword_pattern && `正则:${r.keyword_pattern}`,
        r.memory_pattern && `内存:${r.memory_pattern}`,
        r.storage_pattern && `存储:${r.storage_pattern}`,
      ]
        .filter(Boolean)
        .join(" | ");
      return `<tr>
        <td><b>${esc(r.name)}</b><div class="mt-6">${typeLabel} ${r.enabled ? '<span class="pill pill-green">启用</span>' : '<span class="pill pill-gray">停用</span>'}</div></td>
        <td><div>${esc(r.endpoint)} / ${esc(r.subsidiary)}</div><div class="muted">${esc(conds || "全部新品")}</div></td>
        <td><div>上限: ${r.max_price > 0 ? r.max_price : "不限"}</div><div class="muted">${r.auto_snipe ? (r.dry_run ? "自动演练抢购" : "自动实单抢购") : "仅推送提醒"}</div></td>
        <td><div>基线 ${r.known_plan_codes.length} · 命中 ${r.matched_plan_codes.length}</div><div class="muted">${esc(r.matched_plan_codes.slice(-3).join(", ") || r.last_check_at || "未扫描")}</div></td>
        <td>
          <div class="toolbar">
            <button class="btn btn-ghost btn-sm" onclick="toggleRadar('${r.id}', ${!r.enabled})">${r.enabled ? "停用" : "启用"}</button>
            <button class="btn btn-danger btn-sm" onclick="deleteRadar('${r.id}')">删</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

async function toggleRadar(id, enabled) {
  await api(`/api/radar/${id}`, { method: "PUT", body: { enabled } });
  refreshAll();
}

async function deleteRadar(id) {
  if (!confirm("确定删除该雷达规则？")) return;
  await api(`/api/radar/${id}`, { method: "DELETE" });
  toast("雷达规则已删除", "ok");
  refreshAll();
}

// ---------- VPS ----------

function renderVps() {
  const box = document.getElementById("vps-list");
  if (!state.vps.length) {
    box.innerHTML = `<div class="muted">暂无 VPS 监控项，点击“+ 添加 VPS 监控”添加热门套餐（如 vps-2025-model1）。</div>`;
    return;
  }
  box.innerHTML = state.vps
    .map((w) => {
      const states = Object.entries(w.last_state || {})
        .map(([dc, info]) => {
          const st = (info && info.status) || "unknown";
          const cls = st === "available" ? "pill-green" : "pill-gray";
          return `<span class="pill ${cls}">${esc(dc)}: ${esc(st)}</span>`;
        })
        .join(" ");
      return `<div class="list-row" style="flex-direction:column;align-items:stretch">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
          <div>
            <code class="mono">${esc(w.plan_code)}</code>
            <span class="muted">(${esc(w.endpoint)} / ${esc(w.subsidiary)})</span>
            ${w.auto_order ? `<span class="pill ${w.dry_run ? "pill-blue" : "pill-amber"}">${w.dry_run ? "自动演练" : "自动下单"}</span>` : `<span class="pill pill-gray">仅监控</span>`}
          </div>
          <div class="toolbar">
            <span class="muted">${esc(w.last_check_at || "尚未检测")}</span>
            <button class="btn btn-ghost btn-sm" onclick="toggleVps('${w.id}', ${!w.enabled})">${w.enabled ? "暂停" : "启用"}</button>
            <button class="btn btn-danger btn-sm" onclick="deleteVps('${w.id}')">删除</button>
          </div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">${states || '<span class="muted">点击“立即刷新全部”获取各机房库存状态</span>'}</div>
      </div>`;
    })
    .join("");
}

async function toggleVps(id, enabled) {
  await api(`/api/vps/${id}`, { method: "PUT", body: { enabled } });
  refreshAll();
}

async function deleteVps(id) {
  if (!confirm("确定删除该 VPS 监控？")) return;
  await api(`/api/vps/${id}`, { method: "DELETE" });
  refreshAll();
}

// ---------- Orders ----------

function renderOrders() {
  const tbody = document.getElementById("orders-tbody");
  if (!state.orders.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted text-center">暂无订单历史</td></tr>`;
    return;
  }
  tbody.innerHTML = state.orders
    .map((o) => {
      const price =
        o.price_without_tax != null || o.price_with_tax != null
          ? `${o.price_without_tax ?? "-"} / ${o.price_with_tax ?? "-"} ${esc(o.currency)}`
          : "-";
      const link = o.order_url
        ? `<a class="btn btn-primary btn-sm" href="${esc(o.order_url)}" target="_blank" rel="noreferrer">前往付款 #${esc(o.order_id)} ↗</a>`
        : `<span class="muted">${esc(o.order_id || o.error_message || "-")}</span>`;
      return `<tr>
        <td class="muted">${esc(o.created_at)}</td>
        <td><code class="mono">${esc(o.plan_code)}</code> @ <b>${esc(o.datacenter.toUpperCase())}</b><div class="muted">${esc((o.options || []).join(", "))}</div></td>
        <td>${esc(o.account_name || "-")}</td>
        <td>${statusPillForOrder(o.status)}</td>
        <td class="mono">${price}</td>
        <td>${link}</td>
      </tr>`;
    })
    .join("");
}

// ---------- Accounts ----------

function renderAccounts() {
  const tbody = document.getElementById("accounts-tbody");
  if (!state.accounts.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted text-center">尚未配置 OVH API 账号。仅查库存可不配置；自动下单请先点击“+ 添加 OVH 账号”。</td></tr>`;
    return;
  }
  tbody.innerHTML = state.accounts
    .map((a) => {
      const def = a.is_default ? `<span class="pill pill-amber">默认</span>` : "";
      const ver = a.nichandle
        ? `<span class="pill pill-green">${esc(a.nichandle)} (${esc(a.currency)})</span><div class="muted">${esc(a.last_verified_at)}</div>`
        : `<span class="muted">未验证</span>`;
      return `<tr>
        <td><b>${esc(a.name)}</b> ${def}</td>
        <td><code>${esc(a.endpoint)}</code> / <b>${esc(a.subsidiary)}</b></td>
        <td class="mono">${esc(a.app_key)}</td>
        <td>${ver}</td>
        <td>
          <div class="toolbar">
            <button class="btn btn-success btn-sm" onclick="testAccount('${a.id}')">测试连通</button>
            <button class="btn btn-ghost btn-sm" onclick="editAccount('${a.id}')">编辑</button>
            <button class="btn btn-danger btn-sm" onclick="deleteAccount('${a.id}')">删</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

function populateAccountSelects() {
  const opts = state.accounts.length
    ? state.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} (${esc(a.endpoint)}/${esc(a.subsidiary)})</option>`).join("")
    : `<option value="">（尚未配置 OVH 账号）</option>`;
  ["task-account-select", "radar-account-select", "vps-account-select"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = opts;
  });
}

function editAccount(id) {
  const a = state.accounts.find((x) => x.id === id);
  if (!a) return;
  const form = document.getElementById("account-form");
  form.reset();
  document.getElementById("account-modal-title").textContent = "编辑 OVH 账号";
  form.elements.id.value = a.id;
  form.elements.name.value = a.name;
  form.elements.endpoint.value = a.endpoint;
  form.elements.subsidiary.value = a.subsidiary;
  form.elements.app_key.value = a.app_key;
  form.elements.app_secret.value = "";
  form.elements.consumer_key.value = "";
  form.elements.is_default.checked = !!a.is_default;
  openModal("account-modal");
}

async function testAccount(id) {
  toast("正在验证 OVH API 凭据...", "info");
  try {
    const res = await api(`/api/accounts/${id}/test`, { method: "POST", body: {} });
    const info = res.info || {};
    const bal = info.credit_balance != null ? ` | 余额: ${info.credit_balance} ${info.currency}` : "";
    toast(`验证成功! 账号 ${info.nichandle} (${info.subsidiary})${bal}`, "ok");
    refreshAll();
  } catch (err) {
    toast(`验证失败: ${err.message}`, "err");
  }
}

async function deleteAccount(id) {
  if (!confirm("确定删除该 OVH 账号？")) return;
  await api(`/api/accounts/${id}`, { method: "DELETE" });
  toast("账号已删除", "ok");
  refreshAll();
}

// ---------- Settings ----------

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    const form = document.getElementById("settings-form");
    for (const el of form.elements) {
      if (!el.name || !(el.name in s)) continue;
      if (el.type === "checkbox") el.checked = !!s[el.name];
      else el.value = s[el.name] ?? "";
    }
  } catch (err) {
    toast(err.message, "err");
  }
}

// ---------- Logs & SSE ----------

async function loadLogs() {
  const level = document.getElementById("log-filter-level").value;
  const source = document.getElementById("log-filter-source").value;
  const q = new URLSearchParams({ limit: "250", level, source });
  try {
    const data = await api(`/api/logs?${q.toString()}`);
    state.logs = data.items || [];
    renderLogs();
  } catch (err) {
    console.error(err);
  }
}

function renderLogLine(entry) {
  return `<div class="log-line"><span class="log-ts">${esc(entry.created_at)}</span><span class="log-src">[${esc(entry.source)}]</span><span class="log-lvl-${esc(entry.level)}">${esc(entry.level)}</span> ${esc(entry.message)}</div>`;
}

function renderLogs() {
  const term = document.getElementById("log-terminal");
  term.innerHTML = state.logs.map(renderLogLine).join("") || `<div class="muted">暂无日志</div>`;
  if (document.getElementById("log-autoscroll").checked) {
    term.scrollTop = term.scrollHeight;
  }
}

function connectLogStream() {
  if (state.logSource) state.logSource.close();
  const es = new EventSource("/api/logs/stream");
  es.onmessage = (ev) => {
    try {
      const entry = JSON.parse(ev.data);
      const lvlFilter = document.getElementById("log-filter-level").value;
      const srcFilter = document.getElementById("log-filter-source").value;
      if (lvlFilter && entry.level !== lvlFilter) return;
      if (srcFilter && entry.source !== srcFilter) return;
      state.logs.push(entry);
      if (state.logs.length > 400) state.logs.shift();
      const term = document.getElementById("log-terminal");
      term.insertAdjacentHTML("beforeend", renderLogLine(entry));
      if (document.getElementById("log-autoscroll").checked) {
        term.scrollTop = term.scrollHeight;
      }
    } catch (_) {}
  };
  state.logSource = es;
}

// ---------- Event Listeners & Init ----------

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Login form
  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = document.getElementById("login-error");
    errEl.classList.add("hidden");
    try {
      await api("/api/auth/login", { method: "POST", body: { password: document.getElementById("login-password").value } });
      hideLoginOverlay();
      document.getElementById("btn-logout").classList.remove("hidden");
      await bootAfterAuth();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  });

  document.getElementById("btn-logout").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST", body: {} });
    showLoginOverlay();
  });

  // Engine toggles
  [
    ["toggle-sniper", "sniper_enabled"],
    ["toggle-radar", "radar_enabled"],
    ["toggle-vps", "vps_monitor_enabled"],
  ].forEach(([elId, key]) => {
    document.getElementById(elId).addEventListener("change", async (e) => {
      try {
        state.status = await api("/api/engine/toggle", { method: "POST", body: { [key]: e.target.checked } });
        renderStatus();
        toast("引擎状态已更新", "ok");
      } catch (err) {
        toast(err.message, "err");
      }
    });
  });

  // Quick check form
  document.getElementById("quick-check-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = document.getElementById("quick-check-code").value.trim();
    const ep = document.getElementById("quick-check-endpoint").value;
    const box = document.getElementById("quick-check-result");
    box.innerHTML = `<div class="muted">查询中...</div>`;
    try {
      const res = await api(`/api/availability/${encodeURIComponent(code)}?endpoint=${encodeURIComponent(ep)}`);
      box.innerHTML = renderAvailabilityCards(code, res.items || []);
    } catch (err) {
      box.innerHTML = `<div class="error-text">${esc(err.message)}</div>`;
    }
  });

  // Catalog
  document.getElementById("btn-load-catalog").addEventListener("click", () => loadCatalog(false));
  document.getElementById("btn-refresh-catalog").addEventListener("click", () => loadCatalog(true));
  document.getElementById("cat-search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadCatalog(false);
  });

  // Task Modal
  document.getElementById("btn-quick-new-task").addEventListener("click", () => openTaskModal({ dry_run: true }));
  document.getElementById("btn-open-task-modal").addEventListener("click", () => openTaskModal({ dry_run: true }));
  document.getElementById("task-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const payload = {
      name: f.elements.name.value.trim(),
      account_id: f.elements.account_id.value,
      product_line: f.elements.product_line.value,
      plan_code: f.elements.plan_code.value.trim(),
      datacenters: f.elements.datacenters.value,
      memory_filter: f.elements.memory_filter.value.trim(),
      storage_filter: f.elements.storage_filter.value.trim(),
      options: f.elements.options.value,
      max_price: parseFloat(f.elements.max_price.value || "0") || 0,
      duration: f.elements.duration.value,
      pricing_mode: f.elements.pricing_mode.value.trim() || "auto",
      mode: f.elements.mode.value,
      max_orders: parseInt(f.elements.max_orders.value || "1", 10) || 1,
      poll_interval: parseInt(f.elements.poll_interval.value || "5", 10) || 5,
      os_code: f.elements.os_code.value.trim() || "none_64.en",
      coupons: f.elements.coupons.value,
      dry_run: f.elements.dry_run.checked,
      auto_pay: f.elements.auto_pay.checked,
      only_instant: f.elements.only_instant.checked,
      price_includes_tax: f.elements.price_includes_tax.checked,
      waive_retractation: f.elements.waive_retractation.checked,
    };
    const id = f.elements.id.value;
    try {
      if (id) await api(`/api/tasks/${id}`, { method: "PUT", body: payload });
      else await api("/api/tasks", { method: "POST", body: payload });
      closeModal("task-modal");
      toast(id ? "任务已更新" : "抢购任务已创建", "ok");
      refreshAll();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Account Modal
  document.getElementById("btn-open-account-modal").addEventListener("click", () => {
    const f = document.getElementById("account-form");
    f.reset();
    f.elements.id.value = "";
    document.getElementById("account-modal-title").textContent = "添加 OVH 账号";
    openModal("account-modal");
  });
  document.getElementById("account-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const id = f.elements.id.value;
    const payload = {
      name: f.elements.name.value.trim(),
      endpoint: f.elements.endpoint.value,
      subsidiary: f.elements.subsidiary.value.trim().toUpperCase(),
      app_key: f.elements.app_key.value.trim(),
      app_secret: f.elements.app_secret.value.trim(),
      consumer_key: f.elements.consumer_key.value.trim(),
      is_default: f.elements.is_default.checked,
    };
    try {
      if (id) await api(`/api/accounts/${id}`, { method: "PUT", body: payload });
      else await api("/api/accounts", { method: "POST", body: payload });
      closeModal("account-modal");
      toast("账号已保存", "ok");
      refreshAll();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Radar Modal & Scan
  document.getElementById("btn-open-radar-modal").addEventListener("click", () => {
    document.getElementById("radar-form").reset();
    openModal("radar-modal");
  });
  document.getElementById("radar-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/api/radar", {
        method: "POST",
        body: {
          name: f.elements.name.value.trim(),
          rule_type: f.elements.rule_type.value,
          account_id: f.elements.account_id.value,
          endpoint: f.elements.endpoint.value,
          subsidiary: f.elements.subsidiary.value.trim().toUpperCase(),
          keyword_pattern: f.elements.keyword_pattern.value.trim(),
          memory_pattern: f.elements.memory_pattern.value.trim(),
          storage_pattern: f.elements.storage_pattern.value.trim(),
          datacenters: f.elements.datacenters.value,
          max_price: parseFloat(f.elements.max_price.value || "0") || 0,
          auto_snipe: f.elements.auto_snipe.checked,
          dry_run: f.elements.dry_run.checked,
          auto_pay: f.elements.auto_pay.checked,
        },
      });
      closeModal("radar-modal");
      toast("黑五雷达规则已创建", "ok");
      refreshAll();
    } catch (err) {
      toast(err.message, "err");
    }
  });
  document.getElementById("btn-radar-scan").addEventListener("click", async () => {
    toast("正在扫描 OVH 目录...", "info");
    try {
      const res = await api("/api/radar/scan", { method: "POST", body: {} });
      toast(`扫描完成，新命中 ${res.hits || 0} 个目标`, "ok");
      refreshAll();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // VPS Modal & Scan
  document.getElementById("btn-open-vps-modal").addEventListener("click", () => {
    document.getElementById("vps-form").reset();
    openModal("vps-modal");
  });
  document.getElementById("vps-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/api/vps", {
        method: "POST",
        body: {
          plan_code: f.elements.plan_code.value.trim(),
          endpoint: f.elements.endpoint.value,
          subsidiary: f.elements.subsidiary.value.trim().toUpperCase(),
          datacenters: f.elements.datacenters.value,
          account_id: f.elements.account_id.value,
          auto_order: f.elements.auto_order.checked,
          dry_run: f.elements.dry_run.checked,
          auto_pay: f.elements.auto_pay.checked,
        },
      });
      closeModal("vps-modal");
      toast("VPS 监控已添加", "ok");
      refreshAll();
    } catch (err) {
      toast(err.message, "err");
    }
  });
  document.getElementById("btn-vps-scan").addEventListener("click", async () => {
    toast("正在刷新全部 VPS 库存...", "info");
    try {
      await api("/api/vps/check", { method: "POST", body: {} });
      toast("VPS 库存已更新", "ok");
      refreshAll();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Orders actions
  document.getElementById("btn-clear-orders").addEventListener("click", async () => {
    if (!confirm("确定清空本地订单历史记录？")) return;
    await api("/api/orders", { method: "DELETE" });
    toast("已清空本地订单记录", "ok");
    refreshAll();
  });
  document.getElementById("btn-fetch-ovh-orders").addEventListener("click", async () => {
    if (!state.accounts.length) {
      toast("请先配置 OVH 账号", "err");
      return;
    }
    const acc = state.accounts[0];
    toast(`正在从 ${acc.name} 拉取 OVH 官方最近订单...`, "info");
    try {
      const res = await api(`/api/accounts/${acc.id}/ovh-orders`);
      const card = document.getElementById("ovh-official-orders-card");
      const list = document.getElementById("ovh-official-orders-list");
      const items = res.items || [];
      list.innerHTML = items.length
        ? items
            .map(
              (o) => `<div class="list-row">
              <div>
                <b>#${esc(o.orderId)}</b> · <span class="muted">${esc(o.date)}</span>
                <div class="muted">含税: ${(o.priceWithTax && o.priceWithTax.text) || "-"}</div>
              </div>
              ${o.url ? `<a class="btn btn-primary btn-sm" href="${esc(o.url)}" target="_blank" rel="noreferrer">查看 / 付款 ↗</a>` : ""}
            </div>`
            )
            .join("")
        : `<div class="muted">该账号近期无官方订单</div>`;
      card.classList.remove("hidden");
    } catch (err) {
      toast(`拉取失败: ${err.message}`, "err");
    }
  });

  // Settings
  document.getElementById("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = {};
    for (const el of f.elements) {
      if (!el.name) continue;
      if (el.type === "checkbox") body[el.name] = el.checked;
      else if (el.type === "number") body[el.name] = parseFloat(el.value);
      else body[el.name] = el.value.trim();
    }
    try {
      await api("/api/settings", { method: "PUT", body });
      toast("设置已保存并立即生效", "ok");
      loadSettings();
    } catch (err) {
      toast(err.message, "err");
    }
  });
  document.getElementById("btn-test-notify").addEventListener("click", async () => {
    toast("正在发送测试通知...", "info");
    try {
      const res = await api("/api/notifications/test", { method: "POST", body: {} });
      const okChannels = Object.entries(res.results || {})
        .filter(([, v]) => v)
        .map(([k]) => k);
      if (okChannels.length) toast(`测试成功! 已送达: ${okChannels.join(", ")}`, "ok");
      else toast("未送达任何渠道，请先填写并保存至少一个推送渠道", "err");
    } catch (err) {
      toast(err.message, "err");
    }
  });
  document.getElementById("btn-save-password").addEventListener("click", async () => {
    const pw = document.getElementById("new-panel-password").value;
    try {
      await api("/api/auth/password", { method: "POST", body: { password: pw } });
      document.getElementById("new-panel-password").value = "";
      toast(pw ? "面板访问密码已更新" : "已清除数据库密码", "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  });
  document.getElementById("btn-export-config").addEventListener("click", async () => {
    try {
      const data = await api("/api/export");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `ovh-sniper-backup-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Logs
  document.getElementById("log-filter-level").addEventListener("change", loadLogs);
  document.getElementById("log-filter-source").addEventListener("change", loadLogs);
  document.getElementById("btn-clear-logs").addEventListener("click", async () => {
    await api("/api/logs", { method: "DELETE" });
    state.logs = [];
    renderLogs();
  });
}

async function bootAfterAuth() {
  state.meta = await api("/api/meta");
  await refreshAll();
  loadLogs();
  connectLogStream();
  setInterval(refreshAll, 5000);
}

async function init() {
  bindEvents();
  try {
    const auth = await api("/api/auth/status");
    if (auth.auth_enabled) {
      document.getElementById("btn-logout").classList.remove("hidden");
    }
    if (auth.auth_enabled && !auth.authenticated) {
      showLoginOverlay();
      return;
    }
    await bootAfterAuth();
  } catch (err) {
    console.error("Init error:", err);
  }
}

window.switchTab = switchTab;
window.closeModal = closeModal;
window.inspectAvailability = inspectAvailability;
window.openTaskFromCatalog = openTaskFromCatalog;
window.openTaskFromQuick = openTaskFromQuick;
window.toggleDcChip = toggleDcChip;
window.toggleTaskStatus = toggleTaskStatus;
window.runTaskNow = runTaskNow;
window.editTask = editTask;
window.deleteTask = deleteTask;
window.toggleRadar = toggleRadar;
window.deleteRadar = deleteRadar;
window.toggleVps = toggleVps;
window.deleteVps = deleteVps;
window.testAccount = testAccount;
window.editAccount = editAccount;
window.deleteAccount = deleteAccount;

document.addEventListener("DOMContentLoaded", init);
