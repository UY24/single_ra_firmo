// backend/app/static/js/new_run.js — 4-step "start a new run" flow.
//
// Endpoint/form-field mapping (read from routers/ai_mode.py + serpwow/legacy_app.py):
//   ai_bulk / ai_deep → POST /uploads/ai-mode        (file, mode, company_id)
//   gmaps             → POST /uploads/gmaps          (file, company_id)
//   gsearch           → POST /uploads/gsearch        (file, company_id; phase defaults to "all")
//   full              → POST /uploads                (file, company_id)
//   firmographics     → POST /uploads/firmographics  (file, company_id)
import { api, el, fmtNum } from "./api.js";
import { errorCard, head, cell } from "./ui.js";

const PIPELINES = [
  { key: "ai_bulk", label: "AI Mode 1 — Bulk", endpoint: "/uploads/ai-mode", ai: true,
    desc: "Large batches, broad search — get as much as we can." },
  { key: "ai_deep", label: "AI Mode 2 — Deep Search", endpoint: "/uploads/ai-mode", ai: true,
    desc: "2–3 entities per request, thorough multi-angle investigation." },
  { key: "gmaps", label: "Google Maps", endpoint: "/uploads/gmaps",
    desc: "SerpWow Google Maps discovery pipeline." },
  { key: "gsearch", label: "Google Search", endpoint: "/uploads/gsearch",
    desc: "SerpWow Google Search pipeline (all phases)." },
  { key: "full", label: "Full pipeline", endpoint: "/uploads",
    desc: "SerpWow full pipeline: discovery, crawl and extraction." },
  { key: "firmographics", label: "Firmographics", endpoint: "/uploads/firmographics",
    desc: "Firmographics enrichment for rows that already have a website." },
];

const SAMPLE_COLS = ["company_name", "country", "sno", "company_local_name",
                     "address", "firm_id", "industry"];

const inputCls = "rounded-lg border border-gray-300 px-3 py-2 text-sm " +
  "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500";
const buttonCls = "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white " +
  "hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed";

function redCallout(text) {
  return el("div", { class: "rounded-lg border border-red-200 bg-red-50 p-3" },
    el("p", { class: "text-sm text-red-800 whitespace-pre-wrap" }, text));
}

function amberCallout(lines) {
  return el("div", { class: "rounded-lg border border-amber-200 bg-amber-50 p-3" },
    ...lines.map((w) => el("p", { class: "text-sm text-amber-800" }, w)));
}

function stepCard(n, title, body) {
  const card = el("div", { class: "rounded-xl border border-gray-200 bg-white p-5 shadow-sm" },
    el("div", { class: "mb-3 flex items-center gap-3" },
      el("span", {
        class: "flex h-6 w-6 shrink-0 items-center justify-center rounded-full " +
               "bg-indigo-600 text-xs font-semibold text-white",
      }, String(n)),
      el("h2", { class: "text-sm font-semibold text-gray-700" }, title),
    ),
    body,
  );
  card.setEnabled = (enabled) => {
    card.classList.toggle("opacity-40", !enabled);
    card.classList.toggle("pointer-events-none", !enabled);
  };
  return card;
}

function previewTables(preview) {
  const parts = [];
  parts.push(el("p", { class: "text-sm text-gray-700" },
    el("span", { class: "font-medium text-gray-900" }, fmtNum(preview.total_rows)),
    " rows detected."));

  if ((preview.warnings ?? []).length) parts.push(amberCallout(preview.warnings));
  if (preview.positional) {
    parts.push(amberCallout([
      "No recognized headers — columns were read positionally (col 1 = company, col 2 = country).",
    ]));
  }

  const mapping = preview.columns_detected ?? {};
  if (Object.keys(mapping).length) {
    parts.push(
      el("div", { class: "overflow-hidden rounded-lg border border-gray-200" },
        el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
          el("thead", { class: "bg-gray-50" },
            el("tr", {}, head("Field"), head("CSV header"))),
          el("tbody", { class: "divide-y divide-gray-100" },
            ...Object.entries(mapping).map(([field, header]) =>
              el("tr", {}, cell(field, "font-medium text-gray-900"), cell(header ?? "—"))),
          ),
        ),
      ),
    );
  }

  const sample = preview.sample_rows ?? [];
  if (sample.length) {
    parts.push(
      el("div", { class: "overflow-x-auto rounded-lg border border-gray-200" },
        el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
          el("thead", { class: "bg-gray-50" },
            el("tr", {}, ...SAMPLE_COLS.map((c) => head(c)))),
          el("tbody", { class: "divide-y divide-gray-100" },
            ...sample.map((row) =>
              el("tr", {}, ...SAMPLE_COLS.map((c) =>
                cell(row[c] == null || row[c] === "" ? "—" : String(row[c]))))),
          ),
        ),
      ),
    );
  }
  return el("div", { class: "space-y-3" }, ...parts);
}

export async function render(root) {
  const state = { companyId: "", companyName: "", pipeline: null, file: null, preview: null };

  // ---- Step 1: company -----------------------------------------------------
  const companySelect = el("select", { class: `${inputCls} w-72` });
  const companyArea = el("div", {},
    el("p", { class: "text-sm text-gray-400" }, "Loading companies…"));

  async function loadCompanies(selectId) {
    let companies = [];
    try {
      companies = (await api("/companies")).companies ?? [];
    } catch (e) {
      companyArea.replaceChildren(errorCard(e.message));
      return;
    }
    companySelect.replaceChildren(
      el("option", { value: "" }, "Select a company…"),
      ...companies.map((c) => el("option", { value: c.id }, c.name ?? c.id)),
    );
    if (selectId) companySelect.value = selectId;
    companyArea.replaceChildren(companySelect);
    onCompanyChange();
  }

  function onCompanyChange() {
    state.companyId = companySelect.value;
    state.companyName = companySelect.selectedOptions[0]?.textContent ?? "";
    refresh();
  }
  companySelect.addEventListener("change", onCompanyChange);

  const newCompanyMsg = el("p", { class: "mt-2 hidden text-sm" });
  const newCompanyInput = el("input", {
    type: "text", placeholder: "New company name", class: `${inputCls} w-72`,
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
        newCompanyMsg.textContent = `Created “${company.name ?? name}”.`;
        newCompanyMsg.className = "mt-2 text-sm text-green-700";
        await loadCompanies(company.id);
      } catch (e) {
        newCompanyMsg.textContent = e.message;
        newCompanyMsg.className = "mt-2 text-sm text-red-600";
      } finally {
        newCompanyBtn.disabled = false;
      }
    },
  },
    el("div", { class: "flex items-center gap-3" }, newCompanyInput, newCompanyBtn),
    newCompanyMsg,
  );

  const step1 = stepCard(1, "Company",
    el("div", {},
      companyArea,
      el("p", { class: "mt-4 text-xs text-gray-400" }, "Or create a new one:"),
      newCompanyForm,
    ),
  );

  // ---- Step 2: pipeline ----------------------------------------------------
  const radios = [];
  const pipelineCards = PIPELINES.map((p) => {
    const radio = el("input", {
      type: "radio", name: "pipeline", value: p.key, class: "mt-1 accent-indigo-600",
      onchange: () => { state.pipeline = p; refresh(); },
    });
    radios.push(radio);
    return el("label", {
      class: "flex cursor-pointer items-start gap-3 rounded-lg border border-gray-200 p-3 " +
             "transition hover:border-indigo-300 hover:bg-indigo-50/30",
    },
      radio,
      el("span", {},
        el("span", { class: "block text-sm font-medium text-gray-900" }, p.label),
        el("span", { class: "mt-0.5 block text-xs text-gray-500" }, p.desc),
      ),
    );
  });
  const step2 = stepCard(2, "Pipeline",
    el("div", { class: "grid grid-cols-1 gap-3 sm:grid-cols-2" }, ...pipelineCards));

  // ---- Step 3: file + preview ----------------------------------------------
  const previewArea = el("div", { class: "mt-4" });
  const fileInput = el("input", {
    type: "file", accept: ".csv",
    class: "block text-sm text-gray-700 file:mr-3 file:rounded-lg file:border-0 " +
           "file:bg-indigo-50 file:px-4 file:py-2 file:text-sm file:font-medium " +
           "file:text-indigo-700 hover:file:bg-indigo-100",
    onchange: async () => {
      state.file = fileInput.files[0] ?? null;
      state.preview = null;
      refresh();
      if (!state.file) { previewArea.replaceChildren(); return; }
      previewArea.replaceChildren(el("p", { class: "text-sm text-gray-400" }, "Previewing…"));
      const fd = new FormData();
      fd.append("file", state.file);
      try {
        state.preview = await api("/uploads/preview", { method: "POST", body: fd });
        previewArea.replaceChildren(previewTables(state.preview));
      } catch (e) {
        previewArea.replaceChildren(redCallout(e.message)); // 400 detail lists accepted columns
      }
      refresh();
    },
  });
  const step3 = stepCard(3, "File & preview", el("div", {}, fileInput, previewArea));

  // ---- Step 4: confirm & start ----------------------------------------------
  const summary = el("p", { class: "text-sm text-gray-700" }, "—");
  const startMsg = el("div", { class: "mt-3 hidden" });
  const startBtn = el("button", { class: buttonCls, disabled: "" }, "Start run");
  startBtn.addEventListener("click", async () => {
    if (!state.companyId || !state.pipeline || !state.file || !state.preview) return;
    startBtn.disabled = true;
    startMsg.className = "mt-3";
    startMsg.replaceChildren(el("p", { class: "text-sm text-gray-400" }, "Starting…"));
    const fd = new FormData();
    fd.append("file", state.file);
    fd.append("company_id", state.companyId);
    if (state.pipeline.ai) fd.append("mode", state.pipeline.key);
    try {
      const info = await api(state.pipeline.endpoint, { method: "POST", body: fd });
      startMsg.replaceChildren(
        el("p", { class: "text-sm font-medium text-green-700" },
          `Run started (${info.run_id ?? info.upload_id ?? "ok"}). Redirecting…`));
      const target = state.pipeline.ai
        ? `#/runs/${encodeURIComponent(info.run_id)}`
        : "#/runs"; // SerpWow run_ref = upload_id; list shows it once Supabase records it
      setTimeout(() => { window.location.hash = target; }, 700);
    } catch (e) {
      startBtn.disabled = false;
      startMsg.replaceChildren(redCallout(e.message));
    }
  });
  const step4 = stepCard(4, "Confirm & start",
    el("div", {}, summary, el("div", { class: "mt-4" }, startBtn), startMsg));

  // ---- gating ---------------------------------------------------------------
  function refresh() {
    const hasCompany = Boolean(state.companyId);
    const hasPipeline = Boolean(state.pipeline);
    const hasPreview = Boolean(state.preview);
    step2.setEnabled(hasCompany);
    step3.setEnabled(hasCompany && hasPipeline);
    step4.setEnabled(hasCompany && hasPipeline && hasPreview);
    startBtn.disabled = !(hasCompany && hasPipeline && hasPreview);
    summary.textContent = hasCompany && hasPipeline && hasPreview
      ? `${state.companyName} · ${state.pipeline.label} · ${fmtNum(state.preview.total_rows)} rows`
      : "Complete the steps above to start.";
  }

  root.replaceChildren(el("div", { class: "max-w-3xl space-y-4" }, step1, step2, step3, step4));
  refresh();
  await loadCompanies();
}
