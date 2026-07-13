// backend/app/static/js/main.js — hash router for the app shell.
import { render as renderDashboard } from "./dashboard.js";
import { render as renderCompanies } from "./companies.js";
import { render as renderNewRun } from "./new_run.js";
import { render as renderRuns } from "./runs.js";
import { render as renderRunDetail } from "./run_detail.js";
import { render as renderOperations } from "./operations.js";
import { render as renderTools } from "./tools.js";

const TITLES = {
  dashboard: "Dashboard",
  companies: "Companies",
  "new-run": "New Run",
  runs: "Runs",
  "run-detail": "Run Detail",
  operations: "Operations",
  tools: "Tools",
};

const VIEWS = {
  dashboard: renderDashboard,
  companies: renderCompanies,
  "new-run": renderNewRun,
  runs: renderRuns,
  "run-detail": renderRunDetail,
  operations: renderOperations,
  tools: renderTools,
};

const NAV_ICON_PATHS = {
  dashboard: ["M4 4h6v6H4z", "M14 4h6v6h-6z", "M4 14h6v6H4z", "M14 14h6v6h-6z"],
  companies: ["M4 20V8l8-4v16", "M12 10h8v10", "M8 9v1", "M8 13v1", "M8 17v1", "M16 13v1", "M16 17v1"],
  "new-run": ["M12 3a9 9 0 1 0 9 9", "M12 7v10", "M7 12h10"],
  runs: ["M4 12a8 8 0 1 0 2.34-5.66L4 8", "M4 4v4h4", "M12 8v5l3 2"],
  operations: ["M4 6h10", "M18 6h2", "M4 12h2", "M10 12h10", "M4 18h7", "M15 18h5", "M14 4v4", "M6 10v4", "M11 16v4"],
  tools: ["M14.7 6.3a4 4 0 0 0-5-5L12 4 9 7 6.3 4.3a4 4 0 0 0 5 5L19 17a1.4 1.4 0 0 1-2 2l-7.7-7.7"],
};

function mountNavIcons() {
  const namespace = "http://www.w3.org/2000/svg";
  document.querySelectorAll("#sidebar-nav .nav-link[data-nav]").forEach((link) => {
    if (link.querySelector(".nav-icon")) return;
    const paths = NAV_ICON_PATHS[link.dataset.nav];
    if (!paths) return;

    const icon = document.createElementNS(namespace, "svg");
    icon.setAttribute("class", "nav-icon");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor");
    icon.setAttribute("stroke-width", "1.75");
    icon.setAttribute("stroke-linecap", "round");
    icon.setAttribute("stroke-linejoin", "round");

    paths.forEach((pathData) => {
      const path = document.createElementNS(namespace, "path");
      path.setAttribute("d", pathData);
      icon.append(path);
    });
    link.prepend(icon);
  });
}

function setDrawerOpen(open, { returnFocus = true } = {}) {
  const sidebar = document.getElementById("app-sidebar");
  const toggle = document.getElementById("sidebar-toggle");
  const backdrop = document.getElementById("sidebar-backdrop");
  const main = document.querySelector("main");
  if (!sidebar || !toggle || !backdrop || !main) return;

  const wasOpen = sidebar.classList.contains("is-open");
  sidebar.classList.toggle("is-open", open);
  backdrop.classList.toggle("hidden", !open);
  toggle.setAttribute("aria-expanded", String(open));
  toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  document.body.classList.toggle("nav-open", open);
  main.toggleAttribute("inert", open);

  if (open) {
    main.setAttribute("aria-hidden", "true");
    const firstNavLink = sidebar.querySelector(".nav-link");
    if (firstNavLink) firstNavLink.focus({ preventScroll: true });
  } else {
    main.removeAttribute("aria-hidden");
  }

  if (wasOpen && !open && returnFocus) toggle.focus({ preventScroll: true });
}

function bindShellInteractions() {
  const toggle = document.getElementById("sidebar-toggle");
  const backdrop = document.getElementById("sidebar-backdrop");
  const sidebarNav = document.getElementById("sidebar-nav");
  if (!toggle || !backdrop || !sidebarNav) return;

  mountNavIcons();
  toggle.addEventListener("click", () => {
    setDrawerOpen(toggle.getAttribute("aria-expanded") !== "true");
  });
  backdrop.addEventListener("click", () => setDrawerOpen(false));
  sidebarNav.addEventListener("click", (event) => {
    if (event.target.closest(".nav-link")) setDrawerOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      setDrawerOpen(false);
    }
  });

  const desktopMedia = window.matchMedia("(min-width: 768px)");
  desktopMedia.addEventListener("change", (event) => {
    if (event.matches) setDrawerOpen(false, { returnFocus: false });
  });
}

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

// A view's render() may return a cleanup function (directly or via a promise,
// since renders can be async). The router invokes it before rendering the next
// view so background work (e.g. status pollers) doesn't leak across views.
let activeCleanup = null;
let routeToken = 0;

function runCleanup(fn) {
  try { fn(); } catch (e) { console.error("view cleanup failed:", e); }
}

function route() {
  const token = ++routeToken;
  if (typeof activeCleanup === "function") runCleanup(activeCleanup);
  activeCleanup = null;

  const { view, params } = parseHash();

  document.querySelectorAll("main section[data-view]").forEach((s) => {
    s.classList.toggle("hidden", s.dataset.view !== view);
  });
  document.querySelectorAll("#sidebar-nav .nav-link").forEach((a) => {
    const isActive = a.dataset.nav === view;
    a.classList.toggle("active", isActive);
    if (isActive) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  const title = document.getElementById("view-title");
  if (title) title.textContent = TITLES[view] ?? view;

  const root = document.querySelector(`main section[data-view="${view}"]`);
  if (!root) return;
  Promise.resolve(VIEWS[view](root, params)).then((cleanup) => {
    if (typeof cleanup !== "function") return;
    if (token === routeToken) activeCleanup = cleanup;
    else runCleanup(cleanup); // route changed while rendering: tear down immediately
  }).catch((e) => console.error(e));
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", () => {
  bindShellInteractions();
  route();
});
