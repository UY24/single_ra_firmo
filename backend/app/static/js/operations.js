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
  runHref,
} from "./ui.js";

const REFRESH_MS = 4000; // legacy refreshed the batch tab on a 4s timer
const ROW_TERMINAL = new Set(["completed", "completed_with_errors", "failed"]);
const BATCH_TERMINAL = new Set([
  "not_started", "succeeded", "completed_with_errors", "failed", "skipped", "cancelled",
]);

const uploadIsTerminal = (status) => ROW_TERMINAL.has(String(status?.status ?? ""))
  && BATCH_TERMINAL.has(String(status?.gemini_batch?.status ?? "not_started"));

const isAbortError = (error) => error?.name === "AbortError";

function createLifecycle() {
  let mounted = true;
  const controllers = new Set();
  const cleanups = [];

  const startRequest = (path, opts = {}) => {
    const controller = new AbortController();
    controllers.add(controller);
    const promise = api(path, { ...opts, signal: controller.signal })
      .finally(() => controllers.delete(controller));
    return { controller, promise };
  };

  return {
    isMounted: () => mounted,
    registerCleanup: (fn) => cleanups.push(fn),
    startRequest,
    cleanup: () => {
      if (!mounted) return;
      mounted = false;
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
      cleanups.forEach((fn) => fn());
    },
  };
}

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
    completed_with_errors: "Batch Completed With Errors",
    failed: "Batch Failed",
    skipped: "Batch Skipped",
    not_started: "Batch N/A",
  };
  return map[String(status ?? "")] || `Batch ${String(status ?? "-") || "-"}`;
}

function batchClass(status) {
  const key = String(status ?? "");
  if (key === "succeeded") return "done";
  if (key === "completed_with_errors") return "warn";
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
    href: `/uploads/${encodeURIComponent(uploadId)}/output${format === "json" ? "?download=true" : `?format=${format}&download=true`}`,
    onclick: (ev) => ev.stopPropagation(),
  }, label);
}

function uploadHistoryCard(lifecycle) {
  const { isMounted, registerCleanup, startRequest } = lifecycle;
  let controller = null;
  let generation = 0;
  let timer = null;
  const tbody = el("tbody", {});
  const empty = el("p", { class: "empty-state hidden" },
    "No pipeline uploads found.");
  const note = el("p", { class: "message message--muted", "aria-live": "polite" },
    "Showing all pipeline uploads. Storage column shows S3 paths when S3_BUCKET is configured.");
  const errorArea = el("div", { class: "operations-feedback hidden" });

  function renderRows(items) {
    empty.classList.toggle("hidden", items.length > 0);
    tbody.replaceChildren(...items.map((item) => {
      const uploadId = String(item.upload_id ?? "");
      const ready = ROW_TERMINAL.has(String(item.status ?? ""))
        && BATCH_TERMINAL.has(String(item.gemini_batch?.status ?? "not_started"));
      return el("tr", {
        class: "data-row",
      },
        cell(uploadId ? el("a", {
          class: "table-link font-mono text-xs",
          href: runHref(uploadId, "serpwow"),
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
        cell(ready ? downloadButton(uploadId, "CSV", "csv") : el("span", { class: "text-xs text-slate-500" }, "Processing")),
      );
    }));
  }

  async function refresh() {
    const currentGeneration = ++generation;
    controller?.abort();
    if (timer != null) clearTimeout(timer);
    timer = null;
    const request = startRequest("/uploads?limit=200");
    controller = request.controller;
    try {
      const data = await request.promise;
      if (!isMounted() || currentGeneration !== generation) return;
      errorArea.classList.add("hidden");
      errorArea.replaceChildren();
      renderRows(Array.isArray(data.uploads) ? data.uploads : []);
      note.textContent = "Showing all pipeline uploads. Select a row to open its run detail.";
    } catch (e) {
      if (!isMounted() || currentGeneration !== generation || isAbortError(e)) return;
      errorArea.classList.remove("hidden");
      errorArea.replaceChildren(errorCard(e.message));
    } finally {
      if (isMounted() && currentGeneration === generation) {
        controller = null;
        timer = setTimeout(() => {
          timer = null;
          refresh();
        }, REFRESH_MS);
      }
    }
  }

  const refreshBtn = el("button", {
    class: "btn-ghost min-h-0 px-3 py-1.5 text-xs",
    onclick: refresh,
  }, "Refresh");

  const card = el("section", { class: "detail-section operations-section" },
    sectionHeading(
      "Pipeline Uploads History",
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
              head("Updated"), head("Storage"), head("Output JSON"), head("Output CSV"),
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

  registerCleanup(() => {
    generation += 1;
    controller?.abort();
    controller = null;
    if (timer != null) clearTimeout(timer);
    timer = null;
  });
  return { card, refresh };
}

// ---- Batch Manager ----------------------------------------------------------
function batchManagerCard(lifecycle) {
  const { isMounted, registerCleanup, startRequest } = lifecycle;
  let controller = null;
  let generation = 0;
  let timer = null;
  const busyJobs = new Set();
  let buttonsByJob = new Map();
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

  const setJobBusy = (jobName, busy) => {
    if (busy) busyJobs.add(jobName);
    else busyJobs.delete(jobName);
    for (const button of buttonsByJob.get(jobName) ?? []) button.disabled = busy;
  };

  async function handleAction(action, uploadId, jobName, batchGeneration) {
    if (busyJobs.has(jobName) || !isMounted()) return;
    if (action === "cancel" && !confirm(`Cancel Gemini batch ${jobName}?`)) return;
    if (action === "delete" && !confirm(`Delete Gemini batch ${jobName}? This cannot be undone.`)) return;

    setJobBusy(jobName, true);
    try {
      let path = `/batch/jobs/${action}?job_name=${encodeURIComponent(jobName)}`;
      if ((action === "cancel" || action === "delete") && uploadId) {
        path += `&upload_id=${encodeURIComponent(uploadId)}`;
        if (Number.isInteger(batchGeneration) && batchGeneration >= 0) {
          path += `&expected_generation=${batchGeneration}`;
        }
      }
      const data = await startRequest(path, { method: "POST" }).promise;
      if (!isMounted()) return;
      if (action === "status") {
        setNote(`Live batch status for ${jobName}: ${data.live_state ?? "-"} (done=${Boolean(data.done)})`);
      } else if (action === "delete") {
        setNote(`Delete requested for ${jobName}.`);
      } else {
        setNote(`Cancel requested for ${jobName}.`);
      }
    } catch (e) {
      if (!isMounted() || isAbortError(e)) return;
      setNote(`Action failed (${action}) for ${jobName}: ${e.message}`, true);
    } finally {
      if (isMounted()) {
        setJobBusy(jobName, false);
        await refresh();
      }
    }
  }

  const actionBtn = (label, action, uploadId, jobName, batchGeneration, extra = "") => {
    const btn = el("button", { class: `${actionBtnCls} ${extra}`, type: "button" }, label);
    btn.disabled = busyJobs.has(jobName);
    if (!buttonsByJob.has(jobName)) buttonsByJob.set(jobName, new Set());
    buttonsByJob.get(jobName).add(btn);
    btn.addEventListener("click", () => handleAction(
      action, uploadId, jobName, batchGeneration,
    ));
    return btn;
  };

  function renderRows(items) {
    buttonsByJob = new Map();
    empty.classList.toggle("hidden", items.length > 0);
    tbody.replaceChildren(...items.map((item) => {
      const uploadId = String(item.upload_id ?? "");
      const jobName = String(item.job_name ?? "");
      const batchGeneration = item.batch_generation == null
        ? null
        : Number(item.batch_generation);
      return el("tr", { class: "data-row" },
        cell(uploadId ? el("a", {
          class: "table-link font-mono text-xs",
          href: runHref(uploadId, "serpwow"),
          title: uploadId,
          "aria-label": `Open run ${uploadId}`,
        }, shortId(uploadId)) : "-"),
        cell(pill(statusLabel(item.upload_status), statusClass(item.upload_status))),
        cell(pill(batchLabel(item.batch_status), batchClass(item.batch_status))),
        cell(pill(String(item.live_state ?? "-") || "-", batchClass(item.live_state))),
        cell(el("span", { class: "font-mono text-xs", title: jobName }, jobName || "-")),
        cell(shortDate(item.updated_at), "whitespace-nowrap text-slate-400"),
        cell(el("div", { class: "action-group operations-actions" },
          actionBtn("Get Status", "status", uploadId, jobName, batchGeneration),
          actionBtn("Cancel", "cancel", uploadId, jobName, batchGeneration, "btn-warning"),
          actionBtn("Delete", "delete", uploadId, jobName, batchGeneration, "btn-danger"),
        )),
      );
    }));
  }

  async function refresh() {
    const currentGeneration = ++generation;
    controller?.abort();
    if (timer != null) clearTimeout(timer);
    timer = null;
    const request = startRequest("/batch/jobs?limit=300");
    controller = request.controller;
    try {
      const data = await request.promise;
      if (!isMounted() || currentGeneration !== generation) return;
      errorArea.classList.add("hidden");
      errorArea.replaceChildren();
      renderRows(Array.isArray(data.jobs) ? data.jobs : []);
    } catch (e) {
      if (!isMounted() || currentGeneration !== generation || isAbortError(e)) return;
      errorArea.classList.remove("hidden");
      errorArea.replaceChildren(errorCard(e.message));
    } finally {
      if (isMounted() && currentGeneration === generation) {
        controller = null;
        timer = setTimeout(() => {
          timer = null;
          refresh();
        }, REFRESH_MS);
      }
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

  registerCleanup(() => {
    generation += 1;
    controller?.abort();
    controller = null;
    if (timer != null) clearTimeout(timer);
    timer = null;
    buttonsByJob.clear();
    busyJobs.clear();
  });
  return { card, refresh };
}

// ---- Retry Operations ---------------------------------------------------------
function retryCard(lifecycle) {
  const { isMounted, registerCleanup, startRequest } = lifecycle;
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
  let postController = null;
  let postGeneration = 0;
  registerCleanup(() => {
    postGeneration += 1;
    postController?.abort();
    postController = null;
    if (stopPoll) stopPoll();
    stopPoll = null;
  });

  const btn = el("button", { class: "btn-primary disabled:opacity-50" }, "Retry Failed Rows");

  btn.addEventListener("click", async () => {
    const uid = input.value.trim();
    if (!uid) {
      setMeta("Error: Upload ID is required.", "danger");
      return;
    }
    if (stopPoll) stopPoll();
    stopPoll = null;
    const currentGeneration = ++postGeneration;
    btn.disabled = true;
    setMeta(`Triggering retry for ${uid}…`, "info");
    try {
      const request = startRequest(`/uploads/${encodeURIComponent(uid)}/retry-failed-rows`, { method: "POST" });
      postController = request.controller;
      const data = await request.promise;
      if (!isMounted() || currentGeneration !== postGeneration) return;
      setMeta(`Success! Enqueued ${data.enqueued_rows ?? 0} failed rows for processing.`, "good");
      // Legacy polled /uploads/{id}/status after the retry; keep that, shown inline.
      stopPoll = pollStatus(`/uploads/${encodeURIComponent(uid)}/status`, (status) => {
        if (!isMounted() || currentGeneration !== postGeneration) return;
        const batch = status.gemini_batch?.status ?? "not_started";
        setMeta(
          `Upload ${uid} — status: ${status.status ?? "-"} | rows: ${status.processed_rows ?? 0}/${status.total_rows ?? 0}` +
          ` (ok ${status.success_rows ?? 0}, failed ${status.failed_rows ?? 0}) | batch: ${batch}`,
          "muted",
        );
      }, 2000, uploadIsTerminal);
    } catch (e) {
      if (!isMounted() || currentGeneration !== postGeneration || isAbortError(e)) return;
      setMeta(`Failed: ${e.message}`, "danger");
    } finally {
      if (isMounted() && currentGeneration === postGeneration) {
        postController = null;
        btn.disabled = false;
      }
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
export function render(root) {
  const lifecycle = createLifecycle();
  const history = uploadHistoryCard(lifecycle);
  const batch = batchManagerCard(lifecycle);
  root.replaceChildren(
    el("div", { class: "operations-view" },
      pageIntro(
        "Operations",
        "Run and batch ledger",
        "Monitor upload history, manage Gemini batch jobs, and retry failed rows without leaving the console.",
      ),
      history.card,
      batch.card,
      retryCard(lifecycle),
    ),
  );

  history.refresh();
  batch.refresh();

  return lifecycle.cleanup;
}
