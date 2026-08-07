"use strict";

/* ---------------------------------------------------------------------
 * Config: API base URL. Priority: ?api= query param > localStorage > default.
 * ------------------------------------------------------------------- */
const params = new URLSearchParams(window.location.search);

const API_BASE = (() => {
  const fromQuery = params.get("api");
  if (fromQuery) {
    localStorage.setItem("api_base", fromQuery);
    return fromQuery.replace(/\/$/, "");
  }
  return (localStorage.getItem("api_base") || "http://127.0.0.1:8000").replace(/\/$/, "");
})();

/* ---------------------------------------------------------------------
 * Parámetros de acceso. En producción estos llegan en la URL desde el
 * redirect de Moodle (botón del curso) — nunca se piden en un formulario.
 * ------------------------------------------------------------------- */
const CTX = {
  userid: params.get("userid"),
  courseid: params.get("courseid"),
  cmid: params.get("cmid"),
};

// Debe coincidir con core.UMBRAL_MUESTRA_INSUFICIENTE del backend. Solo se
// usa para el texto de progreso ("N de 5 docentes evaluados"); la decisión
// real siempre la toma el backend (accion="esperar_docentes" / 409).
const UMBRAL_MUESTRA_INSUFICIENTE = 5;

/* ---------------------------------------------------------------------
 * DOM references
 * ------------------------------------------------------------------- */
const el = {
  contextBar: document.getElementById("context-bar"),
  contextName: document.getElementById("context-name"),
  btnRefresh: document.getElementById("btn-refresh"),
  refreshStatus: document.getElementById("refresh-status"),
  accessError: document.getElementById("access-error"),
  emptyFirstTime: document.getElementById("empty-first-time"),
  emptyFirstTimeIcon: document.getElementById("empty-first-time-icon"),
  emptyFirstTimeTitle: document.getElementById("empty-first-time-title"),
  emptyFirstTimeText: document.getElementById("empty-first-time-text"),
  btnGenerarPrimero: document.getElementById("btn-generar-primero"),
  esperandoDocentes: document.getElementById("esperando-docentes"),
  esperandoDocentesText: document.getElementById("esperando-docentes-text"),
  statusBanner: document.getElementById("status-banner"),
  loading: document.getElementById("loading"),
  reportContent: document.getElementById("report-content"),
  statRow: document.getElementById("stat-row"),
  legend: document.getElementById("legend"),
  criteriosChart: document.getElementById("criterios-chart"),
  criteriosTableWrap: document.getElementById("criterios-table-wrap"),
  criteriosTable: document.getElementById("criterios-table"),
  toggleTable: document.getElementById("toggle-table"),
  listaFortalezas: document.getElementById("lista-fortalezas"),
  listaOportunidades: document.getElementById("lista-oportunidades"),
  listaDesafios: document.getElementById("lista-desafios"),
  listaAlertas: document.getElementById("lista-alertas"),
  textoReporte: document.getElementById("texto-reporte"),
  tooltip: document.getElementById("tooltip"),
  themeToggle: document.getElementById("theme-toggle"),
};

/* ---------------------------------------------------------------------
 * Theme toggle (light/dark), persisted
 * ------------------------------------------------------------------- */
(function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved === "light" || saved === "dark") {
    document.documentElement.setAttribute("data-theme", saved);
  }
  el.themeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  });
})();

/* ---------------------------------------------------------------------
 * API calls
 * ------------------------------------------------------------------- */
async function apiGenerar(userid, courseid, cmid) {
  const url = `${API_BASE}/reporte/generar?courseid=${encodeURIComponent(courseid)}&cmid=${encodeURIComponent(cmid)}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ userid: Number(userid) }),
  });
  return handleResponse(res);
}

async function apiConsultar(userid) {
  const url = `${API_BASE}/reporte/consultar?userid=${encodeURIComponent(userid)}`;
  const res = await fetch(url);
  return handleResponse(res);
}

async function handleResponse(res) {
  let body = null;
  try {
    body = await res.json();
  } catch {
    // no-op: respuesta sin cuerpo JSON
  }
  if (!res.ok) {
    const detail = (body && body.detail) || `Error HTTP ${res.status}`;
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return body;
}

/* ---------------------------------------------------------------------
 * UI state helpers
 * ------------------------------------------------------------------- */
function showOnly(...visibleIds) {
  const all = ["access-error", "empty-first-time", "esperando-docentes", "report-content"];
  for (const id of all) {
    document.getElementById(id).hidden = !visibleIds.includes(id);
  }
}

/* Muestra el estado "aún no hay suficientes docentes evaluados" — el
 * backend nunca llama al LLM en este caso (ni /consultar ni un intento de
 * /generar que reciba 409), así que no hay botón de reintento: el reporte
 * aparecerá solo cuando se alcance el mínimo. */
function showEsperandoDocentes(nDocentesEvaluados) {
  const n = nDocentesEvaluados ?? 0;
  el.contextName.textContent = "Esperando evaluaciones docentes…";
  el.btnRefresh.hidden = true;
  el.esperandoDocentesText.textContent =
    `Aún no se alcanza el mínimo de ${UMBRAL_MUESTRA_INSUFICIENTE} docentes evaluados para generar ` +
    `el reporte institucional (${n} de ${UMBRAL_MUESTRA_INSUFICIENTE} hasta el momento). El reporte se ` +
    "generará automáticamente en cuanto se alcance el mínimo.";
  showOnly("esperando-docentes");
}

function setLoading(isLoading) {
  el.loading.hidden = !isLoading;
  if (isLoading) el.statusBanner.hidden = true;
}

function showError(message) {
  el.statusBanner.textContent = message;
  el.statusBanner.hidden = false;
}

function clearError() {
  el.statusBanner.hidden = true;
  el.statusBanner.textContent = "";
}

/* ---------------------------------------------------------------------
 * Arranque: todo depende de los parámetros recibidos en la URL
 * ------------------------------------------------------------------- */
async function init() {
  if (!CTX.userid) {
    el.contextBar.hidden = true;
    showOnly("access-error");
    return;
  }

  setLoading(true);
  try {
    const data = await apiConsultar(CTX.userid);
    setLoading(false);
    clearError();

    if (data.accion === "mostrar") {
      render(data.reporte);
      showOnly("report-content");
      el.btnRefresh.hidden = !(CTX.courseid && CTX.cmid);
      checkForUpdates(data.reporte);
      return;
    }

    if (data.accion === "esperar_docentes") {
      // Aún no llega al mínimo de docentes evaluados: NO se llama a
      // generar (el backend gastaría una llamada al LLM sin necesidad).
      showEsperandoDocentes(data.n_docentes_evaluados);
      return;
    }

    // data.accion === "generar": no hay reporte vigente, se dispara la
    // generación automáticamente, sin que el directivo tenga que pedirlo.
    await generarReporte();
  } catch (err) {
    setLoading(false);

    if (err.status === 404) {
      showOnly("access-error");
      el.accessError.querySelector("p").textContent =
        "Tu usuario no está asociado a ninguna institución educativa registrada.";
      return;
    }

    showOnly("empty-first-time");
    showBlockedState({
      title: "No se pudo cargar el reporte",
      text: err.message || "Ocurrió un error inesperado al consultar el reporte.",
      allowRetry: true,
    });
  }
}

/* Muestra el estado de bloqueo (faltan datos o falló la generación),
 * con o sin botón de reintento según si tiene sentido reintentar. */
function showBlockedState({ title, text, allowRetry }) {
  el.emptyFirstTimeTitle.textContent = title;
  el.emptyFirstTimeText.textContent = text;
  el.btnGenerarPrimero.hidden = !allowRetry;
}

/* Genera el reporte automáticamente (primera vez, o como reintento tras
 * un error). El directivo nunca tiene que pedirlo explícitamente salvo
 * que algo haya fallado. */
async function generarReporte() {
  if (!CTX.courseid || !CTX.cmid) {
    el.contextName.textContent = "Reporte no disponible";
    showOnly("empty-first-time");
    showBlockedState({
      title: "No es posible generar el reporte",
      text: "Faltan los datos de curso (courseid/cmid) en la URL de acceso. Vuelve a ingresar desde el enlace del programa en Moodle.",
      allowRetry: false,
    });
    return;
  }

  el.contextName.textContent = "Generando reporte institucional…";
  el.loading.textContent = "Generando tu reporte institucional, esto puede tardar unos segundos…";
  setLoading(true);
  clearError();
  try {
    const data = await apiGenerar(CTX.userid, CTX.courseid, CTX.cmid);
    setLoading(false);
    el.loading.textContent = "Cargando reporte…";
    render(data);
    showOnly("report-content");
    el.btnRefresh.hidden = false;
  } catch (err) {
    setLoading(false);
    el.loading.textContent = "Cargando reporte…";

    if (err.status === 409) {
      // Muestra insuficiente: reintentar no sirve de nada hasta que haya
      // más docentes evaluados. Se vuelve a consultar (endpoint liviano,
      // no toca el LLM) solo para mostrar el progreso actual.
      let n;
      try {
        n = (await apiConsultar(CTX.userid)).n_docentes_evaluados;
      } catch {
        n = undefined;
      }
      showEsperandoDocentes(n);
      return;
    }

    showOnly("empty-first-time");
    showBlockedState({
      title: "No se pudo generar el reporte",
      text: err.message || "Ocurrió un error al generar el reporte institucional.",
      allowRetry: true,
    });
  }
}

el.btnGenerarPrimero.addEventListener("click", () => {
  generarReporte();
});

/* Revisión de actualizaciones: barata cuando no cambió nada (el backend
 * solo vuelve a llamar al LLM si cambió n_docentes_evaluados). */
async function checkForUpdates(currentData) {
  if (!CTX.courseid || !CTX.cmid) return;
  el.refreshStatus.hidden = false;
  el.refreshStatus.textContent = "Revisando actualizaciones…";
  el.btnRefresh.classList.add("spinning");
  try {
    const fresh = await apiGenerar(CTX.userid, CTX.courseid, CTX.cmid);
    if (fresh.fecha_generacion !== currentData.fecha_generacion) {
      render(fresh);
      el.refreshStatus.textContent = "Reporte actualizado con nueva información.";
      setTimeout(() => { el.refreshStatus.hidden = true; }, 4000);
    } else {
      el.refreshStatus.hidden = true;
    }
  } catch (err) {
    console.warn("No se pudo verificar actualizaciones:", err.message);
    el.refreshStatus.hidden = true;
  } finally {
    el.btnRefresh.classList.remove("spinning");
  }
}

el.btnRefresh.addEventListener("click", () => {
  if (el.btnRefresh.classList.contains("spinning")) return;
  const current = lastRenderedData;
  if (current) checkForUpdates(current);
});

init();

/* ---------------------------------------------------------------------
 * Render
 * ------------------------------------------------------------------- */
const NIVELES = [
  { key: "pct_inicial", label: "Inicial", cls: "seg-inicio", swatch: "var(--status-critical)" },
  { key: "pct_en_proceso", label: "En proceso", cls: "seg-proceso", swatch: "var(--status-warning)" },
  { key: "pct_logrado", label: "Logrado", cls: "seg-logrado", swatch: "var(--status-good)" },
];

let lastRenderedData = null;

function render(data) {
  lastRenderedData = data;
  el.contextName.textContent = `${data.nombre_ie || "—"} · cod. modular ${data.cod_modular || "—"}`;

  renderStatRow(data);
  renderAlertas(data);
  renderLegend();
  renderCriteriosChart(data.distribucion_por_criterio || []);
  renderCriteriosTable(data.distribucion_por_criterio || []);
  renderListas(data);
  el.textoReporte.textContent = data.texto_reporte || "";
}

function statTile(label, value, opts = {}) {
  const tile = document.createElement("div");
  tile.className = "stat-tile";

  const labelEl = document.createElement("div");
  labelEl.className = "label";
  labelEl.textContent = label;

  const valueEl = document.createElement("div");
  valueEl.className = "value" + (opts.small ? " small" : "");
  valueEl.textContent = value;

  tile.append(labelEl, valueEl);

  if (opts.badge) {
    const badge = document.createElement("span");
    badge.className = `badge ${opts.badge.tone}`;
    badge.textContent = opts.badge.text;
    tile.appendChild(badge);
  }

  return tile;
}

function renderStatRow(data) {
  el.statRow.replaceChildren();

  el.statRow.appendChild(statTile("Docentes retroalimentados", data.n_docentes_evaluados ?? "—"));
  el.statRow.appendChild(statTile("Docentes totales", data.n_docentes_total ?? "—"));

  const pctCobertura = data.pct_cobertura ?? 0;
  el.statRow.appendChild(
    statTile("Cobertura", `${pctCobertura}%`, {
      badge:
        pctCobertura < 70
          ? { tone: "critical", text: "< 70% · umbral RTC 1" }
          : { tone: "good", text: "ok" },
    })
  );

  const fecha = data.fecha_generacion
    ? new Date(data.fecha_generacion).toLocaleString("es-PE", {
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
  el.statRow.appendChild(statTile("Última actualización", fecha, { small: true }));

  el.statRow.appendChild(
    statTile("Muestra", data.muestra_insuficiente ? "Referencial" : "Suficiente", {
      badge: data.muestra_insuficiente
        ? { tone: "critical", text: "no concluyente, < 5 docentes" }
        : { tone: "good", text: "ok" },
    })
  );
}

/* ---------------------------------------------------------------------
 * Alertas institucionales (Ficha técnica, sección 8)
 * ------------------------------------------------------------------- */
function computeAlertas(data) {
  const alertas = [];
  const pctCobertura = data.pct_cobertura ?? 0;

  if (pctCobertura < 70) {
    alertas.push({
      level: "warning",
      title: `Cobertura insuficiente al cierre del plazo (${pctCobertura}%)`,
      detail: "Riesgo de que la RTC 1 se realice sin insumo institucional suficiente.",
      accion:
        "Dinamización dirigida a los docentes de la IE pendientes de entrega, antes de la fecha de RTC 1.",
      modalidad: "Dinamización pedagógica",
    });
  }

  if ((data.fortalezas_predominantes || []).length === 0) {
    alertas.push({
      level: "critical",
      title: "Sin fortalezas predominantes",
      detail:
        "La IE no muestra ningún criterio consolidado como fortaleza: posible necesidad de una RTC 1 más desafiante de lo habitual.",
      accion:
        "Visibilizar el hallazgo al Especialista Pedagógico antes de la RTC 1 para preparar una ROP con el directivo reforzado.",
      modalidad: "ROP con el directivo",
    });
  }

  for (const d of data.desafios_prioritarios || []) {
    if (d.tipo === "brecha" && d.pct >= 50) {
      alertas.push({
        level: "warning",
        title: `Brecha ${d.id} extendida (${d.pct}% de las entregas)`,
        detail: "Necesidad formativa compartida y extendida en la institución.",
        accion: "Considerar ROP focalizada por clúster.",
        modalidad: "Formativa focalizada por clúster",
      });
    }
  }

  if (data.muestra_insuficiente) {
    alertas.push({
      level: "critical",
      title: `Muestra insuficiente (${data.n_docentes_evaluados ?? 0} docente(s) evaluado(s))`,
      detail: "El reporte no tiene base suficiente para sustentar decisiones institucionales firmes.",
      accion: "Complementar el diagnóstico con otras fuentes (MPE, ENLA) antes de la RTC 1.",
      modalidad: "Orienta al directivo",
    });
  }

  return alertas;
}

const STATUS_ICON_GLYPH = { good: "✓", warning: "!", critical: "✕" };

function renderAlertas(data) {
  el.listaAlertas.replaceChildren();
  const alertas = computeAlertas(data);

  if (alertas.length === 0) {
    const li = document.createElement("li");
    li.className = "alert-item good";

    const icon = document.createElement("span");
    icon.className = "status-icon good";
    icon.textContent = STATUS_ICON_GLYPH.good;

    const body = document.createElement("div");
    body.className = "alert-body";
    const title = document.createElement("div");
    title.className = "alert-title";
    title.textContent = "Sin alertas institucionales activas para esta IE.";
    body.appendChild(title);

    li.append(icon, body);
    el.listaAlertas.appendChild(li);
    return;
  }

  for (const a of alertas) {
    const li = document.createElement("li");
    li.className = `alert-item ${a.level}`;

    const icon = document.createElement("span");
    icon.className = `status-icon ${a.level}`;
    icon.textContent = STATUS_ICON_GLYPH[a.level] || "!";

    const body = document.createElement("div");
    body.className = "alert-body";

    const title = document.createElement("div");
    title.className = "alert-title";
    title.textContent = a.title;

    const detail = document.createElement("div");
    detail.className = "alert-detail";
    detail.textContent = a.detail;

    const accion = document.createElement("div");
    accion.className = "alert-accion";
    accion.textContent = `Acción sugerida: ${a.accion}`;

    const modalidad = document.createElement("div");
    modalidad.className = "alert-modalidad";
    modalidad.textContent = a.modalidad;

    body.append(title, detail, accion, modalidad);
    li.append(icon, body);
    el.listaAlertas.appendChild(li);
  }
}

function renderLegend() {
  el.legend.replaceChildren();
  for (const nivel of NIVELES) {
    const item = document.createElement("div");
    item.className = "legend-item";

    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.background = nivel.swatch;

    const label = document.createElement("span");
    label.textContent = nivel.label;

    item.append(swatch, label);
    el.legend.appendChild(item);
  }
}

function renderCriteriosChart(criterios) {
  el.criteriosChart.replaceChildren();

  for (const c of criterios) {
    const row = document.createElement("div");
    row.className = "criterio-row";

    const label = document.createElement("div");
    label.className = "criterio-label";
    label.textContent = c.criterio_id;

    const bar = document.createElement("div");
    bar.className = "criterio-bar";

    for (const nivel of NIVELES) {
      const pct = c[nivel.key] ?? 0;
      if (pct <= 0) continue;
      const seg = document.createElement("div");
      seg.className = `criterio-segment ${nivel.cls}`;
      seg.style.width = `${pct}%`;
      seg.tabIndex = 0;
      seg.setAttribute("role", "img");
      seg.setAttribute("aria-label", `${c.criterio_id} — ${nivel.label}: ${pct}%`);

      const showTip = (evt) => showTooltip(evt, `${c.criterio_id} · ${nivel.label}`, `${pct}%`);
      seg.addEventListener("pointermove", showTip);
      seg.addEventListener("pointerenter", showTip);
      seg.addEventListener("focus", (evt) => showTooltip(evt, `${c.criterio_id} · ${nivel.label}`, `${pct}%`, true));
      seg.addEventListener("pointerleave", hideTooltip);
      seg.addEventListener("blur", hideTooltip);

      bar.appendChild(seg);
    }

    const value = document.createElement("div");
    value.className = "criterio-value";
    value.textContent = `${c.pct_logrado ?? 0}%`;

    row.append(label, bar, value);
    el.criteriosChart.appendChild(row);
  }
}

function renderCriteriosTable(criterios) {
  const table = el.criteriosTable;
  table.replaceChildren();

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const h of ["Criterio", "% Inicial", "% En proceso", "% Logrado"]) {
    const th = document.createElement("th");
    th.textContent = h;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);

  const tbody = document.createElement("tbody");
  for (const c of criterios) {
    const tr = document.createElement("tr");
    for (const val of [c.criterio_id, `${c.pct_inicial}%`, `${c.pct_en_proceso}%`, `${c.pct_logrado}%`]) {
      const td = document.createElement("td");
      td.textContent = val;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }

  table.append(thead, tbody);
}

el.toggleTable.addEventListener("click", () => {
  const showingTable = !el.criteriosTableWrap.hidden;
  el.criteriosTableWrap.hidden = showingTable;
  el.criteriosChart.hidden = !showingTable;
  el.legend.hidden = !showingTable;
  el.toggleTable.textContent = showingTable ? "Ver como tabla" : "Ver como gráfico";
});

function renderListaSimple(container, items) {
  container.replaceChildren();
  if (!items || items.length === 0) {
    const li = document.createElement("li");
    li.className = "empty-note";
    li.textContent = "Sin elementos en esta categoría.";
    container.appendChild(li);
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const idSpan = document.createElement("span");
    idSpan.textContent = `Criterio ${item.id}`;
    const pctSpan = document.createElement("span");
    pctSpan.className = "pct";
    pctSpan.textContent = `${item.pct}%`;
    li.append(idSpan, pctSpan);
    container.appendChild(li);
  }
}

function renderListas(data) {
  renderListaSimple(el.listaFortalezas, data.fortalezas_predominantes);
  renderListaSimple(el.listaOportunidades, data.oportunidades_comunes);

  el.listaDesafios.replaceChildren();
  const desafios = data.desafios_prioritarios || [];
  if (desafios.length === 0) {
    const li = document.createElement("li");
    li.className = "empty-note";
    li.textContent = "Sin desafíos prioritarios identificados.";
    el.listaDesafios.appendChild(li);
  } else {
    for (const d of desafios) {
      const li = document.createElement("li");
      if (d.tipo === "brecha" && d.pct >= 50) li.title = "Brecha extendida (≥ 50%): ver panel de alertas";
      const idSpan = document.createElement("span");
      idSpan.textContent = d.tipo === "brecha" ? `Brecha ${d.id}` : `Criterio ${d.id}`;
      const pctSpan = document.createElement("span");
      pctSpan.className = "pct";
      if (d.tipo === "brecha" && d.pct >= 50) pctSpan.style.color = "var(--status-critical)";
      pctSpan.textContent = `${d.pct}%`;
      li.append(idSpan, pctSpan);
      el.listaDesafios.appendChild(li);
    }
  }
}

/* ---------------------------------------------------------------------
 * Tooltip (hover + keyboard focus)
 * ------------------------------------------------------------------- */
function showTooltip(evt, title, value, isFocus = false) {
  el.tooltip.replaceChildren();

  const strong = document.createElement("strong");
  strong.textContent = value;
  const label = document.createElement("div");
  label.textContent = title;

  el.tooltip.append(strong, label);
  el.tooltip.hidden = false;

  let x, y;
  if (isFocus) {
    const rect = evt.target.getBoundingClientRect();
    x = rect.left + rect.width / 2;
    y = rect.top;
  } else {
    x = evt.clientX;
    y = evt.clientY;
  }
  el.tooltip.style.left = `${Math.min(x + 12, window.innerWidth - 230)}px`;
  el.tooltip.style.top = `${Math.max(y - 40, 8)}px`;
}

function hideTooltip() {
  el.tooltip.hidden = true;
}
