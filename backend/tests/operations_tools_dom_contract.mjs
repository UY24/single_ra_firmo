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
  click() { return this.listeners.click?.({ preventDefault() {}, stopPropagation() {} }); }
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

let nextInterval = 0;
let intervals = new Map();
let clearedIntervals = [];
globalThis.setInterval = (callback, ms) => {
  const id = ++nextInterval;
  intervals.set(id, { callback, ms });
  return id;
};
globalThis.clearInterval = (id) => { clearedIntervals.push(id); intervals.delete(id); };

let nextTimeout = 100;
let timeouts = new Map();
globalThis.setTimeout = (callback, ms) => {
  const id = ++nextTimeout;
  timeouts.set(id, { callback, ms });
  return id;
};
globalThis.clearTimeout = (id) => timeouts.delete(id);

let responses = new Map();
let requests = [];
globalThis.fetch = async (path, options = {}) => {
  requests.push({ path: String(path), options });
  if (!responses.has(String(path))) throw new Error(`Unexpected fetch: ${path}`);
  return { ok: true, statusText: "OK", json: async () => responses.get(String(path)) };
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
async function settle() {
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
}

async function operationsContract() {
  requests = [];
  intervals = new Map();
  clearedIntervals = [];
  responses = new Map([
    ["/uploads?limit=200", { uploads: [{
      upload_id: "up id/1?", pipeline: "full", status: "completed",
      total_rows: 2, processed_rows: 2, success_rows: 2, failed_rows: 0,
      gemini_batch: { status: "cancelled" }, file_links: { "state.json": "s3://state" },
    }] }],
    ["/batch/jobs?limit=300", { jobs: [{
      upload_id: "up id/1?", upload_status: "completed", batch_status: "cancelled",
      live_state: "cancelled", job_name: "jobs/one & two",
    }] }],
    ["/batch/jobs/status?job_name=jobs%2Fone%20%26%20two", { live_state: "cancelled", done: true }],
    ["/batch/jobs/cancel?job_name=jobs%2Fone%20%26%20two", {}],
    ["/batch/jobs/delete?job_name=jobs%2Fone%20%26%20two", {}],
    ["/uploads/retry%20id%2F2/retry-failed-rows", { enqueued_rows: 3 }],
    ["/uploads/retry%20id%2F2/status", {
      status: "completed", processed_rows: 3, total_rows: 3, success_rows: 3, failed_rows: 0,
      gemini_batch: { status: "succeeded" },
    }],
  ]);

  const root = new Element("main");
  const cleanup = await renderOperations(root);
  assert(byClass(root, "page-intro").length === 1, "operations page intro missing");
  assert(linkByHref(root, "#/runs/up%20id%2F1%3F"), "history row needs a real encoded run anchor");
  assert(byText(root, "span", "Batch Cancelled"), "canonical cancelled batch label missing");
  assert(intervals.size === 2 && [...intervals.values()].every(({ ms }) => ms === 4000),
    "operations refresh timers changed");

  await byText(root, "button", "Refresh").click();
  const inputs = byTag(root, "input");
  const retryInput = inputs.find((input) => input.getAttribute("id") === "retry-upload-id");
  assert(retryInput && byAttribute(root, "for", "retry-upload-id").length === 1,
    "retry Upload ID label association missing");
  retryInput.value = "retry id/2";
  await byText(root, "button", "Retry Failed Rows").click();
  await settle();

  for (const label of ["Get Status", "Cancel", "Delete"]) {
    await byText(root, "button", label).click();
  }
  for (const [path, method] of [
    ["/uploads/retry%20id%2F2/retry-failed-rows", "POST"],
    ["/uploads/retry%20id%2F2/status", undefined],
    ["/batch/jobs/status?job_name=jobs%2Fone%20%26%20two", "POST"],
    ["/batch/jobs/cancel?job_name=jobs%2Fone%20%26%20two", "POST"],
    ["/batch/jobs/delete?job_name=jobs%2Fone%20%26%20two", "POST"],
  ]) {
    const request = requests.find((candidate) => candidate.path === path);
    assert(request, `operations request missing: ${path}`);
    assert(request.options.method === method, `operations method changed: ${path}`);
  }

  cleanup();
  assert(intervals.size === 0 && clearedIntervals.length === 2, "operations timers were not cleaned up");
}

async function toolsContract() {
  requests = [];
  responses = new Map([
    ["/gmaps/search?q=Acme+%26+Co&country=bd", {
      official_website: "https://acme.example", processing_seconds: 1,
      raw_response: { results: [{ name: "Acme", website: "https://acme.example" }] },
    }],
    ["/gsearch/discover?company_name=Acme+%26+Co&country=Bangladesh&parsed_city_state=Dhaka&full_address=Road+1&industry=Engineering&phase=phase2", {
      queries_run: 1, processing_seconds: 2, candidates: ["acme.example"],
      results: [{ success: true, phase: "phase2", query: "Acme", search_url: "https://google.example/search" }],
    }],
  ]);
  const root = new Element("main");
  await renderTools(root);
  assert(requests.length === 0, "tools made an initial API request");
  assert(byClass(root, "page-intro").length === 1, "tools page intro missing");
  for (const text of ["Discover Places", "Full Search", "Fetch Place Details", "Execute Search"]) {
    assert(byText(root, "button", text), `tool action missing: ${text}`);
  }
  for (const label of byTag(root, "label")) {
    assert(label.getAttribute("for"), `tool label is not associated: ${label.textContent}`);
    assert(byAttribute(root, "id", label.getAttribute("for")).length === 1,
      `tool label target missing: ${label.textContent}`);
  }

  const values = {
    "gmaps-query": "Acme & Co", "gmaps-country": "bd",
    "gsearch-company": "Acme & Co", "gsearch-country": "Bangladesh",
    "gsearch-city": "Dhaka", "gsearch-industry": "Engineering",
    "gsearch-address": "Road 1", "gsearch-phase": "phase2",
  };
  for (const [id, value] of Object.entries(values)) byAttribute(root, "id", id)[0].value = value;
  await byText(root, "button", "Full Search").click();
  await byText(root, "button", "Execute Search").click();
  assert(requests.some(({ path }) => path === "/gmaps/search?q=Acme+%26+Co&country=bd"),
    "Google Maps URL parameters changed");
  assert(requests.some(({ path }) => path.includes("/gsearch/discover?company_name=Acme+%26+Co")
    && path.includes("&country=Bangladesh") && path.includes("&parsed_city_state=Dhaka")
    && path.includes("&full_address=Road+1") && path.includes("&industry=Engineering")
    && path.endsWith("&phase=phase2")), "Google Search URL parameters changed");
  const rawToggle = byText(root, "button", "Show raw JSON");
  assert(rawToggle, "raw JSON toggle missing after Google Maps result");
  rawToggle.click();
  assert(rawToggle.textContent === "Hide raw JSON", "raw JSON toggle did not reveal payload");
}

await operationsContract();
await toolsContract();
