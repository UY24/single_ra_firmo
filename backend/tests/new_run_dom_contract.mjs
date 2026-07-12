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
    this.value = "";
    this.disabled = false;
    this.checked = false;
    this.files = [];
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

  get selectedOptions() {
    if (this.tagName !== "SELECT") return [];
    return this.children.filter((child) =>
      child.tagName === "OPTION" && child.value === this.value);
  }

  dispatch(name) {
    return this.listeners[name]?.({ preventDefault() {} });
  }
}

globalThis.document = {
  createElement: (tag) => new Element(tag),
};
globalThis.window = { location: { hash: "" } };
globalThis.fetch = async (path) => {
  const payloads = {
    "/companies": { companies: [{ id: "co-1", name: "Acme" }] },
    "/uploads/preview": { total_rows: 1, columns_detected: {}, sample_rows: [] },
  };
  if (!(path in payloads)) throw new Error(`Unexpected fetch: ${path}`);
  return {
    ok: true,
    statusText: "OK",
    json: async () => payloads[path],
  };
};

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

function currentSteps(root) {
  return byAttribute(root, "aria-current", "step");
}

const root = new Element("main");
await render(root);

const steps = byClass(root, "workflow-step");
assert(steps.length === 4, "New Run must render four workflow steps");
assert(currentSteps(root).length === 1 && currentSteps(root)[0] === steps[0],
  "company must be the only current step initially");

const fileInput = byAttribute(root, "id", "new-run-csv")[0];
assert(fileInput?.getAttribute("type") === "file", "CSV file input needs a stable id");
assert(byAttribute(root, "for", "new-run-csv")[0]?.tagName === "LABEL",
  "CSV file input needs an associated visible label");
assert(byAttribute(root, "aria-live", "polite").length === 3,
  "company, preview, and start messages must be polite live regions");

const radios = byTag(root, "input").filter((input) => input.getAttribute("type") === "radio");
assert(radios.length === 7, "all pipeline choices must remain radios");
assert(radios.every((radio) => radio.getAttribute("name") === "pipeline" && radio.value),
  "pipeline radios must retain name and value");
assert(byClass(root, "pipeline-option").length === radios.length,
  "each pipeline radio needs a pipeline-option label");
assert(byClass(root, "launch-summary")[0]?.tagName === "ASIDE",
  "launch summary must remain a semantic aside");

const companySelect = byTag(root, "select")[0];
companySelect.value = "co-1";
companySelect.dispatch("change");
assert(currentSteps(root).length === 1 && currentSteps(root)[0] === steps[1],
  "pipeline must become the only current step after company selection");

radios[0].checked = true;
radios[0].dispatch("change");
assert(currentSteps(root).length === 1 && currentSteps(root)[0] === steps[2],
  "file preview must become the only current step after pipeline selection");

fileInput.files = [new File(["company_name,country\nAcme,US"], "companies.csv", { type: "text/csv" })];
await fileInput.dispatch("change");
assert(currentSteps(root).length === 1 && currentSteps(root)[0] === steps[3],
  "confirm must become the only current step after a successful preview");
