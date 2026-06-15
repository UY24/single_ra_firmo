// backend/app/static/js/operations.js — Batch Manager + Retry Operations.
// Ported from the legacy ui.html tabs of the same names: identical endpoints,
// request fields and displayed info — only the markup is new.
//   Batch Manager:  GET  /batch/jobs?limit=300
//                   POST /batch/jobs/status?job_name=…
//                   POST /batch/jobs/cancel?job_name=…
//                   POST /batch/jobs/delete?job_name=…
//   Retry:          POST /uploads/{id}/retry-failed-rows
//                   GET  /uploads/{id}/status   (poll after a retry)
import { api, el, pollStatus } from "./api.js";
import { errorCard, head, cell, shortDate, fmtDuration, copyCell } from "./ui.js";

const REFRESH_MS = 4000; // legacy refreshed the batch tab on a 4s timer

// ---- pills (legacy statusClass/batchClass → tailwind tones) ----------------
const PILL_CLS = {
  done: "bg-emerald-100 text-emerald-800",
  error: "bg-red-100 text-red-800",
  running: "bg-indigo-100 text-indigo-800",
  "": "bg-gray-100 text-gray-600",
};

const pill = (label, kind) =>
  el("span", {
    class: `inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${PILL_CLS[kind] ?? PILL_CLS[""]}`,
  }, label);

function statusLabel(status) {
  const map = {
    queued: "Queued",
    processing: "Processing",
    completed: "Completed",
    completed_with_errors: "Completed With Errors",
  };
  return map[String(status ?? "")] || String(status ?? "-") || "-";
}

function statusClass(status) {
  const key = String(status ?? "");
  if (key === "completed") return "done";
  if (key === "completed_with_errors") return "error";
  if (key === "queued" || key === "processing") return "running";
  return "";
}

function batchLabel(status) {
  const map = {
    waiting_for_rows: "Batch Pending",
    queued: "Batch Queued",
    running: "Batch Running",
    cancel_requested: "Cancel Requested",
    succeeded: "Batch Done",
    failed: "Batch Failed",
    skipped: "Batch Skipped",
    not_started: "Batch N/A",
  };
  return map[String(status ?? "")] || `Batch ${String(status ?? "-") || "-"}`;
}

function batchClass(status) {
  const key = String(status ?? "");
  if (key === "succeeded") return "done";
  if (key === "failed" || key === "skipped" || key === "cancel_requested") return "error";
  if (key === "waiting_for_rows" || key === "queued" || key === "running") return "running";
  return "";
}

const shortId = (id) =>
  id.length > 20 ? `${id.slice(0, 8)}...${id.slice(-8)}` : id;

const inputCls = "control w-full px-3 py-2 text-sm";

const actionBtnCls = "btn-ghost min-h-0 px-2.5 py-1 text-xs disabled:opacity-50";

const HISTORY_PIPELINES = [
  { key: "gmaps", label: "Google Maps" },
  { key: "gsearch", label: "Google Search" },
  { key: "full", label: "Full" },
  { key: "url_discovery", label: "URL Discovery" },
  { key: "firmographics", label: "Firmographics" },
];

const pipelineLabel = (pipeline) =>
  HISTORY_PIPELINES.find((p) => p.key === pipeline)?.label ?? String(pipeline ?? "-");

function storageCell(fileLinks) {
  const links = fileLinks && typeof fileLinks === "object" ? fileLinks : {};
  const stateUrl = String(links["state.json"] || "");
  const outputUrl = String(links["output.json"] || "");
  return el("div", { class: "flex flex-col gap-1" },
    copyCell(stateUrl || "—"),
    copyCell(outputUrl || "—"),
  );
}

function downloadButton(uploadId, label, format) {
  return el("a", {
    class: "btn-ghost min-h-0 px-2.5 py-1 text-xs",
    href: `/uploads/${encodeURIComponent(uploadId)}/output${format === "xlsx" ? "?format=xlsx&download=true" : "?download=true"}`,
    onclick: (ev) => ev.stopPropagation(),
  }, label);
}

function uploadHistoryCard(registerCleanup) {
  const tbody = el("tbody", { class: "divide-y divide-gray-100" });
  const empty = el("p", { class: "hidden p-6 text-center text-sm text-gray-400" },
    "No SerpWow uploads found.");
  const note = el("p", { class: "mt-3 text-xs text-gray-400" },
    "Showing all SerpWow uploads. Storage column shows S3 paths when S3_BUCKET is configured.");
  const errorArea = el("div", { class: "mt-3 hidden" });

  function renderRows(items) {
    empty.classList.toggle("hidden", items.length > 0);
    tbody.replaceChildren(...items.map((item) => {
      const uploadId = String(item.upload_id ?? "");
      const rowsDone = ["completed", "completed_with_errors"].includes(String(item.status ?? ""));
      const batch = item.gemini_batch?.status ?? "not_started";
      const batchTerminal = ["not_started", "succeeded", "failed", "skipped"].includes(String(batch));
      const ready = item.pipeline === "full" ? rowsDone && batchTerminal : rowsDone;
      return el("tr", {
        class: "cursor-pointer hover:bg-gray-50/50",
        onclick: () => { window.location.hash = `#/runs/${encodeURIComponent(uploadId)}`; },
      },
        cell(el("span", { class: "font-mono text-xs", title: uploadId }, uploadId ? shortId(uploadId) : "-")),
        cell(pill(pipelineLabel(item.pipeline), "")),
        cell(pill(statusLabel(item.status), statusClass(item.status))),
        cell(Number(item.total_rows ?? 0).toLocaleString(), "text-right"),
        cell(Number(item.processed_rows ?? 0).toLocaleString(), "text-right"),
        cell(Number(item.success_rows ?? 0).toLocaleString(), "text-right"),
        cell(Number(item.failed_rows ?? 0).toLocaleString(), "text-right"),
        cell(fmtDuration(item.processing_seconds_total), "text-right"),
        cell(fmtDuration(item.processing_seconds_avg), "text-right"),
        cell(shortDate(item.updated_at), "whitespace-nowrap text-slate-400"),
        cell(storageCell(item.file_links)),
        cell(ready ? downloadButton(uploadId, "JSON", "json") : el("span", { class: "text-xs text-slate-500" }, "Processing")),
        cell(ready ? downloadButton(uploadId, "XLSX", "xlsx") : el("span", { class: "text-xs text-slate-500" }, "Processing")),
      );
    }));
  }

  async function refresh() {
    try {
      const data = await api("/uploads?limit=200");
      errorArea.classList.add("hidden");
      errorArea.replaceChildren();
      renderRows(Array.isArray(data.uploads) ? data.uploads : []);
      note.textContent = "Showing all SerpWow uploads. Select a row to open its run detail.";
    } catch (e) {
      errorArea.classList.remove("hidden");
      errorArea.replaceChildren(errorCard(e.message));
    }
  }

  const refreshBtn = el("button", {
    class: "btn-ghost min-h-0 px-3 py-1.5 text-xs",
    onclick: refresh,
  }, "Refresh");

  const card = el("div", { class: "panel" },
    el("div", { class: "flex flex-wrap items-center justify-between gap-3" },
      el("div", {},
        el("h2", { class: "section-title" }, "SerpWow Uploads History"),
        el("p", { class: "mt-1 section-copy" },
          "All modes in one table: progress, timing, downloads, and artifact storage paths."),
      ),
      refreshBtn,
    ),
    errorArea,
    el("div", { class: "table-shell mt-4" },
      el("div", { class: "table-scroll" },
        el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
          el("thead", {},
            el("tr", {},
              head("Upload ID"), head("Mode"), head("Status"),
              head("Total", "text-right"), head("Processed", "text-right"),
              head("Success", "text-right"), head("Failed", "text-right"),
              head("Time Total", "text-right"), head("Avg/row", "text-right"),
              head("Updated"), head("Storage"), head("Output JSON"), head("Output XLSX"),
            ),
          ),
          tbody,
        ),
      ),
      empty,
    ),
    note,
  );

  const timer = setInterval(refresh, REFRESH_MS);
  registerCleanup(() => clearInterval(timer));
  return { card, refresh };
}

// ---- Batch Manager ----------------------------------------------------------
function batchManagerCard() {
  const note = el("p", { class: "mt-3 text-xs text-gray-400" },
    "Use actions to fetch live status or cancel a Gemini batch job.");
  const tbody = el("tbody", { class: "divide-y divide-gray-100" });
  const empty = el("p", { class: "hidden p-6 text-center text-sm text-gray-400" },
    "No Gemini batch jobs found.");
  const errorArea = el("div", { class: "mt-3 hidden" });

  const setNote = (msg, isError = false) => {
    note.textContent = msg;
    note.className = isError ? "mt-3 text-xs text-red-600" : "mt-3 text-xs text-gray-400";
  };

  async function handleAction(action, jobName, btn) {
    if (action === "cancel" && !confirm(`Cancel Gemini batch ${jobName}?`)) return;
    if (action === "delete" && !confirm(`Delete Gemini batch ${jobName}? This cannot be undone.`)) return;

    btn.disabled = true;
    try {
      const data = await api(`/batch/jobs/${action}?job_name=${encodeURIComponent(jobName)}`,
        { method: "POST" });
      if (action === "status") {
        setNote(`Live batch status for ${jobName}: ${data.live_state ?? "-"} (done=${Boolean(data.done)})`);
      } else if (action === "delete") {
        setNote(`Delete requested for ${jobName}.`);
      } else {
        setNote(`Cancel requested for ${jobName}.`);
      }
    } catch (e) {
      setNote(`Action failed (${action}) for ${jobName}: ${e.message}`, true);
    } finally {
      btn.disabled = false;
      await refresh();
    }
  }

  const actionBtn = (label, action, jobName, extra = "") => {
    const btn = el("button", { class: `${actionBtnCls} ${extra}`, type: "button" }, label);
    btn.addEventListener("click", () => handleAction(action, jobName, btn));
    return btn;
  };

  function renderRows(items) {
    empty.classList.toggle("hidden", items.length > 0);
    tbody.replaceChildren(...items.map((item) => {
      const uploadId = String(item.upload_id ?? "");
      const jobName = String(item.job_name ?? "");
      return el("tr", { class: "hover:bg-gray-50/50" },
        cell(el("span", { class: "font-mono text-xs", title: uploadId }, uploadId ? shortId(uploadId) : "-")),
        cell(pill(statusLabel(item.upload_status), statusClass(item.upload_status))),
        cell(pill(batchLabel(item.batch_status), batchClass(item.batch_status))),
        cell(pill(String(item.live_state ?? "-") || "-", batchClass(item.live_state))),
        cell(el("span", { class: "font-mono text-xs", title: jobName }, jobName || "-")),
        cell(shortDate(item.updated_at), "whitespace-nowrap text-gray-400"),
        cell(el("div", { class: "flex gap-1.5" },
          actionBtn("Get Status", "status", jobName),
          actionBtn("Cancel", "cancel", jobName, "text-amber-700 border-amber-300 hover:bg-amber-50"),
          actionBtn("Delete", "delete", jobName, "text-red-700 border-red-300 hover:bg-red-50"),
        )),
      );
    }));
  }

  async function refresh() {
    try {
      const data = await api("/batch/jobs?limit=300");
      errorArea.classList.add("hidden");
      errorArea.replaceChildren();
      renderRows(Array.isArray(data.jobs) ? data.jobs : []);
    } catch (e) {
      errorArea.classList.remove("hidden");
      errorArea.replaceChildren(errorCard(e.message));
    }
  }

  const refreshBtn = el("button", {
    class: "btn-ghost min-h-0 px-3 py-1.5 text-xs",
    onclick: refresh,
  }, "Refresh");

  const card = el("div", { class: "panel" },
    el("div", { class: "flex items-center justify-between" },
      el("div", {},
        el("h2", { class: "section-title" }, "Batch Manager"),
        el("p", { class: "mt-1 section-copy" }, "Gemini batch jobs for full-pipeline uploads."),
      ),
      refreshBtn,
    ),
    errorArea,
    el("div", { class: "table-shell mt-4" },
      el("div", { class: "table-scroll" },
      el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
        el("thead", {},
          el("tr", {},
            head("Upload ID"), head("Upload Status"), head("Batch Status"),
            head("Live State"), head("Job Name"), head("Updated"), head("Actions"),
          ),
        ),
          tbody,
        ),
      ),
      empty,
    ),
    note,
  );

  return { card, refresh };
}

// ---- Retry Operations ---------------------------------------------------------
function retryCard(registerCleanup) {
  const meta = el("p", { class: "mt-4 text-sm text-gray-500" }, "Ready to trigger manual retry.");
  const setMeta = (msg, tone = "text-gray-500") => {
    meta.textContent = msg;
    meta.className = `mt-4 text-sm ${tone}`;
  };

  const input = el("input", {
    class: inputCls, type: "text",
    placeholder: "e.g. fb2884b0-f38c-4776-bf7f-582028f59522",
  });

  let stopPoll = null;
  registerCleanup(() => { if (stopPoll) stopPoll(); });

  const btn = el("button", { class: "btn-primary disabled:opacity-50" }, "Retry Failed Rows");

  btn.addEventListener("click", async () => {
    const uid = input.value.trim();
    if (!uid) {
      setMeta("Error: Upload ID is required.", "text-red-600");
      return;
    }
    btn.disabled = true;
    setMeta(`Triggering retry for ${uid}…`, "text-indigo-600");
    try {
      const data = await api(`/uploads/${encodeURIComponent(uid)}/retry-failed-rows`, { method: "POST" });
      setMeta(`Success! Enqueued ${data.enqueued_rows ?? 0} failed rows for processing.`, "text-emerald-600");
      // Legacy polled /uploads/{id}/status after the retry; keep that, shown inline.
      if (stopPoll) stopPoll();
      stopPoll = pollStatus(`/uploads/${encodeURIComponent(uid)}/status`, (status) => {
        const batch = status.gemini_batch?.status ?? "not_started";
        setMeta(
          `Upload ${uid} — status: ${status.status ?? "-"} | rows: ${status.processed_rows ?? 0}/${status.total_rows ?? 0}` +
          ` (ok ${status.success_rows ?? 0}, failed ${status.failed_rows ?? 0}) | batch: ${batch}`,
          "text-gray-600",
        );
      });
    } catch (e) {
      setMeta(`Failed: ${e.message}`, "text-red-600");
    } finally {
      btn.disabled = false;
    }
  });

  return el("div", { class: "panel" },
    el("h2", { class: "section-title" }, "Retry Operations"),
    el("p", { class: "mt-1 section-copy" },
      "Manually retry all failed, queued, or stuck processing rows for a specific upload ID instantly."),
    el("div", { class: "mt-4 flex max-w-xl flex-col gap-3" },
      el("label", { class: "view-kicker" }, "Upload ID *"),
      input,
      btn,
    ),
    meta,
  );
}

// ---- view ---------------------------------------------------------------------
export async function render(root) {
  const cleanups = [];
  const registerCleanup = (fn) => cleanups.push(fn);

  const history = uploadHistoryCard(registerCleanup);
  const batch = batchManagerCard();
  root.replaceChildren(
    el("div", { class: "space-y-6" }, history.card, batch.card, retryCard(registerCleanup)),
  );

  await history.refresh();
  await batch.refresh();
  const timer = setInterval(batch.refresh, REFRESH_MS);
  registerCleanup(() => clearInterval(timer));

  return () => cleanups.forEach((fn) => fn());
}
