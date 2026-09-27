// Marketing Answers: browser UI for POST /ask.
// Everything from the server is inserted as text (textContent) or handed to
// ECharts as data, which draws it on a canvas; nothing is parsed as HTML.
"use strict";

const $ = (id) => document.getElementById(id);

const KEY_NAME = "mkt.key";
const HISTORY_MAX = 25;
const QUESTION_MAX = 2000;
const QUESTION_MIN = 3;

// What each dataset holds, in plain words, with questions it answers well.
const DATASETS = {
  ga4: {
    title: "Website visits and online sales",
    about: "Google Merchandise Store website: visits, traffic sources, devices, countries, the shopping funnel, orders and products, 1 Nov 2020 to 31 Jan 2021. There is no advertising cost data, so return on ad spend can't be worked out.",
    examples: [
      "Which marketing channel brought in the most revenue?",
      "What was the conversion rate on mobile compared with desktop?",
      "Where do shoppers drop off between viewing a product and buying?",
      "What were the top 10 products by revenue?",
      "How did daily revenue change over the holiday season?",
      "Which countries have the highest average order value?",
    ],
  },
  marketing: {
    title: "Campaigns and ad spend",
    about: "Campaigns, daily ad spend, impressions and clicks, and conversions with revenue, April to September 2026.",
    examples: [
      "What was return on ad spend (ROAS) by channel?",
      "Which channel has the lowest cost per click?",
      "How has weekly ad spend changed over time?",
      "Which 5 campaigns had the highest click-through rate?",
    ],
  },
  customers: {
    title: "Customers",
    about: "Customer sign-ups by date and country. Names and contact details are protected and can't be shown.",
    examples: [
      "Which countries do most of our customers come from?",
      "How many new customers signed up each month?",
    ],
  },
};

// Progress messages while waiting: [seconds elapsed, message].
const STEPS = [
  [0, "Reading your question…"],
  [4, "Finding the right data…"],
  [12, "Running the numbers…"],
  [30, "Checking the results…"],
  [60, "Still working. Bigger questions take longer…"],
  [120, "Nearly at the time limit. Hang on…"],
];

const state = {
  key: null,
  user: null,
  controller: null,
  timer: null,
  current: null, // {id, question, response, at}
  lastQuestion: "",
  chart: null, // live ECharts instance
  mode: null, // "google" (session cookie) or "key" (X-API-Key)
  name: "", // display name from Google
  googleEnabled: false,
  view: "ask",
  usage: null, // last /usage report
  usageChart: null,
};

// Why a Google sign-in came back without a session (?signin_error=...).
const SIGNIN_ERRORS = {
  cancelled: { title: "Sign-in was cancelled", text: "No problem. Continue with Google again whenever you're ready.", tone: "info" },
  expired: { title: "Sign-in timed out", text: "The sign-in took too long or was interrupted. Please try again." },
  unverified: { title: "Email address not verified", text: "Google says this account's email address isn't verified. Verify it with Google, or use your work account." },
  failed: { title: "Sign-in didn't finish", text: "Something went wrong talking to Google. Please try again." },
};

// Match the page language rather than the browser's, so text and numbers read consistently.
const LOCALE = document.documentElement.lang || "en";

// ---------- storage (may be unavailable, e.g. private windows) ----------

function store(persist) {
  try {
    return persist ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

function readKey() {
  for (const persist of [false, true]) {
    try {
      const k = store(persist) && store(persist).getItem(KEY_NAME);
      if (k) return { key: k, persist };
    } catch {}
  }
  return null;
}

function saveKey(key, persist) {
  forgetKey();
  try { store(persist).setItem(KEY_NAME, key); } catch {}
}

function forgetKey() {
  for (const persist of [false, true]) {
    try { store(persist) && store(persist).removeItem(KEY_NAME); } catch {}
  }
}

// History lives next to the key: this tab only, unless "keep me signed in" was ticked.
function historyStore() {
  const saved = readKey();
  return store(saved ? saved.persist : false);
}

function historyName() {
  return "mkt.history." + (state.user ? state.user.user : "");
}

function loadHistory() {
  try {
    const raw = historyStore().getItem(historyName());
    const items = raw ? JSON.parse(raw) : [];
    return Array.isArray(items) ? items : [];
  } catch {
    return [];
  }
}

function saveHistory(items) {
  try { historyStore().setItem(historyName(), JSON.stringify(items.slice(0, HISTORY_MAX))); } catch {}
}

// ---------- API ----------

class ApiError extends Error {
  constructor(status, detail, retryAfter) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

async function api(path, { method = "GET", body, signal, key = state.key } = {}) {
  // The server requires this header on cookie-authenticated writes (CSRF defence).
  const headers = { "X-Requested-With": "fetch" };
  if (key) headers["X-API-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
    cache: "no-store",
  });
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : "";
    throw new ApiError(res.status, detail, Number(res.headers.get("Retry-After")) || null);
  }
  return data;
}

// ---------- field errors (error summary + inline message) ----------

function setFieldError(group, messageEl, input, text) {
  $(group).classList.toggle("is-error", Boolean(text));
  $(messageEl).hidden = !text;
  $(messageEl).textContent = text ? text + "." : "";
  const described = input.getAttribute("aria-describedby").split(" ").filter((id) => id !== messageEl);
  if (text) described.push(messageEl);
  input.setAttribute("aria-describedby", described.join(" "));
  input.setAttribute("aria-invalid", text ? "true" : "false");
}

// ---------- sign in ----------

function showScreen(name) {
  for (const id of ["signin", "pending", "app"]) $(id).hidden = id !== name;
  $("account").hidden = name !== "app";
  $("views").hidden = name !== "app";
}

function showSignin({ alert = null, keyError = "", clearKey = true } = {}) {
  showScreen("signin");
  $("google-signin").hidden = !state.googleEnabled;
  // Without Google sign-in configured (e.g. local development) the key form is the only way in.
  $("key-option").classList.toggle("only", !state.googleEnabled);
  if (!state.googleEnabled || keyError) $("key-option").open = true;

  $("signin-alert").hidden = !alert;
  $("signin-alert").className = "alert " + (alert && alert.tone === "info" ? "alert-info" : "alert-error");
  $("signin-alert-title").textContent = alert ? alert.title : "";
  $("signin-alert-text").textContent = alert ? alert.text : "";

  // On first load, keep anything typed or pasted before the script ran.
  if (clearKey) $("key-input").value = "";
  setFieldError("key-group", "key-error", $("key-input"), keyError);
  (state.googleEnabled && !keyError ? $("google-btn") : $("key-input")).focus();
}

// One field, so the error sits under it (role="alert" announces it) and focus
// returns to the field; a separate error summary would only repeat it.
function showSigninError(text) {
  $("key-option").open = true;
  setFieldError("key-group", "key-error", $("key-input"), text);
  if (text) $("key-input").focus();
}

function showPending(email, detail = "") {
  showScreen("pending");
  $("pending-email").textContent = email;
  // The server explains why (e.g. trials closed for this network); drop the lead-in.
  const reason = detail.includes("does not have access yet.") ? detail.split("does not have access yet.")[1].trim() : "";
  $("pending-reason").textContent = reason;
  $("pending-reason").hidden = !reason;
  $("pending-refresh").focus();
}

async function signIn(key, persist) {
  const user = await api("/whoami", { key });
  state.key = key;
  state.user = user;
  state.mode = "key";
  state.name = "";
  saveKey(key, persist);
  showApp();
}

async function endSession() {
  cancelAsk();
  forgetKey();
  disposeChart();
  if (state.mode === "google" || !$("pending").hidden) {
    try { await api("/auth/logout", { method: "POST", key: null }); } catch {}
  }
  state.key = null;
  state.user = null;
  state.current = null;
  state.mode = null;
  state.name = "";
  state.usage = null;
  disposeUsageChart();
}

async function signOut(alert = null) {
  await endSession();
  showSignin({ alert });
}

function signinErrorText(err) {
  if (err instanceof ApiError) {
    if (err.status === 401) return "That access key wasn't recognised. Check you copied all of it, with no spaces";
    if (err.status === 403) return "Your key works, but it hasn't been given access to any data yet. Ask your admin to set this up";
    return "The service had a problem signing you in. Try again in a minute";
  }
  return "Couldn't reach the service. Check your internet connection and try again";
}

$("signin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("key-input").value.trim();
  if (!key) {
    showSigninError("Enter your access key");
    return;
  }
  const btn = $("signin-btn");
  btn.disabled = true;
  btn.textContent = "Signing in…";
  try {
    await signIn(key, $("remember").checked);
  } catch (err) {
    showSigninError(signinErrorText(err));
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign in";
  }
});

$("signout").addEventListener("click", () => signOut());
$("pending-signout").addEventListener("click", () => signOut());
$("pending-refresh").addEventListener("click", () => start());
$("pending-switch").addEventListener("click", async () => {
  await endSession();
  location.assign("/auth/login");
});

// ---------- app shell ----------

function showApp() {
  if (/Mac|iPhone|iPad/.test(navigator.platform)) $("mod-key").textContent = "⌘";
  showScreen("app");
  $("who-name").textContent = state.name || state.user.user;
  $("who-name").title = state.user.user;
  $("who-avatar").textContent = initials(state.name || state.user.user);
  renderDatasets();
  renderHistory();
  resetView();
  $("who-plan").textContent = PLAN_LABELS[state.user.plan] || "";
  refreshPlan();
  showView(location.hash === "#usage" ? "usage" : "ask");

  const q = new URLSearchParams(location.search).get("q");
  if (q) $("question").value = q.slice(0, QUESTION_MAX);
  updateCount();
  $("question").focus();
}

function initials(name) {
  const parts = String(name).split(/[\s._-]+/).filter(Boolean);
  return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
}

// Small line icons, built as SVG elements (no HTML parsing).
const ICONS = {
  ga4: ["M3 17l4-5 3 3 4-6 3 4", "M3 3v14h14"],
  marketing: ["M3 9v2l9 4V5L3 9z", "M12 7h2a3 3 0 0 1 0 6h-2", "M5 11.5l1 4.5h2l-.5-3.6"],
  customers: ["M7 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "M1.5 17a5.5 5.5 0 0 1 11 0", "M13 3.5a3 3 0 0 1 0 5.5", "M15 12.3a5.5 5.5 0 0 1 3.5 4.7"],
  arrow: ["M4 10h12", "M11 5l5 5-5 5"],
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name] || ICONS.ga4) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function renderDatasets() {
  const list = $("dataset-list");
  list.replaceChildren();
  const known = state.user.allowed_datasets.filter((d) => DATASETS[d]);
  const unknown = state.user.allowed_datasets.filter((d) => !DATASETS[d]);
  for (const name of known) {
    const info = DATASETS[name];
    const card = document.createElement("section");
    card.className = "card dataset";
    const head = document.createElement("div");
    head.className = "dataset-head";
    const badge = document.createElement("span");
    badge.className = "dataset-icon";
    badge.append(icon(name));
    const h = document.createElement("h3");
    h.className = "dataset-title";
    h.textContent = info.title;
    head.append(badge, h);
    const p = document.createElement("p");
    p.className = "dataset-about";
    p.textContent = info.about;
    const ul = document.createElement("ul");
    ul.className = "examples";
    for (const q of info.examples) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "example";
      const label = document.createElement("span");
      label.textContent = q;
      b.append(label, icon("arrow"));
      b.addEventListener("click", () => {
        $("question").value = q;
        updateCount();
        ask(q);
      });
      li.append(b);
      ul.append(li);
    }
    card.append(head, p, ul);
    list.append(card);
  }
  if (unknown.length) {
    const p = document.createElement("p");
    p.textContent = `You can also ask about: ${unknown.join(", ")}. Try "What tables can I ask about, and what's in them?"`;
    list.append(p);
  }
}

function resetView() {
  $("answer").hidden = true;
  $("problem").hidden = true;
  $("working").hidden = true;
  $("start").hidden = false;
  disposeChart();
}

// ---------- character count ----------

function updateCount() {
  const left = QUESTION_MAX - $("question").value.length;
  const el = $("question-count");
  el.textContent = left >= 0
    ? `${left.toLocaleString(LOCALE)} character${left === 1 ? "" : "s"} left`
    : `${(-left).toLocaleString(LOCALE)} characters too many`;
  el.classList.toggle("over", left < 0);
}

$("question").addEventListener("input", () => {
  updateCount();
  if ($("question-group").classList.contains("is-error")) {
    setFieldError("question-group", "question-error", $("question"), "");
  }
});

// ---------- history ----------

function renderHistory() {
  const items = loadHistory();
  const list = $("history-list");
  list.replaceChildren();
  for (const item of items) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.className = "history-item";
    const q = document.createElement("span");
    q.className = "history-q";
    q.textContent = item.question;
    const when = document.createElement("span");
    when.className = "history-when";
    when.textContent = formatWhen(item.at);
    b.append(q, when);
    if (state.current && state.current.id === item.id) b.setAttribute("aria-current", "true");
    b.addEventListener("click", () => {
      if (state.controller) return;
      $("question").value = item.question;
      updateCount();
      showAnswer(item);
      renderHistory();
    });
    li.append(b);
    list.append(li);
  }
  $("history-empty").hidden = items.length > 0;
  $("history-clear").hidden = items.length === 0;
}

$("history-clear").addEventListener("click", () => {
  saveHistory([]);
  renderHistory();
});

// ---------- asking ----------

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  ask($("question").value);
});

$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    ask($("question").value);
  }
});

$("cancel").addEventListener("click", () => {
  cancelAsk();
  resetView();
  $("question").focus();
});

$("problem-retry").addEventListener("click", () => ask(state.lastQuestion));

function cancelAsk() {
  if (state.controller) state.controller.abort();
  state.controller = null;
  clearInterval(state.timer);
  setBusy(false);
}

function setBusy(busy) {
  $("ask-btn").disabled = busy;
  $("ask-btn").textContent = busy ? "Working…" : "Ask";
}

function validateQuestion(question) {
  if (!question) return "Enter a question, or choose one of the examples below";
  if (question.length < QUESTION_MIN) return "Your question is too short. Use a few words, for example 'Revenue by channel'";
  if (question.length > QUESTION_MAX) return "Your question must be 2,000 characters or fewer";
  return "";
}

async function ask(raw) {
  if (state.controller) return;
  const question = (raw || "").trim();
  const invalid = validateQuestion(question);
  setFieldError("question-group", "question-error", $("question"), invalid);
  if (invalid) {
    $("question").focus();
    return;
  }
  state.lastQuestion = question;

  resetView();
  $("start").hidden = true;
  $("working").hidden = false;
  setBusy(true);

  const started = Date.now();
  const tick = () => {
    const s = Math.floor((Date.now() - started) / 1000);
    $("working-time").textContent = s;
    $("working-step").textContent = STEPS.filter(([at]) => s >= at).pop()[1];
  };
  tick();
  state.timer = setInterval(tick, 1000);

  const controller = new AbortController();
  state.controller = controller;
  try {
    const response = await api("/ask", { method: "POST", body: { question }, signal: controller.signal });
    const item = { id: response.request_id, question, response, at: new Date().toISOString() };
    saveHistory([item, ...loadHistory().filter((i) => i.id !== item.id)]);
    showAnswer(item);
    renderHistory();
    refreshPlan();
  } catch (err) {
    if (err.name === "AbortError") return;
    if (err instanceof ApiError && err.status === 401) {
      signOut(state.mode === "google"
        ? { title: "Your session has ended", text: "For security, sessions end after a few hours. Sign in again to continue.", tone: "info" }
        : { title: "Your access key no longer works", text: "It may have been changed or removed. Ask your admin for a new one." });
      return;
    }
    const [title, text, retry, usage] = askErrorText(err);
    showProblem(title, text, retry, usage);
  } finally {
    if (state.controller === controller) {
      state.controller = null;
      clearInterval(state.timer);
      setBusy(false);
      $("working").hidden = true;
    }
  }
}

function askErrorText(err) {
  if (!(err instanceof ApiError)) {
    return ["Couldn't reach the service", "Check your internet connection, then try again.", true];
  }
  switch (err.status) {
    case 403:
      return ["You don't have access to any data", "Your key hasn't been given access to any data. Ask your admin to set this up.", false];
    case 422:
      return ["Your question wasn't accepted", "Questions need to be between 3 and 2,000 characters.", false];
    case 429: {
      if (err.retryAfter) {
        return ["You're asking questions too quickly", `You've asked several questions in the last minute. Wait about ${err.retryAfter} seconds, then try again.`, true];
      }
      // The server's message says which limit; pick a title to match.
      const d = err.detail || "";
      if (d.includes("already have a question running")) return ["A question is already running", d, true];
      if (d.includes("paused")) return ["Questions are paused for today", d, false];
      if (d.includes("daily maximum")) return ["You've reached today's question limit", d, false, true];
      return ["You've reached today's data limit", d || "You've used today's allowance for reading data. It resets at midnight UTC.", false];
    }
    case 502:
      return ["The assistant is busy", "The AI service is temporarily unavailable. Try again in a minute or two.", true];
    case 504:
      return ["That question took too long", "Try a narrower question, for example one channel, one product or a shorter date range.", true];
    default:
      return ["Something went wrong", "Your question couldn't be answered. Try again, or rephrase it. If it keeps happening, tell your admin.", true];
    case 402:
      return ["You've reached your plan's limit", err.detail || "Your plan's allowance is used up for now.", false, true];
  }
}

function showProblem(title, text, retry = false, usage = false) {
  $("problem-title").textContent = title;
  $("problem-text").textContent = text;
  $("problem-retry").hidden = !retry;
  $("problem-usage").hidden = !usage;
  $("problem").hidden = false;
  $("start").hidden = false;
  $("problem").focus();
}

// ---------- answer ----------

function hasChart(response) {
  return Boolean(response.chart && response.chart.points && response.chart.points.length);
}

function showAnswer(item) {
  state.current = item;
  const r = item.response;
  resetView();
  $("start").hidden = true;

  const answered = r.status === "answered";
  const declined = r.status === "declined";
  $("answer-status").className = "pill " + (answered ? "pill-success" : declined ? "pill-info" : "pill-warning");
  $("answer-status").textContent = answered ? "Answered" : declined ? "Not a data question" : "Partial answer";
  $("how-when-top").textContent = formatWhen(item.at);
  $("answer-question").textContent = item.question;
  $("answer-summary").textContent = r.summary;

  const chart = hasChart(r) ? r.chart : null;
  $("chart-wrap").hidden = !chart;
  $("dl-csv").hidden = !chart;
  $("dl-png").hidden = !chart;
  if (chart) {
    $("chart-title").textContent = chart.title;
    renderTable(chart);
  }

  const n = r.sql_used.length;
  $("how-count").textContent = n === 1 ? "1 query" : `${n} queries`;
  $("how-bytes").textContent = formatBytes(r.bytes_processed, n);
  $("how-when").textContent = formatWhen(item.at);
  $("how-id").textContent = r.request_id;
  const sql = $("how-sql");
  sql.replaceChildren();
  r.sql_used.forEach((q, i) => {
    const h = document.createElement("h3");
    h.textContent = n > 1 ? `Query ${i + 1}` : "Query";
    const pre = document.createElement("pre");
    pre.textContent = q;
    sql.append(h, pre);
  });
  $("toast").textContent = "";
  $("answer").hidden = false;
  drawChart();
  $("answer").focus();
}

function renderTable(chart) {
  const table = $("chart-table");
  table.replaceChildren();
  const cap = table.createCaption();
  cap.textContent = chart.title;
  const head = table.createTHead().insertRow();
  [chart.x_label, chart.y_label].forEach((h, i) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = h;
    if (i === 1) th.className = "num";
    head.append(th);
  });
  const body = table.createTBody();
  for (const p of chart.points) {
    const row = body.insertRow();
    const th = document.createElement("th");
    th.scope = "row";
    th.textContent = p.label;
    row.append(th);
    const td = row.insertCell();
    td.className = "num";
    td.textContent = formatNumber(p.value);
  }
}

// ---------- charts (Apache ECharts, bundled locally) ----------

function palette(forExport) {
  if (forExport) {
    // Exported images always use the light palette so they suit slides and documents.
    return { surface: "#ffffff", text: "#101828", muted: "#667085", grid: "#eaecf0", series: "#2f6fd6" };
  }
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return { surface: v("--chart-surface"), text: v("--text"), muted: v("--muted"), grid: v("--chart-grid"), series: v("--chart-series") };
}

function chartOption(chart, p, { forExport = false } = {}) {
  const font = { fontFamily: "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif" };
  const labels = chart.points.map((d) => d.label);
  const values = chart.points.map((d) => d.value);
  const axisText = { color: p.muted, fontSize: 12, ...font };
  const nameText = { color: p.muted, fontSize: 12, ...font };
  const valueAxis = {
    type: "value",
    axisLabel: { ...axisText, formatter: (v) => formatCompact(v) },
    splitLine: { lineStyle: { color: p.grid, width: 1, type: "solid" } },
    axisLine: { show: false },
    axisTick: { show: false },
  };
  const tooltip = {
    // Rendered on the canvas, never as HTML.
    renderMode: "richText",
    backgroundColor: p.surface,
    borderColor: p.grid,
    textStyle: { color: p.text, fontSize: 13, ...font },
    confine: true,
  };
  const base = {
    animation: !forExport,
    backgroundColor: forExport ? p.surface : "transparent",
    textStyle: font,
    aria: { enabled: true },
    title: forExport
      ? { text: chart.title, left: 16, top: 12, textStyle: { color: p.text, fontSize: 18, fontWeight: 600, ...font } }
      : undefined,
  };
  const top = forExport ? 56 : 12;

  if (chart.type === "line") {
    const showDots = values.length <= 60;
    return {
      ...base,
      // Extra room at the top for the y-axis title.
      grid: { left: 16, right: 56, top: top + 30, bottom: 44, containLabel: true },
      tooltip: {
        ...tooltip,
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: p.muted, width: 1 } },
        formatter: (items) => {
          const it = Array.isArray(items) ? items[0] : items;
          return `${it.name}\n${chart.y_label}: ${formatNumber(it.value)}`;
        },
      },
      xAxis: {
        type: "category",
        data: labels,
        boundaryGap: false,
        name: chart.x_label,
        nameLocation: "middle",
        nameGap: 30,
        nameTextStyle: nameText,
        axisLabel: { ...axisText, hideOverlap: true },
        axisLine: { lineStyle: { color: p.grid } },
        axisTick: { show: false },
      },
      yAxis: { ...valueAxis, name: chart.y_label, nameGap: 14, nameTextStyle: { ...nameText, align: "left" } },
      series: [{
        type: "line",
        data: values,
        lineStyle: { width: 2, color: p.series, cap: "round", join: "round" },
        itemStyle: { color: p.series, borderColor: p.surface, borderWidth: 2 },
        symbol: "circle",
        symbolSize: 8,
        showSymbol: showDots,
        areaStyle: { color: p.series, opacity: 0.1 },
        emphasis: { focus: "none", scale: 1.4 },
        // Label only the latest value; the axis, tooltip and table carry the rest.
        endLabel: { show: true, color: p.text, fontWeight: 600, fontSize: 12, formatter: (d) => formatCompact(d.value), ...font },
      }],
    };
  }

  // Horizontal bars keep long category names readable. First item at the top.
  return {
    ...base,
    grid: { left: 16, right: 64, top, bottom: 40, containLabel: true },
    tooltip: {
      ...tooltip,
      trigger: "item",
      formatter: (d) => `${d.name}\n${chart.y_label}: ${formatNumber(d.value)}`,
    },
    yAxis: {
      type: "category",
      data: labels,
      inverse: true,
      axisLabel: { color: p.text, fontSize: 13, ...font, width: 180, overflow: "truncate" },
      axisLine: { lineStyle: { color: p.grid } },
      axisTick: { show: false },
    },
    xAxis: { ...valueAxis, name: chart.y_label, nameLocation: "middle", nameGap: 28, nameTextStyle: nameText },
    series: [{
      type: "bar",
      data: values.map((v) => ({
        value: v,
        // 4px rounded data end, square at the baseline.
        itemStyle: { borderRadius: v < 0 ? [4, 0, 0, 4] : [0, 4, 4, 0] },
      })),
      barMaxWidth: 24,
      barCategoryGap: "35%",
      itemStyle: { color: p.series },
      emphasis: { itemStyle: { opacity: 0.85 } },
      label: {
        show: true,
        position: "right",
        color: p.text,
        fontSize: 12,
        fontWeight: 600,
        ...font,
        formatter: (d) => formatNumber(d.value),
      },
    }],
  };
}

function chartHeight(chart, forExport = false) {
  const extra = forExport ? 44 : 0;
  if (chart.type === "line") return 340 + extra;
  return Math.max(200, chart.points.length * 36 + 70) + extra;
}

function drawChart() {
  disposeChart();
  const chart = state.current && state.current.response.chart;
  if (!chart || !hasChart(state.current.response) || $("answer").hidden || !window.echarts) return;
  const el = $("chart");
  el.style.height = chartHeight(chart) + "px";
  state.chart = window.echarts.init(el, null, { renderer: "canvas" });
  state.chart.setOption(chartOption(chart, palette(false)));
}

function disposeChart() {
  if (state.chart) {
    state.chart.dispose();
    state.chart = null;
  }
}

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (state.chart) state.chart.resize();
    if (state.usageChart) state.usageChart.resize();
  }, 100);
});
// Canvas text uses whatever font is loaded at draw time; redraw once Inter is ready.
if (document.fonts) document.fonts.ready.then(() => { if (state.chart) drawChart(); });
// Redraw with the other palette when the system switches light/dark mode.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (state.chart) drawChart();
  if (state.usageChart && state.usage) drawUsageChart(state.usage);
});

function chartPng(chart) {
  // Render off screen at a fixed, slide-friendly size, then discard.
  const host = document.createElement("div");
  host.style.position = "fixed";
  host.style.left = "-10000px";
  host.style.top = "0";
  host.style.width = "960px";
  host.style.height = chartHeight(chart, true) + "px";
  document.body.append(host);
  const instance = window.echarts.init(host, null, { renderer: "canvas" });
  try {
    instance.setOption(chartOption(chart, palette(true), { forExport: true }));
    const url = instance.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#ffffff" });
    // Decode here: fetch() of a data: URL is blocked by the page's connect-src policy.
    const bytes = Uint8Array.from(atob(url.split(",")[1]), (c) => c.charCodeAt(0));
    return new Blob([bytes], { type: "image/png" });
  } finally {
    instance.dispose();
    host.remove();
  }
}

// ---------- sharing and export ----------

function toast(text) {
  $("toast").textContent = text;
  setTimeout(() => { if ($("toast").textContent === text) $("toast").textContent = ""; }, 4000);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

function answerAsText(item) {
  const r = item.response;
  const lines = [item.question, "", r.summary];
  if (hasChart(r)) {
    lines.push("", r.chart.title);
    for (const p of r.chart.points) lines.push(`- ${p.label}: ${formatNumber(p.value)}`);
  }
  lines.push("", `Source: Marketing Answers, ${formatWhen(item.at)} (ref ${r.request_id})`);
  return lines.join("\n");
}

$("copy-answer").addEventListener("click", async () => {
  if (!state.current) return;
  toast((await copyText(answerAsText(state.current)))
    ? "Answer copied. Paste it into an email, document or chat."
    : "Couldn't copy. Select the text and copy it instead.");
});

$("copy-link").addEventListener("click", async () => {
  if (!state.current) return;
  const url = new URL(location.pathname, location.origin);
  url.searchParams.set("q", state.current.question);
  toast((await copyText(url.toString()))
    ? "Link copied. Colleagues with access can open it to ask the same question."
    : "Couldn't copy the link.");
});

function csvCell(value) {
  let s = String(value);
  // Stop spreadsheets treating a label as a formula.
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function fileName(title, ext) {
  const base = (title || "answer").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
  return `${base || "answer"}.${ext}`;
}

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("dl-csv").addEventListener("click", () => {
  const chart = state.current && state.current.response.chart;
  if (!chart) return;
  const rows = [[chart.x_label, chart.y_label], ...chart.points.map((p) => [p.label, p.value])];
  const csv = "﻿" + rows.map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
  download(new Blob([csv], { type: "text/csv;charset=utf-8" }), fileName(chart.title, "csv"));
  toast("Downloaded. Open it in Excel or Google Sheets.");
});

$("dl-png").addEventListener("click", async () => {
  const chart = state.current && state.current.response.chart;
  if (!chart || !window.echarts) return;
  try {
    download(await chartPng(chart), fileName(chart.title, "png"));
    toast("Downloaded. Drop it into a slide or document.");
  } catch {
    toast("Couldn't create the image in this browser.");
  }
});

// ---------- formatting ----------

function formatNumber(v) {
  const a = Math.abs(v);
  return new Intl.NumberFormat(LOCALE, { maximumFractionDigits: a < 10 ? 2 : a < 1000 ? 1 : 0 }).format(v);
}

function formatCompact(v) {
  return new Intl.NumberFormat(LOCALE, { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

function formatWhen(iso) {
  return new Date(iso).toLocaleString(LOCALE, { dateStyle: "medium", timeStyle: "short" });
}

function formatBytes(n, queries) {
  // BigQuery reads nothing when it reuses a cached result for an identical query.
  if (!n) return queries ? "None: reused a recent result" : "None: answered from the table descriptions";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : n} ${units[i]}`;
}

// ---------- plans and usage ----------

const PLAN_LABELS = { subscription: "Subscription", payg: "Pay as you go", freemium: "Free trial" };

function formatUSD(v) {
  if (v > 0 && v < 0.01) return "<$0.01";
  return new Intl.NumberFormat(LOCALE, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(v);
}

function formatDay(iso) {
  return new Date(iso + (iso.length === 10 ? "T00:00:00Z" : "")).toLocaleDateString(LOCALE, { day: "numeric", month: "short", timeZone: "UTC" });
}

function showView(name) {
  state.view = name;
  $("ask-view").hidden = name !== "ask";
  $("usage-view").hidden = name !== "usage";
  // Recent questions belong with asking; the usage page has its own history.
  $("sidebar").hidden = name !== "ask";
  $("app").classList.toggle("single", name !== "ask");
  $("tab-ask").toggleAttribute("aria-current", name === "ask");
  $("tab-usage").toggleAttribute("aria-current", name === "usage");
  if (name === "ask") $("tab-ask").setAttribute("aria-current", "page");
  else $("tab-usage").setAttribute("aria-current", "page");
  history.replaceState(null, "", location.pathname + location.search + (name === "usage" ? "#usage" : ""));
  if (name === "usage") {
    loadUsagePage();
  } else {
    disposeUsageChart();
    if (state.chart) state.chart.resize();
  }
}

// Back/forward and #usage links switch sections too.
window.addEventListener("hashchange", () => {
  const view = location.hash === "#usage" ? "usage" : "ask";
  if (!$("app").hidden && view !== state.view) showView(view);
});

$("tab-ask").addEventListener("click", () => showView("ask"));
$("tab-usage").addEventListener("click", () => showView("usage"));
$("trial-usage").addEventListener("click", () => showView("usage"));
$("problem-usage").addEventListener("click", () => showView("usage"));

// Keep the plan label and the trial allowance note current.
async function refreshPlan() {
  try {
    state.usage = await api("/usage");
  } catch {
    return;
  }
  renderTrialNote(state.usage);
}

function renderTrialNote(report) {
  const trial = report.plan.id === "freemium";
  $("trial-note").hidden = !trial;
  if (!trial) return;
  const q = report.limits.find((l) => l.id === "trial_questions");
  const parts = [];
  if (q) {
    const left = Math.max(0, q.limit - q.used);
    parts.push(`${left} of ${q.limit} free question${q.limit === 1 ? "" : "s"} left today`);
  }
  if (report.trial_ends_on) parts.push(`trial ends ${formatDay(report.trial_ends_on)}`);
  parts.push("sample data only");
  $("trial-text").textContent = parts.join(" · ");
}

async function loadUsagePage() {
  await refreshPlan();
  const report = state.usage;
  if (!report || state.view !== "usage") return;
  renderUsage(report);
  if (report.is_usage_admin) {
    try {
      renderAdminUsage(await api("/usage/all"));
    } catch {}
  }
}

function formatLimit(value, unit) {
  return unit === "usd" ? formatUSD(value) : new Intl.NumberFormat(LOCALE).format(value);
}

function renderUsage(report) {
  $("plan-name").textContent = report.plan.name;
  $("plan-summary").textContent = report.plan.summary;
  $("plan-period").textContent = report.plan.id === "freemium"
    ? (report.trial_ends_on ? `Trial ends ${formatDay(report.trial_ends_on)}` : "")
    : `This month renews ${formatDay(report.period.renews_on)}`;

  const list = $("limit-list");
  list.replaceChildren();
  for (const item of report.limits) {
    const pct = item.limit > 0 ? Math.min(100, (item.used / item.limit) * 100) : 100;
    const full = item.used >= item.limit;
    const card = document.createElement("div");
    card.className = "card meter" + (full ? " is-full" : pct >= 80 ? " is-warning" : "");
    const head = document.createElement("div");
    head.className = "meter-head";
    const label = document.createElement("span");
    label.className = "meter-label";
    label.textContent = item.label;
    const value = document.createElement("span");
    value.className = "meter-value";
    value.textContent = `${formatLimit(item.used, item.unit)} of ${formatLimit(item.limit, item.unit)}`;
    head.append(label, value);
    const track = document.createElement("div");
    track.className = "meter-track";
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", item.label);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(Math.round(pct)));
    const fill = document.createElement("div");
    fill.className = "meter-fill";
    fill.style.width = pct + "%";
    track.append(fill);
    const foot = document.createElement("div");
    foot.className = "meter-foot";
    const stateText = document.createElement("span");
    stateText.className = "meter-state";
    // Words as well as colour, so the state never depends on colour alone.
    stateText.textContent = full ? "Used up" : pct >= 80 ? "Almost used"
      : item.used > 0 && pct < 1 ? "Less than 1% used" : `${Math.round(pct)}% used`;
    const resets = document.createElement("span");
    resets.textContent = `Resets ${formatDay(item.resets_at.slice(0, 10))}`;
    foot.append(stateText, resets);
    card.append(head, track, foot);
    list.append(card);
  }

  const payg = report.plan.id === "payg";
  $("stat-questions").textContent = new Intl.NumberFormat(LOCALE).format(report.month.questions);
  $("stat-money-label").textContent = payg ? "Charged this month" : report.plan.id === "freemium" ? "Free usage this month" : "Usage this month";
  $("stat-money").textContent = formatUSD(payg ? report.month.charged_usd : report.month.cost_usd);
  $("stat-today").textContent = new Intl.NumberFormat(LOCALE).format(report.today.questions);

  $("usage-chart-title").textContent = payg ? "Charged per day this month" : "Questions per day this month";
  drawUsageChart(report);
  renderDailyTable(report);
  renderRecent(report);
}

function usageMetric(report) {
  const payg = report.plan.id === "payg";
  return {
    payg,
    label: payg ? "Charged (USD)" : "Questions",
    value: (d) => (payg ? d.charged_usd : d.questions),
    format: (v) => (payg ? formatUSD(v) : new Intl.NumberFormat(LOCALE).format(v)),
  };
}

function drawUsageChart(report) {
  disposeUsageChart();
  if (!window.echarts || state.view !== "usage") return;
  const m = usageMetric(report);
  const p = palette(false);
  const font = { fontFamily: "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif" };
  const el = $("usage-chart");
  el.style.height = "240px";
  state.usageChart = window.echarts.init(el, null, { renderer: "canvas" });
  state.usageChart.setOption({
    animation: true,
    backgroundColor: "transparent",
    textStyle: font,
    aria: { enabled: true },
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "item",
      renderMode: "richText",
      backgroundColor: p.surface,
      borderColor: p.grid,
      textStyle: { color: p.text, fontSize: 13, ...font },
      formatter: (d) => `${formatDay(report.daily[d.dataIndex].day)}\n${m.label}: ${m.format(d.value)}`,
    },
    xAxis: {
      type: "category",
      data: report.daily.map((d) => String(Number(d.day.slice(8)))),
      axisLabel: { color: p.muted, fontSize: 11, ...font, hideOverlap: true },
      axisLine: { lineStyle: { color: p.grid } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      minInterval: m.payg ? undefined : 1,
      axisLabel: { color: p.muted, fontSize: 11, ...font, formatter: (v) => (m.payg ? formatUSD(v) : formatCompact(v)) },
      splitLine: { lineStyle: { color: p.grid, width: 1 } },
    },
    series: [{
      type: "bar",
      data: report.daily.map(m.value),
      barMaxWidth: 16,
      itemStyle: { color: p.series, borderRadius: [4, 4, 0, 0] },
      emphasis: { itemStyle: { opacity: 0.85 } },
    }],
  });
}

function disposeUsageChart() {
  if (state.usageChart) {
    state.usageChart.dispose();
    state.usageChart = null;
  }
}

function fillTable(table, headers, rows, numeric = []) {
  table.replaceChildren();
  const head = table.createTHead().insertRow();
  headers.forEach((h, i) => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = h;
    if (numeric.includes(i)) th.className = "num";
    head.append(th);
  });
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    row.forEach((cell, i) => {
      const td = tr.insertCell();
      td.textContent = cell;
      if (numeric.includes(i)) td.className = "num";
      if (typeof cell === "string" && cell.length > 60) {
        td.className = "question";
        td.title = cell;
      }
    });
  }
}

function renderDailyTable(report) {
  const m = usageMetric(report);
  fillTable(
    $("usage-daily-table"),
    ["Day", "Questions", m.payg ? "Charged" : "Usage"],
    report.daily.filter((d) => d.questions).map((d) => [
      formatDay(d.day), String(d.questions), formatUSD(m.payg ? d.charged_usd : d.cost_usd),
    ]),
    [1, 2],
  );
}

const STATUS_LABELS = {
  answered: "Answered", limitation: "Partial answer", declined: "Not a data question", error: "Failed",
};

function renderRecent(report) {
  const payg = report.plan.id === "payg";
  $("usage-recent-empty").hidden = report.recent.length > 0;
  $("usage-recent-table").hidden = report.recent.length === 0;
  fillTable(
    $("usage-recent-table"),
    ["When", "Question", "Result", payg ? "Charged" : "Usage"],
    report.recent.map((r) => [
      formatWhen(r.ts), r.question, STATUS_LABELS[r.status] || r.status, formatUSD(payg ? r.charged_usd : r.cost_usd),
    ]),
    [3],
  );
}

function renderAdminUsage(all) {
  $("usage-admin").hidden = false;
  fillTable(
    $("usage-admin-table"),
    ["Person", "Plan", "Questions", "Usage", "Charged", "Last used"],
    all.users.map((u) => [
      u.user, PLAN_LABELS[u.plan] || u.plan, String(u.questions), formatUSD(u.cost_usd), formatUSD(u.charged_usd), formatWhen(u.last_used),
    ]),
    [2, 3, 4],
  );
}

// ---------- start ----------

async function start() {
  const params = new URLSearchParams(location.search);
  const signinError = params.get("signin_error");
  if (signinError) {
    // Show it once; don't keep it in the address bar or in shared links.
    params.delete("signin_error");
    history.replaceState(null, "", location.pathname + (params.toString() ? "?" + params : ""));
  }

  let session = { google_enabled: false, signed_in: false };
  try {
    session = await api("/auth/session", { key: null });
  } catch {}
  state.googleEnabled = Boolean(session.google_enabled);

  const saved = readKey();
  if (saved) {
    try {
      state.key = saved.key;
      state.mode = "key";
      state.user = await api("/whoami");
      return showApp();
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        await endSession();
        return showSignin({ keyError: signinErrorText(err) });
      }
      // Keep the saved key; the service may just be down for a moment.
      state.key = null;
      return showSignin({ alert: { title: "Couldn't reach the service", text: "Check your internet connection, then refresh the page." } });
    }
  }

  if (session.signed_in) {
    state.mode = "google";
    state.name = session.name || "";
    try {
      state.user = await api("/whoami", { key: null });
      return showApp();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) return showPending(session.email, err.detail);
      if (!(err instanceof ApiError)) {
        return showSignin({ alert: { title: "Couldn't reach the service", text: "Check your internet connection, then refresh the page." } });
      }
    }
  }

  showSignin({
    clearKey: false,
    alert: signinError ? SIGNIN_ERRORS[signinError] || SIGNIN_ERRORS.failed : null,
  });
}

start();
