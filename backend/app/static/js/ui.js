// backend/app/static/js/ui.js — shared UI helpers (cards, badges, table cells, dates).
import { el } from "./api.js";

// Single source of truth for pipeline order + display labels (runs list + run detail).
// Keep in sync with the backend pipeline keys; add new pipelines here, not per-view.
export const PIPELINES = ["ai_bulk", "ai_deep", "gmaps", "gsearch", "relationship",
                          "firmographics"];
export const PIPELINE_LABELS = {
  ai_bulk: "Google AI (Bulk)",
  ai_deep: "Google AI (Deep)",
  gmaps: "Google Maps",
  gsearch: "Google Search",
  relationship: "Financial Relationship",
  firmographics: "Firmographics",
};

export function pipelineLabel(pipeline) {
  const key = String(pipeline ?? "");
  return PIPELINE_LABELS[key] ?? (key || "Run");
}

// AI Mode runs and SerpWow-engine runs are served by DIFFERENT status endpoints, and a
// run id alone doesn't say which. Views that already know the pipeline pass the answer
// along as ?engine=, so run-detail can skip a probe request on every page load.
export const AI_MODE_PIPELINES = new Set(["ai_bulk", "ai_deep"]);

export function engineOf(pipeline) {
  if (pipeline == null || pipeline === "") return null;
  return AI_MODE_PIPELINES.has(String(pipeline)) ? "ai" : "serpwow";
}

/** Link to a run. `engine` is an optional hint ("ai" | "serpwow"); omit when unknown. */
export function runHref(runRef, engine = null) {
  const base = `#/runs/${encodeURIComponent(runRef)}`;
  return engine ? `${base}?engine=${engine}` : base;
}

export function copyText(text) {
  if (!text || text === "-" || text === "—") return;
  navigator.clipboard.writeText(text).catch(() => {});
}

export function copyCell(text) {
  if (!text || text === "-" || text === "—") {
    return el("span", { class: "font-mono text-xs text-slate-500" }, "—");
  }
  const button = el("button", {
    type: "button",
    class: "copy-control",
    "aria-label": `Copy storage path: ${text}`,
    title: `${text}\n(click to copy)`,
  }, text);
  button.addEventListener("click", (ev) => {
    ev.stopPropagation();
    copyText(text);
    button.classList.add("is-copied");
    setTimeout(() => {
      button.classList.remove("is-copied");
    }, 1000);
  });
  return button;
}

export function statusBadge(status) {
  return el("span", { class: "status-badge", "data-status": status ?? "" }, status ?? "—");
}

export function pageIntro(kicker, title, copy, action) {
  const text = el("div", {},
    el("p", { class: "view-kicker" }, kicker),
    el("h2", { class: "page-heading" }, title),
    ...(copy ? [el("p", { class: "page-copy" }, copy)] : []),
  );
  return el("div", { class: "page-intro" }, text, ...(action ? [action] : []));
}

export function sectionHeading(title, copy, action) {
  return el("div", { class: "section-heading" },
    el("div", {},
      el("h2", { class: "section-title" }, title),
      ...(copy ? [el("p", { class: "section-copy" }, copy)] : []),
    ),
    ...(action ? [action] : []),
  );
}

export function metricItem(label, value, tone = "default", detail = "") {
  const allowedTones = new Set(["default", "good", "muted", "warning", "danger", "info"]);
  const safeTone = allowedTones.has(tone) ? tone : "default";
  return el("div", { class: `metric-item metric-item--${safeTone}` },
    el("dt", { class: "metric-label" }, label),
    el("dd", { class: "metric-value" }, value),
    ...(detail ? [el("dd", { class: "metric-detail" }, detail)] : []),
  );
}

export function emptyState(title, copy, action) {
  return el("div", { class: "empty-state" },
    el("h2", { class: "section-title" }, title),
    el("p", { class: "section-copy" }, copy),
    ...(action ? [action] : []),
  );
}

export function errorCard(message) {
  if (/supabase/i.test(message)) {
    return el("div", { class: "callout callout-amber", role: "alert" },
      el("p", { class: "text-sm font-semibold" }, "Supabase not configured / unreachable"),
      el("p", { class: "mt-1 text-sm" }, message),
      el("p", { class: "mt-3 text-xs opacity-80" },
        "In .env, SUPABASE_URL must be the bare REST URL (https://<project-ref>.supabase.co), not the :5432/postgres connection string. Then restart the server."),
    );
  }
  return el("div", { class: "callout callout-red", role: "alert" },
    el("p", { class: "text-sm font-semibold" }, "Something went wrong"),
    el("p", { class: "mt-1 text-sm" }, message),
  );
}

export const loadingCard = () =>
  el("div", { class: "panel panel-tight" },
    el("p", { class: "section-copy" }, "Loading..."));

export const head = (label, extra = "") =>
  el("th", { class: `px-4 py-3 text-left uppercase ${extra}` }, label);

export const cell = (content, extra = "") =>
  el("td", { class: `px-4 py-3 text-sm ${extra}` }, content);

// fmtDuration(95) → "1m 35s"; fmtDuration(42) → "42s"
export const fmtDuration = (s) => {
  if (s == null) return "—";
  const total = Math.round(Number(s));
  if (Number.isNaN(total)) return "—";
  const m = Math.floor(total / 60), sec = total % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
};

// shortDate(iso)                      → "Jun 11, 02:30 PM" (with time)
// shortDate(iso, { withTime: false }) → "Jun 11, 2026"     (date only)
export const shortDate = (iso, { withTime = true } = {}) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return withTime
    ? d.toLocaleString(undefined, {
        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      })
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
};
