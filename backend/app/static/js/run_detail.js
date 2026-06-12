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
import { errorCard, loadingCard, statusBadge } from "./ui.js";

const RESULT_FILES = ["final_report.json", "found.csv", "notFound.csv", "run.log", "input.csv"];

const fmtDuration = (s) => {
  if (s == null) return "—";
  const total = Math.round(Number(s));
  if (Number.isNaN(total)) return "—";
  const m = Math.floor(total / 60), sec = total % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
};

function statTile(label, value) {
  return el("div", { class: "rounded-xl border border-gray-200 bg-white p-4 shadow-sm" },
    el("p", { class: "text-xs text-gray-400" }, label),
    el("p", { class: "mt-1 text-lg font-semibold text-gray-900" }, value),
  );
}

function headerCard(title, subtitle, status, phase) {
  const bits = [statusBadge(status)];
  if (status === "running" && phase) {
    bits.push(el("span", {
      class: "inline-flex items-center rounded-full bg-indigo-50 px-2.5 py-0.5 " +
             "text-xs font-medium text-indigo-700",
    }, phase));
  }
  return el("div", { class: "rounded-xl border border-gray-200 bg-white p-5 shadow-sm" },
    el("div", { class: "flex flex-wrap items-center justify-between gap-3" },
      el("div", {},
        el("p", { class: "text-base font-semibold text-gray-900" }, title),
        el("p", { class: "mt-0.5 text-sm text-gray-500" }, subtitle),
      ),
      el("div", { class: "flex items-center gap-2" }, ...bits),
    ),
  );
}

function progressCard(done, total, running) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return el("div", { class: "rounded-xl border border-gray-200 bg-white p-5 shadow-sm" },
    el("div", { class: "flex items-center justify-between text-sm" },
      el("span", { class: "text-gray-500" }, "Batches"),
      el("span", { class: "font-medium text-gray-900" }, `${fmtNum(done)} / ${fmtNum(total)}`),
    ),
    el("div", { class: "mt-2 h-2 overflow-hidden rounded-full bg-gray-100" },
      el("div", {
        class: `h-full rounded-full bg-indigo-600 transition-all ${running ? "animate-pulse" : ""}`,
        style: `width: ${pct}%`,
      }),
    ),
  );
}

function downloadsCard(ref, available) {
  const files = available?.length ? available : RESULT_FILES;
  return el("div", { class: "rounded-xl border border-gray-200 bg-white p-5 shadow-sm" },
    el("h2", { class: "text-sm font-semibold text-gray-700" }, "Downloads"),
    el("div", { class: "mt-3 flex flex-wrap gap-2" },
      ...RESULT_FILES.map((name) => el("button", {
        class: "rounded-lg border border-gray-300 px-3 py-1.5 text-sm text-gray-700 " +
               "hover:border-indigo-400 hover:text-indigo-700 disabled:opacity-40 " +
               "disabled:cursor-not-allowed",
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
    class: "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white " +
           "hover:bg-indigo-500 disabled:opacity-50",
    onclick: async () => {
      btn.disabled = true;
      msg.className = "mt-3";
      msg.replaceChildren(el("p", { class: "text-sm text-gray-400" }, "Starting re-run…"));
      try {
        const info = await api(`/uploads/ai-mode/${encodeURIComponent(ref)}/rerun`, { method: "POST" });
        msg.replaceChildren(el("p", { class: "text-sm font-medium text-green-700" },
          `Re-run started (carried over ${fmtNum(info.carried_over)} rows). Redirecting…`));
        setTimeout(() => { window.location.hash = `#/runs/${encodeURIComponent(info.run_id)}`; }, 700);
      } catch (e) {
        btn.disabled = false; // 400/404/503 → inline detail
        msg.replaceChildren(el("p", { class: "text-sm text-red-600" }, e.message));
      }
    },
  }, "Re-run failed rows");
  return el("div", { class: "rounded-xl border border-gray-200 bg-white p-5 shadow-sm" },
    el("h2", { class: "text-sm font-semibold text-gray-700" }, "Re-run"),
    el("p", { class: "mt-1 text-xs text-gray-500" },
      "Retries failed/unscraped rows; successful results are carried over."),
    el("div", { class: "mt-3" }, btn), msg,
  );
}

function warningsNote(warnings) {
  return el("div", { class: "rounded-lg border border-amber-200 bg-amber-50 p-3" },
    ...warnings.map((w) => el("p", { class: "text-xs text-amber-800" }, w)));
}

function renderAiStatus(root, ref, s) {
  const running = ["queued", "running"].includes(s.status);
  const tiles = [
    statTile("Entities processed", `${fmtNum(s.entities_processed)} / ${fmtNum(s.total_rows)}`),
    statTile("Websites found", fmtNum(s.websites_found)),
    statTile("Not found", fmtNum(s.websites_not_found)),
    statTile("LLM errors", fmtNum(s.llm_errors)),
    statTile("scrape.do requests / failed",
      `${fmtNum(s.scrapedo_request_count)} / ${fmtNum(s.failed_request_count)}`),
    statTile("Tokens", fmtNum(s.token_usage?.total_tokens)),
    statTile("Cost", fmtUsd(s.cost?.total_usd)),
    statTile("Duration", fmtDuration(s.batch_duration_seconds)),
  ];
  if (Number(s.carried_over) > 0) {
    tiles.push(statTile("Carried over", fmtNum(s.carried_over)));
  }
  if (s.rerun_of_run_id) {
    tiles.push(el("a", {
      href: `#/runs/${encodeURIComponent(s.rerun_of_run_id)}`,
      class: "rounded-xl border border-gray-200 bg-white p-4 shadow-sm transition " +
             "hover:border-indigo-300",
    },
      el("p", { class: "text-xs text-gray-400" }, "Re-run of"),
      el("p", { class: "mt-1 truncate text-sm font-medium text-indigo-600" }, s.rerun_of_run_id),
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
    parts.push(el("div", { class: "rounded-lg border border-red-200 bg-red-50 p-3" },
      el("p", { class: "text-sm text-red-800" }, s.error)));
  }
  parts.push(downloadsCard(ref, s.available_files));
  if (["failed", "completed_with_errors"].includes(s.status)) parts.push(rerunCard(ref));

  root.replaceChildren(el("div", { class: "space-y-4" }, ...parts));
}

function renderLegacyStatus(root, ref, s) {
  root.replaceChildren(el("div", { class: "space-y-4" },
    headerCard(`Upload ${ref}`, `${s.pipeline ?? "—"} (legacy SerpWow pipeline)`, s.status),
    el("div", { class: "grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4" },
      statTile("Total rows", fmtNum(s.total_rows)),
      statTile("Processed", fmtNum(s.processed_rows)),
      statTile("Succeeded", fmtNum(s.success_rows)),
      statTile("Failed", fmtNum(s.failed_rows)),
      statTile("Processing time", fmtDuration(s.processing_seconds_total)),
      statTile("Avg / row", fmtDuration(s.processing_seconds_avg)),
    ),
    el("div", { class: "rounded-xl border border-gray-200 bg-white p-5 shadow-sm" },
      el("p", { class: "text-xs text-gray-500" },
        "SerpWow pipeline run — row-level detail and outputs are available in the legacy UI at /ui."),
    ),
  ));
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
