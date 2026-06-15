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
import { errorCard, loadingCard, statusBadge, fmtDuration, shortDate } from "./ui.js";

const RESULT_FILES = ["final_report.json", "found.csv", "notFound.csv", "run.log", "input.csv"];

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
  return el("div", { class: "panel" },
    el("h2", { class: "section-title" }, "Downloads"),
    el("div", { class: "mt-3 flex flex-wrap gap-2" },
      ...RESULT_FILES.map((name) => el("button", {
        class: "btn-ghost min-h-0 px-3 py-1.5 text-xs disabled:opacity-40 disabled:cursor-not-allowed",
        ...(files.includes(name) ? {} : { disabled: "" }),
        onclick: () => {
          window.open(
            `/uploads/ai-mode/${encodeURIComponent(ref)}/result` +
            `?file=${encodeURIComponent(name)}&download=true`,
            "_blank");
        },
      }, name)),
    ),
  );
}

function rerunCard(ref) {
  const msg = el("div", { class: "mt-3 hidden" });
  const btn = el("button", {
    class: "btn-primary disabled:opacity-50",
    onclick: async () => {
      btn.disabled = true;
      msg.className = "mt-3";
      msg.replaceChildren(el("p", { class: "section-copy" }, "Starting re-run..."));
      try {
        const info = await api(`/uploads/ai-mode/${encodeURIComponent(ref)}/rerun`, { method: "POST" });
        msg.replaceChildren(el("p", { class: "text-sm font-semibold text-emerald-600" },
          `Re-run started (carried over ${fmtNum(info.carried_over)} rows). Redirecting...`));
        setTimeout(() => { window.location.hash = `#/runs/${encodeURIComponent(info.run_id)}`; }, 700);
      } catch (e) {
        btn.disabled = false; // 400/404/503 → inline detail
        msg.replaceChildren(el("p", { class: "text-sm text-red-600" }, e.message));
      }
    },
  }, "Re-run failed rows");
  return el("div", { class: "panel" },
    el("h2", { class: "section-title" }, "Re-run"),
    el("p", { class: "mt-1 text-xs text-slate-400" },
      "Retries failed/unscraped rows; successful results are carried over."),
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
    statTile("Tokens", fmtNum(s.token_usage?.total_tokens)),
    statTile("LLM cost", fmtUsd(s.cost?.total_usd)),
    statTile("Duration", fmtDuration(s.batch_duration_seconds)),
  ];
  if (Number(s.carried_over) > 0) {
    tiles.push(statTile("Carried over", fmtNum(s.carried_over)));
  }
  if (s.rerun_of_run_id) {
    tiles.push(el("a", {
      href: `#/runs/${encodeURIComponent(s.rerun_of_run_id)}`,
      class: "metric-card block transition hover:border-cyan-400/60",
    },
      el("p", { class: "metric-label" }, "Re-run of"),
      el("p", { class: "mt-1 truncate text-sm font-semibold text-indigo-600" }, s.rerun_of_run_id),
    ));
  }

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
  if (["failed", "completed_with_errors"].includes(s.status)) parts.push(rerunCard(ref));

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
            el("p", { class: "mt-1 truncate font-mono text-xs text-slate-400", title: String(path) },
              String(path)),
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
