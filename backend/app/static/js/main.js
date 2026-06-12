// backend/app/static/js/main.js — hash router for the app shell.
import { el } from "./api.js";
import { render as renderDashboard } from "./dashboard.js";
import { render as renderCompanies } from "./companies.js";

const TITLES = {
  dashboard: "Dashboard",
  companies: "Companies",
  "new-run": "New Run",
  runs: "Runs",
  "run-detail": "Run Detail",
  operations: "Operations",
};

function placeholder(root, label) {
  root.replaceChildren(
    el("div", { class: "rounded-xl border border-gray-200 bg-white p-10 shadow-sm text-center" },
      el("p", { class: "text-2xl text-gray-300" }, "—"),
      el("p", { class: "mt-2 text-sm text-gray-500" }, `${label} — coming in a later task.`),
    ),
  );
}

const VIEWS = {
  dashboard: renderDashboard,
  companies: renderCompanies,
  "new-run": (root) => placeholder(root, "New Run"),
  runs: (root) => placeholder(root, "Runs"),
  "run-detail": (root, params) =>
    placeholder(root, params.runRef ? `Run ${params.runRef}` : "Run Detail"),
  operations: (root) => placeholder(root, "Operations"),
};

function parseHash() {
  const hash = window.location.hash.replace(/^#\/?/, ""); // e.g. "runs?company_id=x" | "runs/abc"
  const [path, queryString] = hash.split("?");
  const segments = path.split("/").filter(Boolean);
  const query = Object.fromEntries(new URLSearchParams(queryString ?? ""));

  if (segments.length === 0) return { view: "dashboard", params: { query } };
  if (segments[0] === "runs" && segments.length > 1) {
    return { view: "run-detail", params: { runRef: decodeURIComponent(segments[1]), query } };
  }
  const view = segments[0];
  if (!(view in VIEWS)) return { view: "dashboard", params: { query } };
  return { view, params: { query } };
}

function route() {
  const { view, params } = parseHash();

  document.querySelectorAll("main section[data-view]").forEach((s) => {
    s.classList.toggle("hidden", s.dataset.view !== view);
  });
  document.querySelectorAll("#sidebar-nav .nav-link").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === view);
  });
  const title = document.getElementById("view-title");
  if (title) title.textContent = TITLES[view] ?? view;

  const root = document.querySelector(`main section[data-view="${view}"]`);
  if (root) VIEWS[view](root, params);
}

window.addEventListener("hashchange", route);
window.addEventListener("load", route);
