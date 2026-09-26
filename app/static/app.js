// Marketing Answers: browser UI for POST /ask.
// Everything from the server is inserted as text or SVG attributes, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);

const KEY_NAME = "mkt.key";
const HISTORY_MAX = 25;

const EXAMPLES = {
  marketing: [
    "Which channel brought in the most revenue?",
    "What was return on ad spend (ROAS) by channel?",
    "How has weekly ad spend changed over time?",
    "Which 5 campaigns had the highest click-through rate?",
    "Which channel has the lowest cost per click?",
  ],
  customers: [
    "Which countries do most of our customers come from?",
    "How many new customers signed up each month?",
  ],
};

// Progress messages shown while waiting; [seconds elapsed, message].
const STEPS = [
  [0, "Reading your question…"],
  [4, "Looking up the right tables…"],
  [12, "Running the numbers…"],
  [30, "Checking the results…"],
  [60, "Still working. Bigger questions take longer…"],
  [120, "Almost at the time limit. Hang on…"],
];

const state = {
  key: null,
  user: null,
  controller: null,
  timer: null,
  current: null, // {id, question, response, at}
  lastQuestion: "",
};

// ---------- storage (may be unavailable, e.g. private windows) ----------

function store(persist) {
  try {
    return persist ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

function readKey() {
  for (const s of [store(false), store(true)]) {
    try {
      const k = s && s.getItem(KEY_NAME);
      if (k) return { key: k, persist: s === store(true) };
    } catch {}
  }
  return null;
}

function saveKey(key, persist) {
  forgetKey();
  try { store(persist).setItem(KEY_NAME, key); } catch {}
}

function forgetKey() {
  for (const s of [store(false), store(true)]) {
    try { s && s.removeItem(KEY_NAME); } catch {}
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

// ---------- sign in ----------

function showSignin(message, clearKey = true) {
  $("app").hidden = true;
  $("signin").hidden = false;
  const err = $("signin-error");
  err.textContent = message || "";
  err.hidden = !message;
  // On first load, keep anything typed or pasted before the script ran.
  if (clearKey) $("key-input").value = "";
  $("key-input").focus();
}

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
  state.key = null;
  state.user = null;
  state.current = null;
  showSignin(message);
}

function signinErrorText(err) {
  if (err instanceof ApiError) {
    if (err.status === 401) return "That key wasn't recognised. Check you copied all of it, with no spaces.";
    if (err.status === 403) return "Your key works, but it hasn't been given access to any data yet. Ask your admin to set this up.";
    return "The service had a problem signing you in. Try again in a minute.";
  }
  return "Couldn't reach the service. Check your internet connection and try again.";
}

$("signin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("key-input").value.trim();
  if (!key) {
    showSigninError("Paste your access key first.");
    return;
  }
  const btn = e.submitter || e.target.querySelector("button");
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

function showSigninError(text) {
  const err = $("signin-error");
  err.textContent = text;
  err.hidden = false;
}

$("signout").addEventListener("click", () => signOut());

// ---------- app shell ----------

function showApp() {
  if (/Mac|iPhone|iPad/.test(navigator.platform)) $("mod-key").textContent = "⌘";
  $("signin").hidden = true;
  $("app").hidden = false;
  $("who-name").textContent = "Signed in as " + state.user.user;
  $("who-data").textContent = "· Data you can use: " + state.user.allowed_datasets.join(", ");
  renderExamples();
  renderHistory();
  resetView();

  const q = new URLSearchParams(location.search).get("q");
  if (q) $("question").value = q.slice(0, 2000);
  $("question").focus();
}

function renderExamples() {
  const list = $("example-list");
  list.replaceChildren();
  const questions = state.user.allowed_datasets.flatMap((d) => EXAMPLES[d] || []);
  if (!questions.length) questions.push("What tables can I ask about, and what's in them?");
  for (const q of questions) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = q;
    b.addEventListener("click", () => {
      $("question").value = q;
      ask(q);
    });
    list.append(b);
  }
}

function resetView() {
  $("answer").hidden = true;
  $("problem").hidden = true;
  $("working").hidden = true;
  $("examples").hidden = false;
}

// ---------- history ----------

function renderHistory() {
  const items = loadHistory();
  const list = $("history-list");
  list.replaceChildren();
  for (const item of items) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = item.question;
    b.title = item.question;
    if (state.current && state.current.id === item.id) b.setAttribute("aria-current", "true");
    b.addEventListener("click", () => {
      if (state.controller) return;
      $("question").value = item.question;
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

async function ask(raw) {
  const question = (raw || "").trim();
  if (state.controller) return;
  if (question.length < 3) {
    showProblem("Question too short", "Type a question of at least a few words, or pick one of the examples.");
    return;
  }
  state.lastQuestion = question;

  $("answer").hidden = true;
  $("problem").hidden = true;
  $("examples").hidden = true;
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
      signOut("Your access key is no longer valid. Please sign in again.");
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
      return ["No data access", "Your key hasn't been given access to any data. Ask your admin to set this up.", false];
    case 422:
      return ["Question not accepted", "Questions need to be between 3 and 2,000 characters.", false];
    case 429:
      if (err.retryAfter) {
        return ["Slow down a little", `You've asked several questions in the last minute. Try again in about ${err.retryAfter} seconds.`, true];
      }
      return ["Daily limit reached", "You've used today's data allowance. It resets at midnight UTC. If you need more, ask your admin.", false];
    case 502:
      return ["The AI service is busy", "The assistant is temporarily unavailable. Try again in a minute or two.", true];
    case 504:
      return ["That took too long", "Try a narrower question, for example one channel, one campaign or a shorter date range.", true];
    default:
      return ["Something went wrong", "The question couldn't be answered. Try again, or rephrase it. If it keeps happening, tell your admin.", true];
  }
}

function showProblem(title, text, retry = false) {
  $("problem-title").textContent = title;
  $("problem-text").textContent = text;
  $("problem-retry").hidden = !retry;
  $("problem").hidden = false;
}

// ---------- answer ----------

function showAnswer(item) {
  state.current = item;
  const r = item.response;
  resetView();
  $("examples").hidden = true;

  const answered = r.status === "answered";
  const badge = $("answer-badge");
  badge.textContent = answered ? "Answered" : "Partly answered";
  badge.className = "badge " + (answered ? "ok" : "warn");
  $("answer-when").textContent = new Date(item.at).toLocaleString(LOCALE, { dateStyle: "medium", timeStyle: "short" });
  $("answer-question").textContent = item.question;
  $("answer-summary").textContent = r.summary;

  const chart = r.chart && r.chart.points && r.chart.points.length ? r.chart : null;
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
  $("how-id").textContent = r.request_id;
  const sql = $("how-sql");
  sql.replaceChildren();
  r.sql_used.forEach((q, i) => {
    const pre = document.createElement("pre");
    pre.textContent = q;
    pre.setAttribute("aria-label", `Query ${i + 1}`);
    sql.append(pre);
  });
  $("toast").textContent = "";
  $("answer").hidden = false;
  drawChart();
}

// Charts are drawn at the width they are shown, so their text stays readable.
function drawChart() {
  const chart = state.current && state.current.response.chart;
  if (!chart || !chart.points || !chart.points.length || $("answer").hidden) return;
  const width = Math.max(300, Math.floor($("chart").clientWidth) || 720);
  $("chart").replaceChildren(renderChart(chart, cssPalette(), false, width));
}

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(drawChart, 150);
});

function renderTable(chart) {
  const table = $("chart-table");
  table.replaceChildren();
  const head = table.createTHead().insertRow();
  for (const h of [chart.x_label, chart.y_label]) {
    const th = document.createElement("th");
    th.textContent = h;
    head.append(th);
  }
  const body = table.createTBody();
  for (const p of chart.points) {
    const row = body.insertRow();
    row.insertCell().textContent = p.label;
    const td = row.insertCell();
    td.className = "num";
    td.textContent = formatNumber(p.value);
  }
}

// ---------- sharing and export ----------

function toast(text) {
  $("toast").textContent = text;
  setTimeout(() => { if ($("toast").textContent === text) $("toast").textContent = ""; }, 3000);
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
  if (r.chart && r.chart.points.length) {
    lines.push("", r.chart.title);
    for (const p of r.chart.points) lines.push(`- ${p.label}: ${formatNumber(p.value)}`);
  }
  lines.push("", `Source: Marketing Answers, ${new Date(item.at).toLocaleDateString(LOCALE, { dateStyle: "medium" })} (ref ${r.request_id})`);
  return lines.join("\n");
}

$("copy-answer").addEventListener("click", async () => {
  if (!state.current) return;
  toast((await copyText(answerAsText(state.current))) ? "Copied. Paste it into an email, doc or chat." : "Couldn't copy. Select the text and copy it instead.");
});

$("copy-link").addEventListener("click", async () => {
  if (!state.current) return;
  const url = new URL(location.pathname, location.origin);
  url.searchParams.set("q", state.current.question);
  toast((await copyText(url.toString())) ? "Link copied. Anyone with access can open it to ask the same question." : "Couldn't copy the link.");
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
  if (!chart) return;
  try {
    const blob = await chartPng(chart);
    download(blob, fileName(chart.title, "png"));
    toast("Downloaded. Drop it into a slide or document.");
  } catch {
    toast("Couldn't create the image in this browser.");
  }
});

// Exported images always use the light palette so they suit slides and documents.
const LIGHT = { bg: "#ffffff", text: "#1c2430", muted: "#5d6878", grid: "#dde2e8", accent: "#2458d6" };

function cssPalette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return { bg: v("--surface"), text: v("--text"), muted: v("--muted"), grid: v("--border"), accent: v("--accent") };
}

function chartPng(chart) {
  const svg = renderChart(chart, LIGHT, true);
  const w = Number(svg.getAttribute("width"));
  const h = Number(svg.getAttribute("height"));
  const src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(svg));
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const scale = 2;
      const canvas = document.createElement("canvas");
      canvas.width = w * scale;
      canvas.height = h * scale;
      const ctx = canvas.getContext("2d");
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0, w, h);
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("toBlob failed"))), "image/png");
    };
    img.onerror = reject;
    img.src = src;
  });
}

// ---------- charts (plain SVG) ----------

const SVG_NS = "http://www.w3.org/2000/svg";
const FONT = "system-ui, -apple-system, Segoe UI, Roboto, sans-serif";

function el(name, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function textEl(x, y, str, p, attrs = {}) {
  return el("text", { x, y, fill: p.text, "font-size": 13, "font-family": FONT, ...attrs }, str);
}

function niceTicks(min, max, count = 5) {
  if (min === max) max = min + 1;
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const ticks = [];
  for (let t = Math.floor(min / step) * step; t <= max + step * 1e-9; t += step) ticks.push(+t.toFixed(10));
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function renderChart(chart, p, forExport = false, width = 720) {
  const svg = chart.type === "line" ? lineChart(chart, p, width) : barChart(chart, p, width);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", chart.title);
  if (forExport) {
    // Title and background baked in so the image stands alone.
    const w = Number(svg.getAttribute("width"));
    const h = Number(svg.getAttribute("height"));
    const pad = 36;
    const out = el("svg", { xmlns: SVG_NS, width: w + 32, height: h + pad + 16, viewBox: `0 0 ${w + 32} ${h + pad + 16}` });
    out.append(el("rect", { width: "100%", height: "100%", fill: p.bg }));
    out.append(textEl(16, 26, chart.title, p, { "font-size": 16, "font-weight": 600 }));
    const g = el("g", { transform: `translate(16 ${pad})` });
    g.append(...svg.childNodes);
    out.append(g);
    return out;
  }
  return svg;
}

// Horizontal bars: long category names stay readable.
function barChart(chart, p, W) {
  const pts = chart.points;
  const rowH = 32;
  const labelW = Math.min(220, W * 0.32, Math.max(60, Math.max(...pts.map((d) => d.label.length)) * 7.5 + 12));
  const valueW = 72;
  const top = 4;
  const axisH = 36;
  const H = top + pts.length * rowH + axisH;
  const x0 = labelW;
  const x1 = W - valueW;
  const lo = Math.min(0, ...pts.map((d) => d.value));
  const hi = Math.max(0, ...pts.map((d) => d.value));
  const ticks = niceTicks(lo, hi);
  const tmin = ticks[0];
  const tmax = ticks[ticks.length - 1];
  const sx = (v) => x0 + ((v - tmin) / (tmax - tmin)) * (x1 - x0);

  const svg = el("svg", { xmlns: SVG_NS, width: W, height: H, viewBox: `0 0 ${W} ${H}` });
  for (const t of ticks) {
    svg.append(el("line", { x1: sx(t), x2: sx(t), y1: top, y2: top + pts.length * rowH, stroke: p.grid, "stroke-width": 1 }));
    svg.append(textEl(sx(t), top + pts.length * rowH + 16, formatCompact(t), p, { fill: p.muted, "text-anchor": "middle", "font-size": 12 }));
  }
  pts.forEach((d, i) => {
    const y = top + i * rowH;
    const a = sx(Math.min(0, d.value));
    const b = sx(Math.max(0, d.value));
    const bar = el("rect", { x: a, y: y + 6, width: Math.max(b - a, 1), height: rowH - 12, rx: 3, fill: p.accent });
    bar.append(el("title", {}, `${d.label}: ${formatNumber(d.value)}`));
    svg.append(bar);
    svg.append(textEl(labelW - 8, y + rowH / 2 + 4, truncate(d.label, Math.floor(labelW / 7.5)), p, { "text-anchor": "end" }));
    svg.append(textEl(b + 6, y + rowH / 2 + 4, formatNumber(d.value), p, { "font-weight": 600 }));
  });
  svg.append(textEl((x0 + x1) / 2, H - 4, chart.y_label, p, { fill: p.muted, "text-anchor": "middle" }));
  return svg;
}

function lineChart(chart, p, W) {
  const pts = chart.points;
  const H = Math.round(Math.min(340, Math.max(240, W * 0.5)));
  const m = { l: 64, r: 20, t: 12, b: 56 };
  const lo = Math.min(...pts.map((d) => d.value));
  const hi = Math.max(...pts.map((d) => d.value));
  const ticks = niceTicks(Math.min(0, lo), hi);
  const tmin = ticks[0];
  const tmax = ticks[ticks.length - 1];
  const sx = (i) => m.l + (pts.length === 1 ? 0.5 : i / (pts.length - 1)) * (W - m.l - m.r);
  const sy = (v) => H - m.b - ((v - tmin) / (tmax - tmin)) * (H - m.t - m.b);

  const svg = el("svg", { xmlns: SVG_NS, width: W, height: H, viewBox: `0 0 ${W} ${H}` });
  for (const t of ticks) {
    svg.append(el("line", { x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), stroke: p.grid, "stroke-width": 1 }));
    svg.append(textEl(m.l - 8, sy(t) + 4, formatCompact(t), p, { fill: p.muted, "text-anchor": "end", "font-size": 12 }));
  }
  // Label every nth point, always including the last; drop a label that would crowd it.
  const every = Math.max(1, Math.ceil(pts.length / Math.max(3, Math.floor(W / 100))));
  const last = pts.length - 1;
  pts.forEach((d, i) => {
    const show = i === last || (i % every === 0 && (last - i >= every * 0.75 || i === 0));
    if (!show) return;
    const anchor = pts.length > 1 && i === last ? "end" : "middle";
    svg.append(textEl(sx(i), H - m.b + 18, truncate(d.label, 14), p, { fill: p.muted, "text-anchor": anchor, "font-size": 12 }));
  });
  const path = pts.map((d, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(d.value).toFixed(1)}`).join(" ");
  svg.append(el("path", { d: path, fill: "none", stroke: p.accent, "stroke-width": 2.5, "stroke-linejoin": "round" }));
  if (pts.length <= 60) {
    pts.forEach((d, i) => {
      const dot = el("circle", { cx: sx(i), cy: sy(d.value), r: 3.5, fill: p.accent });
      dot.append(el("title", {}, `${d.label}: ${formatNumber(d.value)}`));
      svg.append(dot);
    });
  }
  svg.append(textEl((m.l + W - m.r) / 2, H - 8, chart.x_label, p, { fill: p.muted, "text-anchor": "middle" }));
  svg.append(textEl(14, (m.t + H - m.b) / 2, chart.y_label, p, {
    fill: p.muted, "text-anchor": "middle", transform: `rotate(-90 14 ${(m.t + H - m.b) / 2})`,
  }));
  return svg;
}

// ---------- formatting ----------

// Match the page language rather than the browser's, so text and numbers read consistently.
const LOCALE = document.documentElement.lang || "en";

function formatNumber(v) {
  return new Intl.NumberFormat(LOCALE, { maximumFractionDigits: Math.abs(v) < 10 ? 2 : Math.abs(v) < 1000 ? 1 : 0 }).format(v);
}

function formatCompact(v) {
  return new Intl.NumberFormat(LOCALE, { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

function formatBytes(n) {
  if (!n) return "no stored data (answered from table details)";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : n} ${units[i]} of data`;
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
      // Keep the key; the service may just be down for a moment.
      state.key = null;
      showSignin(signinErrorText(err));
    }
  }
})();
