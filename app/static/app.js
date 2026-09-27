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
  const headers = { "X-API-Key": key };
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
  $(group).classList.toggle("form-group-error", Boolean(text));
  $(messageEl).hidden = !text;
  $(messageEl).textContent = text ? "Error: " + text : "";
  const described = input.getAttribute("aria-describedby").split(" ").filter((id) => id !== messageEl);
  if (text) described.push(messageEl);
  input.setAttribute("aria-describedby", described.join(" "));
  input.setAttribute("aria-invalid", text ? "true" : "false");
}

// ---------- sign in ----------

function showSignin(message, clearKey = true) {
  $("app").hidden = true;
  $("account").hidden = true;
  $("signin").hidden = false;
  showSigninError(message || "");
  // On first load, keep anything typed or pasted before the script ran.
  if (clearKey) $("key-input").value = "";
  if (!message) $("key-input").focus();
}

function showSigninError(text) {
  setFieldError("key-group", "key-error", $("key-input"), text);
  $("signin-errors").hidden = !text;
  $("signin-error-link").textContent = text;
  if (text) $("signin-errors").focus();
}

$("signin-error-link").addEventListener("click", (e) => {
  e.preventDefault();
  $("key-input").focus();
});

async function signIn(key, persist) {
  const user = await api("/whoami", { key });
  state.key = key;
  state.user = user;
  saveKey(key, persist);
  showApp();
}

function signOut(message) {
  cancelAsk();
  forgetKey();
  disposeChart();
  state.key = null;
  state.user = null;
  state.current = null;
  showSignin(message);
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

// ---------- app shell ----------

function showApp() {
  if (/Mac|iPhone|iPad/.test(navigator.platform)) $("mod-key").textContent = "⌘";
  $("signin").hidden = true;
  $("app").hidden = false;
  $("account").hidden = false;
  $("who-name").textContent = "Signed in as " + state.user.user;
  renderDatasets();
  renderHistory();
  resetView();

  const q = new URLSearchParams(location.search).get("q");
  if (q) $("question").value = q.slice(0, QUESTION_MAX);
  updateCount();
  $("question").focus();
}

function renderDatasets() {
  const list = $("dataset-list");
  list.replaceChildren();
  const known = state.user.allowed_datasets.filter((d) => DATASETS[d]);
  const unknown = state.user.allowed_datasets.filter((d) => !DATASETS[d]);
  for (const name of known) {
    const info = DATASETS[name];
    const card = document.createElement("section");
    card.className = "dataset";
    const h = document.createElement("h3");
    h.className = "heading-s";
    h.textContent = info.title;
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = info.about;
    const ul = document.createElement("ul");
    for (const q of info.examples) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "link-button";
      b.textContent = q;
      b.addEventListener("click", () => {
        $("question").value = q;
        updateCount();
        ask(q);
      });
      li.append(b);
      ul.append(li);
    }
    card.append(h, p, ul);
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
    ? `You have ${left.toLocaleString(LOCALE)} character${left === 1 ? "" : "s"} remaining`
    : `You have ${(-left).toLocaleString(LOCALE)} characters too many`;
  el.classList.toggle("over", left < 0);
}

$("question").addEventListener("input", () => {
  updateCount();
  if ($("question-group").classList.contains("form-group-error")) {
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
    b.className = "link-button";
    b.append(item.question);
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = formatWhen(item.at);
    b.append(when);
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
  } catch (err) {
    if (err.name === "AbortError") return;
    if (err instanceof ApiError && err.status === 401) {
      signOut("Your access key is no longer valid. Sign in again");
      return;
    }
    const [title, text, retry] = askErrorText(err);
    showProblem(title, text, retry);
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
    case 429:
      if (err.retryAfter) {
        return ["You're asking questions too quickly", `You've asked several questions in the last minute. Wait about ${err.retryAfter} seconds, then try again.`, true];
      }
      return ["You've reached today's data limit", "You've used today's allowance for reading data. It resets at midnight UTC. If you need more, ask your admin.", false];
    case 502:
      return ["The assistant is busy", "The AI service is temporarily unavailable. Try again in a minute or two.", true];
    case 504:
      return ["That question took too long", "Try a narrower question, for example one channel, one product or a shorter date range.", true];
    default:
      return ["Something went wrong", "Your question couldn't be answered. Try again, or rephrase it. If it keeps happening, tell your admin.", true];
  }
}

function showProblem(title, text, retry = false) {
  $("problem-title").textContent = title;
  $("problem-text").textContent = text;
  $("problem-retry").hidden = !retry;
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
  $("answer-banner").className = "banner" + (answered ? " banner-success" : "");
  $("answer-status").textContent = answered ? "Answer" : "Partly answered: the data couldn't fully answer this";
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
  $("how-bytes").textContent = formatBytes(r.bytes_processed);
  $("how-when").textContent = formatWhen(item.at);
  $("how-id").textContent = r.request_id;
  const sql = $("how-sql");
  sql.replaceChildren();
  r.sql_used.forEach((q, i) => {
    const h = document.createElement("h3");
    h.className = "heading-s";
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
  cap.className = "hint";
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
    return { surface: "#ffffff", text: "#0b0c0c", muted: "#484949", grid: "#e6e8ea", series: "#2a78d6" };
  }
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return { surface: v("--chart-surface"), text: v("--text"), muted: v("--text-secondary"), grid: v("--chart-grid"), series: v("--chart-series") };
}

function chartOption(chart, p, { forExport = false } = {}) {
  const font = { fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif" };
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
  resizeTimer = setTimeout(() => state.chart && state.chart.resize(), 100);
});
// Redraw with the other palette when the system switches light/dark mode.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (state.chart) drawChart();
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

function formatBytes(n) {
  if (!n) return "None: answered from the table descriptions";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : n} ${units[i]}`;
}

// ---------- start ----------

(async function start() {
  const saved = readKey();
  if (!saved) return showSignin("", false);
  try {
    state.key = saved.key;
    state.user = await api("/whoami");
    showApp();
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      signOut(signinErrorText(err));
    } else {
      // Keep the saved key; the service may just be down for a moment.
      state.key = null;
      showSignin(signinErrorText(err));
    }
  }
})();
