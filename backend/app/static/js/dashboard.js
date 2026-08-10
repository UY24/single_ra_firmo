// backend/app/static/js/dashboard.js — company summaries + recent runs ledger.
import { api, el, fmtUsd, fmtNum } from "./api.js";
import {
  cell,
  emptyState,
  errorCard,
  head,
  loadingCard,
  metricItem,
  pageIntro,
  sectionHeading,
  shortDate,
  statusBadge,
  runHref,
  engineOf,
} from "./ui.js";

const runCost = (r) => (r.cost && typeof r.cost === "object") ? r.cost.total_usd : r.cost;

function numericOutcome(value) {
  const number = Number(value ?? 0);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function newRunAction() {
  return el("a", { href: "#/new-run", class: "btn-primary" }, "New run");
}

function companyCard(c) {
  const found = numericOutcome(c.websites_found);
  const notFound = numericOutcome(c.websites_not_found);
  const totalOutcomes = found + notFound;
  const foundRate = totalOutcomes > 0
    ? `${((found / totalOutcomes) * 100).toFixed(1)}%`
    : "—";

  return el("a", {
    class: "company-summary",
    href: `#/runs?company_id=${encodeURIComponent(c.id)}`,
  },
    el("span", { class: "company-name" }, c.name ?? "—"),
    el("span", { class: "company-outcome" },
      el("span", { class: "company-outcome-value" }, `${fmtNum(found)} found`),
      el("span", { class: "company-outcome-rate" }, `${foundRate} of resolved rows`),
    ),
    el("dl", { class: "metric-group company-metrics" },
      metricItem("Runs", fmtNum(c.runs)),
      metricItem("Not found", fmtNum(notFound), "muted"),
      metricItem("Scrape.do searches", fmtNum(c.total_searches), "info"),
      metricItem("Total rows", fmtNum(c.total_rows)),
      metricItem("Input tokens", fmtNum(c.total_input_tokens), "muted"),
      metricItem("Output tokens", fmtNum(c.total_output_tokens), "muted"),
      metricItem("LLM cost", fmtUsd(c.total_cost_usd), "warning"),
    ),
  );
}

function recentRunsTable(runs, companiesById) {
  const rows = runs.map((r) => {
    const companyName = companiesById.get(r.company_id)?.name ?? "-";
    return el("tr", { class: "data-row" },
      cell(el("a", {
        class: "table-link",
        href: runHref(r.run_ref, engineOf(r.pipeline)),
        "aria-label": `View ${companyName} run ${r.run_ref ?? ""}`,
      }, companyName), "font-semibold text-slate-50"),
      cell(r.pipeline ?? "-"),
      cell(statusBadge(r.status)),
      cell(fmtNum(r.total_rows), "text-right"),
      cell(fmtNum(r.websites_found), "text-right"),
      cell(fmtUsd(runCost(r)), "text-right"),
      cell(shortDate(r.created_at), "text-slate-400"),
    );
  });

  return el("div", { class: "table-shell" },
    el("div", { class: "table-scroll max-h-96 overflow-y-auto" },
      el("table", { class: "data-table" },
        el("thead", { class: "sticky-head" },
          el("tr", {},
            head("Company"), head("Pipeline"), head("Status"),
            head("Rows", "text-right"), head("Found", "text-right"),
            head("Cost", "text-right"), head("Created"),
          ),
        ),
        el("tbody", {}, ...rows),
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
  const intro = pageIntro(
    "Overview",
    "Company outcomes",
    "Track discovery coverage, operating volume, and recent pipeline activity.",
    newRunAction(),
  );

  if (companies.length === 0) {
    root.replaceChildren(
      el("div", { class: "core-view" },
        intro,
        emptyState(
          "No companies yet",
          "Create your first company before starting a pipeline run.",
          el("a", { href: "#/companies", class: "btn-primary" }, "Go to Companies"),
        ),
      ),
    );
    return;
  }

  root.replaceChildren(
    el("div", { class: "core-view" },
      intro,
      el("div", { class: "company-summary-grid" }, ...companies.map(companyCard)),
      el("section", { class: "core-section" },
        sectionHeading("Recent runs", "The latest pipeline executions across every company."),
        runs.length === 0
          ? emptyState(
              "No runs yet",
              "Start a run to populate the execution ledger.",
              newRunAction(),
            )
          : recentRunsTable(runs, companiesById),
      ),
    ),
  );
}
