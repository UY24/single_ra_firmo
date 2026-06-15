// backend/app/static/js/dashboard.js — company cards + recent runs feed.
import { api, el, fmtUsd, fmtNum } from "./api.js";
import { errorCard, loadingCard, statusBadge, head, cell, shortDate } from "./ui.js";

const runCost = (r) => (r.cost && typeof r.cost === "object") ? r.cost.total_usd : r.cost;

function statPair(label, value) {
  return el("div", {},
    el("dt", { class: "metric-label" }, label),
    el("dd", { class: "mt-0.5 text-sm font-semibold text-slate-50" }, value),
  );
}

function companyCard(c) {
  return el("button", {
    class: "panel text-left transition hover:border-cyan-400/60",
    onclick: () => { window.location.hash = `#/runs?company_id=${encodeURIComponent(c.id)}`; },
  },
    el("p", { class: "truncate text-sm font-semibold text-slate-50" }, c.name ?? "-"),
    el("dl", { class: "mt-4 grid grid-cols-3 gap-x-4 gap-y-3" },
      statPair("Runs", fmtNum(c.runs)),
      statPair("Found", fmtNum(c.websites_found)),
      statPair("Not found", fmtNum(c.websites_not_found)),
      statPair("Success / failed", `${fmtNum(c.success_count)} / ${fmtNum(c.failed_count)}`),
      statPair("Searches", fmtNum(c.total_searches)),
      statPair("Tokens", fmtNum(c.total_tokens)),
      statPair("LLM cost", fmtUsd(c.total_cost_usd)),
    ),
  );
}

function recentRunsTable(runs, companiesById) {
  const rows = runs.map((r) =>
    el("tr", {
      class: "cursor-pointer hover:bg-indigo-50/40",
      onclick: () => { window.location.hash = `#/runs/${encodeURIComponent(r.run_ref)}`; },
    },
      cell(companiesById.get(r.company_id)?.name ?? "-", "font-semibold text-slate-50"),
      cell(r.pipeline ?? "-"),
      cell(statusBadge(r.status)),
      cell(fmtNum(r.total_rows), "text-right"),
      cell(fmtNum(r.websites_found), "text-right"),
      cell(fmtUsd(runCost(r)), "text-right"),
      cell(shortDate(r.created_at), "text-slate-400"),
    ),
  );

  return el("div", { class: "table-shell" },
    el("div", { class: "table-scroll max-h-96 overflow-y-auto" },
      el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
        el("thead", { class: "sticky-head" },
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
      el("div", { class: "panel p-10 text-center" },
        el("p", { class: "section-title" }, "No companies yet"),
        el("p", { class: "mt-1 section-copy" }, "Create your first company to start running pipelines."),
        el("a", {
          href: "#/companies",
          class: "btn-primary mt-4",
        }, "Go to Companies"),
      ),
    );
    return;
  }

  root.replaceChildren(
    el("div", { class: "grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3" },
      ...companies.map(companyCard)),
    el("h2", { class: "mt-8 mb-3 section-title" }, "Recent runs"),
    runs.length === 0
      ? el("div", { class: "panel panel-tight" },
          el("p", { class: "section-copy" }, "No runs yet."))
      : recentRunsTable(runs, companiesById),
  );
}
