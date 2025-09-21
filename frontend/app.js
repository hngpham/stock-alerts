// app.js — cleaned & refactored (keeps existing API and DOM contracts)

const API = "/api";

/* =========================
 * Global State
 * ========================= */
let currentSymbol = null;           // { id, ticker, note, rating, ... }
let currentGroup = null;            // "watch" | "archived"
let currentAlertsSymbolId = null;

let cacheWatch = [];
let cacheArchived = [];

let activeTab = "watch";
let applyingUI = false;

let saveTimer = null;               // shared debounce timer (alerts)
let noteSaveTimer = null;           // notes debounce timer
let quoteTimer = null;              // polling interval

// Preserve last known earnings chip to avoid flicker
let __lastEarningsExact = "";

/* =========================
 * Small DOM Helpers
 * ========================= */
const $ = (id) => document.getElementById(id);

const setText = (id, value) => {
  const el = $(id);
  if (el) el.textContent = value ?? "";
};

const show = (id) => {
  const el = $(id);
  if (el) el.style.display = "inline-block";
};

const hide = (id) => {
  const el = $(id);
  if (el) el.style.display = "none";
};

const setChip = (id, text) => {
  const el = $(id);
  if (!el) return;
  el.textContent = text || "";
  el.style.display = text ? "inline-flex" : "none";
};

function debounce(fn, refVarSetter, delay = 600) {
  return (...args) => {
    const handle = refVarSetter();
    clearTimeout(handle.v);
    handle.v = setTimeout(() => fn.apply(null, args), delay);
  };
}

/* =========================
 * Formatters
 * ========================= */
function fmt(n, d = 2) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return Number(n).toFixed(d);
}

function formatMarketCap(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const v = Number(value);
  const abs = Math.abs(v);
  if (abs >= 1e12) return (v / 1e12).toFixed(2) + "T";
  if (abs >= 1e9)  return (v / 1e9).toFixed(2) + "B";
  if (abs >= 1e6)  return (v / 1e6).toFixed(2) + "M";
  if (abs >= 1e3)  return (v / 1e3).toFixed(2) + "K";
  return v.toString();
}

function starsString(rating = 0) {
  const r = Math.max(0, Math.min(5, Number(rating) || 0));
  return "★★★★★".slice(0, r) + "☆☆☆☆☆".slice(r);
}

/* =========================
 * Status/UI chips
 * ========================= */
function setStatus(text) {
  setText("saveStatus", text || "");
  if (text && /Saved/.test(text)) {
    setTimeout(() => {
      const el = $("saveStatus");
      if (el && el.textContent.includes("Saved")) el.textContent = "";
    }, 1200);
  }
}

function setNoteStatus(text) {
  setText("noteSaveStatus", text || "");
  if (text && /Saved/.test(text)) {
    setTimeout(() => {
      const el = $("noteSaveStatus");
      if (el && el.textContent.includes("Saved")) el.textContent = "";
    }, 1200);
  }
}

function setUpdateOneStatus(text) {
  setText("updateOneStatus", text || "");
  if (text && /✓|✔/.test(text)) {
    setTimeout(() => {
      const el = $("updateOneStatus");
      if (el && (el.textContent.includes("✓") || el.textContent.includes("✔"))) {
        el.textContent = "";
      }
    }, 1500);
  }
}

function setRunHeadline(text) {
  setText("globalRunStatus", text || "");
}

function setDupHint(text) {
  setText("dupHint", text || "");
}

function setLastEdit(epoch) {
  const el = $("lastEditStatus");
  if (!el) return;
  if (!epoch) {
    el.textContent = "";
    return;
  }
  const dt = new Date(epoch * 1000);
  el.textContent = "Last edit: " + dt.toLocaleString();
}

function setDescription(text) {
  const el = $("q_desc");
  if (!el) return;
  const t = (text && String(text).trim()) || "";
  el.textContent = t || "No description available";
}

/* =========================
 * Earnings Chip (next to H2)
 * ========================= */
function setHeaderEarningsExact(valueMaybe, { clear = false } = {}) {
  if (clear) __lastEarningsExact = "";

  let val = "";
  if (valueMaybe == null) {
    val = "";
  } else if (typeof valueMaybe === "number") {
    val = new Date(valueMaybe * 1000).toISOString().slice(0, 10);
  } else {
    val = String(valueMaybe).trim();
    // Trim ISO datetime to date
    if (/^\d{4}-\d{2}-\d{2}T/.test(val)) val = val.slice(0, 10);
  }

  if (val) __lastEarningsExact = val;
  const shown = __lastEarningsExact;

  setChip("symbolEarningsExact", shown ? `Earnings: ${shown}` : "");
  // Expose for devtools
  window.__earningsChipText = $("symbolEarningsExact")?.textContent;
}

/* =========================
 * API Helpers
 * ========================= */
async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`Request failed: ${r.status}`);
  return r.json();
}

async function fetchSymbols(query = "", scope = "watch", min_rating = 0) {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  params.set("scope", scope);
  if (min_rating && Number(min_rating) > 0) params.set("min_rating", String(min_rating));
  return fetchJSON(`${API}/symbols?${params.toString()}`).catch(() => []);
}

async function fetchSymbolById(id) {
  return fetchJSON(`${API}/symbols/${id}`);
}

async function postRating(id, rating) {
  return fetchJSON(`${API}/rating/${id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rating: Number(rating) || 0 }),
  });
}

/* =========================
 * Sidebar Rendering & Tabs
 * ========================= */
function renderList(ul, list, groupName) {
  if (!ul) return;
  ul.innerHTML = "";
  list.forEach((s) => {
    const li = document.createElement("li");
    li.style.display = "flex";
    li.style.justifyContent = "space-between";
    li.style.alignItems = "center";
    li.style.gap = "8px";

    const left = document.createElement("span");
    left.textContent = s.ticker;

    const right = document.createElement("span");
    right.className = "stars small";
    right.textContent = starsString(s.rating);

    li.append(left, right);
    li.onclick = () => selectSymbol(s, groupName);
    ul.appendChild(li);
  });
}

function renderAll() {
  renderList($("symbolsWatch"), cacheWatch, "watch");
  renderList($("symbolsArchived"), cacheArchived, "archived");
}

function setActiveTab(tab) {
  activeTab = tab;
  const btnW = $("tabWatch");
  const btnA = $("tabArchived");
  const paneW = $("paneWatch");
  const paneA = $("paneArchived");

  [btnW, btnA].forEach((b) => b && b.classList.remove("active"));
  [paneW, paneA].forEach((p) => p && p.classList.remove("active"));

  if (tab === "watch") {
    btnW?.classList.add("active");
    paneW?.classList.add("active");
  } else {
    btnA?.classList.add("active");
    paneA?.classList.add("active");
  }
}

$("tabWatch")?.addEventListener("click", () => setActiveTab("watch"));
$("tabArchived")?.addEventListener("click", () => setActiveTab("archived"));

/* =========================
 * Rating Filter
 * ========================= */
function getMinRating() {
  const sel = $("ratingFilter");
  const v = sel ? Number(sel.value) : 0;
  return Number.isFinite(v) ? v : 0;
}

function wireRatingFilter() {
  const sel = $("ratingFilter");
  if (!sel) return;
  sel.addEventListener("change", () => loadSymbols(false));
}

/* =========================
 * Load Lists
 * ========================= */
async function loadSymbols(initial = false) {
  const minR = getMinRating();
  const [watch, archived] = await Promise.all([
    fetchSymbols("", "watch", minR),
    fetchSymbols("", "archived", minR),
  ]);

  cacheWatch = watch || [];
  cacheArchived = archived || [];
  renderAll();

  if (initial && cacheWatch.length && !currentSymbol) {
    // Optionally auto-select first watch item
  }
}

/* =========================
 * Selection Flow
 * ========================= */
async function selectSymbol(s, groupName) {
  currentSymbol = s;
  currentGroup = groupName || (cacheWatch.find((x) => x.id === s.id) ? "watch" : "archived");

  setActiveTab(currentGroup);
  setText("symbolTitle", s.ticker);

  // Keep chip visible with previous value to prevent flicker during load
  setDescription("");

  // Controls
  show("deleteBtn");
  show("archiveBtn");
  show("updateBtn");

  const archBtn = $("archiveBtn");
  if (archBtn) {
    archBtn.textContent = currentGroup === "watch" ? "Archive" : "Unarchive";
    archBtn.title = currentGroup === "watch" ? "Move to archive" : "Move back to watchlist";
  }

  updateLinkBar(s.ticker);

  // Notes + rating (+ last edit) + description (+ maybe earnings)
  const fresh = await fetchSymbolById(s.id);
  await wireNotesAndRatingForSymbol(s.id);

  const nextDay =
    fresh.next_earning_day ||
    fresh.next_earnings_day ||
    fresh.earnings_date ||
    fresh.nextEarningsDate ||
    "";
  setHeaderEarningsExact(nextDay);

  await loadAlerts(s.id);
  startQuotePolling();
}

/* =========================
 * Move / Delete
 * ========================= */
async function moveCurrent(toGroup) {
  if (!currentSymbol) return;
  await fetch(`${API}/symbols/${currentSymbol.id}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ group: toGroup }),
  });

  const prevTicker = currentSymbol.ticker;
  await loadSymbols(false);

  const src = toGroup === "watch" ? cacheWatch : cacheArchived;
  const found = src.find((x) => x.ticker === prevTicker);
  setActiveTab(toGroup);
  if (found) selectSymbol(found, toGroup);
}

$("archiveBtn")?.addEventListener("click", () => {
  if (!currentSymbol) return;
  moveCurrent(currentGroup === "watch" ? "archived" : "watch");
});

async function deleteCurrent() {
  if (!currentSymbol) return;
  const ok = confirm(`Delete ${currentSymbol.ticker}?`);
  if (!ok) return;

  await fetch(`${API}/symbols/${currentSymbol.id}`, { method: "DELETE" });
  stopQuotePolling();

  currentSymbol = null;
  currentGroup = null;

  setText("symbolTitle", "Select a Symbol");
  hide("deleteBtn");
  hide("archiveBtn");
  hide("updateBtn");

  const noteBox = $("note");
  if (noteBox) noteBox.value = "";

  setLastEdit(null);
  setDescription("");
  setHeaderEarningsExact(null, { clear: true });
  updateLinkBar(null);

  await loadSymbols(false);
}

$("deleteBtn")?.addEventListener("click", deleteCurrent);

/* =========================
 * Add/Search
 * ========================= */
async function addSymbol() {
  const input = $("newTicker");
  const raw = ((input && input.value) || "").trim().toUpperCase();
  if (!raw) return;

  const existingAll = await fetchJSON(`${API}/symbols?q=${encodeURIComponent(raw)}&scope=all`).catch(() => []);
  const exact = existingAll.find((s) => s.ticker === raw);
  if (exact) {
    const inWatch = cacheWatch.find((x) => x.id === exact.id);
    setActiveTab(inWatch ? "watch" : "archived");
    selectSymbol(exact, inWatch ? "watch" : "archived");
    return;
  }

  await fetch(`${API}/symbols`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker: raw, group: "watch" }),
  });

  const sel = $("ratingFilter");
  if (sel && sel.value !== "0") sel.value = "0";
  if (input) input.value = "";

  await loadSymbols(false);
  setActiveTab("watch");
  const found = cacheWatch.find((x) => x.ticker === raw);
  if (found) selectSymbol(found, "watch");
}

$("addBtn")?.addEventListener("click", addSymbol);

function wireAddBox() {
  const input = $("newTicker");
  const addBtn = $("addBtn");
  if (!input) return;

  input.addEventListener("input", async (e) => {
    const term = e.target.value;
    const server = await fetchJSON(`${API}/symbols?scope=all&q=${encodeURIComponent(term)}`).catch(() => []);
    const q = (term || "").trim().toUpperCase();
    const exact = server.find((s) => s.ticker === q);

    if (exact) {
      const inWatch = cacheWatch.find((x) => x.id === exact.id);
      if (addBtn) addBtn.disabled = true;
      setDupHint(`${q} is already in your ${inWatch ? "watchlist" : "archived"}. Press Enter to open it.`);
    } else {
      if (addBtn) addBtn.disabled = false;
      setDupHint("");
    }
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addSymbol();
    }
  });
}

/* =========================
 * Quick Links
 * ========================= */
function buildLinks(t) {
  t = encodeURIComponent((t || "").toUpperCase().trim());
  return {
    hood: `https://robinhood.com/stocks/${t}`,
    yahoo: `https://finance.yahoo.com/quote/${t}/`,
    sa: `https://seekingalpha.com/symbol/${t}`,
    ins: `http://openinsider.com/${t}`,
  };
}

function setLinkEnabled(el, on) {
  if (!el) return;
  el.classList.toggle("disabled", !on);
}

function updateLinkBar(ticker) {
  const lAll = $("lk_all");
  const lH = $("lk_hood");
  const lY = $("lk_yahoo");
  const lS = $("lk_sa");
  const lI = $("lk_ins");

  if (!ticker) {
    [lAll, lH, lY, lS, lI].forEach((a) => {
      if (a) {
        a.href = "#";
        setLinkEnabled(a, false);
      }
    });
    return;
  }

  const L = buildLinks(ticker);
  if (lH) lH.href = L.hood;
  if (lY) lY.href = L.yahoo;
  if (lS) lS.href = L.sa;
  if (lI) lI.href = L.ins;

  [lH, lY, lS, lI, lAll].forEach((a) => setLinkEnabled(a, true));

  if (lAll) {
    lAll.onclick = (e) => {
      e.preventDefault();
      window.open(L.hood, "_blank", "noopener");
      window.open(L.yahoo, "_blank", "noopener");
      window.open(L.sa, "_blank", "noopener");
      window.open(L.ins, "_blank", "noopener");
    };
  }
}

/* =========================
 * Notes & Rating
 * ========================= */
const saveNoteDebounced = debounce(
  async function (symbolId, text) {
    try {
      setNoteStatus("Saving…");
      const res = await fetch(`${API}/note/${symbolId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: text }),
      });
      const data = await res.json();

      if (currentSymbol && currentSymbol.id === symbolId) currentSymbol.note = text;

      let i = cacheWatch.findIndex((x) => x.id === symbolId);
      if (i !== -1) cacheWatch[i].note = text;

      i = cacheArchived.findIndex((x) => x.id === symbolId);
      if (i !== -1) cacheArchived[i].note = text;

      setNoteStatus("Saved ✔");
      setLastEdit(data.last_edit_epoch);
    } catch (e) {
      console.warn("Note autosave failed:", e);
      setNoteStatus("Save failed");
    }
  },
  () => ({ v: noteSaveTimer }),
  500
);

function paintStars(widget, rating) {
  if (!widget) return;
  const spans = widget.querySelectorAll("span[data-star]");
  spans.forEach((sp) => {
    const val = Number(sp.dataset.star);
    sp.textContent = val <= rating ? "★" : "☆";
    sp.classList.toggle("active", val <= rating);
  });
}

function wireRatingWidget(widgetEl) {
  if (!widgetEl) return;

  // Rebind clean listeners by cloning
  const freshEl = widgetEl.cloneNode(true);
  widgetEl.replaceWith(freshEl);
  widgetEl = freshEl;

  // Ensure star spans exist
  if (!widgetEl.querySelector("span[data-star='1']")) {
    widgetEl.innerHTML = `
      <span data-star="1">☆</span>
      <span data-star="2">☆</span>
      <span data-star="3">☆</span>
      <span data-star="4">☆</span>
      <span data-star="5">☆</span>
    `;
  }

  let committed = currentSymbol ? Number(currentSymbol.rating) || 0 : 0;
  paintStars(widgetEl, committed);

  const onOver = (e) => {
    const val = Number(e.target?.dataset?.star);
    if (!val) return;
    paintStars(widgetEl, val);
  };
  const onOut = () => paintStars(widgetEl, committed);
  const onClick = async (e) => {
    const val = Number(e.target?.dataset?.star);
    if (!val || !currentSymbol) return;
    try {
      committed = val;
      paintStars(widgetEl, committed);
      await postRating(currentSymbol.id, committed);

      currentSymbol.rating = committed;

      let i = cacheWatch.findIndex((x) => x.id === currentSymbol.id);
      if (i !== -1) cacheWatch[i].rating = committed;

      i = cacheArchived.findIndex((x) => x.id === currentSymbol.id);
      if (i !== -1) cacheArchived[i].rating = committed;

      renderAll();
    } catch (err) {
      console.warn("Failed to save rating:", err);
    }
  };

  widgetEl.addEventListener("mouseover", onOver);
  widgetEl.addEventListener("mousemove", onOver);
  widgetEl.addEventListener("mouseleave", onOut);
  widgetEl.addEventListener("click", onClick);
}

async function wireNotesAndRatingForSymbol(symbolId) {
  const noteBox = $("note");
  const fresh = await fetchSymbolById(symbolId); // note + rating + last_edit_epoch + description

  setDescription(fresh.description || "");

  if (noteBox) {
    noteBox.value = fresh.note || "";
    noteBox.oninput = () => saveNoteDebounced(symbolId, noteBox.value);
    noteBox.onblur = () => saveNoteDebounced(symbolId, noteBox.value);
  }

  if (currentSymbol && currentSymbol.id === symbolId) {
    currentSymbol.note = fresh.note || "";
    currentSymbol.rating = Number(fresh.rating) || 0;
  }

  let i = cacheWatch.findIndex((x) => x.id === symbolId);
  if (i !== -1) {
    cacheWatch[i].note = fresh.note || "";
    cacheWatch[i].rating = Number(fresh.rating) || 0;
  }
  i = cacheArchived.findIndex((x) => x.id === symbolId);
  if (i !== -1) {
    cacheArchived[i].note = fresh.note || "";
    cacheArchived[i].rating = Number(fresh.rating) || 0;
  }

  const widget = $("ratingStars");
  if (widget) {
    paintStars(widget, Number(fresh.rating) || 0);
    wireRatingWidget(widget);
  }

  setLastEdit(fresh.last_edit_epoch || null);
  return fresh;
}

/* =========================
 * Alerts
 * ========================= */
function readPctCheckboxes(cls) {
  return Array.from(document.querySelectorAll(`input.${cls}:checked`)).map((el) => Number(el.value));
}

function setPctCheckboxes(cls, values) {
  const set = new Set(values.map(Number));
  document.querySelectorAll(`input.${cls}`).forEach((el) => {
    el.checked = set.has(Number(el.value));
  });
}

function collectAlertsPayload() {
  const above = $("a_above")?.value ?? "";
  const below = $("a_below")?.value ?? "";
  return {
    above: above === "" ? null : Number(above),
    below: below === "" ? null : Number(below),
    pct_drop: readPctCheckboxes("drop"),
    pct_jump: readPctCheckboxes("jump"),
  };
}

const autosaveAlerts = debounce(
  async function () {
    if (!currentAlertsSymbolId || applyingUI) return;
    try {
      setStatus("Saving…");
      const res = await fetch(`${API}/alerts/${currentAlertsSymbolId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectAlertsPayload()),
      });
      const data = await res.json();
      setStatus("Saved ✔");
      setLastEdit(data.last_edit_epoch);
    } catch {
      setStatus("Save failed");
    }
  },
  () => ({ v: saveTimer }),
  600
);

async function loadAlerts(symbolId) {
  applyingUI = true;
  currentAlertsSymbolId = symbolId;
  try {
    const data = await fetchJSON(`${API}/alerts/${symbolId}`).catch(() => []);
    const aboveEl = $("a_above");
    const belowEl = $("a_below");

    if (aboveEl) aboveEl.value = "";
    if (belowEl) belowEl.value = "";
    setPctCheckboxes("drop", []);
    setPctCheckboxes("jump", []);

    const drops = [];
    const jumps = [];
    data.forEach((a) => {
      if (a.type === "above" && aboveEl) aboveEl.value = a.value;
      if (a.type === "below" && belowEl) belowEl.value = a.value;
      if (a.type === "pct_drop") drops.push(a.value);
      if (a.type === "pct_jump") jumps.push(a.value);
    });
    setPctCheckboxes("drop", drops);
    setPctCheckboxes("jump", jumps);
  } finally {
    applyingUI = false;
  }
}

function wireAlertInputs() {
  const above = $("a_above");
  const below = $("a_below");
  const checks = document.querySelectorAll("input.drop, input.jump");

  ["input", "change", "blur"].forEach((evt) => {
    if (above) above.addEventListener(evt, autosaveAlerts);
    if (below) below.addEventListener(evt, autosaveAlerts);
  });
  checks.forEach((cb) => cb.addEventListener("change", autosaveAlerts));
}

/* =========================
 * Quote Panel (Polling)
 * ========================= */
function startQuotePolling() {
  stopQuotePolling();
  refreshQuote();
  quoteTimer = setInterval(refreshQuote, 15000);
}

function stopQuotePolling() {
  if (quoteTimer) {
    clearInterval(quoteTimer);
    quoteTimer = null;
  }
}

async function refreshQuote() {
  if (!currentSymbol) return;

  // Clear numeric fields each tick (avoid showing stale data)
  [
    "q_price", "q_change", "q_change_pct", "q_open", "q_high", "q_low", "q_prev", "q_vol",
    "q_mktcap", "q_pe", "q_div_yield", "q_52h", "q_52l", "q_qdiv",
  ].forEach((id) => setText(id, "—"));

  try {
    const q = await fetchJSON(`${API}/quote/${currentSymbol.id}`);

    if (!q || q.error) {
      setText("lastCheck", "Checked: —");
      setText("windowStatus", "");
      // Keep earnings chip intact to avoid flicker
      return;
    }

    // Fill core fields
    setText("q_price", fmt(q.price));
    setText("q_change", (q.change > 0 ? "+" : (q.change < 0 ? "" : "")) + (q.change == null ? "—" : fmt(q.change)));
    setText("q_change_pct", q.change_percent || "—");
    setText("q_open", fmt(q.open));
    setText("q_high", fmt(q.high));
    setText("q_low", fmt(q.low));
    setText("q_prev", fmt(q.prev_close));
    setText("q_vol", formatMarketCap(q.volume));
    setText("q_mktcap", formatMarketCap(q.market_cap));
    setText("q_pe", q.pe_ratio != null ? fmt(q.pe_ratio, 2) : "—");
    setText("q_div_yield", q.dividend_yield_percent != null ? `${fmt(q.dividend_yield_percent, 2)}%` : "—");
    setText("q_52h", fmt(q.fifty_two_week_high));
    setText("q_52l", fmt(q.fifty_two_week_low));
    setText("q_qdiv", fmt(q.quarterly_dividend_amount));

    if (q.description) setDescription(q.description);

    const earningsVal =
      q.next_earning_day ||
      q.next_earnings_day ||
      q.earnings_date ||
      q.nextEarningsDate ||
      "";
    setHeaderEarningsExact(earningsVal);

    if (q.last_check_epoch) {
      const dt = new Date(q.last_check_epoch * 1000);
      const dateStr = dt.toLocaleString("en-US", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZoneName: "short",
      });
      let status = "";
      if (q.last_check_note) {
        const isOk = /ok/i.test(q.last_check_note);
        status = isOk ? " (OK)" : " (Err)";
      }
      setText("lastCheck", `Checked${status}: ${dateStr}`);
    } else {
      setText("lastCheck", "Checked: —");
    }

    if (typeof q.window_open === "boolean") {
      setText("windowStatus", `Notify window: ${q.window_open ? "OPEN" : "CLOSED"} · cooldown ${q.cooldown_minutes}m`);
    } else {
      setText("windowStatus", "");
    }
  } catch (e) {
    console.warn("refreshQuote cache fetch failed:", e);
  }
}

/* =========================
 * Global Status Polling
 * ========================= */
async function refreshGlobalLastUpdate() {
  try {
    const d = await fetchJSON(`${API}/last_update`);
    setText("globalLastUpdate", `Last update: ${d && d.text ? d.text : "—"}`);
  } catch {
    setText("globalLastUpdate", "Last update: —");
  }
}

function startGlobalLastUpdatePolling() {
  refreshGlobalLastUpdate();
  setInterval(refreshGlobalLastUpdate, 30000);
}

async function refreshRunStatus() {
  try {
    const d = await fetchJSON(`${API}/run_status`);
    const el = $("globalRunStatus");
    if (!el) return;

    const phase = d.phase || "idle";
    if (phase === "running") {
      el.textContent = "Updating…";
    } else if (phase === "finished") {
      const code = d.status_code || "ok";
      const when = d.finished_text || "";
      const ok = d.ok_count ?? 0;
      const err = d.err_count ?? 0;
      const headline =
        code === "ok" ? "Updated" :
        code === "rate_limited" ? "Rate limited" :
        code === "market_closed" ? "Market closed" :
        code === "alpha_key_missing" ? "Alpha key missing" :
        code === "network_error" ? "Network error" : "Partial";
      el.textContent = `${headline}${when ? " at " + when : ""} (OK: ${ok}, Err: ${err})`;
    } else {
      el.textContent = d.message || "Idle";
    }
  } catch {
    setText("globalRunStatus", "Status unavailable");
  }
}

function startRunStatusPolling() {
  refreshRunStatus();
  setInterval(refreshRunStatus, 10000);
}

/* =========================
 * Update Buttons
 * ========================= */
async function updateCurrentSymbol() {
  const btn = $("updateBtn");
  if (!currentSymbol) return;
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Updating…";
    }
    setUpdateOneStatus("Updating…");
    const res = await fetch(`${API}/update_symbol/${currentSymbol.id}`, { method: "POST" });
    const data = await res.json();

    if (!res.ok || data.status === "error") {
      setUpdateOneStatus(`Failed (${data.error || "update_failed"})`);
    } else {
      setUpdateOneStatus("Updated ✓");
      await refreshQuote();
    }
  } catch (e) {
    console.warn("updateCurrentSymbol failed:", e);
    setUpdateOneStatus("Failed");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Update";
    }
  }
}

async function updateAllWatch() {
  const btn = $("updateAllBtn");
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Updating…";
    }
    setRunHeadline("Updating…");
    const res = await fetch(`${API}/update_all`, { method: "POST" });
    if (!res.ok) {
      setRunHeadline("Failed to start bulk update");
    } else {
      setRunHeadline("Updating…");
    }
  } catch (e) {
    console.warn("updateAllWatch failed:", e);
    setRunHeadline("Failed to start");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Update All";
    }
  }
}

function wireUpdateButtons() {
  const one = $("updateBtn");
  if (one) {
    show("updateBtn");
    one.onclick = updateCurrentSymbol;
  }
  const all = $("updateAllBtn");
  if (all) {
    show("updateAllBtn");
    all.onclick = updateAllWatch;
  }
}

/* =========================
 * Alerts Input Wiring
 * ========================= */
wireAlertInputs();

/* =========================
 * Init
 * ========================= */
(function init() {
  updateLinkBar(null);
  wireAddBox();
  wireRatingFilter();
  wireUpdateButtons();
  loadSymbols(true);
  startGlobalLastUpdatePolling();
  startRunStatusPolling();
})();
