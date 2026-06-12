// backend/app/static/js/runs.js — runs history with company/pipeline/status filters.
// The URL hash is the source of truth: Apply rewrites #/runs?… and the router re-renders.
import { api, el, fmtUsd, fmtNum } from "./api.js";
import { errorCard, loadingCard, statusBadge, head, cell, shortDate, fmtDuration } from "./ui.js";

const PIPELINES = ["ai_bulk", "ai_deep", "gmaps", "gsearch", "full",
                   "firmographics", "url_discovery"];
const STATUSES = ["queued", "running", "completed", "completed_with_errors", "failed"];

const selectCls = "rounded-lg border border-gray-300 px-3 py-2 text-sm " +
  "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500";

const runCost = (r) => (r.cost && typeof r.cost === "object") ? r.cost.total_usd : r.cost;

function filterBar(companies, query) {
  const companySel = el("select", { class: selectCls },
    el("option", { value: "" }, "All companies"),
    ...companies.map((c) => el("option", { value: c.id }, c.name ?? c.id)),
  );
  companySel.value = query.company_id ?? "";

  const pipelineSel = el("select", { class: selectCls },
    el("option", { value: "" }, "All pipelines"),
    ...PIPELINES.map((p) => el("option", { value: p }, p)),
  );
  pipelineSel.value = query.pipeline ?? "";

  const statusSel = el("select", { class: selectCls },
    el("option", { value: "" }, "All statuses"),
    ...STATUSES.map((s) => el("option", { value: s }, s)),
  );
  statusSel.value = query.status ?? "";

  const apply = el("button", {
    class: "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500",
    onclick: () => {
      const params = new URLSearchParams();
      if (companySel.value) params.set("company_id", companySel.value);
      if (pipelineSel.value) params.set("pipeline", pipelineSel.value);
      if (statusSel.value) params.set("status", statusSel.value);
      const qs = params.toString();
      window.location.hash = qs ? `#/runs?${qs}` : "#/runs";
    },
  }, "Apply");

  return el("div", { class: "flex flex-wrap items-center gap-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm" },
    companySel, pipelineSel, statusSel, apply);
}

function runsTable(runs, companiesById) {
  const rows = runs.map((r) =>
    el("tr", {
      class: "cursor-pointer hover:bg-indigo-50/40",
      onclick: () => { window.location.hash = `#/runs/${encodeURIComponent(r.run_ref)}`; },
    },
      cell(shortDate(r.created_at), "text-gray-400 whitespace-nowrap"),
      cell(companiesById.get(r.company_id)?.name ?? "—", "font-medium text-gray-900"),
      cell(r.pipeline ?? "—"),
      cell(statusBadge(r.status)),
      cell(fmtNum(r.total_rows), "text-right"),
      cell(fmtNum(r.websites_found), "text-right"),
      cell(fmtUsd(runCost(r)), "text-right"),
      cell(fmtDuration(r.duration_seconds), "text-right"),
    ),
  );

  return el("div", { class: "overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm" },
    el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
      el("thead", { class: "bg-gray-50" },
        el("tr", {},
          head("Created"), head("Company"), head("Pipeline"), head("Status"),
          head("Rows", "text-right"), head("Found", "text-right"),
          head("Cost", "text-right"), head("Duration", "text-right"),
        ),
      ),
      el("tbody", { class: "divide-y divide-gray-100" }, ...rows),
    ),
  );
}

export async function render(root, params) {
  const query = params.query ?? {};
  root.replaceChildren(loadingCard());

  const runsParams = new URLSearchParams();
  if (query.company_id) runsParams.set("company_id", query.company_id);
  if (query.pipeline) runsParams.set("pipeline", query.pipeline);
  const runsPath = runsParams.toString()
    ? `/companies/runs?${runsParams.toString()}` : "/companies/runs";

  let companiesResp, runsResp;
  try {
    [companiesResp, runsResp] = await Promise.all([api("/companies"), api(runsPath)]);
  } catch (e) {
    root.replaceChildren(errorCard(e.message)); // Supabase 503 → setup card
    return;
  }

  const companies = companiesResp.companies ?? [];
  const companiesById = new Map(companies.map((c) => [c.id, c]));
  let runs = runsResp.runs ?? [];
  if (query.status) runs = runs.filter((r) => r.status === query.status); // client-side

  root.replaceChildren(
    filterBar(companies, query),
    el("div", { class: "mt-4" },
      runs.length === 0
        ? el("div", { class: "rounded-xl border border-gray-200 bg-white p-10 text-center shadow-sm" },
            el("p", { class: "text-sm font-medium text-gray-900" }, "No runs found"),
            el("p", { class: "mt-1 text-sm text-gray-500" },
              "Adjust the filters or start a new run."),
          )
        : runsTable(runs, companiesById),
    ),
  );
}
