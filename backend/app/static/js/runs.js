// backend/app/static/js/runs.js — runs history with company/pipeline/status filters.
// The URL hash is the source of truth: Apply rewrites #/runs?… and the router re-renders.
import { api, el, fmtUsd, fmtNum } from "./api.js";
import {
  cell,
  emptyState,
  errorCard,
  fmtDuration,
  head,
  loadingCard,
  pageIntro,
  PIPELINE_LABELS,
  PIPELINES,
  runHref,
  engineOf,
  shortDate,
  statusBadge,
} from "./ui.js";

const STATUSES = ["queued", "running", "completed", "completed_with_errors", "failed"];

const selectCls = "control px-3 py-2 text-sm";

const runCost = (r) => (r.cost && typeof r.cost === "object") ? r.cost.total_usd : r.cost;

function newRunAction() {
  return el("a", { href: "#/new-run", class: "btn-primary" }, "New run");
}

function filterField(label, id, control) {
  control.setAttribute("id", id);
  return el("div", { class: "filter-field" },
    el("label", { for: id, class: "filter-label" }, label),
    control,
  );
}

function filterBar(companies, query) {
  const companySel = el("select", { class: selectCls },
    el("option", { value: "" }, "All companies"),
    ...companies.map((c) => el("option", { value: c.id }, c.name ?? c.id)),
  );
  companySel.value = query.company_id ?? "";

  const pipelineSel = el("select", { class: selectCls },
    el("option", { value: "" }, "All pipelines"),
    ...PIPELINES.map((p) => el("option", { value: p }, PIPELINE_LABELS[p] ?? p)),
  );
  pipelineSel.value = query.pipeline ?? "";

  const statusSel = el("select", { class: selectCls },
    el("option", { value: "" }, "All statuses"),
    ...STATUSES.map((s) => el("option", { value: s }, s)),
  );
  statusSel.value = query.status ?? "";

  const apply = el("button", {
    type: "button",
    class: "btn-primary",
    onclick: () => {
      const params = new URLSearchParams();
      if (companySel.value) params.set("company_id", companySel.value);
      if (pipelineSel.value) params.set("pipeline", pipelineSel.value);
      if (statusSel.value) params.set("status", statusSel.value);
      const qs = params.toString();
      window.location.hash = qs ? `#/runs?${qs}` : "#/runs";
    },
  }, "Apply");

  return el("div", { class: "filter-toolbar" },
    filterField("Company", "runs-company-filter", companySel),
    filterField("Pipeline", "runs-pipeline-filter", pipelineSel),
    filterField("Status", "runs-status-filter", statusSel),
    el("div", { class: "filter-actions" },
      apply,
      el("a", { class: "btn-ghost", href: "#/runs" }, "Clear"),
    ),
  );
}

function runsTable(runs, companiesById) {
  const rows = runs.map((r) => {
    const companyName = companiesById.get(r.company_id)?.name ?? "-";
    return el("tr", { class: "data-row" },
      cell(shortDate(r.created_at), "text-slate-400 whitespace-nowrap"),
      cell(el("a", {
        class: "table-link",
        href: runHref(r.run_ref, engineOf(r.pipeline)),
        "aria-label": `View ${companyName} run ${r.run_ref ?? ""}`,
      }, companyName), "font-semibold text-slate-50"),
      cell(PIPELINE_LABELS[r.pipeline] ?? r.pipeline ?? "-"),
      cell(statusBadge(r.status)),
      cell(fmtNum(r.total_rows), "text-right"),
      cell(fmtNum(r.websites_found ?? r.success_count), "text-right"),
      cell(fmtUsd(runCost(r)), "text-right"),
      cell(fmtDuration(r.duration_seconds), "text-right"),
    );
  });

  return el("div", { class: "table-shell" },
    el("div", { class: "table-scroll" },
      el("table", { class: "data-table" },
        el("thead", {},
          el("tr", {},
            head("Created"), head("Company"), head("Pipeline"), head("Status"),
            head("Rows", "text-right"), head("Found", "text-right"),
            head("Cost", "text-right"), head("Duration", "text-right"),
          ),
        ),
        el("tbody", {}, ...rows),
      ),
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
    el("div", { class: "core-view" },
      pageIntro(
        "Execution ledger",
        "Runs",
        "Filter and inspect pipeline activity across every company.",
        newRunAction(),
      ),
      filterBar(companies, query),
      runs.length === 0
        ? emptyState(
            "No runs found",
            "Adjust the filters or start a new run.",
            newRunAction(),
          )
        : runsTable(runs, companiesById),
    ),
  );
}
