// Plans and pricing, rendered from GET /plans/catalog. Shared by the public
// /plans page and the Plans tab in the app. Built with DOM calls; no HTML parsing.
"use strict";

const PLANS_LOCALE = document.documentElement.lang || "en";

function plansEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function plansMoney(v, digits = 2) {
  return new Intl.NumberFormat(PLANS_LOCALE, { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
}

function plansList(items, className = "check-list") {
  const ul = plansEl("ul", className);
  for (const item of items) ul.append(plansEl("li", "", item));
  return ul;
}

function plansSection(title, intro) {
  const section = plansEl("section", "card pricing-section");
  section.append(plansEl("h2", "section-title", title));
  if (intro) section.append(plansEl("p", "muted pricing-intro", intro));
  return section;
}

/**
 * options.current: {plan, state, canChange} for a signed-in person, or null on the public page.
 * options.onChoose(planId): called when they pick a different plan (signed in only).
 * options.signInHref: where the public page's buttons go.
 */
function renderPlanCatalog(root, catalog, options = {}) {
  root.replaceChildren();
  const current = options.current || null;

  if (!catalog.billing_enabled) {
    const note = plansEl("div", "alert alert-info pricing-notice");
    const body = plansEl("div");
    body.append(
      plansEl("p", "alert-title", "Billing isn't switched on yet"),
      plansEl("p", "alert-text", "Plans and usage are tracked exactly as described below, but nobody is charged until billing is connected."),
    );
    note.append(body);
    root.append(note);
  }

  // Plan cards
  const grid = plansEl("div", "pricing-grid");
  for (const plan of catalog.plans) {
    const isCurrent = current && current.plan === plan.id && current.state !== "none";
    const card = plansEl("article", "card price-card" + (isCurrent ? " is-current" : ""));
    card.setAttribute("aria-label", plan.name);
    const head = plansEl("div", "price-head");
    head.append(plansEl("h2", "price-name", plan.name));
    if (isCurrent) head.append(plansEl("span", "pill pill-success", current.state === "ending" ? "Current · ending" : "Your plan"));
    const priceRow = plansEl("p", "price-amount");
    priceRow.append(plansEl("span", "price-value", plan.price), plansEl("span", "price-unit", " " + plan.price_unit));
    card.append(head, priceRow, plansEl("p", "price-best", plan.best_for));
    card.append(plansEl("p", "price-subhead", "Includes"), plansList(plan.includes));
    card.append(plansEl("p", "price-subhead", "Limits"), plansList(plan.limits, "dot-list"));
    card.append(plansEl("p", "price-subhead", "Cancelling"), plansEl("p", "price-cancel", plan.cancel));

    const action = plansEl("div", "price-action");
    if (current) {
      if (isCurrent) {
        action.append(plansEl("p", "muted small", current.state === "ending" ? "Cancelled. Manage it on the Usage page." : "This is your current plan."));
      } else if (current.canChange && plan.id !== "freemium") {
        const b = plansEl("button", "btn btn-primary btn-block", current.state === "none" ? `Choose ${plan.name}` : `Switch to ${plan.name}`);
        b.type = "button";
        b.addEventListener("click", () => options.onChoose && options.onChoose(plan.id));
        action.append(b);
      } else if (!current.canChange) {
        action.append(plansEl("p", "muted small", "Ask an admin to change your plan."));
      }
    } else if (plan.id !== "freemium") {
      const a = plansEl("a", "btn btn-secondary btn-block", "Sign in to choose");
      a.href = options.signInHref || "/";
      action.append(a);
    }
    card.append(action);
    grid.append(card);
  }
  root.append(grid);

  // What a question costs
  const costs = plansSection(
    "What a question costs",
    "Typical prices, measured on real questions. The exact cost of every question you ask is on your Usage page.",
  );
  const table = plansEl("table", "table pricing-table");
  const head = table.createTHead().insertRow();
  ["Question", "Typical cost", "Pay as you go price", "Subscription"].forEach((h, i) => {
    const th = plansEl("th", i ? "num" : "", h);
    th.scope = "col";
    head.append(th);
  });
  const body = table.createTBody();
  for (const ex of catalog.examples) {
    const row = body.insertRow();
    const th = plansEl("th");
    th.scope = "row";
    th.append(plansEl("span", "cost-label", ex.label), plansEl("span", "cost-desc", ex.description));
    row.append(th);
    row.append(plansEl("td", "num", "≈ " + plansMoney(ex.cost_usd)));
    row.append(plansEl("td", "num", "≈ " + plansMoney(ex.payg_usd)));
    row.append(plansEl("td", "num", "Uses ≈ " + plansMoney(ex.cost_usd) + " of the allowance"));
  }
  const scroll = plansEl("div", "table-scroll");
  scroll.append(table);
  costs.append(scroll);
  root.append(costs);

  // How usage is measured
  const p = catalog.prices;
  const measured = plansSection("How usage is measured", "No estimates or rounding in our favour: each question is priced from what it actually used.");
  measured.append(plansList([
    `AI (${p.model}): ${plansMoney(p.input_per_mtok)} per million tokens read, ${plansMoney(p.output_per_mtok)} per million written, ${plansMoney(p.cache_read_per_mtok)} per million re-read from cache and ${plansMoney(p.cache_write_per_mtok)} per million cached. These are the provider's list prices.`,
    `Data: ${plansMoney(p.bigquery_per_tib)} per TiB of data your question scans. Most questions scan a few megabytes, well under a cent.`,
    p.payg_markup > 1
      ? `Pay as you go adds ${Math.round((p.payg_markup - 1) * 100)}% to cover running the service. Subscriptions use the cost itself.`
      : "Pay as you go is charged at cost.",
    "Questions stopped by a limit before they start are free. A question that fails part-way counts only the data it scanned.",
  ], "dot-list"));
  root.append(measured);

  // Fair use
  const fair = plansSection("Limits on every plan", "These protect the service and your bill.");
  fair.append(plansList(catalog.fair_use, "dot-list"));
  root.append(fair);

  // Changing plans
  const change = plansSection("Changing or cancelling");
  change.append(plansList([
    "Switch between Subscription and Pay as you go at any time from the Usage or Plans page. The new plan starts straight away.",
    "Cancelling a subscription keeps it working until the end of the month, and you can undo it until then.",
    "Stopping Pay as you go takes effect at once.",
    "With no plan, you can still sign in and see your usage; questions resume when you choose a plan.",
  ], "dot-list"));
  root.append(change);
}
