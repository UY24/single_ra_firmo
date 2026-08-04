class ClassList {
  constructor(element) {
    this.element = element;
  }

  _tokens() {
    return this.element.className.split(/\s+/).filter(Boolean);
  }

  add(...tokens) {
    this.element.className = [...new Set([...this._tokens(), ...tokens])].join(" ");
  }

  remove(...tokens) {
    const removed = new Set(tokens);
    this.element.className = this._tokens().filter((token) => !removed.has(token)).join(" ");
  }

  contains(token) {
    return this._tokens().includes(token);
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
    this.selected = false;
    this.disabled = false;
    this.classList = new ClassList(this);
  }

  setAttribute(name, value) {
    const stringValue = String(value);
    this.attributes[name] = stringValue;
    if (name === "class") this.className = stringValue;
    if (name === "value") this.value = stringValue;
  }

  getAttribute(name) {
    if (name === "class") return this.className || null;
    return this.attributes[name] ?? null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }

  append(...children) {
    this.childNodes.push(...children);
  }

  replaceChildren(...children) {
    this.childNodes = children;
  }

  get children() {
    return this.childNodes.filter((child) => child instanceof Element);
  }

  get textContent() {
    return this.childNodes.map((child) =>
      child instanceof Element ? child.textContent : String(child)).join("");
  }

  set textContent(value) {
    this.childNodes = [String(value)];
  }

  click() {
    this.listeners.click?.({ preventDefault() {} });
  }
}

globalThis.document = {
  createElement: (tag) => new Element(tag),
};
globalThis.window = { location: { hash: "" } };

let responses = new Map();
globalThis.fetch = async (path) => {
  if (!responses.has(path)) throw new Error(`Unexpected fetch: ${path}`);
  return {
    ok: true,
    statusText: "OK",
    json: async () => responses.get(path),
  };
};

const [{ render: renderDashboard }, { render: renderCompanies }, { render: renderRuns }] =
  await Promise.all([
    import("../app/static/js/dashboard.js"),
    import("../app/static/js/companies.js"),
    import("../app/static/js/runs.js"),
  ]);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function all(node) {
  return [node, ...node.children.flatMap(all)];
}

function byClass(node, className) {
  return all(node).filter((candidate) => candidate.classList.contains(className));
}

function byTag(node, tagName) {
  return all(node).filter((candidate) => candidate.tagName === tagName.toUpperCase());
}

function byAttribute(node, name, value) {
  return all(node).filter((candidate) => candidate.getAttribute(name) === value);
}

function linkByHref(node, href) {
  return byTag(node, "a").find((candidate) => candidate.getAttribute("href") === href);
}

function hasDescendant(node, tagName) {
  return node.children.some((child) =>
    child.tagName === tagName.toUpperCase() || hasDescendant(child, tagName));
}

async function dashboardContract() {
  responses = new Map([
    ["/companies/stats", { companies: [{
      id: "co id/&",
      name: "Zero Co",
      runs: 1,
      websites_found: 0,
      websites_not_found: 0,
      total_searches: 0,
      total_rows: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cost_usd: 0,
    }] }],
    ["/companies/runs", { runs: [{
      run_ref: "run id/1?",
      company_id: "co id/&",
      pipeline: "gmaps",
      status: "completed",
      total_rows: 0,
      websites_found: 0,
      cost: 0,
      created_at: "2026-07-12T10:00:00Z",
    }] }],
  ]);
  const root = new Element("main");
  await renderDashboard(root);

  assert(byClass(root, "page-intro").length === 1, "dashboard page intro missing");
  assert(linkByHref(root, "#/new-run")?.textContent === "New run", "dashboard New run link missing");
  const summary = byClass(root, "company-summary")[0];
  assert(summary?.tagName === "A", "company summary must be an anchor");
  assert(summary.getAttribute("href") === "#/runs?company_id=co%20id%2F%26", "company summary href is not encoded");
  assert(byClass(summary, "company-outcome-rate")[0]?.textContent.startsWith("—"), "zero outcomes must render an em dash rate");
  assert(!byTag(root, "button").some((button) => hasDescendant(button, "dl")), "a button must not wrap company metrics");
  const runLink = linkByHref(root, "#/runs/run%20id%2F1%3F?engine=serpwow");
  assert(runLink, "dashboard run link is not a real encoded anchor");
  assert(runLink.getAttribute("aria-label")?.includes("Zero Co"), "dashboard run link needs a helpful aria-label");
}

async function companiesContract() {
  responses = new Map([
    ["/companies/stats", { companies: [{
      id: "co id/&",
      name: "Zero Co",
      runs: 1,
      websites_found: 0,
      websites_not_found: 0,
      total_cost_usd: 0,
      created_at: "2026-07-12T10:00:00Z",
    }] }],
  ]);
  const root = new Element("main");
  await renderCompanies(root);

  const input = byAttribute(root, "id", "company-name")[0];
  const label = byAttribute(root, "for", "company-name")[0];
  assert(input?.tagName === "INPUT" && label?.tagName === "LABEL", "company-name label association missing");
  assert(byAttribute(root, "aria-live", "polite").length === 1, "company form message must be aria-live polite");
  const companyLink = linkByHref(root, "#/runs?company_id=co%20id%2F%26");
  assert(companyLink?.textContent === "Zero Co", "company name must be a real encoded anchor");
}

async function runsContract() {
  responses = new Map([
    ["/companies", { companies: [{ id: "c 1", name: "Acme" }] }],
    ["/companies/runs?company_id=c+1&pipeline=gmaps", { runs: [{
      run_ref: "run /2?",
      company_id: "c 1",
      pipeline: "gmaps",
      status: "completed",
      total_rows: 3,
      websites_found: 2,
      cost: 0,
      duration_seconds: 12,
      created_at: "2026-07-12T10:00:00Z",
    }] }],
  ]);
  const root = new Element("main");
  await renderRuns(root, { query: { company_id: "c 1", pipeline: "gmaps", status: "completed" } });

  for (const id of ["runs-company-filter", "runs-pipeline-filter", "runs-status-filter"]) {
    assert(byAttribute(root, "id", id)[0]?.tagName === "SELECT", `${id} select missing`);
    assert(byAttribute(root, "for", id)[0]?.tagName === "LABEL", `${id} label missing`);
  }
  const [companySelect, pipelineSelect, statusSelect] = [
    "runs-company-filter", "runs-pipeline-filter", "runs-status-filter",
  ].map((id) => byAttribute(root, "id", id)[0]);
  assert(companySelect.value === "c 1", "company filter did not hydrate from query");
  assert(pipelineSelect.value === "gmaps", "pipeline filter did not hydrate from query");
  assert(statusSelect.value === "completed", "status filter did not hydrate from query");
  assert(linkByHref(root, "#/runs")?.textContent === "Clear", "Clear anchor missing");
  const runLink = linkByHref(root, "#/runs/run%20%2F2%3F?engine=serpwow");
  assert(runLink, "runs table link is not a real encoded anchor");
  assert(runLink.getAttribute("aria-label")?.includes("Acme"), "runs table link needs a helpful aria-label");

  byTag(root, "button").find((button) => button.textContent === "Apply").click();
  assert(
    window.location.hash === "#/runs?company_id=c+1&pipeline=gmaps&status=completed",
    "Apply did not preserve URL-backed company, pipeline, and status filters",
  );
}

await dashboardContract();
await companiesContract();
await runsContract();
