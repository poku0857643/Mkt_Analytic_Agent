// Public /plans page: load the catalog and render it (no sign-in needed).
"use strict";

(async function () {
  const root = document.getElementById("plans-root");
  try {
    const res = await fetch("/plans/catalog", { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    renderPlanCatalog(root, await res.json(), { signInHref: "/" });
  } catch {
    root.replaceChildren();
    const p = document.createElement("p");
    p.className = "alert alert-error";
    p.textContent = "Plans couldn't be loaded. Refresh the page to try again.";
    root.append(p);
  }
})();
