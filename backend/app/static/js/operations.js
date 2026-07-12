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
import {
  errorCard,
  head,
  cell,
  shortDate,
  fmtDuration,
  copyCell,
  pageIntro,
  sectionHeading,
} from "./ui.js";

const REFRESH_MS = 4000; // legacy refreshed the batch tab on a 4s timer

// ---- pills (legacy statusClass/batchClass → shared .pill tones) ------------
const PILL_CLS = {
  done: "pill--good",
  error: "pill--danger",
  running: "pill--info",
  warn: "pill--warn",
  "": "pill--muted",
};

const pill = (label, kind) =>
  el("span", {
    class: `pill ${PILL_CLS[kind] ?? PILL_CLS[""]}`,
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
    cancelled: "Batch Cancelled",
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
  if (key === "cancelled") return "";
  if (key === "cancel_requested") return "warn";
  if (key === "failed" || key === "skipped") return "error";
  if (key === "waiting_for_rows" || key === "queued" || key === "running") return "running";
  return "";
}

const shortId = (id) =>
  id.length > 20 ? `${id.slice(0, 8)}...${id.slice(-8)}` : id;

const inputCls = "control w-full px-3 py-2 text-sm";

const actionBtnCls = "btn-ghost min-h-0 px-2.5 py-1 text-xs disabled:opacity-50";

const HISTORY_PIPELINES = [
  { key: "relationship", label: "Relationship" },
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
  const tbody = el("tbody", {});
  const empty = el("p", { class: "empty-state hidden" },
    "No SerpWow uploads found.");
  const note = el("p", { class: "message message--muted", "aria-live": "polite" },
    "Showing all SerpWow uploads. Storage column shows S3 paths when S3_BUCKET is configured.");
  const errorArea = el("div", { class: "operations-feedback hidden" });

  function renderRows(items) {
    empty.classList.toggle("hidden", items.length > 0);
    tbody.replaceChildren(...items.map((item) => {
      const uploadId = String(item.upload_id ?? "");
      const rowsDone = ["completed", "completed_with_errors"].includes(String(item.status ?? ""));
      const batch = item.gemini_batch?.status ?? "not_started";
      const batchTerminal = ["not_started", "succeeded", "failed", "skipped", "cancelled"].includes(String(batch));
      const ready = ["full", "gsearch", "relationship"].includes(item.pipeline)
        ? rowsDone && batchTerminal : rowsDone;
      return el("tr", {
        class: "data-row",
      },
        cell(uploadId ? el("a", {
          class: "table-link font-mono text-xs",
          href: `#/runs/${encodeURIComponent(uploadId)}`,
          title: uploadId,
          "aria-label": `Open run ${uploadId}`,
        }, shortId(uploadId)) : "-"),
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

  const card = el("section", { class: "detail-section operations-section" },
    sectionHeading(
      "SerpWow Uploads History",
      "All modes in one table: progress, timing, downloads, and artifact storage paths.",
      refreshBtn,
    ),
    el("div", { class: "detail-section-body" },
      errorArea,
      el("div", { class: "table-shell" },
      el("div", { class: "table-scroll" },
        el("table", { class: "data-table operations-table" },
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
    ),
  );

  const timer = setInterval(refresh, REFRESH_MS);
  registerCleanup(() => clearInterval(timer));
  return { card, refresh };
}

// ---- Batch Manager ----------------------------------------------------------
function batchManagerCard() {
  const note = el("p", { class: "message message--muted", "aria-live": "polite" },
    "Use actions to fetch live status or cancel a Gemini batch job.");
  const tbody = el("tbody", {});
  const empty = el("p", { class: "empty-state hidden" },
    "No Gemini batch jobs found.");
  const errorArea = el("div", { class: "operations-feedback hidden" });

  const setNote = (msg, isError = false) => {
    note.textContent = msg;
    note.className = isError ? "message message--danger" : "message message--muted";
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
      return el("tr", { class: "data-row" },
        cell(el("span", { class: "font-mono text-xs", title: uploadId }, uploadId ? shortId(uploadId) : "-")),
        cell(pill(statusLabel(item.upload_status), statusClass(item.upload_status))),
        cell(pill(batchLabel(item.batch_status), batchClass(item.batch_status))),
        cell(pill(String(item.live_state ?? "-") || "-", batchClass(item.live_state))),
        cell(el("span", { class: "font-mono text-xs", title: jobName }, jobName || "-")),
        cell(shortDate(item.updated_at), "whitespace-nowrap text-slate-400"),
        cell(el("div", { class: "action-group operations-actions" },
          actionBtn("Get Status", "status", jobName),
          actionBtn("Cancel", "cancel", jobName, "btn-warning"),
          actionBtn("Delete", "delete", jobName, "btn-danger"),
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

  const card = el("section", { class: "detail-section operations-section" },
    sectionHeading("Batch Manager", "Gemini batch jobs for full-pipeline uploads.", refreshBtn),
    el("div", { class: "detail-section-body" },
      errorArea,
      el("div", { class: "table-shell" },
      el("div", { class: "table-scroll" },
      el("table", { class: "data-table operations-table" },
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
    ),
  );

  return { card, refresh };
}

// ---- Retry Operations ---------------------------------------------------------
function retryCard(registerCleanup) {
  const meta = el("p", { class: "message message--muted", "aria-live": "polite" },
    "Ready to trigger manual retry.");
  const setMeta = (msg, tone = "muted") => {
    meta.textContent = msg;
    meta.className = `message message--${tone}`;
  };

  const input = el("input", {
    class: inputCls, type: "text",
    id: "retry-upload-id",
    placeholder: "e.g. fb2884b0-f38c-4776-bf7f-582028f59522",
  });

  let stopPoll = null;
  registerCleanup(() => { if (stopPoll) stopPoll(); });

  const btn = el("button", { class: "btn-primary disabled:opacity-50" }, "Retry Failed Rows");

  btn.addEventListener("click", async () => {
    const uid = input.value.trim();
    if (!uid) {
      setMeta("Error: Upload ID is required.", "danger");
      return;
    }
    btn.disabled = true;
    setMeta(`Triggering retry for ${uid}…`, "info");
    try {
      const data = await api(`/uploads/${encodeURIComponent(uid)}/retry-failed-rows`, { method: "POST" });
      setMeta(`Success! Enqueued ${data.enqueued_rows ?? 0} failed rows for processing.`, "good");
      // Legacy polled /uploads/{id}/status after the retry; keep that, shown inline.
      if (stopPoll) stopPoll();
      stopPoll = pollStatus(`/uploads/${encodeURIComponent(uid)}/status`, (status) => {
        const batch = status.gemini_batch?.status ?? "not_started";
        setMeta(
          `Upload ${uid} — status: ${status.status ?? "-"} | rows: ${status.processed_rows ?? 0}/${status.total_rows ?? 0}` +
          ` (ok ${status.success_rows ?? 0}, failed ${status.failed_rows ?? 0}) | batch: ${batch}`,
          "muted",
        );
      });
    } catch (e) {
      setMeta(`Failed: ${e.message}`, "danger");
    } finally {
      btn.disabled = false;
    }
  });

  return el("section", { class: "detail-section operations-section" },
    sectionHeading(
      "Retry Operations",
      "Manually retry all failed, queued, or stuck processing rows for a specific upload ID instantly.",
    ),
    el("div", { class: "detail-section-body retry-control-group" },
      el("label", { class: "filter-label", for: "retry-upload-id" }, "Upload ID *"),
      input,
      btn,
      meta,
    ),
  );
}

// ---- view ---------------------------------------------------------------------
export async function render(root) {
  const cleanups = [];
  const registerCleanup = (fn) => cleanups.push(fn);

  const history = uploadHistoryCard(registerCleanup);
  const batch = batchManagerCard();
  root.replaceChildren(
    el("div", { class: "operations-view" },
      pageIntro(
        "Operations",
        "Run and batch ledger",
        "Monitor upload history, manage Gemini batch jobs, and retry failed rows without leaving the console.",
      ),
      history.card,
      batch.card,
      retryCard(registerCleanup),
    ),
  );

  await history.refresh();
  await batch.refresh();
  const timer = setInterval(batch.refresh, REFRESH_MS);
  registerCleanup(() => clearInterval(timer));

  return () => cleanups.forEach((fn) => fn());
}
