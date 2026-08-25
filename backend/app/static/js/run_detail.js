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
  pipelineLabel,
  sectionHeading,
  shortDate,
  statusBadge,
  fmtDuration,
} from "./ui.js";

const RESULT_FILES = ["final_report.json", "found.csv", "notFound.csv", "run.log", "input.csv"];
const ROW_TERMINAL_STATUSES = new Set(["completed", "completed_with_errors", "failed"]);
// Pipelines that write result files at the end of a run. firmographics joined on
// 2026-08-20 with its S3-only migration.
const REPORTING_PIPELINES = new Set(["gsearch", "gmaps", "relationship",
                                     "firmographics"]);
// Pipelines whose ONLY provider is scrape.do, billed in credits. Used to label the cost
// card before any call has been billed — at zero, the numbers cannot say who the provider
// was. gsearch is absent: it is the last one still on SerpWow.
const SCRAPEDO_PIPELINES = new Set(["gmaps", "relationship", "firmographics"]);

// The S3-only pipelines keep NO state.json, so /uploads/{id}/output (and its CSV form)
// 404 for them — their per-row data is in the result files instead.
const NO_STATE_PIPELINES = new Set(["relationship", "firmographics"]);
const BATCH_TERMINAL_STATUSES = new Set([
  "succeeded", "completed_with_errors", "failed", "cancelled", "skipped", "not_started",
]);
const STOPPABLE_BATCH_STATUSES = new Set(["waiting_for_rows", "queued", "running"]);
function deriveLegacyRunState(s) {
  const status = String(s?.status ?? "");
  const pipeline = String(s?.pipeline ?? "");
  const reporting = REPORTING_PIPELINES.has(pipeline);
  const rowTerminal = ROW_TERMINAL_STATUSES.has(status);
  const batchStatus = s?.gemini_batch?.status == null
    ? null
    : String(s.gemini_batch.status);
  const batchTerminal = !reporting || batchStatus == null
    || BATCH_TERMINAL_STATUSES.has(batchStatus);
  const finalizing = reporting && rowTerminal && !batchTerminal;
  const pollTerminal = rowTerminal && !finalizing;
  const filesReady = pollTerminal;
  const cancellationRequested = status === "cancel_requested" || batchStatus === "cancel_requested";
  const canStop = !cancellationRequested
    && (!rowTerminal || (finalizing && STOPPABLE_BATCH_STATUSES.has(batchStatus)));
  return {
    reporting,
    rowTerminal,
    batchStatus,
    batchTerminal,
    finalizing,
    pollTerminal,
    filesReady,
    canStop,
  };
}

// ── inline file viewer modal ──────────────────────────────────────────────────
let _modal = null;
let _fileRequestToken = 0;
let _reloadTimer = null;

function _scheduleReload() {
  if (_reloadTimer != null) clearTimeout(_reloadTimer);
  _reloadTimer = setTimeout(() => {
    _reloadTimer = null;
    window.location.reload();
  }, 700);
}

function _clearReloadTimer() {
  if (_reloadTimer == null) return;
  clearTimeout(_reloadTimer);
  _reloadTimer = null;
}

function _invalidateFileRequest() {
  _fileRequestToken += 1;
  if (_modal?.controller) _modal.controller.abort();
  if (_modal) {
    _modal.controller = null;
    _modal.requestToken = _fileRequestToken;
  }
}

function closeFileModal() {
  if (!_modal) return;
  _invalidateFileRequest();
  _modal.overlay.classList.add("hidden");
  const { main, mainState, previousFocus } = _modal;
  if (main && mainState) {
    main.inert = mainState.inert;
    if (mainState.hadInertAttribute) main.setAttribute("inert", "");
    else main.removeAttribute("inert");
    if (mainState.ariaHidden == null) main.removeAttribute("aria-hidden");
    else main.setAttribute("aria-hidden", mainState.ariaHidden);
  }
  _modal.main = null;
  _modal.mainState = null;
  _modal.previousFocus = null;
  if (previousFocus?.focus) previousFocus.focus();
}

function _handleModalKeydown(event) {
  if (!_modal || _modal.overlay.classList.contains("hidden")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeFileModal();
    return;
  }
  if (event.key !== "Tab") return;
  const first = _modal.dlBtn;
  const last = _modal.closeBtn;
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function _ensureModal() {
  if (_modal) return _modal;
  const title = el("span", {
    id: "run-file-modal-title",
    class: "truncate text-sm font-semibold text-slate-50",
  });
  const dlBtn = el("a", {
    class: "file-modal-action btn-ghost min-h-0 px-3 py-1.5 text-xs shrink-0",
    target: "_blank",
  }, "Download");
  const closeBtn = el("button", {
    class: "file-modal-action btn-ghost min-h-0 px-2 py-1 text-xs shrink-0",
    onclick: closeFileModal,
  }, "Close");
  const pre = el("pre", {
    class: "code-block flex-1 overflow-auto whitespace-pre-wrap break-words p-4 text-xs leading-5 text-slate-300 font-mono",
  });
  const loadingMsg = el("p", {
    class: "p-6 text-sm text-slate-400",
  }, "Loading…");
  const body = el("div", { class: "flex flex-col overflow-hidden" }, loadingMsg);
  const overlay = el("div", {
    class: "file-modal hidden fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4",
    onclick: (e) => { if (e.target === overlay) closeFileModal(); },
    onkeydown: _handleModalKeydown,
  },
    el("div", {
      class: "modal-surface flex flex-col w-full max-w-4xl h-[80vh] rounded-xl border border-slate-700 bg-slate-900 shadow-2xl overflow-hidden",
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": "run-file-modal-title",
    },
      el("div", { class: "flex items-center gap-3 border-b border-slate-700 px-4 py-3 shrink-0" },
        title, dlBtn, closeBtn,
      ),
      body,
    ),
  );
  document.body.appendChild(overlay);
  _modal = {
    overlay, title, dlBtn, closeBtn, pre, loadingMsg, body,
    controller: null, requestToken: 0, main: null, mainState: null, previousFocus: null,
  };
  return _modal;
}

function openFileModal(filename, downloadUrl) {
  const m = _ensureModal();
  const wasClosed = m.overlay.classList.contains("hidden");
  if (wasClosed) {
    m.previousFocus = document.activeElement;
    m.main = document.querySelector("main");
    if (m.main) {
      m.mainState = {
        inert: Boolean(m.main.inert),
        hadInertAttribute: m.main.hasAttribute("inert"),
        ariaHidden: m.main.getAttribute("aria-hidden"),
      };
      m.main.inert = true;
      m.main.setAttribute("inert", "");
      m.main.setAttribute("aria-hidden", "true");
    }
  }
  m.title.textContent = filename;
  m.dlBtn.href = downloadUrl;
  m.body.replaceChildren(m.loadingMsg);
  m.overlay.classList.remove("hidden");
  m.closeBtn.focus();
  return m;
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
  const m = openFileModal(filename, downloadUrl);
  if (m.controller) m.controller.abort();
  const controller = typeof AbortController === "undefined" ? null : new AbortController();
  const requestToken = ++_fileRequestToken;
  m.controller = controller;
  m.requestToken = requestToken;
  const isCurrent = () => m.requestToken === requestToken
    && m.controller === controller
    && !m.overlay.classList.contains("hidden");
  try {
    const res = await fetch(url, controller ? { signal: controller.signal } : {});
    if (!isCurrent()) return;
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const text = await res.text();
    if (!isCurrent()) return;
    if (/\.csv$/i.test(filename)) {
      m.body.replaceChildren(csvTable(text));
    } else {
      m.pre.textContent = text;
      m.body.replaceChildren(m.pre);
    }
  } catch (e) {
    if (!isCurrent() || e?.name === "AbortError") return;
    m.body.replaceChildren(
      el("p", { class: "detail-error p-6 text-sm text-red-400" }, `Failed to load: ${e.message}`),
    );
  } finally {
    if (m.requestToken === requestToken && m.controller === controller) m.controller = null;
  }
}
// ─────────────────────────────────────────────────────────────────────────────

function safeCount(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function outcomeSummary({
  found,
  notFound = null,
  errors = 0,
  total,
  skipped = null,
  failureLabel = "Errors",
  primaryLabel = "Websites found",
}) {
  const safeFound = safeCount(found);
  const safeNotFound = notFound == null ? null : safeCount(notFound);
  const safeErrors = safeCount(errors);
  const safeTotal = safeCount(total);
  const safeSkipped = skipped == null ? null : safeCount(skipped);
  const hasDenominator = safeTotal > 0;
  const rate = hasDenominator ? Math.min(100, Math.round((safeFound / safeTotal) * 100)) : null;
  const secondary = [];
  if (safeNotFound != null) secondary.push(metricItem("Not found", fmtNum(safeNotFound), "muted"));
  secondary.push(metricItem(failureLabel, fmtNum(safeErrors), safeErrors > 0 ? "danger" : "muted"));
  if (safeSkipped != null) secondary.push(metricItem("Skipped", fmtNum(safeSkipped), "warning"));
  return el("section", { class: "outcome-summary", "aria-label": "Run outcome" },
    el("div", { class: "outcome-primary" },
      el("p", { class: "outcome-label" }, primaryLabel),
      el("div", { class: "outcome-result" },
        el("span", { class: "outcome-value" }, hasDenominator
          ? `${fmtNum(safeFound)} of ${fmtNum(safeTotal)}`
          : fmtNum(safeFound)),
        ...(hasDenominator ? [el("span", { class: "outcome-rate" }, `${fmtNum(rate)}%`)] : []),
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
  const text = String(value);
  return el("span", { class: `pill pill--${tone}` },
    el("span", { class: "pill-label" }, label),
    el("span", { class: "pill-value", title: text }, text),
  );
}

// Cost breakdown card: LLM (LLM pipelines only) · provider · Total. The provider cell is
// parameterized: SerpWow searches + USD by default (gsearch/relationship/firmographics),
// or Scrape.do credits for pipelines billed that way (gmaps, AI Mode) — see the call sites.
// `g` is serpwow_summary; reads g.cost {llm_usd, serpwow_usd, serpwow_searches,
// scrapedo_credits, total_usd}.
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
  searchUnit = "searches",
  failedSearchCount = null,
  llmCostKey = "llm_usd",
  totalCostKey = "total_usd",
} = {}) {
  const cost = g.cost || {};
  const isLlm = g.confidence_mode === "llm" || !!g.model;
  const failed = failedSearchCount ?? (
    cost[searchKey] != null && cost.serpwow_billable_searches != null
      ? Math.max(0, Number(cost[searchKey]) - Number(cost.serpwow_billable_searches))
      : 0);
  const searchSub = cost[searchKey] == null ? null
    : `${fmtNum(cost[searchKey])} ${searchUnit}`
      + (failed ? ` · ${fmtNum(failed)} failed` : "");
  const items = [];
  if (isLlm && llmCostKey) items.push(costItem("LLM", fmtUsd(cost[llmCostKey])));
  items.push(costItem(
    providerLabel,
    providerCostKey && cost[providerCostKey] != null ? fmtUsd(cost[providerCostKey]) : null,
    searchSub,
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

// Rows the provider answered with nothing usable. Branch on the DATA, not the pipeline:
// gmaps makes ONE Maps call per row and is billed per HTTP 200, so the question there is
// not "was the 200 empty" (it almost never is) but why the bill is under rows × 10 —
// answered by the calls that never reached 200: no Maps listing, or dead after every
// retry. Both are free, and the phase split rendered two permanent zeroes for it.
// relationship makes ONE scrape.do AI Mode call per row, so it reports a single
// {no_ai_text: N} — there are no phases to split by. gsearch runs several SerpWow
// phases per row and splits them all / some.
// `g` is serpwow_summary (needs cost + outcome_breakdown, not just the breakdown).
function emptyResponsesSection(g) {
  const eb = g.empty_response_breakdown || {};
  const cost = g.cost || {};
  // firmographics buys TWO differently-priced calls: a Google Search (10 credits per
  // HTTP 200) and, only when Google defers the AI Overview, a follow-up fetch (5
  // credits). The totals alone cannot explain the bill, so split by endpoint.
  if (eb.no_ai_overview != null) {
    const rows = safeCount(g.total_rows);
    const searchBilled = safeCount(cost.scrapedo_search_successful);
    const aioBilled = safeCount(cost.scrapedo_ai_overview_successful);
    const searchCredits = searchBilled * 10;
    const aioCredits = aioBilled * 5;
    const unbilled = Math.max(0, safeCount(cost.scrapedo_requests)
                                 - safeCount(cost.scrapedo_successful_requests));
    // Billed and answered, but no overview to extract from — Google had none, or the
    // deferred fetch failed. Credits spent for nothing: the refund claim.
    const paidForNothing = safeCount(eb.no_ai_overview);
    const failed = safeCount(cost.scrapedo_error_requests);
    return el("section", { class: "detail-section" },
      sectionHeading(
        "Scrape.do billing (10 credits per search, 5 per deferred AI Overview)",
        "Credits are charged on HTTP 200s only, so failed attempts and retries are free. "
        + "A deferred overview means Google had not finished generating it when the SERP "
        + "was served, so a second call was needed for that row."),
      el("div", { class: "detail-section-body relationship-verdict" },
        chip("Search calls billed",
          rows ? `${fmtNum(searchBilled)} of ${fmtNum(rows)} rows` : fmtNum(searchBilled),
          "good"),
        chip("Search credits", fmtNum(searchCredits), "muted"),
        chip("AI Overview follow-ups", fmtNum(aioBilled), aioBilled ? "info" : "muted"),
        chip("AI Overview credits", fmtNum(aioCredits), "muted"),
        chip("Deferred rows", fmtNum(eb.deferred ?? 0), (eb.deferred ?? 0) ? "info" : "muted"),
        chip("Billed but no overview", fmtNum(paidForNothing),
          paidForNothing ? "danger" : "muted"),
        // The one bucket here a rerun can fix: scraped and billed, overview captured,
        // but the Gemini shard never returned. Rerunning redoes the LLM only.
        chip("LLM never completed", fmtNum(eb.llm_incomplete ?? 0),
          (eb.llm_incomplete ?? 0) ? "warn" : "muted"),
        chip("Failed after retries", fmtNum(failed), failed ? "warn" : "muted"),
        chip("Unbilled attempts", fmtNum(unbilled), "muted"),
      ),
    );
  }
  if (eb.no_listing != null) {
    const billed = safeCount(cost.scrapedo_successful_requests);
    const rows = safeCount(g.total_rows);
    // Every attempt that did not return 200. This is the retry story: a dead row burns
    // the full SCRAPEDO_MAX_RETRIES + 1 attempts before it gives up, for free.
    const unbilled = Math.max(0, safeCount(cost.scrapedo_requests) - billed);
    // Paid and got nothing usable — an empty results array or an error body. Same
    // refund claim either way, and the only two ways a credit buys nothing.
    const billedErrors = safeCount(cost.scrapedo_billed_errors);
    const paidForNothing = safeCount(eb.billed_empty) + billedErrors;
    // Never got a 200 at all, so never charged: rows Google has no listing for, plus
    // dead rows whose every attempt failed. Rows that died WITH a billed 200 are
    // subtracted — they are counted above, and would otherwise be counted twice.
    const failed = safeCount(eb.no_listing)
      + Math.max(0, safeCount(g.outcome_breakdown?.errored) - billedErrors);
    return el("section", { class: "detail-section" },
      sectionHeading(
        "Scrape.do billing (10 credits per HTTP 200)",
        "A 200 is billed whether or not it carried data. Attempts that never returned "
        + "200 are free, which is why a run can cost less than 10 credits a row. "
        + "Download retry.csv for the rows behind these numbers."),
      el("div", { class: "detail-section-body relationship-verdict" },
        chip("Billed calls",
          rows ? `${fmtNum(billed)} of ${fmtNum(rows)} rows` : fmtNum(billed), "good"),
        chip("Billed but no result", fmtNum(paidForNothing),
          paidForNothing ? "danger" : "muted"),
        chip("Failed after retries", fmtNum(failed), failed ? "warn" : "muted"),
        chip("Unbilled attempts", fmtNum(unbilled), "muted"),
      ),
    );
  }
  const aiMode = eb.no_ai_text != null;
  const chips = aiMode
    ? [chip("No AI Mode text", fmtNum(eb.no_ai_text), eb.no_ai_text ? "danger" : "muted"),
       chip("LLM never completed", fmtNum(eb.llm_incomplete ?? 0),
         (eb.llm_incomplete ?? 0) ? "warn" : "muted")]
    : [
        chip("All phases", fmtNum(eb.all_phases ?? 0), (eb.all_phases ?? 0) ? "danger" : "muted"),
        chip("Some phases", fmtNum(eb.some_phases ?? 0), (eb.some_phases ?? 0) ? "warn" : "muted"),
      ];
  return el("section", { class: "detail-section" },
    sectionHeading(
      aiMode ? "Empty AI Mode answers (HTTP 200)" : "Empty responses (HTTP 200)",
      aiMode
        ? "Rows scrape.do billed and answered, but where AI Mode wrote no text_blocks"
        : "Rows where the provider returned 200 but no AI overview and no candidates"),
    el("div", { class: "detail-section-body relationship-verdict" }, ...chips),
  );
}

function failedRowsSection(ref, count, companyLabel) {
  const regionId = `failed-rows-${encodeURIComponent(ref)}`;
  const results = el("div", {
    id: regionId,
    class: "failed-rows-results mt-3 hidden",
    "aria-live": "polite",
  });
  let loaded = false;
  const button = el("button", {
    class: "btn-secondary min-h-0 px-3 py-1.5 text-xs disabled:opacity-50",
    "aria-expanded": "false",
    "aria-controls": regionId,
    onclick: async () => {
      const expanding = button.getAttribute("aria-expanded") !== "true";
      button.setAttribute("aria-expanded", String(expanding));
      results.classList[expanding ? "remove" : "add"]("hidden");
      if (!expanding || loaded) return;
      button.disabled = true;
      results.replaceChildren(el("p", { class: "section-copy" }, "Loading failed rows…"));
      try {
        const data = await api(
          `/uploads/${encodeURIComponent(ref)}/failure-analysis?sample_limit=100`,
        );
        const rows = data.sample_failed_rows ?? [];
        const table = el("table", { class: "data-table w-full text-xs" },
          el("thead", {}, el("tr", { class: "data-row" },
            ...["CSV row", companyLabel, "Attempts", "Error source", "Category", "Error"]
              .map((heading) => el("th", {}, heading)))),
          el("tbody", {}, ...rows.map((row) => el("tr", { class: "data-row" },
            el("td", {}, row.row_index ?? "—"),
            el("td", {}, row.company_name ?? "—"),
            // Calls this row cost before it died. A "4" here is the retries working:
            // the row was tried the full SCRAPEDO_MAX_RETRIES + 1 times.
            el("td", {}, row.attempts != null ? fmtNum(row.attempts) : "—"),
            el("td", {}, row.error_source ?? "—"),
            el("td", {}, row.error_category ?? "—"),
            el("td", {}, row.error ?? "—"),
          ))),
        );
        const total = safeCount(data.failed_rows);
        results.replaceChildren(
          ...(total > rows.length ? [el("p", { class: "section-copy mb-2" },
            `Showing first ${fmtNum(rows.length)} of ${fmtNum(total)} failed rows.`)] : []),
          rows.length
            ? el("div", { class: "overflow-x-auto" }, table)
            : el("p", { class: "section-copy" }, "No failed rows found."),
        );
        loaded = true;
      } catch (e) {
        results.replaceChildren(el("p", { class: "text-sm text-red-600" }, e.message));
      } finally {
        button.disabled = false;
      }
    },
  }, `View failed rows (${fmtNum(count)})`);
  return el("section", { class: "detail-section failed-rows-section" },
    sectionHeading("Failed rows"),
    el("div", { class: "detail-section-body" }, button, results),
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
    el("p", { class: "detail-title text-base font-semibold text-slate-50" }, title),
    el("p", { class: "detail-subtitle mt-0.5 section-copy" }, subtitle),
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
  return el("div", {
    class: "detail-section progress-section",
    role: "group",
    "aria-label": "Batch progress",
  },
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
// both AI Mode and the gsearch/gmaps detail view.
function filesSection(allFiles, baseUrl, available, extras) {
  const files = Array.isArray(available) ? available : allFiles;
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
          ...(isAvailable
            ? { href: `${baseUrl(name)}&download=true`, download: name }
            : { "aria-disabled": "true" }),
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
        _scheduleReload();
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

// The S3-only pipelines' rerun. NOT the AI-Mode one above: different endpoint, and
// different semantics — there are no batches here, only per-row objects, so "redo what
// failed" means "delete the error markers and re-drive". Until this existed the only way
// to retry a gmaps/relationship/firmographics run was to hand-type its id into the
// Operations page, which is exactly the friction that let a dead Gemini shard sit
// unnoticed: nothing on the run itself offered the one action that fixes it.
function rerunRunSection(ref, { llmIncomplete = 0, errors = 0 } = {}) {
  const msg = el("div", { class: "mt-3 hidden" });
  const btn = el("button", {
    class: "btn-primary disabled:opacity-50",
    onclick: async () => {
      btn.disabled = true;
      msg.className = "mt-3";
      msg.replaceChildren(el("p", { class: "section-copy" }, "Rerunning failed work..."));
      try {
        const data = await api(
          `/uploads/${encodeURIComponent(ref)}/retry-failed-rows`, { method: "POST" });
        msg.replaceChildren(el("p", { class: "text-sm font-semibold text-emerald-600" },
          `Rerun started for ${fmtNum(data.enqueued_rows ?? 0)} row(s). Reloading...`));
        _scheduleReload();
      } catch (e) {
        btn.disabled = false;
        msg.replaceChildren(el("p", { class: "text-sm text-red-600" }, e.message));
      }
    },
  }, "Rerun failed");
  // Say what this run will actually redo, because the two cases cost very different
  // things: a dead row is re-scraped at full provider price, an unjudged row is not.
  const detail = [];
  if (errors) detail.push(`${fmtNum(errors)} failed row(s) will be re-scraped`);
  if (llmIncomplete) {
    detail.push(`${fmtNum(llmIncomplete)} scraped row(s) will be re-sent to the LLM `
      + `(no provider re-spend — their scrape is already stored)`);
  }
  return el("section", { class: "detail-section detail-action" },
    sectionHeading("Rerun failed"),
    el("div", { class: "detail-section-body" },
      el("p", { class: "text-xs text-slate-400" },
        detail.length
          ? `${detail.join("; ")}. Rows that already have a result are skipped entirely.`
          : "Re-drives this run. Every row that already has a result is skipped, so "
            + "answered rows cost nothing to leave in place."),
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
  const hasOutcome = s.outcome_breakdown != null;
  const total = s.total_rows ?? s.entities_processed;
  // Newer AI payloads report an exclusive three-way outcome. Older payloads expose
  // websites_not_found inclusive of LLM errors, so subtract the error fallback once.
  const errors = safeCount(hasOutcome ? outcome.errored : s.llm_errors);
  const found = safeCount(hasOutcome ? outcome.found : s.websites_found);
  const notFound = hasOutcome
    ? safeCount(outcome.not_found)
    : Math.max(safeCount(s.websites_not_found) - errors, 0);
  const chips = [];
  if (s.model) chips.push(chip("Model", s.model, "muted"));
  if (s.is_batch != null) {
    chips.push(chip("Batch mode", s.is_batch ? "On" : "Off", s.is_batch ? "good" : "muted"));
  }
  const execution = [
    {
      label: "Total / Processed",
      value: total != null || s.entities_processed != null
        ? `${fmtNum(total)} / ${fmtNum(s.entities_processed)}` : null,
    },
    { label: "Duration", value: s.batch_duration_seconds == null ? null : fmtDuration(s.batch_duration_seconds) },
    { label: "Input tokens", value: s.token_usage?.prompt_tokens == null ? null : fmtNum(s.token_usage.prompt_tokens), tone: "muted" },
    { label: "Output tokens", value: s.token_usage?.completion_tokens == null ? null : fmtNum(s.token_usage.completion_tokens), tone: "muted" },
    { label: "Batch mode", value: s.is_batch == null ? null : s.is_batch ? "Yes" : "No" },
    {
      label: "Scrape.do requests",
      value: s.scrapedo_request_count == null ? null : fmtNum(s.scrapedo_request_count),
      detail: s.failed_request_count == null ? "" : `${fmtNum(s.failed_request_count)} failed`,
    },
  ];

  const parts = [
    headerCard(s.company_name || "—",
      `${s.mode_label ?? s.mode ?? "—"} · run ${ref}`, s.status, s.phase, chips),
    outcomeSummary({
      found,
      notFound,
      errors,
      total,
    }),
    executionStrip(execution),
    progressSection(s.batches_done ?? 0, s.batches_total ?? 0, running),
  ];
  if ((s.warnings ?? []).length) parts.push(warningsNote(s.warnings));
  if (s.error) {
    parts.push(el("div", { class: "callout callout-red" },
      el("p", { class: "detail-error text-sm" }, s.error)));
  }
  if (s.cost) {
    parts.push(costSection({ confidence_mode: "llm", model: s.model, cost: s.cost }, {
      providerLabel: "Scrape.do",
      providerCostKey: null,
      searchKey: "scrapedo_searches",
      failedSearchCount: s.scrapedo_failed_requests ?? s.failed_request_count ?? null,
      llmCostKey: s.cost?.llm_usd != null ? "llm_usd" : "total_usd",
      totalCostKey: null,
    }));
  }
  // Keep the availability surface visible throughout a run; unavailable files are disabled.
  parts.push(downloadsSection(ref, s.available_files));
  if (["failed", "completed_with_errors"].includes(s.status)) parts.push(rerunFailedSection(ref));

  root.replaceChildren(el("div", { class: "run-detail space-y-4" }, ...parts));
}

function renderLegacyStatus(root, ref, s) {
  const runState = deriveLegacyRunState(s);
  const g = s.serpwow_summary;
  const outcome = g?.outcome_breakdown ?? null;

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
  } else if (g?.llm_mode) {
    // A pipeline that uses an LLM for something OTHER than confidence — firmographics
    // normalises the AI Overview into its six fields. It reports confidence_mode: null
    // (it is handed the website, so there is nothing to be confident about), which used to
    // hide the fact that a paid model ran at all, and whether batched or not.
    chips.push(chip("LLM", g.llm_mode === "batch" ? "Batch" : "Inline",
      g.llm_mode === "batch" ? "good" : "info"));
    if (g.model) chips.push(chip("Model", g.model, "muted"));
  }
  // Which provider is actually working right now. `phase` is served by the
  // counter-driven status endpoint; without this the run looked identical whether
  // scrape.do was mid-flight, Gemini was chewing a batch, or nothing was running at all.
  const PHASE_LABELS = {
    queued: ["Queued", "muted"],
    scraping: ["Scraping (scrape.do)", "info"],
    cleaning: ["LLM (Gemini batch)", "info"],
    reporting: ["Writing outputs", "info"],
    completed: ["Done", "good"],
    stopped: ["Stopped", "warning"],
    failed: ["Failed", "danger"],
  };
  if (s.phase && PHASE_LABELS[s.phase]) {
    const [label, tone] = PHASE_LABELS[s.phase];
    chips.push(chip("Phase", label, tone));
  }
  const phaseSecs = g?.phase_seconds;
  if (phaseSecs && (phaseSecs.scraping || phaseSecs.cleaning)) {
    chips.push(chip("scrape.do", fmtDuration(phaseSecs.scraping ?? 0), "muted"));
    chips.push(chip("LLM", fmtDuration(phaseSecs.cleaning ?? 0), "muted"));
  }
  // Row-parallel pipelines have no run-level phase split, so their time is reported as a
  // per-row AVERAGE. Labelled "/row" so it is not read as wall clock: summing it across
  // rows at high concurrency would exceed the run's own duration.
  const avgSecs = g?.phase_seconds_avg;
  if (avgSecs && (avgSecs.provider || avgSecs.llm)) {
    chips.push(chip("scrape.do/row", fmtDuration(avgSecs.provider ?? 0), "muted"));
    chips.push(chip("LLM/row", fmtDuration(avgSecs.llm ?? 0), "muted"));
  }

  const isRel = s.pipeline === "relationship";
  const total = s.total_rows;
  // Canonical reporting outcomes are already exclusive and original-row-level.
  // Older reporting payloads omit the block and expose inclusive not-found counts.
  const errors = safeCount(outcome ? outcome.errored : s.failed_rows);
  const found = safeCount(outcome ? outcome.found : (g ? g.websites_found : s.success_rows));
  const notFound = outcome
    ? safeCount(outcome.not_found)
    : (g ? Math.max(safeCount(g.websites_not_found) - errors, 0) : null);
  const execution = [
    {
      label: "Total / Processed",
      value: total != null || s.processed_rows != null
        ? `${fmtNum(total)} / ${fmtNum(s.processed_rows)}` : null,
      detail: "Rows",
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
    { label: "Batch job", value: runState.batchStatus, tone: runState.finalizing ? "warning" : "default" },
  ];

  const timestamp = s.updated_at ?? s.created_at;
  const timestampLabel = s.updated_at ? "Updated" : "Created";
  const context = [`Run ${ref}`];
  if (timestamp) context.push(`${timestampLabel} ${shortDate(timestamp)}`);

  const parts = [
    headerCard(pipelineLabel(s.pipeline), context.join(" · "),
      runState.finalizing ? "running" : s.status,
      runState.finalizing ? "finalizing" : null, chips),
    outcomeSummary({
      found,
      notFound,
      errors,
      total,
      skipped: null,
      failureLabel: g ? "Errors" : "Failed",
      primaryLabel: g ? "Websites found" : "Succeeded",
    }),
    executionStrip(execution),
  ];
  if ((s.warnings ?? []).length) parts.push(warningsNote(s.warnings));
  if (s.error) {
    parts.push(el("div", { class: "callout callout-red" },
      el("p", { class: "detail-error text-sm" }, s.error)));
  }
  // Data first, pipeline as the tie-break. Truthy, not != null: the cost block carries
  // these keys as 0 for SerpWow pipelines too, and checking failed/credits as well keeps
  // an all-failed run on the Scrape.do card. But zero is also what every scrape.do run
  // reads BEFORE its first billed call lands, which labelled an in-flight gmaps run
  // "SerpWow" — so a pipeline that only ever talks to scrape.do says so even at zero.
  // The data check still comes first, so a gmaps run predating the migration keeps
  // rendering its real serpwow_searches/serpwow_usd card.
  if (g) {
    const isScrapedo = !!(g.cost?.scrapedo_requests || g.cost?.scrapedo_credits
                          || g.cost?.scrapedo_failed_requests)
      || (SCRAPEDO_PIPELINES.has(s.pipeline) && !g.cost?.serpwow_searches);
    parts.push(isScrapedo
      ? costSection(g, {
          providerLabel: "Scrape.do",
          providerCostKey: null,
          searchKey: "scrapedo_credits",
          searchUnit: "credits",
          // Real errors ONLY — attempts on rows that failed after every retry. A 502
          // that recovered on retry, or one meaning "Google has no listing", is not a
          // failure, and counting either here would make a clean run look broken.
          failedSearchCount: g.cost.scrapedo_error_requests ?? 0,
          totalCostKey: null,
        })
      : costSection(g));
  }
  if (isRel && g?.relationship_breakdown) parts.push(verdictSection(g.relationship_breakdown));
  if (g?.empty_response_breakdown) parts.push(emptyResponsesSection(g));
  if (runState.pollTerminal && errors > 0) {
    parts.push(failedRowsSection(ref, errors, isRel ? "Company Y" : "Company"));
  }
  // task_errors are shard/row TASKS that raised — a whole Gemini shard dying is the
  // common one, and it strands rows without making any of them an "error" row. Without
  // this the run said completed_with_errors while every visible count read zero.
  const taskErrors = safeCount(g?.error_breakdown?.task_errors);
  const llmIncomplete = safeCount(g?.empty_response_breakdown?.llm_incomplete);
  if (runState.pollTerminal && taskErrors > 0) {
    parts.push(el("div", { class: "callout callout-amber" },
      el("p", { class: "text-xs" },
        `${fmtNum(taskErrors)} background task(s) failed during this run`
        + (llmIncomplete
            ? ` — ${fmtNum(llmIncomplete)} row(s) were scraped but never got an LLM result.`
              + " Rerun below redoes only the LLM half; the scrape is already paid for."
            : ". See report.json error_breakdown for detail."))));
  }
  // Offered on any terminal run of these pipelines: a re-drive skips every row that has a
  // result, so the button is safe even when there is nothing to redo.
  // SCRAPEDO_PIPELINES, not NO_STATE_PIPELINES: the question is "does
  // /uploads/{id}/retry-failed-rows find an S3 run for this id", and that is exactly the
  // three run-per-message pipelines. (NO_STATE_PIPELINES omits gmaps — it is about which
  // /output links to advertise, a different question.)
  if (runState.pollTerminal && SCRAPEDO_PIPELINES.has(s.pipeline)) {
    parts.push(rerunRunSection(ref, { llmIncomplete, errors }));
  }

  // Stop button while the run is still doing work (rows in flight, or the
  // Gemini batch still running). Remaining rows are marked failed; retryable
  // later via "Retry failed rows".
  if (runState.canStop) {
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
          _scheduleReload();
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
  if (runState.filesReady) {
    const resultUrl = (name) => `/uploads/${encodeURIComponent(ref)}/result?file=${encodeURIComponent(name)}`;
    // retry.csv is the rerun/refund list, written only by the two S3-only pipelines —
    // gsearch shares this branch and never produces one, so don't advertise it there.
    // retry.csv is the rerun/refund list, written only by the S3-only pipelines — gsearch
    // shares this branch and never produces one, so don't advertise it there.
    const retryFile = ["gmaps", "relationship", "firmographics"].includes(s.pipeline)
      ? ["retry.csv"] : [];
    // enriched/notEnriched for firmographics: it is HANDED the website, so a found/notFound
    // split would name a discovery result it never computed.
    const PAIRS = {
      relationship: ["confirmed_relation.csv", "notconfirmed_relation.csv"],
      firmographics: ["enriched.csv", "notEnriched.csv"],
    };
    const resultFiles = (runState.reporting && runState.batchTerminal)
      ? [...(PAIRS[s.pipeline] ?? ["found.csv", "notFound.csv"]), ...retryFile,
         "report.json", "run.log"]
      : [];
    // The S3-only pipelines are counter-driven: there is no state.json to build
    // output.json (or its CSV) from, so both endpoints 404. Their per-row detail lives in
    // the result CSVs above — don't advertise two links that cannot work.
    const extras = NO_STATE_PIPELINES.has(s.pipeline) ? [] : [
      { name: "output.json", href: `/uploads/${encodeURIComponent(ref)}/output?download=true` },
      // CSV, not XLSX: it opens in Excel just the same (the bytes carry a UTF-8 BOM) and
      // is the format the per-row output is actually loaded from. ?format=xlsx still works
      // for anyone with the old link, it just isn't advertised.
      { name: "output.csv", href: `/uploads/${encodeURIComponent(ref)}/output?format=csv&download=true` },
    ];
    parts.push(filesSection(resultFiles, resultUrl, g?.available_files, extras));
  }
  root.replaceChildren(el("div", { class: "run-detail space-y-4" }, ...parts));
}

export async function render(root, params) {
  const ref = params.runRef;
  root.replaceChildren(loadingCard());
  let stop = null;

  // A run id alone doesn't say which engine owns it, so each candidate endpoint is
  // probed with a raw fetch (a 404 must route to the other one rather than be swallowed
  // by the poller's retry). Views that know the pipeline pass ?engine= so the right one
  // is tried FIRST and the wasted 404 disappears — but the hint is only an ordering,
  // never a requirement: bookmarked or hand-typed URLs carry no hint, and a stale one
  // still resolves via the second candidate.
  const candidates = [
    {
      engine: "ai",
      path: `/uploads/ai-mode/${encodeURIComponent(ref)}/status`,
      start: (p) => pollStatus(p, (s) => renderAiStatus(root, ref, s)),
    },
    {
      engine: "serpwow",
      path: `/uploads/${encodeURIComponent(ref)}/status`,
      start: (p) => pollStatus(p, (s) => renderLegacyStatus(root, ref, s), 2000,
                               (s) => deriveLegacyRunState(s).pollTerminal),
    },
  ];
  if (params.query?.engine === "serpwow") candidates.reverse();

  let failure = null;
  for (const candidate of candidates) {
    let probe;
    try {
      probe = await fetch(candidate.path);
    } catch (e) {
      failure = e.message;
      break;
    }
    if (probe.ok) {
      stop = candidate.start(candidate.path);
      break;
    }
    if (probe.status !== 404) {
      failure = probe.statusText;
      try { failure = (await probe.json()).detail ?? failure; } catch {}
      break;
    }
    // 404 → not this engine; fall through to the next candidate.
  }
  if (!stop) {
    root.replaceChildren(errorCard(failure ?? `Run "${ref}" was not found.`));
  }

  // The router invokes this before the next view renders — stops the poller.
  return () => {
    if (stop) stop();
    _clearReloadTimer();
    closeFileModal();
  };
}
