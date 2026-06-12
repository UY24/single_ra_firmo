// backend/app/static/js/dashboard.js — company cards + recent runs feed.
import { api, el, fmtUsd, fmtNum } from "./api.js";
import { errorCard, loadingCard, statusBadge, head, cell, shortDate } from "./ui.js";

const runCost = (r) => (r.cost && typeof r.cost === "object") ? r.cost.total_usd : r.cost;

function statPair(label, value) {
  return el("div", {},
    el("dt", { class: "text-xs text-gray-400" }, label),
    el("dd", { class: "mt-0.5 text-sm font-medium text-gray-900" }, value),
  );
}

function companyCard(c) {
  return el("button", {
    class: "rounded-xl border border-gray-200 bg-white p-5 text-left shadow-sm transition " +
           "hover:border-indigo-300 hover:shadow",
    onclick: () => { window.location.hash = `#/runs?company_id=${encodeURIComponent(c.id)}`; },
  },
    el("p", { class: "truncate text-sm font-semibold text-gray-900" }, c.name ?? "—"),
    el("dl", { class: "mt-4 grid grid-cols-3 gap-x-4 gap-y-3" },
      statPair("Runs", fmtNum(c.runs)),
      statPair("Found", fmtNum(c.websites_found)),
      statPair("Not found", fmtNum(c.websites_not_found)),
      statPair("Success / failed", `${fmtNum(c.success_count)} / ${fmtNum(c.failed_count)}`),
      statPair("Tokens", fmtNum(c.total_tokens)),
      statPair("Cost", fmtUsd(c.total_cost_usd)),
    ),
  );
}

function recentRunsTable(runs, companiesById) {
  const rows = runs.map((r) =>
    el("tr", {
      class: "cursor-pointer hover:bg-indigo-50/40",
      onclick: () => { window.location.hash = `#/runs/${encodeURIComponent(r.run_ref)}`; },
    },
      cell(companiesById.get(r.company_id)?.name ?? "—", "font-medium text-gray-900"),
      cell(r.pipeline ?? "—"),
      cell(statusBadge(r.status)),
      cell(fmtNum(r.total_rows), "text-right"),
      cell(fmtNum(r.websites_found), "text-right"),
      cell(fmtUsd(runCost(r)), "text-right"),
      cell(shortDate(r.created_at), "text-gray-400"),
    ),
  );

  return el("div", { class: "overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm" },
    el("div", { class: "max-h-96 overflow-y-auto" },
      el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
        el("thead", { class: "sticky-head bg-gray-50" },
          el("tr", {},
            head("Company"), head("Pipeline"), head("Status"),
            head("Rows", "text-right"), head("Found", "text-right"),
            head("Cost", "text-right"), head("Created"),
          ),
        ),
        el("tbody", { class: "divide-y divide-gray-100" }, ...rows),
      ),
    ),
  );
}

export async function render(root) {
  root.replaceChildren(loadingCard());

  let stats, runsResp;
  try {
    [stats, runsResp] = await Promise.all([api("/companies/stats"), api("/companies/runs")]);
  } catch (e) {
    root.replaceChildren(errorCard(e.message));
    return;
  }

  const companies = stats.companies ?? [];
  const runs = (runsResp.runs ?? []).slice(0, 20);
  const companiesById = new Map(companies.map((c) => [c.id, c]));

  if (companies.length === 0) {
    root.replaceChildren(
      el("div", { class: "rounded-xl border border-gray-200 bg-white p-10 text-center shadow-sm" },
        el("p", { class: "text-sm font-medium text-gray-900" }, "No companies yet"),
        el("p", { class: "mt-1 text-sm text-gray-500" }, "Create your first company to start running pipelines."),
        el("a", {
          href: "#/companies",
          class: "mt-4 inline-block rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500",
        }, "Go to Companies"),
      ),
    );
    return;
  }

  root.replaceChildren(
    el("div", { class: "grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3" },
      ...companies.map(companyCard)),
    el("h2", { class: "mt-8 mb-3 text-sm font-semibold text-gray-700" }, "Recent runs"),
    runs.length === 0
      ? el("div", { class: "rounded-xl border border-gray-200 bg-white p-6 shadow-sm" },
          el("p", { class: "text-sm text-gray-500" }, "No runs yet."))
      : recentRunsTable(runs, companiesById),
  );
}
