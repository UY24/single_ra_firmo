class ClassList {
  constructor(element) { this.element = element; }
  _tokens() { return this.element.className.split(/\s+/).filter(Boolean); }
  add(...tokens) { this.element.className = [...new Set([...this._tokens(), ...tokens])].join(" "); }
  remove(...tokens) {
    const removed = new Set(tokens);
    this.element.className = this._tokens().filter((token) => !removed.has(token)).join(" ");
  }
  contains(token) { return this._tokens().includes(token); }
  toggle(token, force) {
    const enabled = force === undefined ? !this.contains(token) : Boolean(force);
    if (enabled) this.add(token); else this.remove(token);
    return enabled;
  }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attributes = {};
    this.className = "";
    this.childNodes = [];
    this.listeners = {};
    this.value = "";
    this.disabled = false;
    this.classList = new ClassList(this);
  }
  setAttribute(name, value) {
    const stringValue = String(value);
    this.attributes[name] = stringValue;
    if (name === "class") this.className = stringValue;
    if (name === "value") this.value = stringValue;
  }
  getAttribute(name) { return name === "class" ? this.className || null : this.attributes[name] ?? null; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  append(...children) { this.childNodes.push(...children); }
  appendChild(child) { this.childNodes.push(child); return child; }
  replaceChildren(...children) { this.childNodes = children; }
  get children() { return this.childNodes.filter((child) => child instanceof Element); }
  get textContent() {
    return this.childNodes.map((child) => child instanceof Element ? child.textContent : String(child)).join("");
  }
  set textContent(value) { this.childNodes = [String(value)]; }
  click() {
    if (this.disabled) return undefined;
    return this.listeners.click?.({ preventDefault() {}, stopPropagation() {} });
  }
}

globalThis.document = {
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => String(text),
};
globalThis.window = { location: { hash: "" } };
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: { clipboard: { writeText: async () => {} } },
});
globalThis.confirm = () => true;

let nextTimer = 0;
let timers = new Map();
let intervalCalls = [];
globalThis.setTimeout = (callback, ms) => {
  const id = ++nextTimer;
  timers.set(id, { callback, ms });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
globalThis.setInterval = (callback, ms) => {
  intervalCalls.push({ callback, ms });
  return ++nextTimer;
};
globalThis.clearInterval = () => {};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function response(payload) {
  return { ok: true, statusText: "OK", json: async () => payload };
}

let queues = new Map();
let requests = [];
function queue(path, ...items) { queues.set(path, items); }
globalThis.fetch = (path, options = {}) => {
  const key = String(path);
  const request = { path: key, options, aborted: options.signal?.aborted ?? false };
  options.signal?.addEventListener("abort", () => { request.aborted = true; });
  requests.push(request);
  const items = queues.get(key) ?? [];
  if (items.length === 0) throw new Error(`Unexpected fetch: ${key}`);
  const item = items.shift();
  return item?.promise ?? Promise.resolve(response(item));
};

const [{ render: renderOperations }, { render: renderTools }] = await Promise.all([
  import("../app/static/js/operations.js"),
  import("../app/static/js/tools.js"),
]);

function assert(condition, message) { if (!condition) throw new Error(message); }
function all(node) { return [node, ...node.children.flatMap(all)]; }
function byClass(node, name) { return all(node).filter((candidate) => candidate.classList.contains(name)); }
function byTag(node, name) { return all(node).filter((candidate) => candidate.tagName === name.toUpperCase()); }
function byText(node, name, text) { return byTag(node, name).find((candidate) => candidate.textContent === text); }
function byAttribute(node, name, value) {
  return all(node).filter((candidate) => candidate.getAttribute(name) === value);
}
function linkByHref(node, href) {
  return byTag(node, "a").find((candidate) => candidate.getAttribute("href") === href);
}
function countRequests(path) { return requests.filter((request) => request.path === path).length; }
async function settle() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve();
}
async function runTimer(ms) {
  const entry = [...timers.entries()].find(([, timer]) => timer.ms === ms);
  assert(entry, `missing ${ms}ms timer`);
  timers.delete(entry[0]);
  entry[1].callback();
  await settle();
}
function reset() {
  queues = new Map();
  requests = [];
  timers = new Map();
  intervalCalls = [];
}

const emptyJobs = { jobs: [] };
const emptyUploads = { uploads: [] };

async function terminalAndActionContract() {
  reset();
  queue("/uploads?limit=200", { uploads: [
    {
      upload_id: "partial", pipeline: "gmaps", status: "completed_with_errors",
      gemini_batch: { status: "completed_with_errors" }, file_links: {},
    },
    {
      upload_id: "gmaps-running", pipeline: "gmaps", status: "completed",
      gemini_batch: { status: "running" }, file_links: {},
    },
  ] });
  const managedJob = {
    upload_id: "up id/1?", upload_status: "completed", batch_status: "cancelled",
    live_state: "cancelled", job_name: "jobs/one & two", batch_generation: 7,
  };
  const partialJob = {
    upload_id: "partial", upload_status: "completed_with_errors",
    batch_status: "completed_with_errors", live_state: "completed_with_errors",
    job_name: "jobs/partial",
  };
  queue("/batch/jobs?limit=300", { jobs: [managedJob, partialJob] }, { jobs: [managedJob] }, emptyJobs);
  const cancelPath = "/batch/jobs/cancel?job_name=jobs%2Fone%20%26%20two&upload_id=up%20id%2F1%3F&expected_generation=7";
  const deletePath = "/batch/jobs/delete?job_name=jobs%2Fone%20%26%20two&upload_id=up%20id%2F1%3F&expected_generation=7";
  const cancelPending = deferred();
  queue(cancelPath, cancelPending);
  queue(deletePath, {});

  const root = new Element("main");
  const cleanup = renderOperations(root);
  assert(typeof cleanup === "function", "operations render must return cleanup synchronously");
  await settle();
  assert(intervalCalls.length === 0, "operations must not use setInterval");
  assert([...timers.values()].filter(({ ms }) => ms === 4000).length === 2,
    "each settled refresh needs one sequential timer");
  assert(linkByHref(root, "/uploads/partial/output?download=true"),
    "completed_with_errors batch should enable JSON download");
  assert(!linkByHref(root, "/uploads/gmaps-running/output?download=true"),
    "gmaps running batch must not enable downloads");
  assert(linkByHref(root, "#/runs/up%20id%2F1%3F?engine=serpwow"),
    `batch upload ID needs a real run link: ${byTag(root, "a").map((link) => link.getAttribute("href")).join(",")}`);
  assert(byText(root, "span", "Batch Cancelled"), "cancelled label missing");
  const partialLabel = byText(root, "span", "Batch Completed With Errors");
  assert(partialLabel?.classList.contains("pill--warn"),
    "completed_with_errors batch needs a distinct warning label");

  const cancel = byText(root, "button", "Cancel");
  const firstClick = cancel.click();
  cancel.click();
  assert(countRequests(cancelPath) === 1, "busy job allowed duplicate cancel request");
  for (const text of ["Get Status", "Cancel", "Delete"]) {
    assert(byText(root, "button", text).disabled, `${text} was not disabled for busy job`);
  }
  cancelPending.resolve(response({ local_state_updated: true }));
  await firstClick;
  await settle();

  await byText(root, "button", "Delete").click();
  assert(countRequests(deletePath) === 1, "delete did not include encoded upload_id");
  cleanup();
  assert(timers.size === 0, "operations cleanup left refresh timers");
}

async function retryTerminalContract() {
  reset();
  queue("/uploads?limit=200", emptyUploads);
  queue("/batch/jobs?limit=300", emptyJobs);
  queue("/uploads/retry/retry-failed-rows", { enqueued_rows: 1 });
  queue("/uploads/retry/status",
    { status: "completed", gemini_batch: { status: "running" } },
    { status: "completed", gemini_batch: { status: "succeeded" } });
  const root = new Element("main");
  const cleanup = renderOperations(root);
  await settle();
  const input = byAttribute(root, "id", "retry-upload-id")[0];
  input.value = "retry";
  await byText(root, "button", "Retry Failed Rows").click();
  await settle();
  assert(countRequests("/uploads/retry/status") === 1, "retry did not start status poll");
  assert([...timers.values()].some(({ ms }) => ms === 2000),
    "completed rows with running batch incorrectly stopped polling");
  await runTimer(2000);
  assert(countRequests("/uploads/retry/status") === 2, "retry did not continue until batch terminal");
  assert(![...timers.values()].some(({ ms }) => ms === 2000),
    "succeeded batch did not stop retry polling");
  cleanup();
}

async function operationsCleanupAndGenerationContract() {
  reset();
  const staleUploads = deferred();
  const staleJobs = deferred();
  queue("/uploads?limit=200", staleUploads);
  queue("/batch/jobs?limit=300", staleJobs);
  const root = new Element("main");
  const cleanup = renderOperations(root);
  const initialText = root.textContent;
  cleanup();
  assert(requests.every((request) => request.aborted), "cleanup did not abort pending operations requests");
  staleUploads.resolve(response({ uploads: [{ upload_id: "stale" }] }));
  staleJobs.resolve(response({ jobs: [{ job_name: "stale" }] }));
  await settle();
  assert(root.textContent === initialText, "pending operations request mutated DOM after cleanup");
  assert(timers.size === 0, "pending operations request scheduled after cleanup");

  reset();
  const oldUploads = deferred();
  queue("/uploads?limit=200", oldUploads, { uploads: [{
    upload_id: "latest", status: "completed", gemini_batch: { status: "not_started" }, file_links: {},
  }] });
  queue("/batch/jobs?limit=300", emptyJobs);
  const latestRoot = new Element("main");
  const latestCleanup = renderOperations(latestRoot);
  await settle();
  await byText(latestRoot, "button", "Refresh").click();
  await settle();
  assert(latestRoot.textContent.includes("latest"), "manual refresh did not render latest result");
  assert(requests[0].aborted, "manual refresh did not abort prior history request");
  oldUploads.resolve(response({ uploads: [{ upload_id: "stale" }] }));
  await settle();
  assert(!latestRoot.textContent.includes("stale"), "out-of-order history response won over latest");
  latestCleanup();
}

async function retryCleanupContract() {
  reset();
  queue("/uploads?limit=200", emptyUploads);
  queue("/batch/jobs?limit=300", emptyJobs);
  const pendingPost = deferred();
  queue("/uploads/retry/retry-failed-rows", pendingPost);
  const root = new Element("main");
  const cleanup = renderOperations(root);
  await settle();
  byAttribute(root, "id", "retry-upload-id")[0].value = "retry";
  byText(root, "button", "Retry Failed Rows").click();
  const postRequest = requests.find(({ path }) => path === "/uploads/retry/retry-failed-rows");
  cleanup();
  assert(postRequest.aborted, "retry POST was not aborted on cleanup");
  pendingPost.resolve(response({ enqueued_rows: 1 }));
  await settle();
  assert(countRequests("/uploads/retry/status") === 0, "cleanup allowed orphan retry poll");
}

async function retryReplacementStopsOldPollContract() {
  reset();
  queue("/uploads?limit=200", emptyUploads);
  queue("/batch/jobs?limit=300", emptyJobs);
  const secondPost = deferred();
  queue("/uploads/retry/retry-failed-rows", { enqueued_rows: 1 }, secondPost);
  queue("/uploads/retry/status", { status: "completed", gemini_batch: { status: "running" } });
  const root = new Element("main");
  const cleanup = renderOperations(root);
  await settle();
  byAttribute(root, "id", "retry-upload-id")[0].value = "retry";
  await byText(root, "button", "Retry Failed Rows").click();
  await settle();
  assert([...timers.values()].some(({ ms }) => ms === 2000), "first retry poll did not start");
  byText(root, "button", "Retry Failed Rows").click();
  assert(![...timers.values()].some(({ ms }) => ms === 2000),
    "starting a replacement retry left the prior poll scheduled");
  cleanup();
  secondPost.resolve(response({ enqueued_rows: 1 }));
  await settle();
}

async function sequentialRefreshContract() {
  reset();
  const pendingNextHistory = deferred();
  queue("/uploads?limit=200", emptyUploads, pendingNextHistory);
  queue("/batch/jobs?limit=300", emptyJobs);
  const root = new Element("main");
  const cleanup = renderOperations(root);
  await settle();
  assert([...timers.values()].filter(({ ms }) => ms === 4000).length === 2,
    "initial refresh loops were not scheduled");
  await runTimer(4000);
  assert(countRequests("/uploads?limit=200") === 2, "history refresh timer did not run");
  assert([...timers.values()].filter(({ ms }) => ms === 4000).length === 1,
    "history loop scheduled another timer before its request settled");
  pendingNextHistory.resolve(response(emptyUploads));
  await settle();
  assert([...timers.values()].filter(({ ms }) => ms === 4000).length === 2,
    "history loop did not resume after its request settled");
  cleanup();
}

async function toolsLifecycleAndAccessibilityContract() {
  reset();
  const root = new Element("main");
  const cleanup = renderTools(root);
  assert(typeof cleanup === "function", "tools render must return cleanup synchronously");
  assert(requests.length === 0, "tools made an initial API request");
  for (const label of byTag(root, "label")) {
    assert(label.getAttribute("for"), `tool label is not associated: ${label.textContent}`);
    assert(byAttribute(root, "id", label.getAttribute("for")).length === 1,
      `tool label target missing: ${label.textContent}`);
  }
  const pending = deferred();
  queue("/gmaps/search?q=Acme&country=bd", pending);
  byAttribute(root, "id", "gmaps-query")[0].value = "Acme";
  byAttribute(root, "id", "gmaps-country")[0].value = "bd";
  byText(root, "button", "Full Search").click();
  const toolRequest = requests[0];
  cleanup();
  assert(toolRequest.aborted, "tools cleanup did not abort pending request");
  pending.resolve(response({ official_website: "https://stale.example", processing_seconds: 1 }));
  await settle();
  assert(!root.textContent.includes("stale.example"), "tool request mutated DOM after cleanup");

  reset();
  queue("/gmaps/search?q=Acme&country=bd", {
    official_website: "https://acme.example", processing_seconds: 1, raw_response: { results: [] },
  });
  queue("/gsearch/discover?company_name=Acme&country=BD&parsed_city_state=Dhaka&full_address=Road+1&industry=Engineering&phase=phase2", {
    queries_run: 1, candidates: [],
    results: [{ success: true, phase: "phase1", query: "Acme", search_url: "https://search.example" }],
  });
  const liveRoot = new Element("main");
  const liveCleanup = renderTools(liveRoot);
  byAttribute(liveRoot, "id", "gmaps-query")[0].value = "Acme";
  byAttribute(liveRoot, "id", "gmaps-country")[0].value = "bd";
  await byText(liveRoot, "button", "Full Search").click();
  const raw = byText(liveRoot, "button", "Show raw JSON");
  assert(raw.getAttribute("aria-expanded") === "false", "raw JSON initial aria-expanded missing");
  const controls = raw.getAttribute("aria-controls");
  assert(controls && byAttribute(liveRoot, "id", controls).length === 1, "raw JSON aria-controls target missing");
  raw.click();
  assert(raw.getAttribute("aria-expanded") === "true", "raw JSON aria-expanded did not update");

  byAttribute(liveRoot, "id", "gsearch-company")[0].value = "Acme";
  byAttribute(liveRoot, "id", "gsearch-country")[0].value = "BD";
  byAttribute(liveRoot, "id", "gsearch-city")[0].value = "Dhaka";
  byAttribute(liveRoot, "id", "gsearch-address")[0].value = "Road 1";
  byAttribute(liveRoot, "id", "gsearch-industry")[0].value = "Engineering";
  byAttribute(liveRoot, "id", "gsearch-phase")[0].value = "phase2";
  await byText(liveRoot, "button", "Execute Search").click();
  const arrow = linkByHref(liveRoot, "https://search.example");
  assert(arrow.getAttribute("aria-label")?.includes("phase1"), "arrow search link lacks descriptive aria-label");
  liveCleanup();
}

await terminalAndActionContract();
await retryTerminalContract();
await operationsCleanupAndGenerationContract();
await retryCleanupContract();
await retryReplacementStopsOldPollContract();
await sequentialRefreshContract();
await toolsLifecycleAndAccessibilityContract();
