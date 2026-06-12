// backend/app/static/js/ui.js — shared UI helpers (cards, badges, table cells, dates).
import { el } from "./api.js";

export function statusBadge(status) {
  return el("span", { class: "status-badge", "data-status": status ?? "" }, status ?? "—");
}

export function errorCard(message) {
  if (/supabase/i.test(message)) {
    return el("div", { class: "rounded-xl border border-amber-200 bg-amber-50 p-6 shadow-sm" },
      el("p", { class: "text-sm font-semibold text-amber-900" }, "Supabase not configured / unreachable"),
      el("p", { class: "mt-1 text-sm text-amber-800" }, message),
      el("p", { class: "mt-3 text-xs text-amber-700" },
        "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the project .env, then restart the server."),
    );
  }
  return el("div", { class: "rounded-xl border border-red-200 bg-red-50 p-6 shadow-sm" },
    el("p", { class: "text-sm font-semibold text-red-900" }, "Something went wrong"),
    el("p", { class: "mt-1 text-sm text-red-800" }, message),
  );
}

export const loadingCard = () =>
  el("div", { class: "rounded-xl border border-gray-200 bg-white p-6 shadow-sm" },
    el("p", { class: "text-sm text-gray-400" }, "Loading…"));

export const head = (label, extra = "") =>
  el("th", { class: `px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wide text-gray-400 ${extra}` }, label);

export const cell = (content, extra = "") =>
  el("td", { class: `px-4 py-2.5 text-sm text-gray-700 ${extra}` }, content);

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
