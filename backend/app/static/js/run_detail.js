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
import { errorCard, loadingCard, statusBadge, fmtDuration, shortDate, copyCell } from "./ui.js";

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
    class: "flex-1 overflow-auto whitespace-pre-wrap break-words p-4 text-xs leading-5 text-slate-300 font-mono",
  });
  const loadingMsg = el("p", {
    class: "p-6 text-sm text-slate-400",
  }, "Loading…");
  const body = el("div", { class: "flex flex-col overflow-hidden" }, loadingMsg);
  const overlay = el("div", {
    class: "hidden fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4",
    onclick: (e) => { if (e.target === overlay) overlay.classList.add("hidden"); },
  },
    el("div", { class: "flex flex-col w-full max-w-4xl h-[80vh] rounded-xl border border-slate-700 bg-slate-900 shadow-2xl overflow-hidden" },
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
  const table = el("table", { class: "w-full border-collapse text-xs" },
    el("thead", {},
      el("tr", {},
        ...header.map((h) => el("th", {
          class: "sticky top-0 z-10 bg-slate-800 border border-slate-700 px-3 py-2 text-left font-semibold text-slate-100 whitespace-nowrap",
        }, h)),
      ),
    ),
    el("tbody", {},
      ...bodyRows.map((r, ri) => el("tr", { class: ri % 2 ? "bg-slate-900/40" : "" },
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

function statTile(label, value) {
  return el("div", { class: "metric-card" },
    el("p", { class: "metric-label" }, label),
    el("p", { class: "metric-value" }, value),
  );
}

function summaryPair(label, value) {
  const text = value == null || value === "" ? "-" : String(value);
  return el("div", { class: "panel-muted p-3" },
    el("dt", { class: "view-kicker" }, label),
    el("dd", { class: "mt-1 truncate text-sm font-semibold text-slate-50", title: text }, text),
  );
}

function headerCard(title, subtitle, status, phase) {
  const bits = [statusBadge(status)];
  if (status === "running" && phase) {
    bits.push(el("span", {
      class: "status-badge",
    }, phase));
  }
  return el("div", { class: "panel" },
    el("div", { class: "flex flex-wrap items-center justify-between gap-3" },
      el("div", {},
        el("p", { class: "text-base font-semibold text-slate-50" }, title),
        el("p", { class: "mt-0.5 section-copy" }, subtitle),
      ),
      el("div", { class: "flex items-center gap-2" }, ...bits),
    ),
  );
}

function progressCard(done, total, running) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return el("div", { class: "panel" },
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

function downloadsCard(ref, available) {
  const files = available?.length ? available : RESULT_FILES;
  const baseUrl = (name) => `/uploads/ai-mode/${encodeURIComponent(ref)}/result?file=${encodeURIComponent(name)}`;
  return el("div", { class: "panel" },
    el("h2", { class: "section-title" }, "Files"),
    el("div", { class: "mt-3 flex flex-col gap-2" },
      ...RESULT_FILES.map((name) => {
        const isAvailable = files.includes(name);
        return el("div", { class: "flex items-center gap-2" },
          el("span", { class: `w-40 shrink-0 font-mono text-xs ${isAvailable ? "text-slate-300" : "text-slate-600"}` }, name),
          el("button", {
            class: "btn-ghost min-h-0 px-3 py-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed",
            ...(isAvailable ? {} : { disabled: "" }),
            onclick: () => viewFile(baseUrl(name), name, `${baseUrl(name)}&download=true`),
          }, "View"),
          el("a", {
            class: `btn-ghost min-h-0 px-3 py-1 text-xs ${isAvailable ? "" : "pointer-events-none opacity-40"}`,
            ...(isAvailable ? { href: `${baseUrl(name)}&download=true`, download: name } : {}),
          }, "Download"),
        );
      }),
    ),
  );
}

function rerunFailedCard(ref) {
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
  return el("div", { class: "panel" },
    el("h2", { class: "section-title" }, "Rerun failed"),
    el("p", { class: "mt-1 text-xs text-slate-400" },
      "Re-runs this same run and redoes only what failed: Phase 1 (scrape) re-fetches "
      + "only batches that failed to scrape, Phase 2 (LLM cleanup) re-does only batches "
      + "that failed to clean. Successful scrapes and cleaned results are reused — no "
      + "scrape.do or LLM re-spend on them. (Not-found rows are final; use AI Mode Deep for those.)"),
    el("div", { class: "mt-3" }, btn), msg,
  );
}

function warningsNote(warnings) {
  return el("div", { class: "callout callout-amber" },
    ...warnings.map((w) => el("p", { class: "text-xs" }, w)));
}

function renderAiStatus(root, ref, s) {
  const running = ["queued", "running"].includes(s.status);
  const tiles = [
    statTile("Entities processed", `${fmtNum(s.entities_processed)} / ${fmtNum(s.total_rows)}`),
    statTile("Websites found", fmtNum(s.websites_found)),
    statTile("Not found", fmtNum(s.websites_not_found)),
    statTile("LLM errors", fmtNum(s.llm_errors)),
    statTile("Searches / failed",
      `${fmtNum(s.scrapedo_request_count)} / ${fmtNum(s.failed_request_count)}`),
    statTile("Input tokens", fmtNum(s.token_usage?.prompt_tokens)),
    statTile("Output tokens", fmtNum(s.token_usage?.completion_tokens)),
    statTile("Model", s.model ?? "—"),
    statTile("Batch mode", s.is_batch == null ? "—" : s.is_batch ? "Yes" : "No"),
    statTile("Searches", fmtNum(s.cost?.scrapedo_searches)),
    statTile("LLM cost", fmtUsd(s.cost?.total_usd)),
    statTile("Duration", fmtDuration(s.batch_duration_seconds)),
  ];

  const parts = [
    headerCard(s.company_name || "—",
      `${s.mode_label ?? s.mode ?? "—"} · run ${ref}`, s.status, s.phase),
    progressCard(s.batches_done ?? 0, s.batches_total ?? 0, running),
  ];
  if ((s.warnings ?? []).length) parts.push(warningsNote(s.warnings));
  parts.push(el("div", { class: "grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4" }, ...tiles));
  if (s.error) {
    parts.push(el("div", { class: "callout callout-red" },
      el("p", { class: "text-sm" }, s.error)));
  }
  parts.push(downloadsCard(ref, s.available_files));
  if (["failed", "completed_with_errors"].includes(s.status)) parts.push(rerunFailedCard(ref));

  root.replaceChildren(el("div", { class: "space-y-4" }, ...parts));
}

function renderLegacyStatus(root, ref, s) {
  const rowsDone = ["completed", "completed_with_errors"].includes(String(s.status ?? ""));
  const outputJson = `/uploads/${encodeURIComponent(ref)}/output?download=true`;
  const outputXlsx = `/uploads/${encodeURIComponent(ref)}/output?format=xlsx&download=true`;
  const parts = [
    headerCard(`Upload ${ref}`, `${s.pipeline ?? "—"} (legacy SerpWow pipeline)`, s.status),
    el("div", { class: "grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4" },
      statTile("Total rows", fmtNum(s.total_rows)),
      statTile("Processed", fmtNum(s.processed_rows)),
      statTile("Succeeded", fmtNum(s.success_rows)),
      statTile("Failed", fmtNum(s.failed_rows)),
      statTile("Processing time", fmtDuration(s.processing_seconds_total)),
      statTile("Avg / row", fmtDuration(s.processing_seconds_avg)),
    ),
  ];
  const fileLinks = s.file_links && typeof s.file_links === "object" ? s.file_links : null;
  if (fileLinks) {
    parts.push(el("div", { class: "panel" },
      el("h2", { class: "section-title" }, "Artifacts"),
      el("div", { class: "mt-3 grid grid-cols-1 gap-3 md:grid-cols-2" },
        ...Object.entries(fileLinks).map(([name, path]) =>
          el("div", { class: "panel-muted p-3" },
            el("p", { class: "view-kicker" }, name),
            copyCell(String(path)),
          )),
      ),
    ));
  }
  parts.push(el("div", { class: "panel" },
    el("p", { class: "text-xs text-slate-400" },
      `SerpWow pipeline run - row-level detail and outputs are available via the API status endpoint at /uploads/${ref}/status.`),
  ));
  parts.push(el("div", { class: "panel" },
    el("div", { class: "flex flex-wrap items-center justify-between gap-3" },
      el("div", {},
        el("p", { class: "view-kicker" }, "Run summary"),
        el("h2", { class: "mt-1 section-title" }, "SerpWow upload snapshot"),
      ),
      el("div", { class: "flex flex-wrap gap-2" },
        rowsDone
          ? el("a", { class: "btn-ghost min-h-0 px-3 py-1.5 text-xs", href: outputJson }, "Download JSON")
          : el("span", { class: "text-xs text-slate-500" }, "Downloads available after completion"),
        rowsDone
          ? el("a", { class: "btn-ghost min-h-0 px-3 py-1.5 text-xs", href: outputXlsx }, "Download XLSX")
          : "",
      ),
    ),
    el("dl", { class: "mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4" },
      summaryPair("Mode", s.pipeline ?? "-"),
      summaryPair("Status", s.status ?? "-"),
      summaryPair("Total", fmtNum(s.total_rows)),
      summaryPair("Processed", fmtNum(s.processed_rows)),
      summaryPair("Success", fmtNum(s.success_rows)),
      summaryPair("Failed", fmtNum(s.failed_rows)),
      summaryPair("Time total", fmtDuration(s.processing_seconds_total)),
      summaryPair("Avg / row", fmtDuration(s.processing_seconds_avg)),
      summaryPair("Updated", shortDate(s.updated_at)),
      summaryPair("Storage", fileLinks ? Object.values(fileLinks).join(" | ") : "-"),
    ),
  ));
  root.replaceChildren(el("div", { class: "space-y-4" }, ...parts));
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
