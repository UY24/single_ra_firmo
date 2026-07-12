class ClassList {
  constructor(element) { this.element = element; }
  _tokens() { return this.element.className.split(/\s+/).filter(Boolean); }
  contains(token) { return this._tokens().includes(token); }
  add(...tokens) { this.element.className = [...new Set([...this._tokens(), ...tokens])].join(" "); }
  remove(...tokens) {
    const removed = new Set(tokens);
    this.element.className = this._tokens().filter((token) => !removed.has(token)).join(" ");
  }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attributes = {};
    this.className = "";
    this.childNodes = [];
    this.listeners = {};
    this.disabled = false;
    this.classList = new ClassList(this);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = String(value);
    if (name === "disabled") this.disabled = true;
  }
  getAttribute(name) {
    if (name === "class") return this.className || null;
    return this.attributes[name] ?? null;
  }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  append(...children) { this.childNodes.push(...children); }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this.childNodes = children; }
  get children() { return this.childNodes.filter((child) => child instanceof Element); }
  get textContent() {
    return this.childNodes.map((child) => child instanceof Element ? child.textContent : String(child)).join("");
  }
  set textContent(value) { this.childNodes = [String(value)]; }
  async click() {
    if (this.disabled) return;
    return this.listeners.click?.({ target: this, preventDefault() {} });
  }
}

globalThis.document = {
  body: new Element("body"),
  createElement: (tag) => new Element(tag),
};
globalThis.window = {
  confirm: () => true,
  location: { reload() {} },
};
globalThis.setTimeout = () => 1;

const { render } = await import("../app/static/js/run_detail.js");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function all(node) { return [node, ...node.children.flatMap(all)]; }
function byClass(node, name) { return all(node).filter((candidate) => candidate.classList.contains(name)); }
function byTag(node, name) { return all(node).filter((candidate) => candidate.tagName === name.toUpperCase()); }
function byText(node, name, text) { return byTag(node, name).find((candidate) => candidate.textContent === text); }
function labelValue(root, label) {
  const item = byClass(root, "metric-item").find((candidate) => candidate.children[0]?.textContent === label);
  return item?.children[1]?.textContent;
}
function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    json: async () => payload,
    text: async () => String(payload),
  };
}

let requests = [];

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

async function renderStatus(ref, status, { ai = false } = {}) {
  requests = [];
  const aiPath = `/uploads/ai-mode/${encodeURIComponent(ref)}/status`;
  const legacyPath = `/uploads/${encodeURIComponent(ref)}/status`;
  let aiCalls = 0;
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    if (ai) {
      if (path === aiPath) {
        aiCalls += 1;
        return response(aiCalls === 1 ? {} : status);
      }
    } else {
      if (path === aiPath) return response({ detail: "Not Found" }, 404);
      if (path === legacyPath) return response(status);
    }
    if (String(path).includes("/result?file=")) {
      return response('name,notes\nA,"line one\nline two"\n');
    }
    if (String(path).endsWith("/resume") || String(path).endsWith("/stop")) {
      return response({ stopped_rows: 1, batch_cancelled: false });
    }
    throw new Error(`Unexpected fetch: ${path}`);
  };
  const root = new Element("main");
  const cleanup = await render(root, { runRef: ref });
  await settle();
  return { root, cleanup };
}

function assertOutcomeFirst(root, expected) {
  const summary = byClass(root, "outcome-summary")[0];
  const strip = byClass(root, "metric-strip")[0];
  const files = byClass(root, "files-section")[0];
  assert(summary?.getAttribute("aria-label") === "Run outcome", "run outcome landmark missing");
  assert(summary.textContent.includes(expected), `outcome missing ${expected}`);
  assert(!byClass(root, "metric-card").length, "run detail must not render metric cards");
  const nodes = all(root);
  assert(nodes.indexOf(summary) < nodes.indexOf(strip), "outcome must appear before execution metrics");
  if (files) assert(nodes.indexOf(strip) < nodes.indexOf(files), "execution metrics must appear before files");
}

async function completedGsearchLlm() {
  const ref = "g llm/&";
  const { root } = await renderStatus(ref, {
    pipeline: "gsearch", status: "completed", total_rows: 4, processed_rows: 4,
    success_rows: 4, failed_rows: 0, processing_seconds_total: 12,
    processing_seconds_avg: 3, gemini_batch: { status: "succeeded" },
    serpwow_summary: {
      confidence_mode: "llm", is_batch: true, model: "gemini-test",
      websites_found: 4, websites_not_found: 0,
      outcome_breakdown: { errored: 0 },
      token_usage: { prompt_tokens: 100, completion_tokens: 20 },
      cost: { llm_usd: 0.01, serpwow_usd: 0.02, serpwow_searches: 4, total_usd: 0.03 },
    },
  });
  assertOutcomeFirst(root, "4 of 4");
  assert(root.textContent.includes("100%"), "gsearch percentage missing");
  assert(labelValue(root, "Not found") === "0" && labelValue(root, "Errors") === "0", "gsearch secondary outcomes wrong");
  for (const text of ["12s", "3s", "100", "20", "succeeded", "gemini-test", "$0.0300", "4 searches"]) {
    assert(root.textContent.includes(text), `gsearch detail missing ${text}`);
  }
  const encoded = encodeURIComponent(ref);
  assert(byTag(root, "a").some((link) => link.getAttribute("href") === `/uploads/${encoded}/result?file=found.csv&download=true`), "result download URL changed");
  const view = byText(root, "button", "View");
  assert(view?.listeners.click, "file View handler missing");
  await view.click();
  assert(requests.some(({ path }) => path === `/uploads/${encoded}/result?file=found.csv`), "file View URL changed");
  assert(byClass(document.body, "modal-surface").length === 1, "semantic file modal surface missing");
  assert(byClass(document.body, "data-table").length === 1, "CSV data table missing");
  assert(document.body.textContent.includes("line one\nline two"), "embedded CSV newline was not preserved");
  assert(document.body.textContent.includes("1 row"), "CSV row count changed");
}

async function completedGmapsHeuristic() {
  const { root } = await renderStatus("gmaps", {
    pipeline: "gmaps", status: "completed", total_rows: 3, processed_rows: 3,
    processing_seconds_total: 6, processing_seconds_avg: 2,
    serpwow_summary: {
      confidence_mode: "heuristic", websites_found: 2, websites_not_found: 1,
      outcome_breakdown: { errored: 0 }, cost: { serpwow_usd: 0.1, total_usd: 0.1 },
    },
  });
  assertOutcomeFirst(root, "2 of 3");
  assert(root.textContent.includes("Heuristic"), "heuristic metadata missing");
  assert(!root.textContent.includes("Input tokens"), "heuristic run exposed token metrics");
}

async function completedRelationship() {
  const { root } = await renderStatus("relationship", {
    pipeline: "relationship", status: "completed", total_rows: 2, processed_rows: 2,
    processing_seconds_total: 5, processing_seconds_avg: 2.5,
    serpwow_summary: {
      confidence_mode: "llm", is_batch: false, model: "gemini-rel",
      total_rows_original: 5, websites_found: 3, websites_not_found: 1, blank_rows: 1,
      outcome_breakdown: { errored: 0 }, unique_pairs: 2,
      relationship_breakdown: { confirmed: 2, not_confirmed: 1, unclear: 1 },
      token_usage: { prompt_tokens: 50, completion_tokens: 10 }, cost: { total_usd: 0.2 },
    },
  });
  assertOutcomeFirst(root, "3 of 5");
  assert(labelValue(root, "Skipped") === "1", "relationship skipped outcome missing");
  for (const text of ["Relationship verdict", "Confirmed2", "Not confirmed1", "Unclear1", "Unique pairs"]) {
    assert(root.textContent.includes(text), `relationship detail missing ${text}`);
  }
}

async function finalizingBatch() {
  const { root } = await renderStatus("final", {
    pipeline: "gsearch", status: "completed", total_rows: 4, processed_rows: 4,
    gemini_batch: { status: "running" },
    serpwow_summary: {
      confidence_mode: "llm", is_batch: true, websites_found: 4, websites_not_found: 0,
      outcome_breakdown: { errored: 0 }, cost: {},
    },
  });
  assertOutcomeFirst(root, "4 of 4");
  assert(root.textContent.includes("running") && root.textContent.includes("finalizing"), "finalizing status missing");
  assert(!byClass(root, "files-section").length, "finalizing run exposed files early");
  const stop = byText(root, "button", "Stop run");
  assert(stop?.listeners.click, "finalizing run lost Stop action");
  await stop.click();
  assert(requests.some(({ path, options }) =>
    path === "/uploads/final/stop" && options.method === "POST"), "Stop endpoint changed");
}

async function legacyCompatibility() {
  const { root } = await renderStatus("legacy", {
    pipeline: "full", status: "completed_with_errors", total_rows: 7, processed_rows: 7,
    success_rows: 5, failed_rows: 2, processing_seconds_total: 14, processing_seconds_avg: 2,
  });
  assertOutcomeFirst(root, "5 of 7");
  assert(labelValue(root, "Not found") === "0", "legacy not-found fallback wrong");
  assert(labelValue(root, "Errors") === "2", "legacy failed rows must map to errors");
  assert(byClass(root, "files-section").length === 1, "legacy terminal files missing");
}

function aiPayload(status, errors = 0) {
  return {
    company_name: "AI Co", mode_label: "AI Mode", status, phase: "cleanup",
    total_rows: 6, entities_processed: 6, websites_found: 4, websites_not_found: 1,
    llm_errors: errors, outcome_breakdown: errors ? { errored: errors } : undefined,
    batches_done: 2, batches_total: 2, batch_duration_seconds: 18,
    token_usage: { prompt_tokens: 120, completion_tokens: 30 }, model: "gemini-ai", is_batch: true,
    scrapedo_request_count: 5, failed_request_count: 0,
    cost: { scrapedo_searches: 5, total_usd: 0.4 }, available_files: ["found.csv", "run.log"],
  };
}

async function completedAiMode() {
  const { root } = await renderStatus("ai", aiPayload("completed"), { ai: true });
  assertOutcomeFirst(root, "4 of 6");
  for (const text of ["18s", "120", "30", "gemini-ai", "Yes", "Batches", "found.csv"]) {
    assert(root.textContent.includes(text), `AI Mode detail missing ${text}`);
  }
  const unavailable = byClass(root, "file-row").find((row) => row.textContent.includes("final_report.json"));
  assert(unavailable?.children[1]?.children[0]?.disabled, "unavailable AI file View must be disabled");
  assert(unavailable?.children[1]?.children[1]?.getAttribute("href") == null,
    "unavailable AI file Download must not have an href");
  assert(!byText(root, "button", "Rerun failed"), "successful AI Mode run exposed rerun");
}

async function erroredAiMode() {
  const ref = "ai errors";
  const { root } = await renderStatus(ref, aiPayload("completed_with_errors", 1), { ai: true });
  assertOutcomeFirst(root, "4 of 6");
  assert(labelValue(root, "Errors") === "1", "AI Mode outcome errors wrong");
  const rerun = byText(root, "button", "Rerun failed");
  assert(rerun?.listeners.click, "AI Mode rerun action missing");
  await rerun.click();
  assert(requests.some(({ path, options }) =>
    path === `/uploads/ai-mode/${encodeURIComponent(ref)}/resume` && options.method === "POST"),
  "AI Mode rerun endpoint changed");
}

await completedGsearchLlm();
await completedGmapsHeuristic();
await completedRelationship();
await finalizingBatch();
await legacyCompatibility();
await completedAiMode();
await erroredAiMode();
