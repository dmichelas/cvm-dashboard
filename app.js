const searchInput = document.getElementById("search");
const resultsBox = document.getElementById("results");
const panel = document.getElementById("panel");

let companies = [];

fetch("data/companies.json")
  .then(r => r.json())
  .then(data => { companies = data; });

fetch("data/meta.json")
  .then(r => r.json())
  .then(meta => {
    document.getElementById("meta-note").textContent =
      `Dados CVM (${meta.years.join(", ")}) · ${meta.company_count} companhias · atualizado em ${meta.generated_at.slice(0, 10)}`;
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
