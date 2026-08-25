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
    this.inert = false;
    this.classList = new ClassList(this);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = String(value);
    if (name === "disabled") this.disabled = true;
    if (name === "inert") this.inert = true;
  }
  getAttribute(name) {
    if (name === "class") return this.className || null;
    return this.attributes[name] ?? null;
  }
  hasAttribute(name) { return this.getAttribute(name) != null; }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "disabled") this.disabled = false;
    if (name === "inert") this.inert = false;
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
  focus() { document.activeElement = this; }
  dispatch(name, event = {}) {
    return this.listeners[name]?.({ target: this, preventDefault() {}, ...event });
  }
  async click() {
    if (this.disabled) return;
    return this.listeners.click?.({ target: this, preventDefault() {} });
  }
}

let currentMain = null;
globalThis.document = {
  body: new Element("body"),
  activeElement: null,
  createElement: (tag) => new Element(tag),
  querySelector: (selector) => selector === "main" ? currentMain : null,
};
globalThis.window = {
  confirm: () => true,
  location: { reload() {} },
};
let timers = [];
globalThis.setTimeout = (callback) => {
  timers.push(callback);
  return timers.length;
};
globalThis.clearTimeout = (id) => {
  if (Number.isInteger(id) && id > 0) timers[id - 1] = null;
};

const [{ render }, { pollStatus }] = await Promise.all([
  import("../app/static/js/run_detail.js"),
  import("../app/static/js/api.js"),
]);

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

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
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
  timers = [];
  const aiPath = `/uploads/ai-mode/${encodeURIComponent(ref)}/status`;
  const legacyPath = `/uploads/${encodeURIComponent(ref)}/status`;
  const failurePath = `/uploads/${encodeURIComponent(ref)}/failure-analysis?sample_limit=100`;
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
    if (path === failurePath) {
      return response({
        failed_rows: 2,
        sample_failed_rows: [
          { row_index: 63, company_name: "Kitche", error_source: "serpwow",
            error_category: "timeout", error: "ReadTimeout" },
          { row_index: 67, company_name: "CASCADE COFFEE", error_source: "serpwow",
            error_category: "timeout", error: "ReadTimeout" },
        ],
      });
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
  currentMain = root;
  const cleanup = await render(root, { runRef: ref });
  await settle();
  return { root, cleanup };
}

async function renderLegacySequence(ref, statuses) {
  requests = [];
  timers = [];
  const aiPath = `/uploads/ai-mode/${encodeURIComponent(ref)}/status`;
  const legacyPath = `/uploads/${encodeURIComponent(ref)}/status`;
  let legacyCalls = 0;
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    if (path === aiPath) return response({ detail: "Not Found" }, 404);
    if (path === legacyPath) {
      legacyCalls += 1;
      const index = legacyCalls === 1 ? 0 : Math.min(legacyCalls - 2, statuses.length - 1);
      return response(statuses[index]);
    }
    if (String(path).endsWith("/stop")) return response({ stopped_rows: 1, batch_cancelled: false });
    throw new Error(`Unexpected fetch: ${path}`);
  };
  const root = new Element("main");
  currentMain = root;
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
      outcome_breakdown: { found: 4, not_found: 0, errored: 0 },
      error_breakdown: { by_source: {}, by_category: {} },
      available_files: ["found.csv", "report.json"],
      token_usage: { prompt_tokens: 100, completion_tokens: 20 },
      cost: { llm_usd: 0.01, serpwow_usd: 0.02, serpwow_searches: 4, total_usd: 0.03 },
    },
  });
  assertOutcomeFirst(root, "4 of 4");
  assert(byClass(root, "outcome-label")[0]?.textContent === "Websites found",
    "reporting pipeline lost its website-specific outcome label");
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
  const unavailableLog = byClass(root, "file-row").find((row) => row.textContent.includes("run.log"));
  assert(unavailableLog?.children[1]?.children[0]?.disabled,
    "absent SerpWow result file View must be disabled");
  assert(unavailableLog?.children[1]?.children[1]?.getAttribute("href") == null,
    "absent SerpWow result file Download must not have an href");
  assert(unavailableLog?.children[1]?.children[1]?.getAttribute("aria-disabled") === "true",
    "absent SerpWow Download must expose aria-disabled");
  await byText(document.body, "button", "Close").click();
}

async function completedGmapsHeuristic() {
  const { root } = await renderStatus("gmaps", {
    pipeline: "gmaps", status: "completed_with_errors", total_rows: 4, processed_rows: 4,
    failed_rows: 1,
    processing_seconds_total: 6, processing_seconds_avg: 2,
    serpwow_summary: {
      confidence_mode: "heuristic", websites_found: 2, websites_not_found: 2,
      outcome_breakdown: { found: 2, not_found: 1, errored: 1 },
      error_breakdown: { by_source: { serpwow: 1 }, by_category: { upstream: 1 } },
      available_files: [],
      cost: { serpwow_usd: 0.1, total_usd: 0.1 },
    },
  });
  assertOutcomeFirst(root, "2 of 4");
  assert(labelValue(root, "Not found") === "1", "SerpWow inclusive not-found double counted errors");
  assert(labelValue(root, "Errors") === "1", "SerpWow failed rows did not map to errors");
  assert(root.textContent.includes("Heuristic"), "heuristic metadata missing");
  assert(!root.textContent.includes("Input tokens"), "heuristic run exposed token metrics");
}

async function gmapsBillingBreakdown() {
  // Based on the real 100-row run 8ffe96d9 (139 attempts, 87 billed 200s = 870 credits,
  // 13 rows Google has no listing for at 4 free attempts each), plus a billed-empty row
  // and two 200s whose BODY carried an error — both are credits spent for nothing.
  // The card explains the 1000-vs-870 gap; it used to render "All phases 0 / Some 0".
  const { root } = await renderStatus("gmaps-billing", {
    pipeline: "gmaps", status: "completed_with_errors", total_rows: 100,
    processed_rows: 100, failed_rows: 3,
    serpwow_summary: {
      confidence_mode: "heuristic", total_rows: 100,
      websites_found: 68, websites_not_found: 32,
      // 3 dead rows: 2 were billed (HTTP 200 + error body), 1 never got a 200.
      outcome_breakdown: { found: 68, not_found: 29, errored: 3 },
      empty_response_breakdown: { no_listing: 13, billed_empty: 1 },
      available_files: [],
      cost: {
        scrapedo_requests: 139, scrapedo_successful_requests: 87,
        scrapedo_failed_requests: 52, scrapedo_error_requests: 4,
        scrapedo_no_results: 13, scrapedo_billed_empty: 1,
        scrapedo_billed_errors: 2, scrapedo_credits: 870,
        llm_usd: 0.0, total_usd: 0.0,
      },
    },
  });
  assert(root.textContent.includes("Scrape.do billing (10 credits per HTTP 200)"),
    "gmaps billing heading missing");
  assert(!root.textContent.includes("All phases"),
    "gmaps still renders gsearch's per-phase empty-response split");
  const pillValue = (label) => byClass(root, "pill")
    .find((pill) => pill.children[0]?.textContent === label)?.children[1]?.textContent;
  assert(pillValue("Billed calls") === "87 of 100 rows", "billed calls wrong");
  // Charged and got nothing usable: the empty 200 plus both error-body 200s.
  assert(pillValue("Billed but no result") === "3", "billed-for-nothing count wrong");
  // Every attempt failed, so nothing was charged: 13 no-listing rows + the one dead row
  // that never got a 200. The two BILLED error rows must not be counted here.
  assert(pillValue("Failed after retries") === "14", "unbilled dead rows wrong");
  assert(pillValue("Unbilled attempts") === "52", "retry attempts not shown");
  assert(!root.textContent.includes("No Maps listing"),
    "no-listing chip should be folded into the unbilled-failure count");
  assert(root.textContent.includes("870"), "credits missing from the cost card");
}

async function inFlightScrapedoRunIsNotLabelledSerpWow() {
  // A gmaps run that has not billed a call yet: every scrapedo_* counter is 0, which is
  // ALSO what a SerpWow run carries, so the truthiness check alone fell through to the
  // default "SerpWow" provider label. Seen live on run 7a03eaa0, which stalled at
  // phase="queued" and rendered a SerpWow cost card for a pipeline that cannot use it.
  const { root } = await renderStatus("gmaps-inflight", {
    pipeline: "gmaps", status: "processing", total_rows: 100,
    processed_rows: 0, failed_rows: 0,
    serpwow_summary: {
      confidence_mode: "heuristic", total_rows: 100,
      websites_found: 0, websites_not_found: 100,
      outcome_breakdown: { found: 0, not_found: 0, errored: 0 },
      available_files: [],
      cost: { scrapedo_requests: 0, scrapedo_credits: 0, scrapedo_failed_requests: 0,
              llm_usd: 0.0, total_usd: 0.0 },
    },
  });
  assert(!root.textContent.includes("SerpWow"),
    "in-flight gmaps run labelled its provider SerpWow");
  assert(root.textContent.includes("Scrape.do"),
    "in-flight gmaps run missing the Scrape.do cost card");
}

async function preMigrationGmapsKeepsSerpWowCard() {
  // The other half of the same rule: a gmaps run from BEFORE the scrape.do migration has
  // real serpwow_searches, and must keep rendering them rather than being relabelled by
  // its pipeline key.
  const { root } = await renderStatus("gmaps-legacy", {
    pipeline: "gmaps", status: "completed", total_rows: 10,
    processed_rows: 10, failed_rows: 0,
    serpwow_summary: {
      confidence_mode: "heuristic", total_rows: 10,
      websites_found: 8, websites_not_found: 2,
      outcome_breakdown: { found: 8, not_found: 2, errored: 0 },
      available_files: [],
      cost: { serpwow_searches: 21, serpwow_usd: 0.21, llm_usd: 0.0, total_usd: 0.21 },
    },
  });
  assert(root.textContent.includes("SerpWow"),
    "pre-migration gmaps run lost its SerpWow cost card");
}

async function deadGeminiShardIsVisibleAndRerunnable() {
  // scrape.do did its job and was billed; the Gemini shard that had to read the answer
  // died. Before this the run said "completed_with_errors" while every visible count read
  // zero, the rows were indistinguishable from "Google had no overview" (which is FINAL),
  // and the only retry surface was hand-typing the run id into the Operations page.
  const { root } = await renderStatus("firmo-dead-shard", {
    pipeline: "firmographics", status: "completed_with_errors",
    total_rows: 100, processed_rows: 100, failed_rows: 0,
    serpwow_summary: {
      confidence_mode: null, model: "gemini-2.5-flash-lite", llm_mode: "batch",
      total_rows: 100, websites_found: 60, websites_not_found: 40,
      outcome_breakdown: { found: 60, not_found: 40, errored: 0 },
      error_breakdown: { by_source: {}, by_category: {}, task_errors: 2 },
      empty_response_breakdown: {
        no_ai_overview: 5, deferred: 12, no_website: 0,
        llm_incomplete: 35, never_processed: 0,
      },
      available_files: [],
      cost: {
        scrapedo_requests: 100, scrapedo_successful_requests: 100,
        scrapedo_search_successful: 100, scrapedo_ai_overview_successful: 12,
        scrapedo_credits: 1060, scrapedo_error_requests: 0,
        llm_usd: 0.01, total_usd: 0.01,
      },
    },
  });
  const pillValue = (label) => byClass(root, "pill")
    .find((pill) => pill.children[0]?.textContent === label)?.children[1]?.textContent;
  assert(pillValue("LLM never completed") === "35",
    "unjudged rows not broken out from Billed-but-no-overview");
  assert(pillValue("Billed but no overview") === "5",
    "no_ai_overview must not absorb the rows that DID have an overview");
  assert(/2 background task\(s\) failed/.test(root.textContent),
    "task_errors never surfaced — completed_with_errors had no visible cause");
  assert(root.textContent.includes("no provider re-spend"),
    "rerun copy must say the scrape is not re-bought");
  const btn = byText(root, "button", "Rerun failed");
  assert(btn?.listeners.click, "no Rerun button on an S3-only run");
}

async function rerunButtonShowsOnACleanS3Run() {
  // Offered on any terminal run of these pipelines: a re-drive skips every row that has a
  // result, so the button is safe with nothing to redo — and a user looking for it should
  // not have to first produce a failure to find out where it lives.
  const { root } = await renderStatus("gmaps-clean", {
    pipeline: "gmaps", status: "completed", total_rows: 10,
    processed_rows: 10, failed_rows: 0,
    serpwow_summary: {
      confidence_mode: "heuristic", total_rows: 10,
      websites_found: 10, websites_not_found: 0,
      outcome_breakdown: { found: 10, not_found: 0, errored: 0 },
      available_files: [],
      cost: { scrapedo_requests: 10, scrapedo_successful_requests: 10,
              scrapedo_credits: 100, llm_usd: 0, total_usd: 0 },
    },
  });
  assert(byText(root, "button", "Rerun failed"), "gmaps run has no Rerun button");
  assert(!/background task\(s\) failed/.test(root.textContent),
    "clean run showed a task-error callout");
}

async function failedRowsViewer() {
  const ref = "failed rows/&";
  const { root } = await renderStatus(ref, {
    pipeline: "relationship", status: "completed_with_errors",
    total_rows: 100, processed_rows: 100, failed_rows: 2,
    serpwow_summary: {
      confidence_mode: "llm", websites_found: 50, websites_not_found: 50,
      outcome_breakdown: { found: 50, not_found: 48, errored: 2 },
      error_breakdown: { by_source: { serpwow: 2 }, by_category: { timeout: 2 } },
      relationship_breakdown: { confirmed: 50, not_confirmed: 48, unclear: 0 },
      available_files: [], cost: {},
    },
  });
  const button = byText(root, "button", "View failed rows (2)");
  assert(button?.listeners.click, "failed-row viewer button missing");
  assert(button.getAttribute("aria-expanded") === "false", "failed rows started expanded");
  assert(!requests.some(({ path }) => String(path).includes("failure-analysis")),
    "failed rows fetched before expansion");
  await button.click();
  await settle();
  assert(requests.some(({ path }) => path ===
    `/uploads/${encodeURIComponent(ref)}/failure-analysis?sample_limit=100`),
  "failure-analysis URL changed");
  assert(button.getAttribute("aria-expanded") === "true", "failed rows did not expand");
  const results = byClass(root, "failed-rows-results")[0];
  assert(results?.getAttribute("aria-live") === "polite", "failed rows missing live region");
  for (const text of ["63", "Kitche", "67", "CASCADE COFFEE", "serpwow", "timeout", "ReadTimeout"]) {
    assert(results.textContent.includes(text), `failed rows missing ${text}`);
  }
  assert(byClass(results, "overflow-x-auto").length === 1,
    "failed-row table is not horizontally scrollable");
  assert(byClass(results, "data-table").length === 1, "failed-row data table missing");
}

async function completedRelationship() {
  const { root } = await renderStatus("relationship", {
    pipeline: "relationship", status: "completed", total_rows: 5, processed_rows: 5,
    processing_seconds_total: 5, processing_seconds_avg: 2.5,
    serpwow_summary: {
      confidence_mode: "llm", is_batch: false, model: "gemini-rel",
      websites_found: 3, websites_not_found: 2,
      outcome_breakdown: { found: 3, not_found: 2, errored: 0 },
      available_files: ["confirmed_relation.csv", "notconfirmed_relation.csv",
        "retry.csv", "report.json", "run.log"],
      relationship_breakdown: { confirmed: 2, not_confirmed: 1, unclear: 1 },
      token_usage: { prompt_tokens: 50, completion_tokens: 10 }, cost: { total_usd: 0.2 },
    },
  });
  assertOutcomeFirst(root, "3 of 5");
  for (const text of ["Relationship verdict", "Confirmed2", "Not confirmed1", "Unclear1"]) {
    assert(root.textContent.includes(text), `relationship detail missing ${text}`);
  }
}

// The two payloads below are byte-for-byte what engine._relationship_status builds
// after the scrape.do migration: no gemini_batch, no rows[], no file_links, no
// success_rows — a relationship run has no state.json to derive them from. This case
// is the proof that the counter-driven response still drives the unchanged UI.
async function counterDrivenRelationshipTerminal() {
  const { root } = await renderStatus("rel-counter", {
    upload_id: "rel-counter", pipeline: "relationship", company_name: "Acme",
    status: "completed", total_rows: 5, processed_rows: 5, failed_rows: 0,
    phase: "completed", updated_at: "2026-08-04T00:00:00Z",
    serpwow_summary: {
      pipeline: "relationship", status: "completed", total_rows: 5,
      websites_found: 3, websites_not_found: 2,
      relationship_breakdown: { confirmed: 3, not_confirmed: 2, unclear: 0 },
      outcome_breakdown: { found: 3, not_found: 2, errored: 0 },
      error_breakdown: { by_source: {}, by_category: {} },
      empty_response_breakdown: { no_ai_text: 1 },
      confidence_mode: "llm",
      available_files: ["confirmed_relation.csv", "notconfirmed_relation.csv",
        "retry.csv", "report.json", "run.log"],
      cost: {
        scrapedo_requests: 5, scrapedo_successful_requests: 5,
        scrapedo_failed_requests: 0, scrapedo_error_requests: 0,
        scrapedo_billed_empty: 1, scrapedo_credits: 50,
        llm_usd: 0.0, total_usd: 0.0,
      },
    },
    files: ["confirmed_relation.csv", "notconfirmed_relation.csv", "report.json", "run.log"],
  });
  assertOutcomeFirst(root, "3 of 5");
  assert(root.textContent.includes("Relationship verdict"), "verdict section missing");
  assert(root.textContent.includes("Scrape.do"), "scrape.do cost card missing");
  assert(root.textContent.includes("50"), "scrape.do credits missing");
  // One AI Mode call per row means ONE empty-response number, keyed on text_blocks.
  // The SerpWow-era per-phase split (Both phases / Phase 1 only / Phase 2 only)
  // rendered three permanent zeroes here and never showed the actual count.
  assert(root.textContent.includes("Empty AI Mode answers (HTTP 200)"),
    "empty AI Mode answers section missing");
  assert(root.textContent.includes("No AI Mode text"),
    "no_ai_text chip missing");
  assert(!/Phase 1 only|All phases|SerpWow/.test(root.textContent),
    "relationship still renders SerpWow-shaped empty-response chips");
  assert(!byText(root, "button", "Stop run"), "terminal run still offered Stop");
  const files = byClass(root, "files-section")[0];
  assert(files, "counter-driven terminal run rendered no Files card");
  // retry.csv is the rerun/refund list — the rows that got no answer, ready to upload back.
  for (const name of ["confirmed_relation.csv", "notconfirmed_relation.csv",
    "retry.csv", "report.json", "run.log"]) {
    assert(files.textContent.includes(name), `Files card missing ${name}`);
  }
  assert(!byText(root, "span", "found.csv"), "relationship run advertised gsearch files");
  assert(!byTag(files, "button").some((button) => button.disabled),
    "terminal relationship files rendered disabled");
  // A relationship run has no state.json, so /output (json and csv) 404s — the Files
  // card must not offer them. gsearch keeps them (see failedReportingRunShowsFiles).
  assert(!files.textContent.includes("output.json")
    && !files.textContent.includes("output.csv"),
  "relationship Files card advertised the state-driven output endpoints");
}

async function counterDrivenRelationshipFailedMidScrape() {
  // Terminal (so filesReady is true) but write_outputs never ran: available_files is
  // empty and every file link must render disabled rather than as an enabled 404.
  const { root } = await renderStatus("rel-failed", {
    upload_id: "rel-failed", pipeline: "relationship", company_name: "Acme",
    status: "failed", total_rows: 5, processed_rows: 2, failed_rows: 2,
    phase: "failed", updated_at: "2026-08-04T00:00:00Z",
    serpwow_summary: {
      total_rows: 5, websites_found: 0, websites_not_found: 3,
      confidence_mode: "llm", available_files: [],
      outcome_breakdown: { found: 0, not_found: 0, errored: 2 },
      empty_response_breakdown: { no_ai_text: 0 },
      cost: {
        scrapedo_requests: 2, scrapedo_credits: 20,
        scrapedo_error_requests: 0, scrapedo_billed_empty: 0,
        llm_usd: 0.0, total_usd: 0.0,
      },
    },
    files: ["confirmed_relation.csv", "notconfirmed_relation.csv", "retry.csv",
      "report.json", "run.log"],
  });
  const files = byClass(root, "files-section")[0];
  assert(files, "failed relationship run hid the Files surface");
  const buttons = byTag(files, "button");
  assert(buttons.length === 5, `expected 5 file buttons, got ${buttons.length}`);
  assert(buttons.every((button) => button.disabled),
    "failed mid-scrape run offered enabled links to files it never wrote");
  // The failed-rows viewer is offered, so its endpoint must answer (see
  // FailureAnalysisTests in test_relationship_endpoint.py).
  assert(byText(root, "button", "View failed rows (2)"), "failed-row viewer missing");
}

async function counterDrivenRelationshipRunning() {
  const { root } = await renderStatus("rel-running", {
    upload_id: "rel-running", pipeline: "relationship", company_name: "Acme",
    status: "processing", total_rows: 500000, processed_rows: 1236, failed_rows: 2,
    phase: "scraping", updated_at: "2026-08-04T00:00:00Z",
    serpwow_summary: {
      total_rows: 500000, websites_found: 0, websites_not_found: 498764,
      confidence_mode: "llm", available_files: [],
      outcome_breakdown: { found: 0, not_found: 0, errored: 2 },
      empty_response_breakdown: { no_ai_text: 5 },
      cost: {
        scrapedo_requests: 1240, scrapedo_credits: 12340,
        scrapedo_error_requests: 0, scrapedo_billed_empty: 5,
        llm_usd: 0.0, total_usd: 0.0,
      },
    },
    files: ["confirmed_relation.csv", "notconfirmed_relation.csv", "report.json", "run.log"],
  });
  // A missing gemini_batch must not make a running run look "finalizing" or terminal.
  assert(byText(root, "button", "Stop run"), "running relationship run offered no Stop");
  assert(!byClass(root, "files-section").length, "running run exposed files that do not exist yet");
  assertOutcomeFirst(root, "0 of 500,000");
}

async function finalizingBatch() {
  const base = {
    pipeline: "gsearch", status: "completed", total_rows: 4, processed_rows: 4,
    serpwow_summary: {
      confidence_mode: "llm", is_batch: true, websites_found: 4, websites_not_found: 0,
      outcome_breakdown: { found: 4, not_found: 0, errored: 0 },
      error_breakdown: { by_source: {}, by_category: {} }, available_files: [],
      cost: {},
    },
  };
  const { root } = await renderLegacySequence("final", [
    { ...base, gemini_batch: { status: "running" } },
    { ...base, gemini_batch: { status: "succeeded" } },
  ]);
  assertOutcomeFirst(root, "4 of 4");
  assert(root.textContent.includes("running") && root.textContent.includes("finalizing"), "finalizing status missing");
  assert(!byClass(root, "files-section").length, "finalizing run exposed files early");
  const stop = byText(root, "button", "Stop run");
  assert(stop?.listeners.click, "finalizing run lost Stop action");
  assert(timers.length === 1, "completed rows with running batch stopped polling");
  await timers.shift()();
  await settle();
  assert(byClass(root, "files-section").length === 1, "terminal batch poll did not reveal files");
  assert(!root.textContent.includes("finalizing"), "terminal batch remained finalizing");
}

async function completedWithErrorsBatchIsTerminal() {
  const { root } = await renderStatus("batch-errors", {
    pipeline: "gsearch", status: "completed_with_errors", total_rows: 3, processed_rows: 3,
    failed_rows: 1, gemini_batch: { status: "completed_with_errors" },
    serpwow_summary: {
      confidence_mode: "llm", is_batch: true, websites_found: 2, websites_not_found: 1,
      outcome_breakdown: { found: 2, not_found: 0, errored: 1 },
      error_breakdown: { by_source: { gemini: 1 }, by_category: { llm_error: 1 } },
      available_files: ["found.csv", "notFound.csv", "report.json", "run.log"],
      cost: { serpwow_searches: 15, serpwow_billable_searches: 0,
              serpwow_usd: 0, total_usd: 0 },
    },
  });
  assert(byClass(root, "files-section").length === 1,
    "completed_with_errors batch must be terminal and expose files");
  assert(root.textContent.includes("15 searches") && root.textContent.includes("15 failed"),
    "SerpWow failed attempts were not visible beside cost");
}

async function firmographicsIsS3OnlyAndCarriesNoBatchState() {
  // firmographics moved to the S3-only runner on 2026-08-20, so it has no state.json and
  // therefore no gemini_batch field at all — the case this used to cover (a non-reporting
  // pipeline handed batch state) can no longer be produced by the server.
  //
  // What must hold instead: it is terminal on its own status, it advertises its OWN result
  // files, and it does NOT offer output.json / output.csv — both 404 without a state.json.
  const { root } = await renderStatus("firmo-s3", {
    pipeline: "firmographics", status: "completed", total_rows: 2, processed_rows: 2,
    success_rows: 2, failed_rows: 0,
    serpwow_summary: {
      total_rows: 2, websites_found: 2, websites_not_found: 0,
      confidence_mode: null, model: "gemini-2.5-flash-lite", llm_mode: "inline",
      available_files: ["enriched.csv", "notEnriched.csv", "retry.csv",
                        "report.json", "run.log"],
      cost: { llm_usd: 0.0004, scrapedo_requests: 2, scrapedo_credits: 20,
              scrapedo_search_successful: 2, scrapedo_ai_overview_successful: 0,
              total_usd: 0.0004 },
      outcome_breakdown: { found: 2, not_found: 0, errored: 0 },
      empty_response_breakdown: { no_ai_overview: 0, deferred: 0 },
      token_usage: { prompt_tokens: 200, completion_tokens: 100, total_tokens: 300 },
    },
  });
  assert(timers.length === 0, "terminal S3-only run kept polling");
  const files = byClass(root, "files-section")[0];
  assert(files, "terminal firmographics run hid its Files surface");
  assert(files.textContent.includes("enriched.csv")
    && files.textContent.includes("notEnriched.csv"),
  "firmographics did not advertise its own result files");
  assert(!files.textContent.includes("found.csv"),
    "firmographics advertised gsearch's file names");
  assert(!files.textContent.includes("output.json")
    && !files.textContent.includes("output.csv"),
  "firmographics advertised state-driven output endpoints it has no state for");
  // No Confidence chip: the website is an input, so there is nothing to be confident
  // about. The LLM chip is what says a paid model ran, and in which mode.
  assert(!root.textContent.includes("Heuristic"),
    "firmographics showed a confidence mode it does not have");
  assert(root.textContent.includes("Inline"), "firmographics hid its LLM mode");
}

async function failedReportingRunShowsFiles() {
  const { root } = await renderStatus("failed-report", {
    pipeline: "gsearch", status: "failed", total_rows: 3, processed_rows: 3,
    success_rows: 1, failed_rows: 2,
    serpwow_summary: {
      confidence_mode: "llm", is_batch: false,
      outcome_breakdown: { found: 1, not_found: 0, errored: 2 },
      error_breakdown: { by_source: { serpwow: 2 }, by_category: { upstream: 2 } },
      websites_found: 1, websites_not_found: 2, available_files: ["run.log"], cost: {},
    },
  });
  assert(timers.length === 0, "failed reporting run did not terminate polling");
  const files = byClass(root, "files-section")[0];
  assert(files, "failed reporting run hid Files surface");
  const log = byClass(files, "file-row").find((row) => row.textContent.includes("run.log"));
  assert(!log?.children[1]?.children[0]?.disabled, "available failed-run log was disabled");
  const found = byClass(files, "file-row").find((row) => row.textContent.includes("found.csv"));
  assert(found?.children[1]?.children[0]?.disabled, "absent failed-run result was enabled");
  assert(files.textContent.includes("output.json") && files.textContent.includes("output.csv"),
    "failed run lost output endpoint fallbacks");
}

async function cancelledBatchTerminalizes() {
  const base = {
    pipeline: "gsearch", status: "completed", total_rows: 3, processed_rows: 3,
    success_rows: 1, failed_rows: 0,
    serpwow_summary: {
      confidence_mode: "llm", is_batch: true,
      outcome_breakdown: { found: 1, not_found: 2, errored: 0 },
      error_breakdown: { by_source: {}, by_category: {} },
      websites_found: 1, websites_not_found: 2, available_files: ["run.log"], cost: {},
    },
  };
  const { root } = await renderLegacySequence("cancel", [
    { ...base, gemini_batch: { status: "cancel_requested" } },
    { ...base, gemini_batch: { status: "cancelled" } },
  ]);
  assert(timers.length === 1, "cancel_requested stopped polling before terminal state");
  assert(!byText(root, "button", "Stop run"), "cancel_requested exposed duplicate Stop action");
  assert(!byClass(root, "files-section").length, "cancel_requested exposed terminal files");
  await timers.shift()();
  await settle();
  assert(timers.length === 0, "cancelled batch did not terminalize polling");
  assert(!root.textContent.includes("finalizing"), "cancelled batch remained finalizing");
  assert(!byText(root, "button", "Stop run"), "cancelled batch exposed Stop action");
  const files = byClass(root, "files-section")[0];
  assert(files, "cancelled batch did not reveal Files surface");
  const log = byClass(files, "file-row").find((row) => row.textContent.includes("run.log"));
  assert(!log?.children[1]?.children[0]?.disabled, "cancelled batch disabled available run.log");
}

async function legacyCompatibility() {
  const { root } = await renderStatus("legacy", {
    pipeline: "firmographics", status: "completed_with_errors", total_rows: 7, processed_rows: 7,
    success_rows: 5, failed_rows: 2, processing_seconds_total: 14, processing_seconds_avg: 2,
    updated_at: "2026-07-12T10:30:00Z",
  });
  assertOutcomeFirst(root, "5 of 7");
  assert(byClass(root, "outcome-label")[0]?.textContent === "Succeeded",
    "non-reporting pipeline must describe generic successes, not websites");
  assert(labelValue(root, "Not found") == null, "non-reporting pipeline invented Not found");
  assert(labelValue(root, "Failed") === "2", "legacy failed rows must use Failed label");
  const title = byClass(root, "detail-title")[0]?.textContent;
  const subtitle = byClass(root, "detail-subtitle")[0]?.textContent;
  assert(title === "Firmographics", "legacy header exposed raw pipeline code or upload reference");
  assert(subtitle?.includes("Run legacy") && subtitle.includes("Updated") && subtitle.includes("Jul"),
    "legacy header omitted the human-readable run context timestamp");
  assert(byClass(root, "files-section").length === 1, "legacy terminal files missing");
}

function aiPayload(status, errors = 0) {
  const notFound = Math.max(0, 2 - errors);
  return {
    company_name: "AI Co", mode_label: "AI Mode", status, phase: "cleanup",
    total_rows: 6, entities_processed: 6, websites_found: 4, websites_not_found: 2,
    llm_errors: errors,
    outcome_breakdown: { found: 4, not_found: notFound, errored: errors },
    batches_done: 2, batches_total: 2, batch_duration_seconds: 18,
    token_usage: { prompt_tokens: 120, completion_tokens: 30 }, model: "gemini-ai", is_batch: true,
    scrapedo_request_count: 5, failed_request_count: 0,
    cost: { scrapedo_searches: 5, llm_usd: 0.4, total_usd: 0.4 },
    available_files: ["found.csv", "run.log"],
  };
}

function assertProductionTerminalAiShape(payload) {
  const outcome = payload.outcome_breakdown;
  assert(outcome.found + outcome.not_found + outcome.errored === payload.total_rows,
    "terminal AI fixture outcome does not reconcile to total");
  assert(payload.websites_found === outcome.found,
    "terminal AI fixture websites_found differs from canonical found");
  assert(payload.websites_not_found === outcome.not_found + outcome.errored,
    "terminal AI fixture websites_not_found is not inclusive of errors");
  assert(payload.cost.llm_usd === payload.cost.total_usd,
    "terminal AI fixture does not match production LLM cost shape");
}

async function completedAiMode() {
  const payload = aiPayload("completed");
  assertProductionTerminalAiShape(payload);
  const { root } = await renderStatus("ai", payload, { ai: true });
  assertOutcomeFirst(root, "4 of 6");
  for (const text of ["18s", "120", "30", "gemini-ai", "Yes", "Batches", "found.csv"]) {
    assert(root.textContent.includes(text), `AI Mode detail missing ${text}`);
  }
  const progress = byClass(root, "progress-section")[0];
  assert(progress?.getAttribute("role") === "group"
    && progress.getAttribute("aria-label") === "Batch progress",
  "batch progress semantics missing");
  const llmCost = byClass(root, "cost-item").find((item) => item.children[0]?.textContent === "LLM");
  assert(llmCost?.children[1]?.textContent === "$0.4000", "AI cost did not prefer explicit llm_usd");
  const unavailable = byClass(root, "file-row").find((row) => row.textContent.includes("final_report.json"));
  assert(unavailable?.children[1]?.children[0]?.disabled, "unavailable AI file View must be disabled");
  assert(unavailable?.children[1]?.children[1]?.getAttribute("href") == null,
    "unavailable AI file Download must not have an href");
  assert(unavailable?.children[1]?.children[1]?.getAttribute("aria-disabled") === "true",
    "unavailable AI file Download must expose aria-disabled");
  assert(!byText(root, "button", "Rerun failed"), "successful AI Mode run exposed rerun");
}

async function erroredAiMode() {
  const ref = "ai errors";
  const payload = aiPayload("completed_with_errors", 1);
  payload.failed_request_count = 1;
  payload.scrapedo_failed_requests = 1;
  assertProductionTerminalAiShape(payload);
  const { root } = await renderStatus(ref, payload, { ai: true });
  assertOutcomeFirst(root, "4 of 6");
  assert(labelValue(root, "Not found") === "1", "AI inclusive not-found double counted errors");
  assert(labelValue(root, "Errors") === "1", "AI Mode outcome errors wrong");
  const scrapeCost = byClass(root, "cost-item").find((item) => item.children[0]?.textContent === "Scrape.do");
  assert(scrapeCost?.textContent.includes("5 searches") && scrapeCost.textContent.includes("1 failed"),
    "AI scrape.do cost did not show failed search count");
  const rerun = byText(root, "button", "Rerun failed");
  assert(rerun?.listeners.click, "AI Mode rerun action missing");
  await rerun.click();
  assert(requests.some(({ path, options }) =>
    path === `/uploads/ai-mode/${encodeURIComponent(ref)}/resume` && options.method === "POST"),
  "AI Mode rerun endpoint changed");
}

async function queuedAiFiles() {
  const payload = {
    run_id: "queued-ai", status: "queued", mode: "bulk", mode_label: "AI Mode Bulk",
    company_id: "co-1", company_name: "AI Co", columns_detected: ["company_name"],
    warnings: [], total_rows: 6, batch_size: 20,
    llm_provider: "gemini", llm_model: "gemini-2.5-flash-lite",
    batches_total: 0, batches_done: 0, entities_processed: 0,
    entities_without_scrape_data: 0, llm_errors: 0,
    websites_found: 0, websites_not_found: 0,
    failed_request_count: 0, scrapedo_request_count: 0, scrapedo_failed_requests: 0,
    scrapedo_seconds_total: 0.0, llm_seconds_total: 0.0,
    token_usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    error: null, available_files: ["input.csv"],
  };
  const { root } = await renderStatus("queued-ai", payload, { ai: true });
  assertOutcomeFirst(root, "0 of 6");
  assert(labelValue(root, "Not found") === "0", "queued AI not-found count is not production-real");
  assert(labelValue(root, "Errors") === "0", "queued AI error count is not production-real");
  assert(!byClass(root, "pill").some((pill) => pill.children[0]?.textContent === "Model"),
    "queued AI initial status invented a runtime model chip");
  assert(!byClass(root, "cost-section").length, "queued AI initial status invented a cost section");
  const files = byClass(root, "files-section")[0];
  assert(files, "queued AI run must retain file availability surface");
  assert(byClass(files, "file-row").length === 5, "queued AI file rows missing");
  const input = byClass(files, "file-row").find((row) => row.textContent.includes("input.csv"));
  assert(!input?.children[1]?.children[0]?.disabled, "always-present queued input.csv was disabled");
  assert(byClass(files, "file-row").filter((row) => !row.textContent.includes("input.csv")).every((row) =>
    row.children[1]?.children[0]?.disabled), "queued AI enabled a result that is not available");
}

async function malformedLegacyAiInclusiveOutcome() {
  const payload = aiPayload("completed_with_errors", 1);
  delete payload.outcome_breakdown;
  const { root } = await renderStatus("malformed-legacy-ai", payload, { ai: true });
  assert(labelValue(root, "Not found") === "1", "legacy inclusive not-found fallback regressed");
  assert(labelValue(root, "Errors") === "1", "legacy llm_errors fallback regressed");
}

async function unknownTotalAndLongModel() {
  const longModel = "gemini-2.5-pro-preview-with-an-extremely-long-model-identifier";
  const payload = aiPayload("completed");
  payload.total_rows = null;
  payload.entities_processed = null;
  payload.model = longModel;
  const { root } = await renderStatus("unknown-total", payload, { ai: true });
  const primary = byClass(root, "outcome-primary")[0];
  assert(byClass(primary, "outcome-value")[0]?.textContent === "4", "unknown total must retain found value");
  assert(!primary.textContent.includes("of 0") && !primary.textContent.includes("%"),
    "unknown total must omit denominator and percentage");
  const modelPill = byClass(root, "pill").find((pill) => pill.children[0]?.textContent === "Model");
  const value = modelPill?.children[1];
  assert(value?.classList.contains("pill-value"), "model pill value class missing");
  assert(value?.getAttribute("title") === longModel, "model pill must expose full value as title");
  assert(byClass(root, "pill").some((pill) => pill.children[0]?.textContent === "Batch mode"),
    "AI Batch mode header chip missing");
  assert(!root.textContent.includes("Total / Processed"), "unknown total execution metric should be omitted");
}

async function customPollTerminalPredicate() {
  timers = [];
  const updates = [];
  const statuses = [
    { status: "completed", batch: "running" },
    { status: "completed", batch: "succeeded" },
  ];
  globalThis.fetch = async () => response(statuses.shift());
  pollStatus("/predicate", (status) => updates.push(status), 1,
    (status) => status.batch === "succeeded");
  await settle();
  assert(updates.length === 1 && timers.length === 1, "custom terminal predicate did not continue polling");
  await timers.shift()();
  await settle();
  assert(updates.length === 2 && timers.length === 0, "custom terminal predicate did not stop polling");
}

async function accessibleModalLifecycleAndRace() {
  const ref = "modal-run";
  const { root, cleanup } = await renderStatus(ref, {
    pipeline: "gsearch", status: "completed", total_rows: 2, processed_rows: 2,
    success_rows: 2, failed_rows: 0,
    serpwow_summary: {
      confidence_mode: "llm", is_batch: false,
      outcome_breakdown: { found: 2, not_found: 0, errored: 0 },
      error_breakdown: { by_source: {}, by_category: {} },
      websites_found: 2, websites_not_found: 0,
      available_files: ["found.csv", "run.log"], cost: {},
    },
  });
  root.setAttribute("aria-hidden", "false");
  const rows = byClass(root, "file-row");
  const foundView = rows.find((row) => row.textContent.includes("found.csv")).children[1].children[0];
  const logView = rows.find((row) => row.textContent.includes("run.log")).children[1].children[0];
  foundView.focus();

  const first = deferred();
  const second = deferred();
  const third = deferred();
  const fourth = deferred();
  const signals = [];
  let fileRequest = 0;
  globalThis.fetch = (_path, options = {}) => {
    signals.push(options.signal);
    fileRequest += 1;
    return [first.promise, second.promise, third.promise, fourth.promise][fileRequest - 1];
  };

  const firstLoad = foundView.click();
  const surface = byClass(document.body, "modal-surface")[0];
  const overlay = byClass(document.body, "file-modal")[0];
  const title = surface.children[0].children[0];
  const download = surface.children[0].children[1];
  const close = surface.children[0].children[2];
  assert(surface.getAttribute("role") === "dialog", "file viewer missing dialog role");
  assert(surface.getAttribute("aria-modal") === "true", "file viewer missing aria-modal");
  assert(surface.getAttribute("aria-labelledby") === title.getAttribute("id"),
    "file viewer title association missing");
  assert(title.textContent === "found.csv", "file viewer title was not set before fetch");
  assert(root.inert && root.getAttribute("aria-hidden") === "true", "file viewer did not isolate main");
  assert(document.activeElement === close, "file viewer did not focus Close");

  overlay.dispatch("keydown", { key: "Tab", shiftKey: false });
  assert(document.activeElement === download, "Tab did not wrap from last to first modal control");
  overlay.dispatch("keydown", { key: "Tab", shiftKey: true });
  assert(document.activeElement === close, "Shift+Tab did not wrap from first to last modal control");

  overlay.dispatch("keydown", { key: "Escape" });
  assert(overlay.classList.contains("hidden"), "Escape did not close file viewer");
  assert(!root.inert && root.getAttribute("aria-hidden") === "false", "Escape did not restore main state");
  assert(document.activeElement === foundView, "Escape did not restore prior focus");
  assert(signals[0]?.aborted, "closing did not abort the active file request");
  first.resolve(response("ignored after close"));
  await firstLoad;
  assert(!document.body.textContent.includes("Failed to load"), "aborted request rendered an error");

  foundView.focus();
  const staleLoad = foundView.click();
  const currentLoad = logView.click();
  third.resolve(response("second response"));
  await currentLoad;
  second.resolve(response("first response"));
  await staleLoad;
  assert(title.textContent === "run.log", "stale request overwrote newer filename");
  assert(surface.textContent.includes("second response") && !surface.textContent.includes("first response"),
    "stale request overwrote newer file body");
  assert(signals[1]?.aborted, "opening a second file did not abort the first request");

  overlay.dispatch("click", { target: overlay });
  assert(overlay.classList.contains("hidden"), "backdrop click did not close file viewer");
  assert(document.activeElement === foundView, "backdrop close did not restore original focus");

  const routeLoad = foundView.click();
  assert(!overlay.classList.contains("hidden"), "file viewer did not reopen");
  cleanup();
  assert(overlay.classList.contains("hidden"), "route cleanup did not close file viewer");
  assert(!root.inert && root.getAttribute("aria-hidden") === "false",
    "route cleanup did not restore main state");
  assert(signals[3]?.aborted, "route cleanup did not abort pending file request");
  // fetch intentionally remains pending; cleanup must make it unable to update the UI.
  void routeLoad;
}

await customPollTerminalPredicate();
await completedGsearchLlm();
await completedGmapsHeuristic();
await gmapsBillingBreakdown();
await deadGeminiShardIsVisibleAndRerunnable();
await rerunButtonShowsOnACleanS3Run();
await inFlightScrapedoRunIsNotLabelledSerpWow();
await preMigrationGmapsKeepsSerpWowCard();
await failedRowsViewer();
await completedRelationship();
await counterDrivenRelationshipTerminal();
await counterDrivenRelationshipRunning();
await counterDrivenRelationshipFailedMidScrape();
await finalizingBatch();
await completedWithErrorsBatchIsTerminal();
await firmographicsIsS3OnlyAndCarriesNoBatchState();
await failedReportingRunShowsFiles();
await cancelledBatchTerminalizes();
await legacyCompatibility();
await completedAiMode();
await erroredAiMode();
await queuedAiFiles();
await malformedLegacyAiInclusiveOutcome();
await unknownTotalAndLongModel();
await accessibleModalLifecycleAndRace();
