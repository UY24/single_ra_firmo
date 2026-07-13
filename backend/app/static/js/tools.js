// backend/app/static/js/tools.js — Interactive single-entity GMaps and GSearch tools.
//
// These call existing endpoints directly (not tracked batch runs):
//   GET /gmaps/discover?q=...&country=...   → {query, gl, cids, processing_seconds}
//   GET /gmaps/search?q=...&country=...     → {official_website, raw_response, processing_seconds}
//   GET /gmaps/details?cid=...             → place detail object
//   GET /gsearch/discover?company_name=...&country=...&phase=...&...
//     → {company_name, country, phase, queries_run, candidates, results, processing_seconds}
import { api, el } from "./api.js";
import { errorCard, pageIntro, sectionHeading } from "./ui.js";

const inputCls = "control w-full px-3 py-2 text-sm";
const btnPrimary = "btn-primary disabled:opacity-50 disabled:cursor-not-allowed";
const btnSecondary = "btn-secondary disabled:opacity-50 disabled:cursor-not-allowed";
const btnGreen = "btn-secondary disabled:opacity-50 disabled:cursor-not-allowed";
let rawJsonId = 0;

const isAbortError = (error) => error?.name === "AbortError";

function createLifecycle() {
  let mounted = true;
  const controllers = new Set();
  return {
    isMounted: () => mounted,
    request: (path, opts = {}) => {
      const controller = new AbortController();
      controllers.add(controller);
      return api(path, { ...opts, signal: controller.signal })
        .finally(() => controllers.delete(controller));
    },
    cleanup: () => {
      if (!mounted) return;
      mounted = false;
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
    },
  };
}

const PHASES = [
  { value: "all",      label: "All Phases Combined" },
  { value: "phase1",   label: "Phase 1: Initial Hook & Punctuation" },
  { value: "phase2",   label: "Phase 2: AI NL Prompts" },
  { value: "phase3",   label: "Phase 3: Address Pivot" },
  { value: "phase4",   label: "Phase 4: Document Hunting" },
  { value: "phase5",   label: "Phase 5: Dynamic Expansion" },
  { value: "fallback", label: "Fallback Searches Only" },
];

function labeled(labelText, input, optional = false) {
  const div = el("div", { class: "control-field" });
  div.appendChild(el("label", {
    class: "filter-label",
    for: input.getAttribute("id"),
  }, labelText + (optional ? " (optional)" : " *")));
  div.appendChild(input);
  return div;
}

function rawJsonToggle(data) {
  const contentId = `tool-raw-json-${++rawJsonId}`;
  const pre = el("pre", {
    class: "code-block tool-code",
  }, JSON.stringify(data, null, 2));
  const wrap = el("div", { class: "raw-json hidden", id: contentId });
  wrap.appendChild(pre);
  const btn = el("button", {
    class: btnSecondary + " mt-3 min-h-0 py-1 px-2.5 text-xs",
    "aria-controls": contentId,
    "aria-expanded": "false",
  },
    "Show raw JSON");
  btn.addEventListener("click", () => {
    const nowHidden = wrap.classList.toggle("hidden");
    btn.textContent = nowHidden ? "Show raw JSON" : "Hide raw JSON";
    btn.setAttribute("aria-expanded", String(!nowHidden));
  });
  const wrapper = el("div", { class: "raw-json-toggle" });
  wrapper.appendChild(btn);
  wrapper.appendChild(wrap);
  return wrapper;
}

function sectionCard(title, subtitle, body) {
  const card = el("section", { class: "detail-section tool-section" });
  card.appendChild(sectionHeading(title, subtitle));
  card.appendChild(el("div", { class: "detail-section-body" }, body));
  return card;
}

// ── Google Maps ──────────────────────────────────────────────────────────────

function gmapsDiscoverSearchCard(lifecycle) {
  const { isMounted, request } = lifecycle;
  const queryIn = el("input", {
    class: inputCls, type: "text",
    id: "gmaps-query",
    placeholder: "e.g. Acme Engineering Banani Dhaka",
  });
  const countryIn = el("input", {
    class: inputCls, type: "text",
    id: "gmaps-country",
    placeholder: "e.g. Bangladesh or bd",
  });
  const discoverBtn = el("button", { class: btnSecondary }, "Discover Places");
  const searchBtn = el("button", { class: btnPrimary }, "Full Search");
  const metaEl = el("p", { class: "message message--muted", "aria-live": "polite" });
  const resultsEl = el("div", { class: "tool-results" });

  function setMeta(msg, tone = "muted") {
    metaEl.textContent = msg;
    metaEl.className = `message message--${tone}`;
  }

  async function run(action) {
    const q = queryIn.value.trim();
    if (!q) { setMeta("Search query is required.", "danger"); return; }
    const params = new URLSearchParams({ q });
    const country = countryIn.value.trim();
    if (country) params.set("country", country);
    discoverBtn.disabled = searchBtn.disabled = true;
    setMeta("Running…", "info");
    resultsEl.replaceChildren();
    try {
      const data = await request(`/gmaps/${action}?${params}`);
      if (!isMounted()) return;
      setMeta(`Done in ${data.processing_seconds ?? "?"}s`, "muted");
      const out = el("div", { class: "result-stack" });
      if (action === "discover") {
        const cids = data.cids ?? [];
        const header = el("p", { class: "data-value" },
          `Found ${cids.length} CID${cids.length !== 1 ? "s" : ""}` +
          ` for "${data.query}"${data.gl ? ` (gl=${data.gl})` : ""}`);
        out.appendChild(header);
        if (cids.length > 0) {
          const list = el("ul", { class: "data-list" });
          cids.forEach((c) => list.appendChild(el("li", { class: "data-list-row data-value font-mono" }, c)));
          out.appendChild(list);
        } else {
          out.appendChild(el("p", { class: "section-copy" }, "No CIDs discovered."));
        }
      } else {
        const website = data.official_website;
        const websiteEl = el("p", { class: "data-value" });
        websiteEl.appendChild(document.createTextNode("Website: "));
        if (website) {
          const a = el("a", {
            href: website, target: "_blank",
            class: "semantic-link break-all",
          }, website);
          websiteEl.appendChild(a);
        } else {
          websiteEl.appendChild(el("span", { class: "section-copy" }, "none found"));
        }
        out.appendChild(websiteEl);
        const raw = data.raw_response ?? data;
        const places = (raw.results ?? []).slice(0, 6);
        if (places.length > 0) {
          const listHeader = el("p", { class: "data-label" },
            `Top ${places.length} place(s):`);
          out.appendChild(listHeader);
          const list = el("ul", { class: "data-list" });
          places.forEach((p) => {
            const site = p.website ?? p.official_website ?? "";
            list.appendChild(el("li", { class: "data-list-row data-value break-all" },
              `${p.name ?? "—"}${site ? " — " + site : ""}${p.address ? " — " + p.address : ""}`));
          });
          out.appendChild(list);
        }
      }
      out.appendChild(rawJsonToggle(data));
      resultsEl.appendChild(out);
    } catch (e) {
      if (!isMounted() || isAbortError(e)) return;
      setMeta("", "muted");
      resultsEl.appendChild(errorCard(e.message));
    } finally {
      if (isMounted()) discoverBtn.disabled = searchBtn.disabled = false;
    }
  }

  discoverBtn.addEventListener("click", () => run("discover"));
  searchBtn.addEventListener("click", () => run("search"));

  const body = el("div", { class: "tool-form" });
  const grid = el("div", { class: "control-grid" });
  grid.appendChild(labeled("Search Query", queryIn));
  grid.appendChild(labeled("Country Hint", countryIn, true));
  body.appendChild(grid);
  const btns = el("div", { class: "action-group tool-actions" });
  btns.appendChild(discoverBtn);
  btns.appendChild(searchBtn);
  body.appendChild(btns);
  body.appendChild(metaEl);
  body.appendChild(resultsEl);

  return sectionCard(
    "Google Maps — Discovery & Search",
    "Discover place CIDs or run a full Google Maps search. Results are immediate, not a tracked run.",
    body,
  );
}

function gmapsDetailsCard(lifecycle) {
  const { isMounted, request } = lifecycle;
  const cidIn = el("input", {
    class: inputCls, type: "text",
    id: "gmaps-cid",
    placeholder: "e.g. 0x3755c7a0f75e10d3:0x4d59a7213b28b7e2",
  });
  const fetchBtn = el("button", { class: btnGreen }, "Fetch Place Details");
  const metaEl = el("p", { class: "message message--muted", "aria-live": "polite" });
  const resultsEl = el("div", { class: "tool-results" });

  function setMeta(msg, tone = "muted") {
    metaEl.textContent = msg;
    metaEl.className = `message message--${tone}`;
  }

  fetchBtn.addEventListener("click", async () => {
    const cid = cidIn.value.trim();
    if (!cid) { setMeta("CID is required.", "danger"); return; }
    fetchBtn.disabled = true;
    setMeta("Fetching…", "info");
    resultsEl.replaceChildren();
    try {
      const data = await request(`/gmaps/details?cid=${encodeURIComponent(cid)}`);
      if (!isMounted()) return;
      setMeta(`Done in ${data.processing_seconds ?? "?"}s`, "muted");
      const FIELD_LABELS = [
        ["name", "Name"], ["website", "Website"], ["phone", "Phone"],
        ["address", "Address"], ["category", "Category"], ["type", "Type"],
        ["rating", "Rating"], ["reviews_count", "Reviews"],
      ];
      const rows = FIELD_LABELS
        .map(([key, label]) => [label, data[key]])
        .filter(([, v]) => v != null && v !== "");
      if (rows.length > 0) {
        const table = el("div", { class: "data-list" });
        rows.forEach(([label, value]) => {
          const row = el("div", { class: "data-list-row" });
          row.appendChild(el("span", {
            class: "data-label",
          }, label));
          if (label === "Website" && value) {
            row.appendChild(el("a", {
              href: value, target: "_blank",
              class: "semantic-link break-all",
            }, String(value)));
          } else {
            row.appendChild(el("span", { class: "data-value break-all" }, String(value)));
          }
          table.appendChild(row);
        });
        const rawWrap = el("div", { class: "data-list-row" });
        rawWrap.appendChild(rawJsonToggle(data));
        table.appendChild(rawWrap);
        resultsEl.appendChild(table);
      } else {
        resultsEl.appendChild(el("p", { class: "section-copy" }, "No details returned."));
        resultsEl.appendChild(rawJsonToggle(data));
      }
    } catch (e) {
      if (!isMounted() || isAbortError(e)) return;
      setMeta("", "muted");
      resultsEl.appendChild(errorCard(e.message));
    } finally {
      if (isMounted()) fetchBtn.disabled = false;
    }
  });

  const body = el("div", { class: "tool-form" });
  const fieldWrap = el("div", { class: "control-stack" });
  fieldWrap.appendChild(labeled("Place CID (data_cid)", cidIn));
  body.appendChild(fieldWrap);
  const btnWrap = el("div", { class: "action-group tool-actions" });
  btnWrap.appendChild(fetchBtn);
  body.appendChild(btnWrap);
  body.appendChild(metaEl);
  body.appendChild(resultsEl);

  return sectionCard(
    "Google Maps — Place Details by CID",
    "Fetch full place details (name, website, phone, address, rating) from a Google Maps data_cid.",
    body,
  );
}

// ── Google Search ────────────────────────────────────────────────────────────

function gsearchCard(lifecycle) {
  const { isMounted, request } = lifecycle;
  const companyIn = el("input", {
    class: inputCls, type: "text", id: "gsearch-company", placeholder: "e.g. Acme Engineering",
  });
  const countryIn = el("input", {
    class: inputCls, type: "text", id: "gsearch-country", placeholder: "e.g. Bangladesh or bd",
  });
  const cityIn = el("input", {
    class: inputCls, type: "text", id: "gsearch-city", placeholder: "e.g. Dhaka",
  });
  const industryIn = el("input", {
    class: inputCls, type: "text", id: "gsearch-industry", placeholder: "e.g. Engineering",
  });
  const addressIn = el("input", {
    class: inputCls, type: "text",
    id: "gsearch-address",
    placeholder: "e.g. Plot-12, Road-5, Block-B, Banani",
  });
  const phaseSelect = el("select", { class: inputCls, id: "gsearch-phase" });
  PHASES.forEach((p) => phaseSelect.appendChild(el("option", { value: p.value }, p.label)));

  const execBtn = el("button", { class: btnPrimary }, "Execute Search");
  const metaEl = el("p", { class: "message message--muted", "aria-live": "polite" });
  const resultsEl = el("div", { class: "tool-results" });

  function setMeta(msg, tone = "muted") {
    metaEl.textContent = msg;
    metaEl.className = `message message--${tone}`;
  }

  execBtn.addEventListener("click", async () => {
    const company = companyIn.value.trim();
    const country = countryIn.value.trim();
    if (!company || !country) {
      setMeta("Company Name and Country are required.", "danger");
      return;
    }
    const params = new URLSearchParams({ company_name: company, country });
    const city = cityIn.value.trim();
    const industry = industryIn.value.trim();
    const address = addressIn.value.trim();
    if (city) params.set("parsed_city_state", city);
    if (address) params.set("full_address", address);
    if (industry) params.set("industry", industry);
    params.set("phase", phaseSelect.value);

    execBtn.disabled = true;
    setMeta("Running…", "info");
    resultsEl.replaceChildren();
    try {
      const data = await request(`/gsearch/discover?${params}`);
      if (!isMounted()) return;
      const qCount = data.queries_run ?? 0;
      setMeta(
        `Ran ${qCount} quer${qCount === 1 ? "y" : "ies"} in ${data.processing_seconds ?? "?"}s`,
        "muted",
      );

      const candidates = data.candidates ?? [];
      const queryResults = data.results ?? [];
      const out = el("div", { class: "result-stack" });

      // Candidates
      const candCard = el("div", { class: "result-group" });
      candCard.appendChild(el("p", {
        class: "data-label",
      }, `URL Candidates (${candidates.length})`));
      if (candidates.length > 0) {
        const chips = el("div", { class: "result-links" });
        candidates.forEach((c) => {
          const href = c.startsWith("http") ? c : `https://${c}`;
          chips.appendChild(el("a", {
            href, target: "_blank",
            class: "pill pill--info semantic-link",
          }, c));
        });
        candCard.appendChild(chips);
      } else {
        candCard.appendChild(el("p", { class: "section-copy" }, "No candidates found."));
      }
      out.appendChild(candCard);

      // Query log
      if (queryResults.length > 0) {
        const logCard = el("div", { class: "result-group" });
        logCard.appendChild(el("p", {
          class: "data-label",
        }, `Search Execution Log (${queryResults.length})`));
        const log = el("div", { class: "data-list result-log" });
        queryResults.forEach((r) => {
          const isOk = Boolean(r.success);
          const row = el("div", {
            class: `data-list-row result-row ${isOk ? "result-row--good" : "result-row--muted"}`,
          });
          const rowHead = el("div", { class: "result-row-heading" });
          rowHead.appendChild(el("span", {
            class: `pill ${isOk ? "pill--good" : "pill--muted"}`,
          }, r.phase ?? "—"));
          if (r.search_url) {
            const link = el("a", {
              href: r.search_url, target: "_blank",
              class: "semantic-link",
              title: "Open search URL",
              "aria-label": `Open search query for ${r.phase ?? "unknown phase"}`,
            }, "↗");
            rowHead.appendChild(link);
          }
          row.appendChild(rowHead);
          row.appendChild(el("p", { class: "data-value break-all" }, r.query ?? ""));
          if (r.error) {
            row.appendChild(el("p", { class: "message message--danger" }, r.error));
          }
          log.appendChild(row);
        });
        logCard.appendChild(log);
        out.appendChild(logCard);
      }

      resultsEl.appendChild(out);
    } catch (e) {
      if (!isMounted() || isAbortError(e)) return;
      setMeta("", "muted");
      resultsEl.appendChild(errorCard(e.message));
    } finally {
      if (isMounted()) execBtn.disabled = false;
    }
  });

  const body = el("div", { class: "tool-form" });
  const grid = el("div", { class: "control-grid" });
  grid.appendChild(labeled("Company Name", companyIn));
  grid.appendChild(labeled("Country", countryIn));
  grid.appendChild(labeled("City / State", cityIn, true));
  grid.appendChild(labeled("Industry", industryIn, true));
  body.appendChild(grid);
  const addressRow = el("div", { class: "control-stack" });
  addressRow.appendChild(labeled("Full Address", addressIn, true));
  body.appendChild(addressRow);
  const phaseRow = el("div", { class: "control-stack" });
  phaseRow.appendChild(labeled("Search Phase", phaseSelect));
  body.appendChild(phaseRow);
  const btnWrap = el("div", { class: "action-group tool-actions" });
  btnWrap.appendChild(execBtn);
  body.appendChild(btnWrap);
  body.appendChild(metaEl);
  body.appendChild(resultsEl);

  return sectionCard(
    "Google Search — Single Entity Discovery",
    "Run a live search for a single company across selected phases. Not a tracked batch run.",
    body,
  );
}

// ── view ─────────────────────────────────────────────────────────────────────

export function render(root) {
  const lifecycle = createLifecycle();
  const page = el("div", { class: "tools-view" });

  page.appendChild(pageIntro(
    "Tools",
    "Interactive discovery workbench",
    "Run immediate Google Maps and Google Search lookups without creating a tracked batch run.",
  ));

  // GMaps heading
  const gmapsHead = el("section", { class: "tool-category" });
  gmapsHead.appendChild(sectionHeading(
    "Google Maps",
    "Interactive place lookup — results are immediate, not tracked batch runs.",
  ));
  const gmapsCards = el("div", { class: "tool-section-list" });
  gmapsCards.appendChild(gmapsDiscoverSearchCard(lifecycle));
  gmapsCards.appendChild(gmapsDetailsCard(lifecycle));
  gmapsHead.appendChild(gmapsCards);
  page.appendChild(gmapsHead);

  // GSearch heading
  const gsHead = el("section", { class: "tool-category" });
  gsHead.appendChild(sectionHeading(
    "Google Search",
    "Interactive single-entity search — not a tracked batch run.",
  ));
  const gsCards = el("div", { class: "tool-section-list" });
  gsCards.appendChild(gsearchCard(lifecycle));
  gsHead.appendChild(gsCards);
  page.appendChild(gsHead);

  root.replaceChildren(page);
  return lifecycle.cleanup;
}
