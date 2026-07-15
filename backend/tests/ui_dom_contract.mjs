class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attributes = {};
    this.className = "";
    this.childNodes = [];
    this.listeners = {};
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }

  append(...children) {
    this.childNodes.push(...children);
  }

  get children() {
    return this.childNodes.filter((child) => child instanceof Element);
  }

  get textContent() {
    return this.childNodes.map((child) =>
      child instanceof Element ? child.textContent : String(child)).join("");
  }
}

globalThis.document = {
  createElement: (tag) => new Element(tag),
};

const {
  copyCell,
  emptyState,
  errorCard,
  metricItem,
  pageIntro,
  sectionHeading,
} = await import("../app/static/js/ui.js");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function hasClass(node, className) {
  return node.className.split(/\s+/).includes(className);
}

function findByClass(node, className) {
  if (hasClass(node, className)) return node;
  for (const child of node.children) {
    const match = findByClass(child, className);
    if (match) return match;
  }
  return null;
}

const intro = pageIntro("Overview", "Run ledger");
assert(findByClass(intro, "view-kicker")?.textContent === "Overview", "pageIntro kicker missing");
assert(findByClass(intro, "page-heading")?.textContent === "Run ledger", "pageIntro title missing");
assert(!findByClass(intro, "page-copy"), "pageIntro rendered absent copy");
assert(intro.children.length === 1, "pageIntro rendered absent action");

const introAction = document.createElement("a");
const introComplete = pageIntro("Overview", "Run ledger", "Latest outcomes", introAction);
assert(findByClass(introComplete, "page-copy")?.textContent === "Latest outcomes", "pageIntro copy missing");
assert(introComplete.children[1] === introAction, "pageIntro action missing");

const heading = sectionHeading("Execution");
assert(!findByClass(heading, "section-copy"), "sectionHeading rendered absent copy");
assert(heading.children.length === 1, "sectionHeading rendered absent action");

const headingAction = document.createElement("button");
const headingComplete = sectionHeading("Execution", "Runtime details", headingAction);
assert(findByClass(headingComplete, "section-copy")?.textContent === "Runtime details", "sectionHeading copy missing");
assert(headingComplete.children[1] === headingAction, "sectionHeading action missing");

const dangerMetric = metricItem("Errors", "3", "danger", "Failed rows");
assert(dangerMetric.children.map((child) => child.tagName).join(",") === "DT,DD,DD", "metricItem must contain only DT/DD children");
assert(hasClass(dangerMetric, "metric-item--danger"), "metricItem danger tone missing");
assert(hasClass(dangerMetric.children[2], "metric-detail"), "metricItem detail class missing");

const fallbackMetric = metricItem("Errors", "3", "not-a-tone");
assert(hasClass(fallbackMetric, "metric-item--default"), "metricItem invalid tone did not fall back");
assert(!hasClass(fallbackMetric, "metric-item--not-a-tone"), "metricItem exposed invalid tone class");

const empty = emptyState("No runs", "Start a run to see results");
assert(empty.children.length === 2, "emptyState rendered absent action");
const emptyAction = document.createElement("a");
const emptyComplete = emptyState("No runs", "Start a run to see results", emptyAction);
assert(emptyComplete.children[2] === emptyAction, "emptyState action missing");

const supabaseError = errorCard("Supabase is unavailable");
assert(supabaseError.getAttribute("role") === "alert", "Supabase error missing alert role");
assert(supabaseError.children[0].textContent === "Supabase not configured / unreachable", "Supabase error heading changed");
assert(supabaseError.textContent.includes("SUPABASE_URL"), "Supabase guidance missing");

const genericError = errorCard("Request failed");
assert(genericError.getAttribute("role") === "alert", "generic error missing alert role");
assert(genericError.children[0].textContent === "Something went wrong", "generic error heading changed");

const copy = copyCell("s3://bucket/run/state.json");
assert(copy.tagName === "BUTTON", "copyCell must use a keyboard-operable button");
assert(copy.getAttribute("type") === "button", "copyCell button must not submit surrounding forms");
assert(copy.getAttribute("aria-label") === "Copy storage path: s3://bucket/run/state.json",
  "copyCell accessible name must identify its storage path");
assert(copy.listeners.click, "copyCell button lost its copy handler");
assert(hasClass(copy, "copy-control"), "copyCell semantic styling hook missing");

const emptyCopy = copyCell("—");
assert(emptyCopy.tagName === "SPAN", "empty copyCell placeholder must remain non-interactive");
