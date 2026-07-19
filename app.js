const searchInput = document.getElementById("search");
const resultsBox = document.getElementById("results");
const panel = document.getElementById("panel");

let companies = [];

fetch("data/companies.json")
  .then(r => r.json())
  .then(data => { companies = data; });

Promise.all([
  fetch("data/meta.json").then(r => r.json()),
  fetch("data/monthly.json").then(r => r.json()),
  fetch("data/bb_monthly.json").then(r => r.json()),
]).then(([meta, monthly, bbMonthly]) => {
  document.getElementById("meta-note").textContent =
    `Dados CVM (${meta.years.join(", ")}) · ${meta.company_count} companhias · atualizado em ${meta.generated_at.slice(0, 10)}`;
  initRanking(meta, monthly, bbMonthly);
});

function norm(s) {
  return (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

searchInput.addEventListener("input", () => {
  const q = norm(searchInput.value.trim());
  if (!q) { resultsBox.hidden = true; return; }
  const matches = companies
    .filter(c => norm(c.name).includes(q) || c.tickers.some(t => norm(t).includes(q)))
    .slice(0, 12);
  renderResults(matches);
});

function renderResults(matches) {
  resultsBox.innerHTML = "";
  if (matches.length === 0) { resultsBox.hidden = true; return; }
  for (const c of matches) {
    const row = document.createElement("div");
    row.className = "result-row";
    row.innerHTML = `<span class="name">${c.name}</span><span class="tickers">${c.tickers.join(" · ")}</span>`;
    row.addEventListener("click", () => selectCompany(c));
    resultsBox.appendChild(row);
  }
  resultsBox.hidden = false;
}

document.addEventListener("click", e => {
  if (!e.target.closest(".search-wrap")) resultsBox.hidden = true;
});

function selectCompany(c) {
  resultsBox.hidden = true;
  searchInput.value = c.name;
  fetch(`data/by_company/${c.cnpj_digits}.json`)
    .then(r => r.json())
    .then(renderCompany);
}

const ROLE_ORDER = [
  "Diretor ou Vinculado",
  "Conselho de Administração ou Vinculado",
  "Conselho Fiscal ou Vinculado",
  "Controlador ou Vinculado",
  "Órgão Estatutário ou Vinculado",
];
const ROLE_LABELS = {
  "Diretor ou Vinculado": "Diretoria",
  "Conselho de Administração ou Vinculado": "Conselho de Administração",
  "Conselho Fiscal ou Vinculado": "Conselho Fiscal",
  "Controlador ou Vinculado": "Controlador",
  "Órgão Estatutário ou Vinculado": "Órgão Estatutário",
};

function direction(movement) {
  if (movement.startsWith("Compra")) return "buy";
  if (movement.startsWith("Venda")) return "sell";
  return null;
}

const SHARE_ASSETS = new Set(["Ações", "Units", "BDR Patrocinados"]);

function monthlyAggregate(records) {
  const byMonth = new Map();
  for (const r of records) {
    if (!r.is_trade || !SHARE_ASSETS.has(r.asset)) continue;
    const dir = direction(r.movement);
    if (!dir) continue;
    const month = (r.ref || "").slice(0, 7);
    if (!month) continue;
    if (!byMonth.has(month)) byMonth.set(month, { month, buy: 0, sell: 0 });
    byMonth.get(month)[dir] += r.qty || 0;
  }
  return [...byMonth.values()].sort((a, b) => a.month.localeCompare(b.month));
}

function roleAggregate(records) {
  const byRole = new Map();
  for (const r of records) {
    if (!r.is_trade || !r.role || !SHARE_ASSETS.has(r.asset)) continue;
    const dir = direction(r.movement);
    if (!dir) continue;
    if (!byRole.has(r.role)) byRole.set(r.role, { buy: 0, sell: 0 });
    byRole.get(r.role)[dir] += r.qty || 0;
  }
  return byRole;
}

function fmtCompact(n) {
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return Math.round(n).toLocaleString("pt-BR");
}

function renderCompany(data) {
  panel.hidden = false;
  document.getElementById("company-name").textContent = data.name;
  const tickerRow = document.getElementById("company-tickers");
  tickerRow.innerHTML = data.tickers.map(t => `<span class="ticker-chip">${t}</span>`).join("");

  const buybackMonthly = monthlyAggregate(data.buybacks);
  const insiderMonthly = monthlyAggregate(data.insiders);
  const roleTotals = roleAggregate(data.insiders);

  renderStats(buybackMonthly, insiderMonthly);
  renderDivergingChart("buyback-chart", "buyback-empty", buybackMonthly);
  renderDivergingChart("insider-chart", "insider-empty", insiderMonthly);
  renderRoleBreakdown(roleTotals);
}

function renderStats(buybackMonthly, insiderMonthly) {
  const sum = (arr, key) => arr.reduce((s, m) => s + m[key], 0);
  const stats = [
    { label: "Ações recompradas (24m)", value: fmtCompact(sum(buybackMonthly, "buy")), cls: "buy" },
    { label: "Ações vendidas pela cia (24m)", value: fmtCompact(sum(buybackMonthly, "sell")), cls: "sell" },
    { label: "Compras de insiders (24m)", value: fmtCompact(sum(insiderMonthly, "buy")), cls: "buy" },
    { label: "Vendas de insiders (24m)", value: fmtCompact(sum(insiderMonthly, "sell")), cls: "sell" },
  ];
  document.getElementById("stat-row").innerHTML = stats.map(s =>
    `<div class="stat-tile"><div class="label">${s.label}</div><div class="value ${s.cls}">${s.value}</div></div>`
  ).join("");
}

function renderDivergingChart(containerId, emptyId, monthly) {
  const container = document.getElementById(containerId);
  const emptyNote = document.getElementById(emptyId);
  container.innerHTML = "";
  if (monthly.length === 0) {
    container.hidden = true;
    emptyNote.hidden = false;
    return;
  }
  container.hidden = false;
  emptyNote.hidden = true;

  const width = container.clientWidth || 680;
  const height = 220;
  const padL = 44, padR = 12, padT = 10, padB = 26;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const midY = padT + plotH / 2;

  const maxVal = Math.max(1, ...monthly.map(m => Math.max(m.buy, m.sell)));
  const scale = (plotH / 2 - 6) / maxVal;
  const barW = Math.min(24, plotW / monthly.length * 0.6);
  const step = plotW / monthly.length;

  const legend = document.createElement("div");
  legend.className = "legend";
  legend.innerHTML = `
    <span class="legend-item"><span class="legend-swatch" style="background:var(--buy)"></span>Compra</span>
    <span class="legend-item"><span class="legend-swatch" style="background:var(--sell)"></span>Venda</span>`;
  container.appendChild(legend);

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", height);

  const gridline = document.createElementNS(svg.namespaceURI, "line");
  gridline.setAttribute("x1", padL); gridline.setAttribute("x2", width - padR);
  gridline.setAttribute("y1", midY); gridline.setAttribute("y2", midY);
  gridline.setAttribute("stroke", "var(--baseline)");
  gridline.setAttribute("stroke-width", "1");
  svg.appendChild(gridline);

  addAxisLabel(svg, padL - 6, padT + 4, fmtCompact(maxVal));
  addAxisLabel(svg, padL - 6, midY, "0");
  addAxisLabel(svg, padL - 6, height - padB - 2, fmtCompact(maxVal));

  const tooltip = document.createElement("div");
  tooltip.className = "bar-tooltip";
  container.style.position = "relative";

  monthly.forEach((m, i) => {
    const cx = padL + step * i + step / 2;
    if (m.buy > 0) {
      const h = m.buy * scale;
      appendBar(svg, cx - barW / 2, midY - h, barW, h, "var(--buy)", true);
      addHover(svg, cx, midY - h, m, "Compra", m.buy, tooltip, container);
    }
    if (m.sell > 0) {
      const h = m.sell * scale;
      appendBar(svg, cx - barW / 2, midY, barW, h, "var(--sell)", false);
      addHover(svg, cx, midY + h, m, "Venda", m.sell, tooltip, container);
    }
    if (i % Math.ceil(monthly.length / 8 || 1) === 0) {
      const label = document.createElementNS(svg.namespaceURI, "text");
      label.setAttribute("x", cx);
      label.setAttribute("y", height - 6);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", "10");
      label.setAttribute("fill", "var(--text-muted)");
      label.textContent = m.month;
      svg.appendChild(label);
    }
  });

  container.appendChild(svg);
  container.appendChild(tooltip);
}

function addAxisLabel(svg, x, y, text) {
  const label = document.createElementNS(svg.namespaceURI, "text");
  label.setAttribute("x", x);
  label.setAttribute("y", y);
  label.setAttribute("text-anchor", "end");
  label.setAttribute("dominant-baseline", "middle");
  label.setAttribute("font-size", "10");
  label.setAttribute("fill", "var(--text-muted)");
  label.textContent = text;
  svg.appendChild(label);
}

function appendBar(svg, x, y, w, h, color, roundTop) {
  const rect = document.createElementNS(svg.namespaceURI, "rect");
  rect.setAttribute("x", x);
  rect.setAttribute("y", y);
  rect.setAttribute("width", w);
  rect.setAttribute("height", h);
  rect.setAttribute("rx", 4);
  rect.setAttribute("fill", color);
  svg.appendChild(rect);
}

function addHover(svg, cx, cy, m, label, value, tooltip, container) {
  const hit = document.createElementNS(svg.namespaceURI, "circle");
  hit.setAttribute("cx", cx);
  hit.setAttribute("cy", cy);
  hit.setAttribute("r", 14);
  hit.setAttribute("fill", "transparent");
  hit.addEventListener("mouseenter", () => {
    tooltip.textContent = `${m.month} · ${label}: ${fmtCompact(value)} ações`;
    tooltip.classList.add("visible");
  });
  hit.addEventListener("mousemove", e => {
    const rect = container.getBoundingClientRect();
    tooltip.style.left = (e.clientX - rect.left) + "px";
    tooltip.style.top = (e.clientY - rect.top) + "px";
  });
  hit.addEventListener("mouseleave", () => tooltip.classList.remove("visible"));
  svg.appendChild(hit);
}

function renderRoleBreakdown(roleTotals) {
  const container = document.getElementById("role-breakdown");
  container.innerHTML = "";
  const maxAbs = Math.max(1, ...ROLE_ORDER.map(r => {
    const t = roleTotals.get(r);
    return t ? Math.max(t.buy, t.sell) : 0;
  }));

  let any = false;
  ROLE_ORDER.forEach((role, i) => {
    const t = roleTotals.get(role);
    if (!t || (t.buy === 0 && t.sell === 0)) return;
    any = true;
    const net = t.buy - t.sell;
    const swatchVar = `var(--role-${i + 1})`;
    const fillPct = (Math.abs(net) / maxAbs) * 100;
    const row = document.createElement("div");
    row.className = "role-row";
    row.innerHTML = `
      <span class="role-label"><span class="role-swatch" style="background:${swatchVar}"></span>${ROLE_LABELS[role]}</span>
      <span class="role-track"><span class="role-fill" style="left:${net >= 0 ? 50 : 50 - fillPct / 2}%;width:${fillPct / 2}%;background:${net >= 0 ? "var(--buy)" : "var(--sell)"}"></span></span>
      <span class="role-value">${net >= 0 ? "+" : ""}${fmtCompact(net)}</span>`;
    container.appendChild(row);
  });
  if (!any) container.innerHTML = `<div class="empty-note">Sem dados de administradores no período.</div>`;
}

/* --- Ranking: top insider purchases, filterable like carteirafundos.com --- */

let rankingMeta = null;
let rankingDatasets = { insiders: [], buybacks: [] };
let activeTab = "insiders";
let selectedMonths = new Set();
let rankingTickerFilter = "";
let groupByTicker = false;
let sortKey = "val";
let sortDir = "desc";
let pickerYear = null;

const TAB_INFO = {
  insiders: {
    title: "Top compras de insiders",
    subtitle: "Negociação de administradores e pessoas ligadas — dados abertos CVM",
    hasPct: false,
  },
  buybacks: {
    title: "Top recompras",
    subtitle: "Negociação de valores mobiliários pela própria companhia, suas controladas e coligadas — dados abertos CVM",
    hasPct: true,
  },
};

const monthBtn = document.getElementById("month-picker-btn");
const monthPanel = document.getElementById("month-picker-panel");
const tickerFilterInput = document.getElementById("ticker-filter");
const groupToggle = document.getElementById("group-toggle");
const rankingTbody = document.getElementById("ranking-tbody");
const rankingTable = document.getElementById("ranking-table");
const rankingEmpty = document.getElementById("ranking-empty");
const rankingTitle = document.getElementById("ranking-title");
const rankingSubtitle = document.getElementById("ranking-subtitle");
const tabInsidersBtn = document.getElementById("tab-insiders");
const tabBuybacksBtn = document.getElementById("tab-buybacks");

function initRanking(meta, monthly, bbMonthly) {
  rankingMeta = meta;
  rankingDatasets.insiders = monthly;
  rankingDatasets.buybacks = bbMonthly;
  selectedMonths = new Set([meta.last_complete_month]);
  pickerYear = Number(meta.last_complete_month.slice(0, 4));

  tickerFilterInput.addEventListener("input", () => {
    rankingTickerFilter = norm(tickerFilterInput.value.trim());
    renderRanking();
  });
  groupToggle.addEventListener("change", () => {
    groupByTicker = groupToggle.checked;
    renderRanking();
  });
  monthBtn.addEventListener("click", () => {
    monthPanel.hidden = !monthPanel.hidden;
    if (!monthPanel.hidden) renderMonthPanel();
  });
  document.addEventListener("click", e => {
    if (!e.target.closest(".month-picker-wrap")) monthPanel.hidden = true;
  });
  rankingTable.querySelectorAll("th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortKey === key) sortDir = sortDir === "desc" ? "asc" : "desc";
      else { sortKey = key; sortDir = key === "tickers" || key === "month" ? "asc" : "desc"; }
      renderRanking();
    });
  });
  tabInsidersBtn.addEventListener("click", () => switchTab("insiders"));
  tabBuybacksBtn.addEventListener("click", () => switchTab("buybacks"));

  updateMonthBtnLabel();
  renderRanking();
}

function switchTab(tab) {
  if (activeTab === tab) return;
  activeTab = tab;
  tabInsidersBtn.classList.toggle("active", tab === "insiders");
  tabBuybacksBtn.classList.toggle("active", tab === "buybacks");
  rankingTitle.textContent = TAB_INFO[tab].title;
  rankingSubtitle.textContent = TAB_INFO[tab].subtitle;
  renderRanking();
}

function updateMonthBtnLabel() {
  const n = selectedMonths.size;
  monthBtn.textContent = n === 1 ? "1 mês selecionado" : `${n} meses selecionados`;
}

function renderMonthPanel() {
  const years = [...new Set(rankingMeta.available_months.map(m => m.slice(0, 4)))].sort();
  monthPanel.innerHTML = "";

  const shortcuts = document.createElement("div");
  shortcuts.className = "month-picker-shortcuts";
  const lastMonthBtn = document.createElement("button");
  lastMonthBtn.textContent = "Último mês";
  lastMonthBtn.addEventListener("click", () => {
    selectedMonths = new Set([rankingMeta.last_complete_month]);
    updateMonthBtnLabel(); renderMonthPanel(); renderRanking();
  });
  const ytdBtn = document.createElement("button");
  ytdBtn.textContent = "YTD";
  ytdBtn.addEventListener("click", () => {
    const year = rankingMeta.last_complete_month.slice(0, 4);
    selectedMonths = new Set(rankingMeta.available_months.filter(m => m.startsWith(year)));
    updateMonthBtnLabel(); renderMonthPanel(); renderRanking();
  });
  shortcuts.append(lastMonthBtn, ytdBtn);
  monthPanel.appendChild(shortcuts);

  const yearRow = document.createElement("div");
  yearRow.className = "year-row";
  const prevBtn = document.createElement("button");
  prevBtn.textContent = "‹";
  prevBtn.disabled = !years.includes(String(pickerYear - 1));
  prevBtn.addEventListener("click", () => { pickerYear--; renderMonthPanel(); });
  const yearLabel = document.createElement("span");
  yearLabel.textContent = pickerYear;
  const nextBtn = document.createElement("button");
  nextBtn.textContent = "›";
  nextBtn.disabled = !years.includes(String(pickerYear + 1));
  nextBtn.addEventListener("click", () => { pickerYear++; renderMonthPanel(); });
  yearRow.append(prevBtn, yearLabel, nextBtn);
  monthPanel.appendChild(yearRow);

  const grid = document.createElement("div");
  grid.className = "month-grid";
  const MONTH_ABBR = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"];
  MONTH_ABBR.forEach((label, i) => {
    const key = `${pickerYear}-${String(i + 1).padStart(2, "0")}`;
    const cell = document.createElement("div");
    const available = rankingMeta.available_months.includes(key);
    cell.className = "month-cell" + (selectedMonths.has(key) ? " selected" : "") + (available ? "" : " unavailable");
    cell.textContent = label;
    if (available) {
      cell.addEventListener("click", () => {
        if (selectedMonths.has(key)) selectedMonths.delete(key);
        else selectedMonths.add(key);
        updateMonthBtnLabel(); renderMonthPanel(); renderRanking();
      });
    }
    grid.appendChild(cell);
  });
  monthPanel.appendChild(grid);

  const footer = document.createElement("div");
  footer.className = "month-picker-footer";
  const clearBtn = document.createElement("button");
  clearBtn.textContent = "Limpar seleção";
  clearBtn.addEventListener("click", () => {
    selectedMonths.clear();
    updateMonthBtnLabel(); renderMonthPanel(); renderRanking();
  });
  const closeBtn = document.createElement("button");
  closeBtn.textContent = "Fechar";
  closeBtn.addEventListener("click", () => { monthPanel.hidden = true; });
  footer.append(clearBtn, closeBtn);
  monthPanel.appendChild(footer);
}

function monthLabel(ym) {
  const [y, m] = ym.split("-");
  return `${m}/${y}`;
}

function fmtBRL(n) {
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  let out;
  if (abs >= 1e9) out = (abs / 1e9).toFixed(1) + "B";
  else if (abs >= 1e6) out = (abs / 1e6).toFixed(1) + "M";
  else if (abs >= 1e3) out = Math.round(abs / 1e3) + "K";
  else out = Math.round(abs).toLocaleString("pt-BR");
  return `${sign}R$ ${out.replace(".", ",")}`;
}

function fmtQty(n) {
  const abs = Math.abs(n);
  let out;
  if (abs >= 1e9) out = (abs / 1e9).toFixed(1) + "B";
  else if (abs >= 1e6) out = (abs / 1e6).toFixed(1) + "M";
  else if (abs >= 1e3) out = Math.round(abs / 1e3) + "K";
  else out = Math.round(abs).toLocaleString("pt-BR");
  return out.replace(".", ",");
}

function fmtPrice(n) {
  return `R$ ${n.toFixed(2).replace(".", ",")}`;
}

function buildRankingRows() {
  const dataset = rankingDatasets[activeTab];
  const hasPct = TAB_INFO[activeTab].hasPct;
  const filtered = dataset.filter(r => {
    if (!selectedMonths.has(r.month)) return false;
    if (rankingTickerFilter) {
      const hay = norm(r.name) + " " + r.tickers.map(norm).join(" ");
      if (!hay.includes(rankingTickerFilter)) return false;
    }
    return true;
  });

  if (!groupByTicker) {
    return filtered.map(r => ({
      cnpj_digits: r.cnpj_digits, name: r.name, tickers: r.tickers,
      monthLabel: monthLabel(r.month), val: r.val, qty: Math.abs(r.qty), price: r.price,
      pct: hasPct ? r.pct : null,
    }));
  }

  const byCompany = new Map();
  for (const r of filtered) {
    let g = byCompany.get(r.cnpj_digits);
    if (!g) {
      g = { cnpj_digits: r.cnpj_digits, name: r.name, tickers: r.tickers, val: 0, qty: 0, grossQty: 0, grossVal: 0, months: new Set(), shares: null };
      byCompany.set(r.cnpj_digits, g);
    }
    g.val += r.val;
    g.qty += r.qty;
    g.grossQty += r.gross_qty;
    g.grossVal += r.gross_val;
    g.months.add(r.month);
    // Total shares is constant per company -- back it out from any one row's
    // pct so the grouped total can be re-expressed as a percentage too.
    if (g.shares === null && r.pct) g.shares = Math.abs(r.qty) / (r.pct / 100);
  }
  return [...byCompany.values()].map(g => ({
    cnpj_digits: g.cnpj_digits, name: g.name, tickers: g.tickers,
    monthLabel: g.months.size === 1 ? monthLabel([...g.months][0]) : `${g.months.size} meses`,
    val: g.val, qty: Math.abs(g.qty), price: g.grossQty ? g.grossVal / g.grossQty : 0,
    pct: hasPct && g.shares ? (Math.abs(g.qty) / g.shares) * 100 : null,
  }));
}

function renderRanking() {
  if (!rankingMeta) return;
  let rows = buildRankingRows();

  rows.sort((a, b) => {
    let av, bv;
    if (sortKey === "tickers") { av = a.tickers[0] || ""; bv = b.tickers[0] || ""; }
    else if (sortKey === "month") { av = a.monthLabel; bv = b.monthLabel; }
    else if (sortKey === "pct") { av = a.pct ?? -1; bv = b.pct ?? -1; }
    else { av = a[sortKey]; bv = b[sortKey]; }
    if (av < bv) return sortDir === "asc" ? -1 : 1;
    if (av > bv) return sortDir === "asc" ? 1 : -1;
    return 0;
  });

  rankingTable.querySelectorAll("th[data-sort]").forEach(th => {
    th.classList.toggle("sorted", th.dataset.sort === sortKey);
  });

  if (rows.length === 0) {
    rankingTbody.innerHTML = "";
    rankingEmpty.hidden = false;
    return;
  }
  rankingEmpty.hidden = true;

  rankingTbody.innerHTML = rows.map(r => `
    <tr data-cnpj="${r.cnpj_digits}">
      <td class="ticker-cell">${r.tickers.join(" · ")}</td>
      <td>${r.monthLabel}</td>
      <td class="${r.val >= 0 ? "val-positive" : "val-negative"}">${fmtBRL(r.val)}</td>
      <td>${fmtQty(r.qty)}</td>
      <td>${fmtPrice(r.price)}</td>
      <td>${r.pct != null ? r.pct.toFixed(2) + "%" : "–"}</td>
    </tr>`).join("");

  rankingTbody.querySelectorAll("tr").forEach(tr => {
    tr.addEventListener("click", () => {
      const c = companies.find(c => c.cnpj_digits === tr.dataset.cnpj);
      if (c) {
        selectCompany(c);
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });
}
