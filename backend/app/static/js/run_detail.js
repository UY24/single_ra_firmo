// backend/app/static/js/run_detail.js — live detail for one run.
//
// Tries the AI-mode status endpoint first (GET /uploads/ai-mode/{ref}/status).
// On 404 it falls back to the legacy SerpWow upload status endpoint
// (GET /uploads/{ref}/status — fields: upload_id, pipeline, status, total_rows,
// processed_rows, success_rows, failed_rows, processing_seconds_*), rendered as
// a simpler header + stat tiles with the same poller (pollStatus stops on
// completed/completed_with_errors/failed and is always stopped by the router
// via the cleanup function this view returns).
import { api, el, fmtUsd, fmtNum, pollStatus } from "./api.js";
import {
  errorCard,
  loadingCard,
  metricItem,
  sectionHeading,
  statusBadge,
  fmtDuration,
} from "./ui.js";

const RESULT_FILES = ["final_report.json", "found.csv", "notFound.csv", "run.log", "input.csv"];

// ── inline file viewer modal ──────────────────────────────────────────────────
let _modal = null;

function _ensureModal() {
  if (_modal) return _modal;
  const title = el("span", { class: "truncate text-sm font-semibold text-slate-50" });
  const dlBtn = el("a", {
    class: "btn-ghost min-h-0 px-3 py-1.5 text-xs shrink-0",
    target: "_blank",
  }, "Download");
  const closeBtn = el("button", {
    class: "btn-ghost min-h-0 px-2 py-1 text-xs shrink-0",
    onclick: () => overlay.classList.add("hidden"),
  }, "✕ Close");
  const pre = el("pre", {
    class: "code-block flex-1 overflow-auto whitespace-pre-wrap break-words p-4 text-xs leading-5 text-slate-300 font-mono",
  });
  const loadingMsg = el("p", {
    class: "p-6 text-sm text-slate-400",
  }, "Loading…");
  const body = el("div", { class: "flex flex-col overflow-hidden" }, loadingMsg);
  const overlay = el("div", {
    class: "file-modal hidden fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4",
    onclick: (e) => { if (e.target === overlay) overlay.classList.add("hidden"); },
  },
    el("div", { class: "modal-surface flex flex-col w-full max-w-4xl h-[80vh] rounded-xl border border-slate-700 bg-slate-900 shadow-2xl overflow-hidden" },
      el("div", { class: "flex items-center gap-3 border-b border-slate-700 px-4 py-3 shrink-0" },
        title, dlBtn, closeBtn,
      ),
      body,
    ),
  );
  document.body.appendChild(overlay);
  _modal = { overlay, title, dlBtn, pre, loadingMsg, body };
  return _modal;
}

// RFC-4180-ish parser: handles quoted fields, "" escapes, and embedded
// newlines/commas (our flags/attempt_log cells contain real newlines).
function parseCsv(text) {
  const rows = [];
  let row = [], field = "", inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += c;
      continue;
    }
    if (c === '"') inQuotes = true;
    else if (c === ',') { row.push(field); field = ""; }
    else if (c === '\r') { /* skip */ }
    else if (c === '\n') { row.push(field); rows.push(row); row = []; field = ""; }
    else field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function csvTable(text) {
  const rows = parseCsv(text);
  if (!rows.length) return el("p", { class: "p-6 text-sm text-slate-400" }, "Empty file");
  const [header, ...bodyRows] = rows;
  const table = el("table", { class: "data-table w-full border-collapse text-xs" },
    el("thead", {},
      el("tr", { class: "data-row" },
        ...header.map((h) => el("th", {
          class: "sticky top-0 z-10 bg-slate-800 border border-slate-700 px-3 py-2 text-left font-semibold text-slate-100 whitespace-nowrap",
        }, h)),
      ),
    ),
    el("tbody", {},
      ...bodyRows.map((r, ri) => el("tr", { class: `data-row ${ri % 2 ? "bg-slate-900/40" : ""}`.trim() },
        ...header.map((_, ci) => el("td", {
          class: "border border-slate-800 px-3 py-2 align-top text-slate-300 whitespace-pre-wrap break-words",
        }, r[ci] ?? "")),
      )),
    ),
  );
  return el("div", { class: "flex-1 overflow-auto p-2" }, table,
    el("p", { class: "px-2 pb-2 pt-1 text-[11px] text-slate-500" },
      `${bodyRows.length} row${bodyRows.length === 1 ? "" : "s"}`),
  );
}

async function viewFile(url, filename, downloadUrl) {
  const m = _ensureModal();
  m.title.textContent = filename;
  m.dlBtn.href = downloadUrl;
  m.body.replaceChildren(m.loadingMsg);
  m.overlay.classList.remove("hidden");
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const text = await res.text();
    if (/\.csv$/i.test(filename)) {
      m.body.replaceChildren(csvTable(text));
    } else {
      m.pre.textContent = text;
      m.body.replaceChildren(m.pre);
    }
  } catch (e) {
    m.body.replaceChildren(
      el("p", { class: "p-6 text-sm text-red-400" }, `Failed to load: ${e.message}`),
    );
  }
}
// ─────────────────────────────────────────────────────────────────────────────

function safeCount(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function outcomeSummary({ found, notFound, errors = 0, total, skipped = null }) {
  const safeFound = safeCount(found);
  const safeNotFound = safeCount(notFound);
  const safeErrors = safeCount(errors);
  const safeTotal = safeCount(total);
  const safeSkipped = skipped == null ? null : safeCount(skipped);
  const rate = safeTotal > 0
    ? Math.min(100, Math.round((safeFound / safeTotal) * 100))
    : 0;
  const secondary = [
    metricItem("Not found", fmtNum(safeNotFound), "muted"),
    metricItem("Errors", fmtNum(safeErrors), safeErrors > 0 ? "danger" : "muted"),
  ];
  if (safeSkipped != null) secondary.push(metricItem("Skipped", fmtNum(safeSkipped), "warning"));
  return el("section", { class: "outcome-summary", "aria-label": "Run outcome" },
    el("div", { class: "outcome-primary" },
      el("p", { class: "outcome-label" }, "Websites found"),
      el("div", { class: "outcome-result" },
        el("span", { class: "outcome-value" }, `${fmtNum(safeFound)} of ${fmtNum(safeTotal)}`),
        el("span", { class: "outcome-rate" }, `${fmtNum(rate)}%`),
      ),
    ),
    el("dl", { class: "outcome-secondary" }, ...secondary),
  );
}

function executionStrip(items) {
  return el("dl", { class: "metric-strip", "aria-label": "Execution details" },
    ...items
      .filter(({ value }) => value != null)
      .map(({ label, value, tone = "default", detail = "" }) =>
        metricItem(label, value, tone, detail)),
  );
}

// Small metadata pill (e.g. "Confidence LLM"). `tone` picks a semantic color
// (good/info/warn/danger/muted) from the shared .pill classes in app.css.
function chip(label, value, tone = "muted") {
  return el("span", { class: `pill pill--${tone}` },
    el("span", { class: "pill-label" }, label),
    el("span", {}, String(value)),
  );
}

// Cost breakdown card: LLM (LLM pipelines only) · SerpWow (+searches) · Total.
// `g` is serpwow_summary; reads g.cost {llm_usd, serpwow_usd, serpwow_searches, total_usd}.
function costItem(label, value, sub, extraClass = "") {
  return el("div", { class: `cost-item ${extraClass}`.trim() },
    el("span", { class: "cost-label" }, label),
    el("span", { class: "cost-value" }, value ?? "—"),
    ...(sub ? [el("span", { class: "cost-sub" }, sub)] : []),
  );
}

function costSection(g, {
  providerLabel = "SerpWow",
  providerCostKey = "serpwow_usd",
  searchKey = "serpwow_searches",
  llmCostKey = "llm_usd",
  totalCostKey = "total_usd",
} = {}) {
  const cost = g.cost || {};
  const isLlm = g.confidence_mode === "llm" || !!g.model;
  const items = [];
  if (isLlm && llmCostKey) items.push(costItem("LLM", fmtUsd(cost[llmCostKey])));
  items.push(costItem(
    providerLabel,
    providerCostKey && cost[providerCostKey] != null ? fmtUsd(cost[providerCostKey]) : null,
    cost[searchKey] != null ? `${fmtNum(cost[searchKey])} searches` : null,
  ));
  if (totalCostKey) {
    items.push(costItem("Total", fmtUsd(cost[totalCostKey]), null, "cost-item--total"));
  }
  return el("section", { class: "detail-section cost-section" },
    sectionHeading("Cost"),
    el("div", { class: "detail-section-body cost-card" }, ...items),
  );
}

// Relationship verdict breakdown: the real 3-way split (confirmed / not_confirmed
// / unclear), distinct from the found/not-found story. `rb` is relationship_breakdown.
function verdictSection(rb) {
  return el("section", { class: "detail-section verdict-section" },
    sectionHeading("Relationship verdict"),
    el("div", { class: "detail-section-body relationship-verdict" },
      chip("Confirmed", fmtNum(rb.confirmed), "good"),
      chip("Not confirmed", fmtNum(rb.not_confirmed), "danger"),
      chip("Unclear", fmtNum(rb.unclear), "warn"),
    ),
  );
}

function headerCard(title, subtitle, status, phase, chips) {
  const bits = [statusBadge(status)];
  if (status === "running" && phase) {
    bits.push(el("span", {
      class: "status-badge",
    }, phase));
  }
  const left = el("div", {},
    el("p", { class: "text-base font-semibold text-slate-50" }, title),
    el("p", { class: "mt-0.5 section-copy" }, subtitle),
  );
  if (chips && chips.length) {
    left.appendChild(el("div", { class: "mt-2 flex flex-wrap items-center gap-1.5" }, ...chips));
  }
  return el("header", { class: "detail-header" },
    el("div", { class: "flex flex-wrap items-center justify-between gap-3" },
      left,
      el("div", { class: "flex items-center gap-2" }, ...bits),
    ),
  );
}

function progressSection(done, total, running) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return el("section", { class: "detail-section progress-section" },
    el("div", { class: "flex items-center justify-between text-sm" },
      el("span", { class: "section-copy" }, "Batches"),
      el("span", { class: "font-semibold text-slate-50" }, `${fmtNum(done)} / ${fmtNum(total)}`),
    ),
    el("div", { class: "mt-2 h-2 overflow-hidden rounded-full bg-slate-800" },
      el("div", {
        class: `h-full rounded-full bg-amber-500 transition-all ${running ? "animate-pulse" : ""}`,
        style: `width: ${pct}%`,
      }),
    ),
  );
}

// Shared "Files" card (View + Download per file). `allFiles` is the full list to
// list; `available` (optional) is the subset that actually exists — others render
// disabled. `baseUrl(name)` builds the per-file result URL (download appends
// "&download=true"). `extras` (optional) are download-only rows {name, href} for
// files served by a different endpoint (e.g. the full output.json/xlsx). Used by
// both AI Mode and the SerpWow gsearch/gmaps detail view.
function filesSection(allFiles, baseUrl, available, extras) {
  const files = available?.length ? available : allFiles;
  const rows = allFiles.map((name) => {
    const isAvailable = files.includes(name);
    return el("div", { class: "file-row" },
      el("span", { class: `file-name font-mono text-xs ${isAvailable ? "text-slate-300" : "text-slate-600"}` }, name),
      el("div", { class: "file-actions" },
        el("button", {
          class: "btn-ghost min-h-0 px-3 py-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed",
          ...(isAvailable ? {} : { disabled: "" }),
          onclick: () => viewFile(baseUrl(name), name, `${baseUrl(name)}&download=true`),
        }, "View"),
        el("a", {
          class: `btn-ghost min-h-0 px-3 py-1 text-xs ${isAvailable ? "" : "pointer-events-none opacity-40"}`,
          ...(isAvailable ? { href: `${baseUrl(name)}&download=true`, download: name } : {}),
        }, "Download"),
      ),
    );
  });
  for (const ex of extras ?? []) {
    rows.push(el("div", { class: "file-row" },
      el("span", { class: "file-name font-mono text-xs text-slate-300" }, ex.name),
      el("div", { class: "file-actions" },
        el("a", {
          class: "btn-ghost min-h-0 px-3 py-1 text-xs",
          href: ex.href, download: ex.name,
        }, "Download"),
      ),
    ));
  }
  return el("section", { class: "detail-section files-section" },
    sectionHeading("Files"),
    el("div", { class: "detail-section-body file-list" }, ...rows),
  );
}

function downloadsSection(ref, available) {
  const baseUrl = (name) => `/uploads/ai-mode/${encodeURIComponent(ref)}/result?file=${encodeURIComponent(name)}`;
  return filesSection(RESULT_FILES, baseUrl, available);
}

function rerunFailedSection(ref) {
  const msg = el("div", { class: "mt-3 hidden" });
  const btn = el("button", {
    class: "btn-primary disabled:opacity-50",
    onclick: async () => {
      btn.disabled = true;
      msg.className = "mt-3";
      msg.replaceChildren(el("p", { class: "section-copy" }, "Rerunning failed work..."));
      try {
        await api(`/uploads/ai-mode/${encodeURIComponent(ref)}/resume`, { method: "POST" });
        msg.replaceChildren(el("p", { class: "text-sm font-semibold text-emerald-600" },
          "Rerun started. Reloading..."));
        setTimeout(() => { window.location.reload(); }, 700);
      } catch (e) {
        btn.disabled = false; // 404/409 → inline detail
        msg.replaceChildren(el("p", { class: "text-sm text-red-600" }, e.message));
      }
    },
  }, "Rerun failed");
  return el("section", { class: "detail-section detail-action" },
    sectionHeading("Rerun failed"),
    el("div", { class: "detail-section-body" },
      el("p", { class: "text-xs text-slate-400" },
        "Re-runs this same run and redoes only what failed: Phase 1 (scrape) re-fetches "
        + "only batches that failed to scrape, Phase 2 (LLM cleanup) re-does only batches "
        + "that failed to clean. Successful scrapes and cleaned results are reused — no "
        + "scrape.do or LLM re-spend on them. (Not-found rows are final; use AI Mode Deep for those.)"),
      el("div", { class: "mt-3" }, btn), msg,
    ),
  );
}

function warningsNote(warnings) {
  return el("div", { class: "callout callout-amber" },
    ...warnings.map((w) => el("p", { class: "text-xs" }, w)));
}

function renderAiStatus(root, ref, s) {
  const running = ["queued", "running"].includes(s.status);
  const outcome = s.outcome_breakdown ?? {};
  const total = s.total_rows ?? s.entities_processed;
  const execution = [
    {
      label: "Total / Processed",
      value: total != null || s.entities_processed != null
        ? `${fmtNum(total)} / ${fmtNum(s.entities_processed)}` : null,
    },
    { label: "Duration", value: s.batch_duration_seconds == null ? null : fmtDuration(s.batch_duration_seconds) },
    { label: "Input tokens", value: s.token_usage?.prompt_tokens == null ? null : fmtNum(s.token_usage.prompt_tokens), tone: "muted" },
    { label: "Output tokens", value: s.token_usage?.completion_tokens == null ? null : fmtNum(s.token_usage.completion_tokens), tone: "muted" },
    { label: "Model", value: s.model ?? null, tone: "info" },
    { label: "Batch mode", value: s.is_batch == null ? null : s.is_batch ? "Yes" : "No" },
    {
      label: "Scrape.do requests",
      value: s.scrapedo_request_count == null ? null : fmtNum(s.scrapedo_request_count),
      detail: s.failed_request_count == null ? "" : `${fmtNum(s.failed_request_count)} failed`,
    },
  ];

  const parts = [
    headerCard(s.company_name || "—",
      `${s.mode_label ?? s.mode ?? "—"} · run ${ref}`, s.status, s.phase),
    outcomeSummary({
      found: s.websites_found,
      notFound: s.websites_not_found,
      errors: outcome.errored ?? s.llm_errors,
      total,
    }),
    executionStrip(execution),
    progressSection(s.batches_done ?? 0, s.batches_total ?? 0, running),
  ];
  if ((s.warnings ?? []).length) parts.push(warningsNote(s.warnings));
  if (s.error) {
    parts.push(el("div", { class: "callout callout-red" },
      el("p", { class: "text-sm" }, s.error)));
  }
  if (s.cost) {
    parts.push(costSection({ confidence_mode: "llm", model: s.model, cost: s.cost }, {
      providerLabel: "Scrape.do",
      providerCostKey: null,
      searchKey: "scrapedo_searches",
      llmCostKey: "total_usd",
      totalCostKey: null,
    }));
  }
  if (["completed", "completed_with_errors", "failed"].includes(s.status)) {
    parts.push(downloadsSection(ref, s.available_files));
  }
  if (["failed", "completed_with_errors"].includes(s.status)) parts.push(rerunFailedSection(ref));

  root.replaceChildren(el("div", { class: "run-detail space-y-4" }, ...parts));
}

function renderLegacyStatus(root, ref, s) {
  const rowsDone = ["completed", "completed_with_errors"].includes(String(s.status ?? ""));
  // gsearch/gmaps batch mode: rows finish before the Gemini batch. Treat the run as
  // "done" only once the batch is terminal so the UI doesn't claim completion early.
  const batchStatus = s.gemini_batch?.status ?? null;
  const batchTerminal = batchStatus == null
    || ["succeeded", "failed", "skipped", "not_started"].includes(String(batchStatus));
  const isSerp = ["gsearch", "gmaps", "relationship"].includes(s.pipeline);
  const finalizing = isSerp && rowsDone && !batchTerminal;
  const active = !["completed", "completed_with_errors", "failed"].includes(String(s.status ?? ""))
    || finalizing;
  const g = s.serpwow_summary;
  const outcome = g?.outcome_breakdown ?? {};

  // Header chips: confidence mode always (gsearch/gmaps); batch + model only when LLM
  // (batch is meaningless in heuristic mode). Non-serpwow pipelines get no chips.
  const chips = [];
  if (g?.confidence_mode) {
    const isLlm = g.confidence_mode === "llm";
    chips.push(chip("Confidence", isLlm ? "LLM" : "Heuristic", isLlm ? "info" : "muted"));
    if (isLlm) {
      chips.push(chip("Batch", g.is_batch ? "On" : "Off", g.is_batch ? "good" : "muted"));
      if (g.model) chips.push(chip("Model", g.model, "muted"));
    }
  }

  const isRel = s.pipeline === "relationship";
  // Relationship totals use original CSV rows; state.total_rows is deduplicated queue work.
  const total = isRel ? (g?.total_rows_original ?? s.total_rows) : s.total_rows;
  const found = g?.websites_found ?? s.success_rows;
  const notFound = g?.websites_not_found ?? 0;
  const errors = outcome.errored ?? (g ? 0 : s.failed_rows);
  const execution = [
    {
      label: "Total / Processed",
      value: total != null || s.processed_rows != null
        ? `${fmtNum(total)} / ${fmtNum(s.processed_rows)}` : null,
      detail: isRel ? "Original rows / pairs processed" : "Rows",
    },
    { label: "Processing time", value: s.processing_seconds_total == null ? null : fmtDuration(s.processing_seconds_total) },
    { label: "Avg / row", value: s.processing_seconds_avg == null ? null : fmtDuration(s.processing_seconds_avg) },
    {
      label: "Input tokens",
      value: g?.confidence_mode === "llm" && g.token_usage?.prompt_tokens != null
        ? fmtNum(g.token_usage.prompt_tokens) : null,
      tone: "muted",
    },
    {
      label: "Output tokens",
      value: g?.confidence_mode === "llm" && g.token_usage?.completion_tokens != null
        ? fmtNum(g.token_usage.completion_tokens) : null,
      tone: "muted",
    },
    { label: "Batch job", value: s.gemini_batch?.status ?? null, tone: finalizing ? "warning" : "default" },
    { label: "Unique pairs", value: isRel && g?.unique_pairs != null ? fmtNum(g.unique_pairs) : null },
  ];

  const parts = [
    headerCard(`Upload ${ref}`, `${s.pipeline ?? "—"} (SerpWow pipeline)`,
      finalizing ? "running" : s.status, finalizing ? "finalizing" : null, chips),
    outcomeSummary({
      found,
      notFound,
      errors,
      total,
      skipped: isRel ? g?.blank_rows ?? 0 : null,
    }),
    executionStrip(execution),
  ];
  if ((s.warnings ?? []).length) parts.push(warningsNote(s.warnings));
  if (s.error) {
    parts.push(el("div", { class: "callout callout-red" },
      el("p", { class: "text-sm" }, s.error)));
  }
  if (g) parts.push(costSection(g));
  if (isRel && g?.relationship_breakdown) parts.push(verdictSection(g.relationship_breakdown));

  // Stop button while the run is still doing work (rows in flight, or the
  // Gemini batch still running). Remaining rows are marked failed; retryable
  // later via "Retry failed rows".
  if (active) {
    const stopMsg = el("div", { class: "mt-3" });
    const stopBtn = el("button", {
      class: "btn-secondary min-h-0 px-3 py-1.5 text-xs text-red-600 disabled:opacity-50",
      onclick: async () => {
        if (!window.confirm(
          "Stop this run? Rows not yet processed are marked failed "
          + "(you can retry them later); a running Gemini batch is cancelled.")) return;
        stopBtn.disabled = true;
        try {
          const res = await api(`/uploads/${encodeURIComponent(ref)}/stop`, { method: "POST" });
          stopMsg.replaceChildren(el("p", { class: "text-sm font-semibold text-emerald-600" },
            `Stopped: ${fmtNum(res.stopped_rows)} row(s) halted`
            + `${res.batch_cancelled ? ", batch cancelled" : ""}. Reloading...`));
          setTimeout(() => { window.location.reload(); }, 700);
        } catch (e) {
          stopBtn.disabled = false;
          stopMsg.replaceChildren(el("p", { class: "text-sm text-red-600" }, e.message));
        }
      },
    }, "Stop run");
    parts.push(el("section", { class: "detail-section detail-action" },
      sectionHeading("Stop"),
      el("div", { class: "detail-section-body" },
        el("p", { class: "text-xs text-slate-400" },
          "Halts remaining work: unprocessed rows are marked failed (retryable via "
          + "Retry failed rows), a running Gemini batch is cancelled, and the run "
          + "finalizes with whatever finished."),
        el("div", { class: "mt-3" }, stopBtn), stopMsg)));
  }

  // Single file surface: result files (gsearch/gmaps) + output.json/xlsx (all pipelines),
  // shown once the run is terminal (and, for batch runs, once the batch is terminal too —
  // result files aren't written until then, so View/Download would 404).
  if (rowsDone && !finalizing) {
    const resultUrl = (name) => `/uploads/${encodeURIComponent(ref)}/result?file=${encodeURIComponent(name)}`;
    const resultFiles = (isSerp && batchTerminal)
      ? (s.pipeline === "relationship"
          ? ["found.csv", "notFound.csv", "skipped.csv", "report.json", "run.log"]
          : ["found.csv", "notFound.csv", "report.json", "run.log"])
      : [];
    const extras = [
      { name: "output.json", href: `/uploads/${encodeURIComponent(ref)}/output?download=true` },
      { name: "output.xlsx", href: `/uploads/${encodeURIComponent(ref)}/output?format=xlsx&download=true` },
    ];
    parts.push(filesSection(resultFiles, resultUrl, undefined, extras));
  }
  root.replaceChildren(el("div", { class: "run-detail space-y-4" }, ...parts));
}

export async function render(root, params) {
  const ref = params.runRef;
  root.replaceChildren(loadingCard());
  let stop = null;

  const aiPath = `/uploads/ai-mode/${encodeURIComponent(ref)}/status`;

  // Probe with a raw fetch so a 404 (not an AI-mode run) can route to the
  // legacy SerpWow fallback instead of being swallowed by the poller's retry.
  let probe;
  try {
    probe = await fetch(aiPath);
  } catch (e) {
    root.replaceChildren(errorCard(e.message));
    return () => {};
  }

  if (probe.ok) {
    stop = pollStatus(aiPath, (s) => renderAiStatus(root, ref, s));
  } else if (probe.status === 404) {
    const legacyPath = `/uploads/${encodeURIComponent(ref)}/status`;
    try {
      await api(legacyPath); // 404 here too → unknown run
      stop = pollStatus(legacyPath, (s) => renderLegacyStatus(root, ref, s));
    } catch (e) {
      root.replaceChildren(errorCard(
        /not found/i.test(e.message) ? `Run "${ref}" was not found.` : e.message));
    }
  } else {
    let detail = probe.statusText;
    try { detail = (await probe.json()).detail ?? detail; } catch {}
    root.replaceChildren(errorCard(detail));
  }

  // The router invokes this before the next view renders — stops the poller.
  return () => { if (stop) stop(); };
}
