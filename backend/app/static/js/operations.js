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
import { errorCard, head, cell, shortDate } from "./ui.js";

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

const inputCls = "w-full rounded-lg border border-gray-300 px-3 py-2 text-sm " +
  "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500";

const actionBtnCls = "rounded-lg border border-gray-300 px-2.5 py-1 text-xs font-medium " +
  "text-gray-700 hover:bg-gray-50 disabled:opacity-50";

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
    class: "rounded-lg border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50",
    onclick: refresh,
  }, "Refresh");

  const card = el("div", { class: "rounded-xl border border-gray-200 bg-white p-6 shadow-sm" },
    el("div", { class: "flex items-center justify-between" },
      el("div", {},
        el("h2", { class: "text-sm font-semibold text-gray-900" }, "Batch Manager"),
        el("p", { class: "mt-1 text-sm text-gray-500" }, "Gemini batch jobs for full-pipeline uploads."),
      ),
      refreshBtn,
    ),
    errorArea,
    el("div", { class: "mt-4 overflow-x-auto rounded-lg border border-gray-200" },
      el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
        el("thead", { class: "bg-gray-50" },
          el("tr", {},
            head("Upload ID"), head("Upload Status"), head("Batch Status"),
            head("Live State"), head("Job Name"), head("Updated"), head("Actions"),
          ),
        ),
        tbody,
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

  const btn = el("button", {
    class: "rounded-lg bg-amber-500 px-4 py-2 text-sm font-medium text-white " +
           "hover:bg-amber-400 disabled:opacity-50",
  }, "Retry Failed Rows");

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

  return el("div", { class: "rounded-xl border border-gray-200 bg-white p-6 shadow-sm" },
    el("h2", { class: "text-sm font-semibold text-gray-900" }, "Retry Operations"),
    el("p", { class: "mt-1 text-sm text-gray-500" },
      "Manually retry all failed, queued, or stuck processing rows for a specific upload ID instantly."),
    el("div", { class: "mt-4 flex max-w-xl flex-col gap-3" },
      el("label", { class: "text-xs font-medium uppercase tracking-wide text-gray-400" }, "Upload ID *"),
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

  const batch = batchManagerCard();
  root.replaceChildren(
    el("div", { class: "space-y-6" }, batch.card, retryCard(registerCleanup)),
  );

  await batch.refresh();
  const timer = setInterval(batch.refresh, REFRESH_MS);
  registerCleanup(() => clearInterval(timer));

  return () => cleanups.forEach((fn) => fn());
}
