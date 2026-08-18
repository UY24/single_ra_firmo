class ClassList {
  constructor(element) {
    this.element = element;
  }

  _tokens() {
    return this.element.className.split(/\s+/).filter(Boolean);
  }

  contains(token) {
    return this._tokens().includes(token);
  }

  toggle(token, force) {
    const tokens = new Set(this._tokens());
    const add = force === undefined ? !tokens.has(token) : force;
    if (add) tokens.add(token);
    else tokens.delete(token);
    this.element.className = [...tokens].join(" ");
    return add;
  }
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attributes = {};
    this.className = "";
    this.childNodes = [];
    this.listeners = {};
    this.parentNode = null;
    this.value = "";
    this.disabled = false;
    this.checked = false;
    this.files = [];
    this.inert = false;
    this.classList = new ClassList(this);
  }

  setAttribute(name, value) {
    const stringValue = String(value);
    this.attributes[name] = stringValue;
    if (name === "class") this.className = stringValue;
    if (name === "value") this.value = stringValue;
    if (name === "disabled") this.disabled = true;
  }

  getAttribute(name) {
    if (name === "class") return this.className || null;
    return this.attributes[name] ?? null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "disabled") this.disabled = false;
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }

  append(...children) {
    for (const child of children) {
      if (child instanceof Element) child.parentNode = this;
      this.childNodes.push(child);
    }
  }

  replaceChildren(...children) {
    this.childNodes = [];
    this.append(...children);
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

  get selectedOptions() {
    if (this.tagName !== "SELECT") return [];
    return this.children.filter((child) =>
      child.tagName === "OPTION" && child.value === this.value);
  }

  dispatch(name) {
    if (this.disabled) return false;
    for (let node = this; node; node = node.parentNode) {
      if (node.inert) return false;
    }
    return this.listeners[name]?.({ preventDefault() {} });
  }
}

globalThis.document = {
  createElement: (tag) => new Element(tag),
};
globalThis.window = { location: { hash: "" } };

const { render } = await import("../app/static/js/new_run.js");

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

function byText(node, tagName, text) {
  return byTag(node, tagName).find((candidate) => candidate.textContent === text);
}

function currentStep(root) {
  const current = byAttribute(root, "aria-current", "step");
  assert(current.length === 1, `expected exactly one current step, found ${current.length}`);
  return current[0];
}

function summaryValue(root, label) {
  const row = byClass(root, "launch-row").find((candidate) =>
    candidate.children[0]?.textContent === label);
  return row?.children[1]?.textContent;
}

function response(payload) {
  return {
    ok: true,
    statusText: "OK",
    json: async () => payload,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function renderWith(fetchImpl) {
  globalThis.fetch = fetchImpl;
  window.location.hash = "";
  const root = new Element("main");
  await render(root);
  return root;
}

function companiesResponse() {
  return response({ companies: [{ id: "co-1", name: "Acme" }] });
}

function selectCompany(root) {
  const select = byAttribute(root, "id", "new-run-company")[0] ?? byTag(root, "select")[0];
  select.value = "co-1";
  select.dispatch("change");
}

function pipelineRadio(root, key) {
  return byTag(root, "input").find((input) =>
    input.getAttribute("type") === "radio" && input.value === key);
}

function choosePipeline(root, key) {
  const radio = pipelineRadio(root, key);
  radio.checked = true;
  return radio.dispatch("change");
}

function upload(root, file) {
  const input = byAttribute(root, "id", "new-run-csv")[0];
  input.files = file ? [file] : [];
  return input.dispatch("change");
}

async function initialAccessibilityContract() {
  const requests = [];
  const root = await renderWith(async (path) => {
    requests.push(path);
    if (path === "/companies") return companiesResponse();
    return response({ total_rows: 1 });
  });
  const steps = byClass(root, "workflow-step");

  assert(steps.length === 4, "New Run must render four workflow steps");
  assert(currentStep(root) === steps[0], "company must be current initially");
  assert(steps.slice(1).every((step) => step.inert), "future steps must be inert initially");

  const disabledRadio = pipelineRadio(root, "ai_bulk");
  assert(disabledRadio.dispatch("change") === false, "inert pipeline control dispatched an event");
  await disabledRadio.listeners.change({ preventDefault() {} });
  assert(summaryValue(root, "Pipeline") === "-", "guard did not reject disabled pipeline handler");

  const disabledFile = byAttribute(root, "id", "new-run-csv")[0];
  disabledFile.files = [new File(["x"], "disabled.csv", { type: "text/csv" })];
  assert(disabledFile.dispatch("change") === false, "inert file control dispatched an event");
  await disabledFile.listeners.change({ preventDefault() {} });
  assert(requests.length === 1, "guard allowed a disabled file preview request");
}

async function labelsAndPlaceholderContract() {
  const root = await renderWith(async (path) => {
    if (path === "/companies") return companiesResponse();
    throw new Error(`Unexpected fetch: ${path}`);
  });

  for (const id of ["new-run-company", "new-company-name", "new-run-phase", "new-run-csv"]) {
    assert(byAttribute(root, "id", id).length === 1, `${id} control missing`);
    assert(byAttribute(root, "for", id)[0]?.tagName === "LABEL", `${id} label association missing`);
  }
  assert(summaryValue(root, "Company") === "-", "company placeholder leaked into summary");
  const companyMessage = byAttribute(root, "aria-live", "polite").find((node) =>
    node.tagName === "P");
  assert(companyMessage && !companyMessage.classList.contains("hidden"),
    "company live region must reserve visible space");

  selectCompany(root);
  assert(!byClass(root, "workflow-step")[1].inert, "company selection did not enable pipeline step");
}

async function invalidRelationshipContract() {
  const warning = "No searchable relationship pairs were detected.";
  const root = await renderWith(async (path) => {
    if (path === "/companies") return companiesResponse();
    if (path === "/uploads/relationship/preview") {
      return response({ total_rows: 0, relationship: true, warnings: [warning] });
    }
    throw new Error(`Unexpected fetch: ${path}`);
  });
  const steps = byClass(root, "workflow-step");

  selectCompany(root);
  await choosePipeline(root, "relationship");
  await upload(root, new File(["x,y"], "relationships.csv", { type: "text/csv" }));

  assert(currentStep(root) === steps[2], "zero-pair relationship preview must remain on step 3");
  assert(steps[3].inert, "zero-pair relationship preview enabled confirmation");
  assert(byText(root, "button", "Start run").disabled, "zero-pair relationship preview enabled start");
  assert(root.textContent.includes(warning), "relationship preview warning disappeared");
}

async function firmographicsPreviewEndpointContract() {
  // A firmographics CSV has no company_name/country, so the shared /uploads/preview
  // (parse_entities_csv) reported it as headerless and positional — "col 1 = company
  // name, col 2 = country" about a file whose first column is a URL.
  const paths = [];
  const root = await renderWith(async (path) => {
    if (path === "/companies") return companiesResponse();
    paths.push(path);
    if (path === "/uploads/firmographics/preview") {
      return response({ total_rows: 72, positional: false,
                        columns_detected: { website_url: "Website" } });
    }
    throw new Error(`Unexpected fetch: ${path}`);
  });

  selectCompany(root);
  await choosePipeline(root, "firmographics");
  await upload(root, new File(["Website\nhttps://acme.com"], "firmo.csv",
                              { type: "text/csv" }));

  assert(paths.includes("/uploads/firmographics/preview"),
    "firmographics did not use its own preview endpoint");
  assert(!paths.includes("/uploads/preview"),
    "firmographics still hit the entity-CSV preview");
  assert(!root.textContent.includes("positional"),
    "firmographics preview still claims positional parsing");
}

async function pipelineInvalidationContract() {
  const nextPreview = deferred();
  const genericRequests = [];
  const root = await renderWith(async (path, opts) => {
    if (path === "/companies") return companiesResponse();
    if (path === "/uploads/relationship/preview") {
      return response({ total_rows: 1, relationship: true });
    }
    if (path === "/uploads/preview") {
      genericRequests.push({ path, opts });
      return nextPreview.promise;
    }
    throw new Error(`Unexpected fetch: ${path}`);
  });
  const steps = byClass(root, "workflow-step");

  selectCompany(root);
  await choosePipeline(root, "relationship");
  await upload(root, new File(["x,y"], "relationships.csv", { type: "text/csv" }));
  assert(currentStep(root) === steps[3], "valid relationship preview did not enable confirmation");

  const pipelineChange = choosePipeline(root, "ai_bulk");
  assert(currentStep(root) === steps[2], "pipeline change did not immediately invalidate preview");
  assert(byText(root, "button", "Start run").disabled, "pipeline change left start enabled");
  assert(genericRequests.length === 1, "pipeline change did not re-preview the selected file");
  assert(genericRequests[0].path === "/uploads/preview", "pipeline change used wrong preview endpoint");

  nextPreview.resolve(response({ total_rows: 2, columns_detected: {}, sample_rows: [] }));
  await pipelineChange;
  assert(currentStep(root) === steps[3], "new pipeline preview did not restore confirmation");
}

async function stalePreviewContract() {
  const pending = [];
  const root = await renderWith(async (path, opts) => {
    if (path === "/companies") return companiesResponse();
    if (path === "/uploads/preview") {
      const request = deferred();
      pending.push({ ...request, opts });
      return request.promise;
    }
    throw new Error(`Unexpected fetch: ${path}`);
  });
  const steps = byClass(root, "workflow-step");

  selectCompany(root);
  await choosePipeline(root, "ai_bulk");
  const firstFile = new File(["first"], "first.csv", { type: "text/csv" });
  const secondFile = new File(["second"], "second.csv", { type: "text/csv" });
  const firstPreview = upload(root, firstFile);
  const secondPreview = upload(root, secondFile);
  assert(pending.length === 2, "expected two concurrent preview requests");

  pending[1].resolve(response({ total_rows: 2, columns_detected: {}, sample_rows: [] }));
  await secondPreview;
  assert(summaryValue(root, "Rows") === "2", "latest preview was not committed");
  assert(currentStep(root) === steps[3], "latest valid preview did not enable confirmation");

  pending[0].resolve(response({ total_rows: 99, columns_detected: {}, sample_rows: [] }));
  await firstPreview;
  assert(summaryValue(root, "Rows") === "2", "stale preview overwrote latest result");
}

async function uploadContract() {
  let uploadRequest;
  const root = await renderWith(async (path, opts) => {
    if (path === "/companies") return companiesResponse();
    if (path === "/uploads/preview") return response({ total_rows: 1 });
    if (path === "/uploads/ai-mode") {
      uploadRequest = opts;
      return response({ run_id: "run /1" });
    }
    throw new Error(`Unexpected fetch: ${path}`);
  });
  const originalSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback) => { callback(); return 0; };
  try {
    selectCompany(root);
    await choosePipeline(root, "ai_deep");
    const file = new File(["company_name,country"], "companies.csv", { type: "text/csv" });
    await upload(root, file);
    await byText(root, "button", "Start run").dispatch("click");

    assert(uploadRequest?.method === "POST", "launch did not POST to pipeline endpoint");
    assert(uploadRequest.body.get("file") === file, "launch changed the selected file");
    assert(uploadRequest.body.get("company_id") === "co-1", "launch omitted company_id");
    assert(uploadRequest.body.get("mode") === "ai_deep", "launch omitted AI mode");
    assert(window.location.hash === "#/runs/run%20%2F1?engine=ai", "AI launch redirect changed");
  } finally {
    globalThis.setTimeout = originalSetTimeout;
  }
}

const failures = [];
for (const [name, contract] of [
  ["initial accessibility", initialAccessibilityContract],
  ["labels and placeholder", labelsAndPlaceholderContract],
  ["invalid relationship", invalidRelationshipContract],
  ["firmographics preview endpoint", firmographicsPreviewEndpointContract],
  ["pipeline invalidation", pipelineInvalidationContract],
  ["stale preview", stalePreviewContract],
  ["upload", uploadContract],
]) {
  try {
    await contract();
  } catch (error) {
    failures.push(`${name}: ${error.message}`);
  }
}

if (failures.length) {
  throw new Error(`New Run DOM contract failures:\n- ${failures.join("\n- ")}`);
}
