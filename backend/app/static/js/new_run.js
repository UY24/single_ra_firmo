// backend/app/static/js/new_run.js - full-width "start a new run" workflow.
import { api, el, fmtNum } from "./api.js";
import { errorCard, head, cell, runHref } from "./ui.js";

const _AI_COLS = [
  { name: "company_name", req: true,  hint: "company, name, entity_name, entity, organization" },
  { name: "country",      req: true,  hint: "country_name, nation  (2-letter code or full name)" },
  { name: "company_local_name", req: false, hint: "local_name, company_name_local, name_local" },
  { name: "firm_id",      req: false, hint: "firmid, id" },
  { name: "industry",     req: false, hint: "input_industry" },
  { name: "full_address", req: false, hint: "address, fulladdress, input_full_address" },
];
const _SW_COLS = [
  { name: "company_name", req: true,  hint: "company, name, entity_name, entity, organization" },
  { name: "country",      req: true,  hint: "country_name, nation  (2-letter code or full name)" },
  { name: "firm_id",      req: false, hint: "firmid, id" },
  { name: "industry",     req: false, hint: "input_industry" },
  { name: "full_address", req: false, hint: "address, fulladdress, input_full_address" },
];
const _FIRMO_COLS = [
  { name: "website_url",      req: true,  hint: "official_website, website, url, domain" },
  { name: "company_name",     req: false, hint: "company, name, entity_name, entity, organization" },
  { name: "country",          req: false, hint: "country_name, nation" },
  { name: "firm_id",          req: false, hint: "firmid, id" },
  { name: "industry",         req: false, hint: "input_industry" },
  { name: "full_address",     req: false, hint: "address, fulladdress, input_full_address" },
];
const _REL_COLS = [
  { name: "Company_Name_Y", req: true,  hint: "the company to find (OCR-derived text is accepted)" },
  { name: "Company_Name_X", req: true,  hint: "the investor firm — the relationship is verified against it" },
  { name: "Input_URL",      req: true,  hint: "Company X's full official page URL; also prevents returning X's own site" },
];

const PIPELINES = [
  { key: "ai_bulk", label: "AI Mode 1 - Bulk", endpoint: "/uploads/ai-mode", ai: true,
    desc: "Large batches, broad search, high throughput for residue lists.", csvCols: _AI_COLS },
  { key: "ai_deep", label: "AI Mode 2 - Deep Search", endpoint: "/uploads/ai-mode", ai: true,
    desc: "Small batches, deeper investigation, better for hard targets.", csvCols: _AI_COLS },
  { key: "gmaps", label: "Google Maps", endpoint: "/uploads/gmaps",
    desc: "Fast Scrape.do Maps discovery for local business signals.", csvCols: _SW_COLS },
  { key: "gsearch", label: "Google Search", endpoint: "/uploads/gsearch",
    desc: "Search-phase pipeline across Google result strategies.", csvCols: _SW_COLS },
  { key: "relationship", label: "Financial Relationship", endpoint: "/uploads/relationship",
    desc: "Verifies a financial relationship and returns Company Y's website only when confirmed.", csvCols: _REL_COLS },
  { key: "firmographics", label: "Firmographics", endpoint: "/uploads/firmographics",
    desc: "Enrichment for rows that already have a website.", csvCols: _FIRMO_COLS },
];

const PHASES = [
  { value: "all",      label: "All Phases Combined" },
  { value: "phase1",   label: "Phase 1: Initial Hook & Punctuation" },
  { value: "phase2",   label: "Phase 2: AI NL Prompts" },
  { value: "phase3",   label: "Phase 3: Address Pivot" },
  { value: "phase4",   label: "Phase 4: Document Hunting" },
  { value: "phase5",   label: "Phase 5: Dynamic Expansion" },
  { value: "fallback", label: "Fallback Searches Only" },
];

const SAMPLE_COLS = ["company_name", "country", "sno", "company_local_name",
                     "address", "firm_id", "industry"];

const inputCls = "control px-3 py-2 text-sm";
const buttonCls = "btn-primary disabled:opacity-50 disabled:cursor-not-allowed";
const secondaryButtonCls = "btn-secondary disabled:opacity-50 disabled:cursor-not-allowed";

function redCallout(text) {
  return el("div", { class: "callout callout-red", role: "alert" },
    el("p", { class: "text-sm whitespace-pre-wrap" }, text));
}

function amberCallout(lines) {
  return el("div", { class: "callout callout-amber" },
    ...lines.map((w) => el("p", { class: "text-sm" }, w)));
}

function stepCard(n, title, body) {
  const card = el("div", { class: "panel workflow-step" },
    el("div", { class: "step-rail mb-4 flex items-center gap-3" },
      el("span", { class: "step-number" }, String(n)),
      el("h2", { class: "section-title" }, title),
    ),
    body,
  );
  card.setEnabled = (enabled) => {
    card.setAttribute("aria-disabled", String(!enabled));
    card.inert = !enabled;
  };
  card.setCurrent = (current) => {
    if (current) card.setAttribute("aria-current", "step");
    else card.removeAttribute("aria-current");
  };
  return card;
}

function csvSchemaPanel(pipeline) {
  if (!pipeline?.csvCols) return el("span");
  const req = pipeline.csvCols.filter((c) => c.req);
  const opt = pipeline.csvCols.filter((c) => !c.req);
  const chip = (col) => el("span", {
    class: `inline-flex items-center gap-1 rounded px-2 py-0.5 font-mono text-xs ring-1 cursor-default ${
      col.req
        ? "bg-amber-500/15 text-amber-300 ring-amber-500/30"
        : "bg-slate-700/50 text-slate-400 ring-slate-600/30"
    }`,
    title: `Accepted aliases: ${col.hint}`,
  },
    col.name,
    col.req
      ? el("span", { class: "ml-0.5 text-amber-400 font-bold leading-none" }, "*")
      : el("span", { class: "ml-0.5 text-slate-400 text-[10px] leading-none" }, "opt"),
  );
  return el("div", { class: "schema-panel mb-4" },
    el("p", { class: "mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400" },
      `Expected CSV columns — ${pipeline.label}`),
    el("div", { class: "flex flex-col gap-2" },
      el("div", { class: "flex flex-wrap items-center gap-2" },
        el("span", { class: "w-16 shrink-0 text-xs text-slate-400" }, "Required"),
        ...req.map(chip),
      ),
      opt.length ? el("div", { class: "flex flex-wrap items-center gap-2" },
        el("span", { class: "w-16 shrink-0 text-xs text-slate-400" }, "Optional"),
        ...opt.map(chip),
      ) : el("span"),
    ),
    el("p", { class: "mt-2 text-xs text-slate-400" },
      "* required · hover any column to see accepted header aliases"),
  );
}

function tableShell(table) {
  return el("div", { class: "table-shell" },
    el("div", { class: "table-scroll" }, table));
}

function previewTables(preview) {
  const parts = [
    el("p", { class: "section-copy" },
      el("span", { class: "font-semibold text-slate-50" }, fmtNum(preview.total_rows)),
      " rows detected."),
  ];
  // Relationship preview: surface the search plan up front.
  if (preview.relationship) {
    parts.push(el("p", { class: "section-copy" },
      "Each row is verified with one Google AI Mode search (relationship + website)."));
  }

  if ((preview.warnings ?? []).length) parts.push(amberCallout(preview.warnings));
  if (preview.positional) {
    parts.push(amberCallout([
      "No recognized headers; columns were read positionally (col 1 = company, col 2 = country).",
    ]));
  }

  const mapping = preview.columns_detected ?? {};
  if (Object.keys(mapping).length) {
    parts.push(tableShell(
      el("table", { class: "data-table preview-table" },
        el("thead", {},
          el("tr", {}, head("Field"), head("CSV header"))),
        el("tbody", {},
          ...Object.entries(mapping).map(([field, header]) =>
            el("tr", { class: "data-row" },
              cell(field, "font-semibold text-slate-50"), cell(header ?? "-"))),
        ),
      ),
    ));
  }

  const sample = preview.sample_rows ?? [];
  if (sample.length) {
    const sampleCols = preview.sample_columns ?? SAMPLE_COLS;
    parts.push(tableShell(
      el("table", { class: "data-table preview-table" },
        el("thead", {},
          el("tr", {}, ...sampleCols.map((c) => head(c)))),
        el("tbody", {},
          ...sample.map((row) =>
            el("tr", { class: "data-row" }, ...sampleCols.map((c) =>
              cell(row[c] == null || row[c] === "" ? "-" : String(row[c]))))),
        ),
      ),
    ));
  }
  return el("div", { class: "space-y-3" }, ...parts);
}

function summaryItem(label, valueNode) {
  return el("div", { class: "launch-row" },
    el("dt", { class: "view-kicker" }, label),
    valueNode,
  );
}

export function previewCanLaunch(preview, pipeline) {
  if (!preview || !pipeline) return false;
  return Number(preview.total_rows ?? 0) > 0;
}

export async function render(root) {
  const state = { companyId: "", companyName: "", pipeline: null, phase: "all", file: null, preview: null };
  let previewGeneration = 0;

  const companySelect = el("select", { id: "new-run-company", class: `${inputCls} w-full` });
  const companyArea = el("div", {}, el("p", { class: "section-copy" }, "Loading companies..."));

  async function loadCompanies(selectId) {
    let companies = [];
    try {
      companies = (await api("/companies")).companies ?? [];
    } catch (e) {
      companyArea.replaceChildren(errorCard(e.message));
      return;
    }
    companySelect.replaceChildren(
      el("option", { value: "" }, "Select a company..."),
      ...companies.map((c) => el("option", { value: c.id }, c.name ?? c.id)),
    );
    if (selectId) companySelect.value = selectId;
    companyArea.replaceChildren(companySelect);
    onCompanyChange();
  }

  function onCompanyChange() {
    state.companyId = companySelect.value;
    state.companyName = state.companyId
      ? companySelect.selectedOptions[0]?.textContent ?? ""
      : "";
    refresh();
  }
  companySelect.addEventListener("change", onCompanyChange);

  const newCompanyMsg = el("p", {
    class: "new-company-message mt-2 text-sm", "aria-live": "polite",
  });
  const newCompanyInput = el("input", {
    id: "new-company-name", type: "text", placeholder: "New company name", class: `${inputCls} w-full`,
  });
  const newCompanyBtn = el("button", { type: "submit", class: buttonCls }, "Create");
  const newCompanyForm = el("form", {
    class: "mt-3",
    onsubmit: async (ev) => {
      ev.preventDefault();
      const name = newCompanyInput.value.trim();
      if (!name) return;
      newCompanyBtn.disabled = true;
      try {
        const company = await api("/companies", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        newCompanyInput.value = "";
        newCompanyMsg.textContent = `Created "${company.name ?? name}".`;
        newCompanyMsg.className = "new-company-message mt-2 text-sm text-emerald-600";
        await loadCompanies(company.id);
      } catch (e) {
        newCompanyMsg.textContent = e.message;
        newCompanyMsg.className = "new-company-message mt-2 text-sm text-red-600";
      } finally {
        newCompanyBtn.disabled = false;
      }
    },
  },
    el("label", { class: "filter-label mb-1.5 block", for: "new-company-name" }, "Company name"),
    el("div", { class: "grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_auto]" },
      newCompanyInput, newCompanyBtn),
    newCompanyMsg,
  );

  const step1 = stepCard(1, "Company",
    el("div", {},
      el("label", { class: "filter-label mb-1.5 block", for: "new-run-company" }, "Company"),
      companyArea,
      el("p", { class: "mt-4 text-xs font-semibold uppercase tracking-wide text-slate-400" },
        "Or create a new one"),
      newCompanyForm,
    ),
  );

  const pipelineCards = PIPELINES.map((p) => {
    const radio = el("input", {
      type: "radio", name: "pipeline", value: p.key, class: "mt-1 accent-amber-500",
      onchange: async () => {
        if (!state.companyId) return;
        state.pipeline = p;
        invalidatePreview();
        refresh();
        if (state.file) await previewFile();
      },
    });
    return el("label", { class: "pipeline-option flex items-start gap-3" },
      radio,
      el("span", {},
        el("span", { class: "block text-sm font-semibold text-slate-50" }, p.label),
        el("span", { class: "mt-1 block text-xs leading-5 text-slate-400" }, p.desc),
      ),
    );
  });

  const phaseSelect = el("select", { id: "new-run-phase", class: `${inputCls} w-full` },
    ...PHASES.map((p) => el("option", { value: p.value }, p.label)));
  phaseSelect.addEventListener("change", () => {
    state.phase = phaseSelect.value;
    refresh();
  });

  const phaseRow = el("div", { class: "phase-field divider-top hidden mt-4 pt-4" },
    el("label", { class: "mb-1.5 block view-kicker", for: "new-run-phase" }, "Search phase"),
    phaseSelect,
    el("p", { class: "mt-2 text-xs text-slate-400" },
      "Only applies to Google Search uploads."),
  );

  const step2 = stepCard(2, "Pipeline",
    el("div", {},
      el("div", { class: "grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-3" }, ...pipelineCards),
      phaseRow,
    ));

  const schemaArea = el("div");
  const previewArea = el("div", { class: "mt-4", "aria-live": "polite" });

  function invalidatePreview() {
    previewGeneration += 1;
    state.preview = null;
    previewArea.replaceChildren();
  }

  async function previewFile() {
    const generation = previewGeneration;
    const file = state.file;
    const pipeline = state.pipeline;
    if (!state.companyId || !file || !pipeline) return;

    const pipelineKey = pipeline.key;
    previewArea.replaceChildren(el("p", { class: "section-copy" }, "Previewing..."));
    // Preview with the parser the UPLOAD will use. Relationship (Company_Name_Y /
    // Company_Name_X / Input_URL) and firmographics (website_url, no company/country
    // required) have their own header shapes, so they get their own dry-run endpoints;
    // the shared /uploads/preview runs parse_entities_csv and would report their files
    // as headerless, positional "col 1 = company, col 2 = country".
    const previewEndpoint = pipeline.ai
      ? "/uploads/preview"
      : ({ relationship: "/uploads/relationship/preview",
           firmographics: "/uploads/firmographics/preview" }[pipelineKey]
         ?? "/uploads/preview");
    const requestIsCurrent = () => generation === previewGeneration
      && state.file === file
      && state.pipeline?.key === pipelineKey;

    try {
      const fd = new FormData();
      fd.append("file", file);
      const preview = await api(previewEndpoint, { method: "POST", body: fd });
      if (!requestIsCurrent()) return;
      state.preview = preview;
      previewArea.replaceChildren(previewTables(preview));
    } catch (e) {
      if (!requestIsCurrent()) return;
      previewArea.replaceChildren(redCallout(e.message));
    }
    if (requestIsCurrent()) refresh();
  }

  const fileInput = el("input", {
    id: "new-run-csv", type: "file", accept: ".csv",
    class: "file-input block w-full text-sm text-slate-300",
    onchange: async () => {
      if (!state.companyId || !state.pipeline) return;
      state.file = fileInput.files[0] ?? null;
      invalidatePreview();
      refresh();
      if (state.file) await previewFile();
    },
  });
  const step3 = stepCard(3, "File and preview", el("div", {}, schemaArea,
    el("div", { class: "file-field" },
      el("label", { class: "file-field-label", for: "new-run-csv" }, "CSV input"),
      fileInput,
    ),
    previewArea,
  ));

  const summary = el("p", { class: "section-copy" }, "-");
  const startMsg = el("div", { class: "mt-3 hidden", "aria-live": "polite" });
  const startBtn = el("button", { class: buttonCls, disabled: "" }, "Start run");
  startBtn.addEventListener("click", async () => {
    if (!state.companyId || !state.pipeline || !state.file
        || !previewCanLaunch(state.preview, state.pipeline)) return;
    startBtn.disabled = true;
    startMsg.className = "mt-3";
    startMsg.replaceChildren(el("p", { class: "section-copy" }, "Starting..."));
    const fd = new FormData();
    fd.append("file", state.file);
    fd.append("company_id", state.companyId);
    if (!state.pipeline.ai) fd.append("company_name", state.companyName ?? "");
    if (state.pipeline.ai) fd.append("mode", state.pipeline.key);
    if (state.pipeline.key === "gsearch") fd.append("phase", state.phase || "all");
    try {
      const info = await api(state.pipeline.endpoint, { method: "POST", body: fd });
      startMsg.replaceChildren(
        el("p", { class: "text-sm font-semibold text-emerald-600" },
          `Run started (${info.run_id ?? info.upload_id ?? "ok"}). Redirecting...`));
      const target = state.pipeline.ai
        ? runHref(info.run_id, "ai")
        : "#/runs";
      setTimeout(() => { window.location.hash = target; }, 700);
    } catch (e) {
      startBtn.disabled = false;
      startMsg.replaceChildren(redCallout(e.message));
    }
  });
  const step4 = stepCard(4, "Confirm and start",
    el("div", {}, summary, el("div", { class: "mt-4" }, startBtn), startMsg));

  const summaryCompany = el("dd", { class: "mt-1 text-sm font-semibold text-slate-50" }, "-");
  const summaryPipeline = el("dd", { class: "mt-1 text-sm font-semibold text-slate-50" }, "-");
  const summaryPhase = el("dd", { class: "mt-1 text-sm font-semibold text-slate-50" }, "-");
  const summaryFile = el("dd", { class: "mt-1 truncate text-sm font-semibold text-slate-50" }, "-");
  const summaryRows = el("dd", { class: "mt-1 text-sm font-semibold text-slate-50" }, "-");
  const readiness = el("p", { class: "mt-4 text-sm text-slate-400" },
    "Complete the workflow to enable launch.");
  const summaryPanel = el("aside", { class: "launch-summary panel" },
    el("p", { class: "view-kicker" }, "Run setup"),
    el("h2", { class: "mt-1 text-base font-semibold text-slate-50" }, "Launch summary"),
    el("dl", { class: "launch-list mt-4" },
      summaryItem("Company", summaryCompany),
      summaryItem("Pipeline", summaryPipeline),
      summaryItem("Phase", summaryPhase),
      summaryItem("Input file", summaryFile),
      summaryItem("Rows", summaryRows),
    ),
    readiness,
    el("div", { class: "mt-4" },
      el("button", {
        class: secondaryButtonCls,
        onclick: () => { window.location.hash = "#/companies"; },
      }, "Manage companies"),
    ),
  );

  function refresh() {
    const hasCompany = Boolean(state.companyId);
    const hasPipeline = Boolean(state.pipeline);
    const canLaunch = hasCompany && hasPipeline && Boolean(state.file)
      && previewCanLaunch(state.preview, state.pipeline);
    step1.setEnabled(true);
    step2.setEnabled(hasCompany);
    step3.setEnabled(hasCompany && hasPipeline);
    step4.setEnabled(canLaunch);
    const currentStep = !hasCompany
      ? step1
      : !hasPipeline
        ? step2
        : !canLaunch
          ? step3
          : step4;
    [step1, step2, step3, step4].forEach((step) => step.setCurrent(step === currentStep));
    startBtn.disabled = !canLaunch;
    phaseRow.classList.toggle("hidden", state.pipeline?.key !== "gsearch");
    schemaArea.replaceChildren(csvSchemaPanel(state.pipeline));
    const phaseInfo = state.pipeline?.key === "gsearch" && state.phase && state.phase !== "all"
      ? ` - ${PHASES.find((p) => p.value === state.phase)?.label ?? state.phase}` : "";
    summary.textContent = canLaunch
      ? `${state.companyName} / ${state.pipeline.label}${phaseInfo} / ${fmtNum(state.preview.total_rows)} rows`
      : "Complete the steps above to start.";
    summaryCompany.textContent = state.companyName || "-";
    summaryPipeline.textContent = state.pipeline?.label || "-";
    summaryPhase.textContent = state.pipeline?.key === "gsearch"
      ? PHASES.find((p) => p.value === state.phase)?.label ?? state.phase
      : "Default";
    summaryFile.textContent = state.file?.name || "-";
    summaryRows.textContent = state.preview ? fmtNum(state.preview.total_rows) : "-";
    readiness.textContent = canLaunch
      ? "Ready to launch. Review the preview before starting."
      : "Complete the workflow to enable launch.";
    readiness.className = canLaunch
      ? "mt-4 text-sm font-semibold text-amber-700"
      : "mt-4 text-sm text-slate-400";
  }

  root.replaceChildren(
    el("div", { class: "mb-5 flex flex-wrap items-end justify-between gap-3" },
      el("div", {},
        el("p", { class: "view-kicker" }, "Upload workflow"),
        el("p", { class: "mt-1 max-w-3xl text-sm text-slate-400" },
          "Select a company, choose a discovery pipeline, validate the CSV, then start the run."),
      ),
    ),
    el("div", { class: "run-grid" },
      el("div", { class: "space-y-4" }, step1, step2, step3, step4),
      summaryPanel,
    ),
  );
  refresh();
  await loadCompanies();
}
