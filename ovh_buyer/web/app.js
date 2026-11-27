/* OVH Sniper Frontend Controller (Zero Dependencies) */

const state = {
  meta: null,
  status: null,
  fxRates: { EUR: 7.85, USD: 7.25, CAD: 5.20, GBP: 9.20, PLN: 1.85, AUD: 4.75, SGD: 5.40, CNY: 1.0 },
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
  shortLinkPreview: null,
};

function fmtCny(amount, currency = "EUR") {
  if (amount === null || amount === undefined || isNaN(Number(amount))) return "";
  const cur = String(currency || "EUR").toUpperCase();
  if (cur === "CNY") return "";
  const rate = state.fxRates && state.fxRates[cur];
  if (!rate) return "";
  const cny = Math.round(Number(amount) * Number(rate));
  return `≈ ¥${cny}`;
}

const TAB_TITLES = {
  dashboard: "总览",
  catalog: "目录与查货",
  tasks: "抢购任务",
  radar: "黑五雷达",
  vps: "VPS 监控",
  orders: "订单与交付",
  accounts: "OVH 账号",
  settings: "通知与设置",
  logs: "运行日志",
};

const TAB_SUBS = {
  dashboard: "引擎状态、实时库存与最近订单一览",
  catalog: "浏览 OVH 官方目录，查看各机房实时库存与幽灵库存标记",
  tasks: "按型号 / 机房 / 硬件条件自动轮询，有货即下单",
  radar: "黑五新品上线与配置指纹自动发现",
  vps: "各机房 VPS 套餐补货状态",
  orders: "短链下单、订单历史、交付进度与硬件中奖检测",
  accounts: "管理多个 OVH API 凭据，检查默认支付方式",
  settings: "推送渠道、引擎限流与面板安全",
  logs: "SSE 实时日志流",
};

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function syncThemeButtons() {
  const pref = (window.__theme && window.__theme.pref()) || "system";
  document.querySelectorAll("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.themeSet === pref ? "true" : "false"));
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
  document.getElementById("page-sub").textContent = TAB_SUBS[tab] || "";
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
  document.getElementById("toggle-fastlane").checked = !!st.fast_lane_enabled;
  document.getElementById("toggle-radar").checked = !!st.radar_enabled;
  document.getElementById("toggle-vps").checked = !!st.vps_monitor_enabled;

  const pCount = st.monitor_proxy_count || 0;
  document.querySelectorAll(".js-open-proxy-modal").forEach((btn) => {
    btn.textContent = pCount > 0 ? `🌐 监控代理 (${pCount})` : "🌐 配置监控代理";
  });

  const pill = document.getElementById("sidebar-engine-pill");
  const txt = document.getElementById("sidebar-engine-text");
  if (st.running && st.sniper_enabled) {
    pill.classList.add("ok");
    txt.textContent = st.fast_lane_enabled ? "引擎在线 · 快速通道就绪" : "引擎在线 · 抢购待命";
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
  if (st === "pending_confirm") return `<span class="pill pill-red">⚠️ 待核对(防重单)</span>`;
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
  const sub = document.getElementById("cat-subsidiary").value;
  tbody.innerHTML = state.catalog
    .slice(0, 120)
    .map((p) => {
      const cny = p.price !== null && p.price !== undefined ? fmtCny(p.price, p.currency) : "";
      const priceStr =
        p.price !== null && p.price !== undefined
          ? `<b>${p.price} ${esc(p.currency)}</b> / ${esc(p.defaultDuration)}${cny ? ` <span class="muted">(${esc(cny)})</span>` : ""}`
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
            <button class="btn btn-ghost btn-sm" onclick="inspectAvailability('${esc(p.planCode)}','${esc(ep)}','${esc(sub)}')">查库存</button>
            <button class="btn btn-primary btn-sm" onclick="openTaskFromCatalog('${esc(p.planCode)}')">+ 抢购</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

async function inspectAvailability(planCode, endpoint = "ovh-eu", subsidiary = "IE") {
  document.getElementById("avail-modal-title").textContent = `实时库存: ${planCode} (${endpoint}/${subsidiary})`;
  const body = document.getElementById("avail-modal-body");
  body.innerHTML = `<div class="muted">正在向 OVH API 查询实时库存与目录可订性校验...</div>`;
  openModal("avail-modal");
  try {
    const q = new URLSearchParams({ endpoint, subsidiary });
    const res = await api(`/api/availability/${encodeURIComponent(planCode)}?${q.toString()}`);
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
      const cfgOk = entry.configOrderable !== false;
      const phantomBadge = !cfgOk
        ? `<span class="pill pill-red" title="${esc(entry.unorderableReason || "目录无此配件")}">⚠️ 幽灵库存(不可订)</span>`
        : entry.isDefault
        ? `<span class="pill pill-blue">默认标配</span>`
        : "";
      const chips = (entry.datacenters || [])
        .map((dc) => {
          const cls = dc.orderable ? "pill-green" : "pill-gray";
          const btn = dc.orderable
            ? ` onclick="openTaskFromQuick('${esc(planCode)}','${esc(dc.datacenter)}','${esc(entry.memory)}','${esc(entry.storage)}')" style="cursor:pointer" title="点击添加该机房抢购任务"`
            : "";
          const expressBtn =
            dc.orderable && dc.express_url
              ? `<a class="dc-express-link" href="${esc(dc.express_url)}" target="_blank" rel="noreferrer" title="直接打开 OVH 官网 Express 结账页">直达↗</a>`
              : "";
          return `<span class="dc-action-group"><span class="pill ${cls}"${btn}>${esc(dc.datacenter.toUpperCase())}: ${esc(dc.availability)}</span>${expressBtn}</span>`;
        })
        .join(" ");
      const reasonLine = !cfgOk && entry.unorderableReason ? `<div class="error-text mt-6" style="margin-bottom:0">⚠️ ${esc(entry.unorderableReason)}</div>` : "";
      return `<div class="list-row" style="flex-direction:column;align-items:stretch;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div><code class="mono">${esc(entry.fqn)}</code> ${phantomBadge}</div>
          <div class="muted">${esc(entry.memory || "默认内存")} · ${esc(entry.storage || "默认存储")}</div>
        </div>
        ${reasonLine}
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
        t.fast_lane ? `<span class="pill pill-purple" title="预建购物车快速通道">⚡快速</span>` : "",
        t.dry_run ? `<span class="pill pill-blue">演练</span>` : `<span class="pill pill-amber">实单</span>`,
        t.auto_pay ? `<span class="pill pill-green">自动付</span>` : "",
        t.allow_non_default === false ? `<span class="pill pill-gray">仅标配</span>` : "",
        t.mode === "notify_only" ? `<span class="pill pill-gray">仅通知</span>` : "",
        t.max_price > 0 ? `<span class="pill pill-gray">≤${t.max_price}</span>` : "",
        t.proxy_url ? `<span class="pill pill-blue" title="使用独立监控代理">🌐代理</span>` : "",
      ].join(" ");
      const stPill =
        t.status === "running"
          ? `<span class="pill pill-green">运行中</span>`
          : t.status === "pending_confirm"
          ? `<span class="pill pill-red">⚠️ 待核对(防重单)</span>`
          : t.status === "completed"
          ? `<span class="pill pill-blue">已完成</span>`
          : `<span class="pill pill-gray">已暂停</span>`;
      const slots = (t.fast_lane_slots || [])
        .map((sl) => `${sl.dc.toUpperCase()}(${sl.ttl_minutes}m)`)
        .join(", ");
      const slotLine = slots ? `<div class="muted mt-6" style="color:#d8b4fe">⚡ 预建就绪: ${esc(slots)}</div>` : "";
      const toggleLabel = t.status === "running" ? "暂停" : t.status === "pending_confirm" ? "核对后恢复" : "启动";
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
          ${slotLine}
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
            <button class="btn btn-ghost btn-sm" onclick="toggleTaskStatus('${esc(t.id)}','${nextStatus}')">${toggleLabel}</button>
            <button class="btn btn-success btn-sm" onclick="runTaskNow('${esc(t.id)}', true)" title="演练创建购物车与实时报价">演练</button>
            <button class="btn btn-ghost btn-sm" onclick="editTask('${esc(t.id)}')">编辑</button>
            <button class="btn btn-danger btn-sm" onclick="deleteTask('${esc(t.id)}')">删</button>
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
  form.elements.poll_interval.value = prefill.poll_interval ?? 5;
  form.elements.os_code.value = prefill.os_code || "none_64.en";
  form.elements.coupons.value = (prefill.coupons || []).join(",");
  if (form.elements.proxy_url) form.elements.proxy_url.value = prefill.proxy_url || "";
  form.elements.fast_lane.checked = !!prefill.fast_lane;
  form.elements.allow_non_default.checked = prefill.allow_non_default !== false;
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
      const proxyTag = r.proxy_url ? ` <span class="pill pill-blue" title="已配独立监控代理">🌐代理</span>` : "";
      return `<tr>
        <td><b>${esc(r.name)}</b><div class="mt-6">${typeLabel} ${r.enabled ? '<span class="pill pill-green">启用</span>' : '<span class="pill pill-gray">停用</span>'}${proxyTag}</div></td>
        <td><div>${esc(r.endpoint)} / ${esc(r.subsidiary)}</div><div class="muted">${esc(conds || "全部新品")}</div></td>
        <td><div>上限: ${r.max_price > 0 ? r.max_price : "不限"}</div><div class="muted">${r.auto_snipe ? (r.dry_run ? "自动演练抢购" : "自动实单抢购") : "仅推送提醒"}</div></td>
        <td><div>基线 ${r.known_plan_codes.length} · 命中 ${r.matched_plan_codes.length}</div><div class="muted">${esc(r.matched_plan_codes.slice(-3).join(", ") || r.last_check_at || "未扫描")}</div></td>
        <td>
          <div class="toolbar">
            <button class="btn btn-ghost btn-sm" onclick="toggleRadar('${esc(r.id)}', ${!r.enabled})">${r.enabled ? "停用" : "启用"}</button>
            <button class="btn btn-ghost btn-sm" onclick="editRadar('${esc(r.id)}')">编辑</button>
            <button class="btn btn-danger btn-sm" onclick="deleteRadar('${esc(r.id)}')">删</button>
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

function editRadar(id) {
  const r = state.radar.find((x) => x.id === id);
  if (!r) return;
  const f = document.getElementById("radar-form");
  f.reset();
  document.getElementById("radar-modal-title").textContent = "编辑雷达规则";
  f.elements.id.value = r.id;
  f.elements.name.value = r.name || "";
  f.elements.rule_type.value = r.rule_type || "new_sku";
  f.elements.account_id.value = r.account_id || "";
  f.elements.endpoint.value = r.endpoint || "ovh-eu";
  f.elements.subsidiary.value = r.subsidiary || "IE";
  f.elements.keyword_pattern.value = r.keyword_pattern || "";
  f.elements.memory_pattern.value = r.memory_pattern || "";
  f.elements.storage_pattern.value = r.storage_pattern || "";
  f.elements.datacenters.value = (r.datacenters || []).join(",");
  f.elements.max_price.value = r.max_price ?? 0;
  if (f.elements.proxy_url) f.elements.proxy_url.value = r.proxy_url || "";
  f.elements.auto_snipe.checked = !!r.auto_snipe;
  f.elements.dry_run.checked = !!r.dry_run;
  f.elements.auto_pay.checked = !!r.auto_pay;
  openModal("radar-modal");
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
            ${w.proxy_url ? `<span class="pill pill-blue" title="已配独立监控代理">🌐代理</span>` : ""}
          </div>
          <div class="toolbar">
            <span class="muted">${esc(w.last_check_at || "尚未检测")}</span>
            <button class="btn btn-ghost btn-sm" onclick="toggleVps('${esc(w.id)}', ${!w.enabled})">${w.enabled ? "暂停" : "启用"}</button>
            <button class="btn btn-ghost btn-sm" onclick="editVps('${esc(w.id)}')">编辑</button>
            <button class="btn btn-danger btn-sm" onclick="deleteVps('${esc(w.id)}')">删除</button>
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

function editVps(id) {
  const w = state.vps.find((x) => x.id === id);
  if (!w) return;
  const f = document.getElementById("vps-form");
  f.reset();
  document.getElementById("vps-modal-title").textContent = "编辑 VPS 监控";
  f.elements.id.value = w.id;
  f.elements.plan_code.value = w.plan_code || "";
  f.elements.endpoint.value = w.endpoint || "ovh-eu";
  f.elements.subsidiary.value = w.subsidiary || "IE";
  f.elements.datacenters.value = (w.datacenters || []).join(",");
  f.elements.account_id.value = w.account_id || "";
  if (f.elements.proxy_url) f.elements.proxy_url.value = w.proxy_url || "";
  f.elements.auto_order.checked = !!w.auto_order;
  f.elements.dry_run.checked = !!w.dry_run;
  f.elements.auto_pay.checked = !!w.auto_pay;
  openModal("vps-modal");
}

// ---------- Orders ----------

function renderOrders() {
  const tbody = document.getElementById("orders-tbody");
  if (!state.orders.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted text-center">暂无订单历史</td></tr>`;
    return;
  }
  const labels = (state.meta && state.meta.order_status_labels) || {};
  tbody.innerHTML = state.orders
    .map((o) => {
      const cny = o.price_with_tax != null ? fmtCny(o.price_with_tax, o.currency) : "";
      const price =
        o.price_without_tax != null || o.price_with_tax != null
          ? `<div>${o.price_without_tax ?? "-"} / <b>${o.price_with_tax ?? "-"}</b> ${esc(o.currency)}</div>${cny ? `<div class="muted">${esc(cny)}</div>` : ""}`
          : "-";

      // Delivery / Lottery badges
      const ovhSt = o.ovh_status ? `<span class="pill pill-gray" title="OVH 官方订单状态">${esc(labels[o.ovh_status] || o.ovh_status)}</span>` : "";
      const spec = o.spec || null;
      const actualText = spec && spec.actual ? spec.actual.text : "";
      let lotteryHtml = "";
      if (spec && spec.lottery) {
        lotteryHtml = `<div class="mt-6"><span class="pill pill-purple">🎰 中奖! ${esc((spec.upgrades || []).join(" · "))}</span></div>`;
        if (actualText) {
          lotteryHtml += `<div class="muted">实机: ${esc(actualText)}</div>`;
        }
      } else if (actualText) {
        lotteryHtml = `<div class="muted mt-6">实机(标准): ${esc(actualText)}</div>`;
      }
      const srvLine = o.server_name
        ? `<div class="muted mt-6">🖥️ <code class="mono">${esc(o.server_name)}</code>${o.server_ip ? ` (${esc(o.server_ip)})` : ""}</div>`
        : "";

      const payBtn = o.order_url
        ? `<a class="btn btn-primary btn-sm" href="${esc(o.order_url)}" target="_blank" rel="noreferrer">付款 #${esc(o.order_id)} ↗</a>`
        : `<span class="muted">${esc(o.order_id || o.error_message || "-")}</span>`;
      const trackBtn =
        o.order_id && !o.dry_run
          ? `<button class="btn btn-ghost btn-sm" onclick="trackOrder('${esc(o.id)}')" title="立即查询 OVH 订单状态、交付主机并比对硬件中奖">查交付</button>`
          : "";

      return `<tr>
        <td class="muted">${esc(o.created_at)}</td>
        <td>
          <code class="mono">${esc(o.plan_code)}</code> @ <b>${esc(o.datacenter.toUpperCase())}</b>
          <div class="muted">${esc((o.options || []).join(", "))}</div>
        </td>
        <td>${esc(o.account_name || "-")}</td>
        <td>
          <div style="display:flex;gap:4px;flex-wrap:wrap">${statusPillForOrder(o.status)} ${ovhSt}</div>
          ${srvLine}
          ${lotteryHtml}
        </td>
        <td class="mono">${price}</td>
        <td><div class="toolbar">${payBtn} ${trackBtn}</div></td>
      </tr>`;
    })
    .join("");
}

async function trackOrder(id) {
  toast("正在查询 OVH 订单状态与硬件交付信息...", "info");
  try {
    const res = await api(`/api/orders/${id}/track`, { method: "POST", body: {} });
    const r = res.result || {};
    if (r.error) {
      toast(`跟踪失败: ${r.error}`, "err");
    } else if (r.lottery) {
      toast(`🎰 恭喜中奖! 主机 ${r.server_name} 硬件升级!`, "ok");
    } else if (r.server_name) {
      toast(`📦 订单已交付: ${r.server_name} (状态: ${r.status || "delivered"})`, "ok");
    } else {
      toast(`当前 OVH 订单状态: ${r.status || r.track_state || "tracking"}`, "ok");
    }
    refreshAll();
  } catch (err) {
    toast(err.message, "err");
  }
}

function renderShortLinkPreview(p) {
  const box = document.getElementById("short-link-preview");
  if (!p) {
    box.classList.add("hidden");
    box.innerHTML = "";
    state.shortLinkPreview = null;
    return;
  }
  state.shortLinkPreview = p;
  const pr = p.prices || {};
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">
      <div>
        <div><b>🎯 ${esc(p.plan_code)}</b> @ <b>${esc(String(p.datacenter || "").toUpperCase())}</b> <span class="pill pill-blue">${esc(p.account_name)} (${esc(p.subsidiary)})</span></div>
        <div class="muted mt-6">配件: ${esc((p.options || []).join(", ") || "默认")} · 周期: ${esc(p.duration)} / ${esc(p.pricing_mode)}</div>
        <div class="mt-6">不含税: <b>${pr.withoutTax ?? "-"} ${esc(pr.currencyCode || "")}</b> · 含税总计: <b style="color:#fcd34d">${esc(p.price_with_tax_cny || `${pr.withTax ?? "-"} ${pr.currencyCode || ""}`)}</b></div>
        <div class="muted mt-6">⚠️ 确认后将生成 OVH 官方未付款订单（不自动扣款），点击一次后自动锁定防止重单。</div>
      </div>
      <div class="toolbar">
        <button type="button" class="btn btn-primary btn-sm" id="btn-short-confirm" onclick="confirmShortLink('${esc(p.token)}')">✅ 确认生成未付款订单</button>
        <button type="button" class="btn btn-ghost btn-sm" onclick="cancelShortLink('${esc(p.token)}')">❌ 取消并删车</button>
      </div>
    </div>
  `;
  box.classList.remove("hidden");
}

async function confirmShortLink(token) {
  const btn = document.getElementById("btn-short-confirm");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ 正在提交订单...";
  }
  try {
    const res = await api("/api/short-link/confirm", { method: "POST", body: { token, auto_pay: false } });
    if (res.success) {
      toast(`✅ 短链下单成功! 订单 #${res.order_id}`, "ok");
      renderShortLinkPreview(null);
      document.getElementById("short-link-url").value = "";
      refreshAll();
    } else if (res.status === "pending_confirm") {
      toast(`⚠️ ${res.error}`, "err");
      renderShortLinkPreview(null);
      refreshAll();
    } else {
      toast(`下单失败: ${res.error || "未知错误"}`, "err");
      renderShortLinkPreview(null);
    }
  } catch (err) {
    toast(`提交失败: ${err.message}`, "err");
    renderShortLinkPreview(null);
  }
}

async function cancelShortLink(token) {
  try {
    await api("/api/short-link/cancel", { method: "POST", body: { token } });
  } catch (_) {}
  renderShortLinkPreview(null);
  toast("已取消预览并释放购物车", "info");
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
      const payBadge = a.nichandle
        ? a.default_payment_type
          ? ` <span class="pill pill-blue" title="已绑定 OVH 默认支付方式">💳 ${esc(a.default_payment_type)}</span>`
          : ` <span class="pill pill-amber" title="未检测到默认支付方式，auto_pay 可能失败">⚠️ 无默认支付</span>`
        : "";
      const ver = a.nichandle
        ? `<div><span class="pill pill-green">${esc(a.nichandle)} (${esc(a.currency)})</span>${payBadge}</div><div class="muted mt-6">${esc(a.last_verified_at)}</div>`
        : `<span class="muted">未验证</span>`;
      const pTag = a.proxy_url ? ` <span class="pill pill-blue" title="已绑定专属固定代理">🌐代理</span>` : "";
      return `<tr>
        <td><b>${esc(a.name)}</b> ${def}${pTag}</td>
        <td><code>${esc(a.endpoint)}</code> / <b>${esc(a.subsidiary)}</b></td>
        <td class="mono">${esc(a.app_key)}</td>
        <td>${ver}</td>
        <td>
          <div class="toolbar">
            <button class="btn btn-success btn-sm" onclick="testAccount('${esc(a.id)}')">测试连通</button>
            <button class="btn btn-ghost btn-sm" onclick="editAccount('${esc(a.id)}')">编辑</button>
            <button class="btn btn-danger btn-sm" onclick="deleteAccount('${esc(a.id)}')">删</button>
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
  ["task-account-select", "radar-account-select", "vps-account-select", "short-link-account"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      const prev = el.value;
      el.innerHTML = opts;
      if (prev && state.accounts.some((a) => a.id === prev)) el.value = prev;
    }
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
  if (form.elements.proxy_url) form.elements.proxy_url.value = a.proxy_url || "";
  form.elements.is_default.checked = !!a.is_default;
  openModal("account-modal");
}

async function testAccount(id) {
  toast("正在验证 OVH API 凭据...", "info");
  try {
    const res = await api(`/api/accounts/${id}/test`, { method: "POST", body: {} });
    const info = res.info || {};
    const bal = info.credit_balance != null ? ` | 余额: ${info.credit_balance} ${info.currency}` : "";
    const pay = info.default_payment_type ? ` | 默认支付: ${info.default_payment_type}` : " | ⚠️ 未绑定默认支付";
    toast(`验证成功! 账号 ${info.nichandle} (${info.subsidiary})${bal}${pay}`, "ok");
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

  // Theme switch (system / light / dark)
  document.querySelectorAll("[data-theme-set]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (window.__theme) window.__theme.set(btn.dataset.themeSet);
      syncThemeButtons();
    });
  });
  syncThemeButtons();

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
    ["toggle-fastlane", "fast_lane_enabled"],
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
      poll_interval: f.elements.poll_interval.value === "" ? 5 : Math.max(0, parseFloat(f.elements.poll_interval.value) || 0),
      os_code: f.elements.os_code.value.trim() || "none_64.en",
      coupons: f.elements.coupons.value,
      proxy_url: f.elements.proxy_url ? f.elements.proxy_url.value.trim() : "",
      fast_lane: f.elements.fast_lane.checked,
      allow_non_default: f.elements.allow_non_default.checked,
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
      proxy_url: f.elements.proxy_url ? f.elements.proxy_url.value.trim() : "",
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
    const f = document.getElementById("radar-form");
    f.reset();
    f.elements.id.value = "";
    document.getElementById("radar-modal-title").textContent = "新建雷达规则";
    openModal("radar-modal");
  });
  document.getElementById("radar-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const id = f.elements.id.value;
    const body = {
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
      proxy_url: f.elements.proxy_url ? f.elements.proxy_url.value.trim() : "",
      auto_snipe: f.elements.auto_snipe.checked,
      dry_run: f.elements.dry_run.checked,
      auto_pay: f.elements.auto_pay.checked,
    };
    try {
      if (id) await api(`/api/radar/${id}`, { method: "PUT", body });
      else await api("/api/radar", { method: "POST", body });
      closeModal("radar-modal");
      toast(id ? "黑五雷达规则已更新" : "黑五雷达规则已创建", "ok");
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
    const f = document.getElementById("vps-form");
    f.reset();
    f.elements.id.value = "";
    document.getElementById("vps-modal-title").textContent = "添加 VPS 监控";
    openModal("vps-modal");
  });
  document.getElementById("vps-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const id = f.elements.id.value;
    const body = {
      plan_code: f.elements.plan_code.value.trim(),
      endpoint: f.elements.endpoint.value,
      subsidiary: f.elements.subsidiary.value.trim().toUpperCase(),
      datacenters: f.elements.datacenters.value,
      account_id: f.elements.account_id.value,
      proxy_url: f.elements.proxy_url ? f.elements.proxy_url.value.trim() : "",
      auto_order: f.elements.auto_order.checked,
      dry_run: f.elements.dry_run.checked,
      auto_pay: f.elements.auto_pay.checked,
    };
    try {
      if (id) await api(`/api/vps/${id}`, { method: "PUT", body });
      else await api("/api/vps", { method: "POST", body });
      closeModal("vps-modal");
      toast(id ? "VPS 监控已更新" : "VPS 监控已添加", "ok");
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

  // Orders & Short-Link & My Servers actions
  document.getElementById("short-link-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = document.getElementById("short-link-url").value.trim();
    const accId = document.getElementById("short-link-account").value;
    const btn = document.getElementById("btn-short-link-preview");
    btn.disabled = true;
    btn.textContent = "解析报价中...";
    try {
      const res = await api("/api/short-link/preview", { method: "POST", body: { url, account_id: accId } });
      renderShortLinkPreview(res.preview);
      toast("短链解析成功，已获取实时含税报价，请确认后下单", "ok");
    } catch (err) {
      toast(`短链解析失败: ${err.message}`, "err");
      renderShortLinkPreview(null);
    } finally {
      btn.disabled = false;
      btn.textContent = "解析并报价";
    }
  });

  document.getElementById("btn-fetch-my-servers").addEventListener("click", async () => {
    if (!state.accounts.length) {
      toast("请先配置 OVH 账号", "err");
      return;
    }
    const acc = state.accounts[0];
    toast(`正在从 ${acc.name} 拉取我的独立服务器列表...`, "info");
    try {
      const res = await api(`/api/accounts/${acc.id}/servers`);
      const card = document.getElementById("my-servers-card");
      const list = document.getElementById("my-servers-list");
      const items = res.items || [];
      list.innerHTML = items.length
        ? items
            .map((s) => {
              const memVal = s.memory && typeof s.memory === "object" ? `${s.memory.value || ""}${s.memory.unit || ""}` : s.memory || "-";
              return `<div class="list-row">
                <div>
                  <div><code class="mono">${esc(s.name)}</code> <span class="pill pill-green">${esc(s.state || "ok")}</span> <span class="pill pill-gray">${esc(String(s.datacenter || "").toUpperCase())}</span></div>
                  <div class="muted mt-6">IP: ${esc(s.ip || "-")} · CPU: ${esc(s.cpu || "-")} · 内存: ${esc(memVal)}</div>
                  <div class="muted">型号线: ${esc(s.commercialRange || "-")} · 到期: ${esc(s.expiration || "-")}</div>
                </div>
              </div>`;
            })
            .join("")
        : `<div class="muted">该账号下暂无独立服务器</div>`;
      card.classList.remove("hidden");
    } catch (err) {
      toast(`拉取独服列表失败: ${err.message}`, "err");
    }
  });

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

  // Monitor Proxy Modal & Tester
  document.querySelectorAll(".js-open-proxy-modal").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const s = await api("/api/settings");
        document.getElementById("proxy-modal-textarea").value = s.monitor_proxies || s.http_proxy || "";
        document.getElementById("proxy-modal-fallback").checked = s.monitor_proxy_fallback_direct !== false;
        document.getElementById("proxy-modal-order-use").checked = !!s.order_use_proxy;
        document.getElementById("proxy-test-results").innerHTML = "";
        openModal("proxy-modal");
      } catch (err) {
        toast(err.message, "err");
      }
    });
  });

  async function runProxyTest(rawText, endpoint = "ovh-eu", targetBox = null, triggerBtn = null) {
    if (!rawText || !rawText.trim()) {
      toast("请先填写至少一条代理地址再测速", "err");
      if (targetBox) targetBox.innerHTML = `<div class="error-text" style="margin:0">请先填写至少一条代理地址再测速</div>`;
      return;
    }
    const origLabel = triggerBtn ? triggerBtn.textContent : "";
    if (triggerBtn) {
      triggerBtn.disabled = true;
      triggerBtn.textContent = "⏳ 测速中...";
    }
    toast("正在测试代理连接 OVH API...", "info");
    if (targetBox) targetBox.innerHTML = `<div class="muted">⏳ 正在并发测试连接 ${esc(endpoint)} ...</div>`;
    try {
      const res = await api("/api/proxy/test", { method: "POST", body: { proxies: rawText, endpoint } });
      const items = res.results || [];
      const capNote =
        res.configured_total && res.configured_total > res.total
          ? `<div class="muted">提示：共配置 ${res.configured_total} 条代理，单次测速显示前 ${res.total} 条。</div>`
          : "";
      if (targetBox) {
        targetBox.innerHTML =
          capNote +
          items
            .map((r) =>
              r.ok
                ? `<div class="list-row"><span>✅ <code class="mono">${esc(r.proxy)}</code></span><span class="pill pill-green">${r.latency_ms} ms</span></div>`
                : `<div class="list-row"><div>❌ <code class="mono">${esc(r.proxy)}</code><div class="error-text" style="margin:4px 0 0">${esc(r.error)}</div></div><span class="pill pill-red">失败</span></div>`
            )
            .join("");
      }
      if (res.passed === res.total) toast(`全部 ${res.total} 条代理测速通过!`, "ok");
      else if (res.passed > 0) toast(`测速完成: ${res.passed}/${res.total} 条代理可用`, "info");
      else toast(`全部 ${res.total} 条代理连接失败，请检查协议/账号密码/防火墙`, "err");
    } catch (err) {
      if (targetBox) targetBox.innerHTML = `<div class="error-text" style="margin:0">${esc(err.message)}</div>`;
      toast(`代理测试失败: ${err.message}`, "err");
    } finally {
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.textContent = origLabel;
      }
    }
  }

  const modalProxyTestBtn = document.getElementById("btn-run-proxy-test");
  modalProxyTestBtn.addEventListener("click", () => {
    const raw = document.getElementById("proxy-modal-textarea").value;
    const ep = document.getElementById("proxy-test-endpoint").value;
    runProxyTest(raw, ep, document.getElementById("proxy-test-results"), modalProxyTestBtn);
  });

  const settingsProxyTestBtn = document.getElementById("btn-test-settings-proxy");
  if (settingsProxyTestBtn) {
    settingsProxyTestBtn.addEventListener("click", () => {
      const f = document.getElementById("settings-form");
      const raw = (f.elements.monitor_proxies && f.elements.monitor_proxies.value.trim()) || f.elements.http_proxy.value.trim();
      const epEl = document.getElementById("settings-proxy-test-endpoint");
      const ep = (epEl && epEl.value) || "ovh-eu";
      runProxyTest(raw, ep, document.getElementById("settings-proxy-test-results"), settingsProxyTestBtn);
    });
  }

  document.getElementById("proxy-modal-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/settings", {
        method: "PUT",
        body: {
          monitor_proxies: document.getElementById("proxy-modal-textarea").value.trim(),
          monitor_proxy_fallback_direct: document.getElementById("proxy-modal-fallback").checked,
          order_use_proxy: document.getElementById("proxy-modal-order-use").checked,
        },
      });
      closeModal("proxy-modal");
      toast("监控代理配置已保存并立即生效!", "ok");
      if (state.activeTab === "settings") loadSettings();
      refreshAll();
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
  if (state.meta && state.meta.version) document.getElementById("version-tag").textContent = `v${state.meta.version}`;
  const eps = (state.meta && state.meta.endpoints) || {};
  if (eps["ovh-eu"] && eps["ovh-eu"].token_url) document.getElementById("token-link-eu").href = eps["ovh-eu"].token_url;
  if (eps["ovh-ca"] && eps["ovh-ca"].token_url) document.getElementById("token-link-ca").href = eps["ovh-ca"].token_url;
  if (eps["ovh-us"] && eps["ovh-us"].token_url) document.getElementById("token-link-us").href = eps["ovh-us"].token_url;
  api("/api/fx")
    .then((res) => {
      if (res && res.rates) state.fxRates = res.rates;
    })
    .catch(() => {});
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
window.editRadar = editRadar;
window.deleteRadar = deleteRadar;
window.toggleVps = toggleVps;
window.editVps = editVps;
window.deleteVps = deleteVps;
window.testAccount = testAccount;
window.editAccount = editAccount;
window.deleteAccount = deleteAccount;
window.trackOrder = trackOrder;
window.confirmShortLink = confirmShortLink;
window.cancelShortLink = cancelShortLink;

document.addEventListener("DOMContentLoaded", init);
