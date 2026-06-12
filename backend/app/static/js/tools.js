// backend/app/static/js/tools.js — Interactive single-entity GMaps and GSearch tools.
//
// These call existing endpoints directly (not tracked batch runs):
//   GET /gmaps/discover?q=...&country=...   → {query, gl, cids, processing_seconds}
//   GET /gmaps/search?q=...&country=...     → {official_website, raw_response, processing_seconds}
//   GET /gmaps/details?cid=...             → place detail object
//   GET /gsearch/discover?company_name=...&country=...&phase=...&...
//     → {company_name, country, phase, queries_run, candidates, results, processing_seconds}
import { api, el } from "./api.js";
import { errorCard } from "./ui.js";

const inputCls =
  "w-full rounded-lg border border-gray-300 px-3 py-2 text-sm " +
  "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500";
const btnPrimary =
  "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white " +
  "hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed";
const btnSecondary =
  "rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm font-medium " +
  "text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed";
const btnGreen =
  "rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white " +
  "hover:bg-emerald-500 disabled:opacity-50 disabled:cursor-not-allowed";

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
  const div = el("div", { class: "flex flex-col gap-1" });
  div.appendChild(el("label", {
    class: "text-xs font-medium uppercase tracking-wide text-gray-400",
  }, labelText + (optional ? " (optional)" : " *")));
  div.appendChild(input);
  return div;
}

function rawJsonToggle(data) {
  const pre = el("pre", {
    class: "overflow-auto rounded-lg bg-slate-950 p-4 text-xs text-indigo-300 " +
           "whitespace-pre-wrap break-all max-h-80",
  }, JSON.stringify(data, null, 2));
  const wrap = el("div", { class: "hidden mt-2" });
  wrap.appendChild(pre);
  const btn = el("button", { class: btnSecondary + " mt-3 py-1 px-2.5 text-xs" },
    "Show raw JSON");
  btn.addEventListener("click", () => {
    const nowHidden = wrap.classList.toggle("hidden");
    btn.textContent = nowHidden ? "Show raw JSON" : "Hide raw JSON";
  });
  const wrapper = el("div", {});
  wrapper.appendChild(btn);
  wrapper.appendChild(wrap);
  return wrapper;
}

function sectionCard(title, subtitle, body) {
  const card = el("div", { class: "rounded-xl border border-gray-200 bg-white p-6 shadow-sm" });
  card.appendChild(el("h2", { class: "text-sm font-semibold text-gray-900" }, title));
  card.appendChild(el("p", { class: "mt-1 text-sm text-gray-500" }, subtitle));
  card.appendChild(body);
  return card;
}

// ── Google Maps ──────────────────────────────────────────────────────────────

function gmapsDiscoverSearchCard() {
  const queryIn = el("input", {
    class: inputCls, type: "text",
    placeholder: "e.g. Acme Engineering Banani Dhaka",
  });
  const countryIn = el("input", {
    class: inputCls, type: "text",
    placeholder: "e.g. Bangladesh or bd",
  });
  const discoverBtn = el("button", { class: btnSecondary }, "Discover Places");
  const searchBtn = el("button", { class: btnPrimary }, "Full Search");
  const metaEl = el("p", { class: "mt-2 text-sm text-gray-400" });
  const resultsEl = el("div", { class: "mt-4" });

  function setMeta(msg, tone = "text-gray-400") {
    metaEl.textContent = msg;
    metaEl.className = `mt-2 text-sm ${tone}`;
  }

  async function run(action) {
    const q = queryIn.value.trim();
    if (!q) { setMeta("Search query is required.", "text-red-600"); return; }
    const params = new URLSearchParams({ q });
    const country = countryIn.value.trim();
    if (country) params.set("country", country);
    discoverBtn.disabled = searchBtn.disabled = true;
    setMeta("Running…", "text-indigo-500");
    resultsEl.replaceChildren();
    try {
      const data = await api(`/gmaps/${action}?${params}`);
      setMeta(`Done in ${data.processing_seconds ?? "?"}s`, "text-gray-500");
      const out = el("div", { class: "space-y-3" });
      if (action === "discover") {
        const cids = data.cids ?? [];
        const header = el("p", { class: "text-sm font-medium text-gray-700" },
          `Found ${cids.length} CID${cids.length !== 1 ? "s" : ""}` +
          ` for "${data.query}"${data.gl ? ` (gl=${data.gl})` : ""}`);
        out.appendChild(header);
        if (cids.length > 0) {
          const list = el("ul", { class: "mt-2 space-y-1 font-mono text-xs text-gray-600" });
          cids.forEach((c) => list.appendChild(el("li", { class: "break-all" }, c)));
          out.appendChild(list);
        } else {
          out.appendChild(el("p", { class: "text-sm text-gray-400" }, "No CIDs discovered."));
        }
      } else {
        const website = data.official_website;
        const websiteEl = el("p", { class: "text-sm font-medium text-gray-700" });
        websiteEl.appendChild(document.createTextNode("Website: "));
        if (website) {
          const a = el("a", {
            href: website, target: "_blank",
            class: "text-indigo-600 hover:underline break-all",
          }, website);
          websiteEl.appendChild(a);
        } else {
          websiteEl.appendChild(el("span", { class: "text-gray-400" }, "none found"));
        }
        out.appendChild(websiteEl);
        const raw = data.raw_response ?? data;
        const places = (raw.results ?? []).slice(0, 6);
        if (places.length > 0) {
          const listHeader = el("p", { class: "text-xs text-gray-400" },
            `Top ${places.length} place(s):`);
          out.appendChild(listHeader);
          const list = el("ul", { class: "mt-1 space-y-1 text-xs text-gray-600" });
          places.forEach((p) => {
            const site = p.website ?? p.official_website ?? "";
            list.appendChild(el("li", { class: "break-all" },
              `${p.name ?? "—"}${site ? " — " + site : ""}${p.address ? " — " + p.address : ""}`));
          });
          out.appendChild(list);
        }
      }
      out.appendChild(rawJsonToggle(data));
      resultsEl.appendChild(out);
    } catch (e) {
      setMeta("", "text-gray-400");
      resultsEl.appendChild(errorCard(e.message));
    } finally {
      discoverBtn.disabled = searchBtn.disabled = false;
    }
  }

  discoverBtn.addEventListener("click", () => run("discover"));
  searchBtn.addEventListener("click", () => run("search"));

  const body = el("div", {});
  const grid = el("div", { class: "mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2" });
  grid.appendChild(labeled("Search Query", queryIn));
  grid.appendChild(labeled("Country Hint", countryIn, true));
  body.appendChild(grid);
  const btns = el("div", { class: "mt-4 flex gap-3" });
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

function gmapsDetailsCard() {
  const cidIn = el("input", {
    class: inputCls, type: "text",
    placeholder: "e.g. 0x3755c7a0f75e10d3:0x4d59a7213b28b7e2",
  });
  const fetchBtn = el("button", { class: btnGreen }, "Fetch Place Details");
  const metaEl = el("p", { class: "mt-2 text-sm text-gray-400" });
  const resultsEl = el("div", { class: "mt-4" });

  function setMeta(msg, tone = "text-gray-400") {
    metaEl.textContent = msg;
    metaEl.className = `mt-2 text-sm ${tone}`;
  }

  fetchBtn.addEventListener("click", async () => {
    const cid = cidIn.value.trim();
    if (!cid) { setMeta("CID is required.", "text-red-600"); return; }
    fetchBtn.disabled = true;
    setMeta("Fetching…", "text-indigo-500");
    resultsEl.replaceChildren();
    try {
      const data = await api(`/gmaps/details?cid=${encodeURIComponent(cid)}`);
      setMeta(`Done in ${data.processing_seconds ?? "?"}s`, "text-gray-500");
      const FIELD_LABELS = [
        ["name", "Name"], ["website", "Website"], ["phone", "Phone"],
        ["address", "Address"], ["category", "Category"], ["type", "Type"],
        ["rating", "Rating"], ["reviews_count", "Reviews"],
      ];
      const rows = FIELD_LABELS
        .map(([key, label]) => [label, data[key]])
        .filter(([, v]) => v != null && v !== "");
      if (rows.length > 0) {
        const table = el("div", {
          class: "rounded-lg border border-gray-200 divide-y divide-gray-100",
        });
        rows.forEach(([label, value]) => {
          const row = el("div", { class: "flex gap-3 px-4 py-2.5 text-sm" });
          row.appendChild(el("span", {
            class: "w-24 shrink-0 text-xs font-medium uppercase tracking-wide text-gray-400",
          }, label));
          if (label === "Website" && value) {
            row.appendChild(el("a", {
              href: value, target: "_blank",
              class: "text-indigo-600 hover:underline break-all",
            }, String(value)));
          } else {
            row.appendChild(el("span", { class: "break-all text-gray-700" }, String(value)));
          }
          table.appendChild(row);
        });
        const rawWrap = el("div", { class: "px-4 py-2.5" });
        rawWrap.appendChild(rawJsonToggle(data));
        table.appendChild(rawWrap);
        resultsEl.appendChild(table);
      } else {
        resultsEl.appendChild(el("p", { class: "text-sm text-gray-400" }, "No details returned."));
        resultsEl.appendChild(rawJsonToggle(data));
      }
    } catch (e) {
      setMeta("", "text-gray-400");
      resultsEl.appendChild(errorCard(e.message));
    } finally {
      fetchBtn.disabled = false;
    }
  });

  const body = el("div", {});
  const fieldWrap = el("div", { class: "mt-4 max-w-lg" });
  fieldWrap.appendChild(labeled("Place CID (data_cid)", cidIn));
  body.appendChild(fieldWrap);
  const btnWrap = el("div", { class: "mt-4" });
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

function gsearchCard() {
  const companyIn = el("input", {
    class: inputCls, type: "text", placeholder: "e.g. Acme Engineering",
  });
  const countryIn = el("input", {
    class: inputCls, type: "text", placeholder: "e.g. Bangladesh or bd",
  });
  const cityIn = el("input", {
    class: inputCls, type: "text", placeholder: "e.g. Dhaka",
  });
  const industryIn = el("input", {
    class: inputCls, type: "text", placeholder: "e.g. Engineering",
  });
  const addressIn = el("input", {
    class: inputCls, type: "text",
    placeholder: "e.g. Plot-12, Road-5, Block-B, Banani",
  });
  const phaseSelect = el("select", { class: inputCls });
  PHASES.forEach((p) => phaseSelect.appendChild(el("option", { value: p.value }, p.label)));

  const execBtn = el("button", { class: btnPrimary }, "Execute Search");
  const metaEl = el("p", { class: "mt-2 text-sm text-gray-400" });
  const resultsEl = el("div", { class: "mt-4" });

  function setMeta(msg, tone = "text-gray-400") {
    metaEl.textContent = msg;
    metaEl.className = `mt-2 text-sm ${tone}`;
  }

  execBtn.addEventListener("click", async () => {
    const company = companyIn.value.trim();
    const country = countryIn.value.trim();
    if (!company || !country) {
      setMeta("Company Name and Country are required.", "text-red-600");
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
    setMeta("Running…", "text-indigo-500");
    resultsEl.replaceChildren();
    try {
      const data = await api(`/gsearch/discover?${params}`);
      const qCount = data.queries_run ?? 0;
      setMeta(
        `Ran ${qCount} quer${qCount === 1 ? "y" : "ies"} in ${data.processing_seconds ?? "?"}s`,
        "text-gray-500",
      );

      const candidates = data.candidates ?? [];
      const queryResults = data.results ?? [];
      const out = el("div", { class: "space-y-4" });

      // Candidates
      const candCard = el("div", { class: "rounded-lg border border-gray-200 p-4" });
      candCard.appendChild(el("p", {
        class: "text-xs font-medium uppercase tracking-wide text-gray-400",
      }, `URL Candidates (${candidates.length})`));
      if (candidates.length > 0) {
        const chips = el("div", { class: "mt-2 flex flex-wrap gap-2" });
        candidates.forEach((c) => {
          const href = c.startsWith("http") ? c : `https://${c}`;
          chips.appendChild(el("a", {
            href, target: "_blank",
            class: "inline-block rounded-full border border-indigo-200 bg-indigo-50 px-3 py-1 " +
                   "text-xs text-indigo-700 hover:bg-indigo-100",
          }, c));
        });
        candCard.appendChild(chips);
      } else {
        candCard.appendChild(el("p", { class: "mt-1 text-sm text-gray-400" }, "No candidates found."));
      }
      out.appendChild(candCard);

      // Query log
      if (queryResults.length > 0) {
        const logCard = el("div", { class: "rounded-lg border border-gray-200 p-4" });
        logCard.appendChild(el("p", {
          class: "text-xs font-medium uppercase tracking-wide text-gray-400",
        }, `Search Execution Log (${queryResults.length})`));
        const log = el("div", { class: "mt-2 space-y-2" });
        queryResults.forEach((r) => {
          const isOk = Boolean(r.success);
          const row = el("div", {
            class: `rounded-lg border p-3 text-xs ${isOk
              ? "border-emerald-200 bg-emerald-50"
              : "border-gray-200 bg-gray-50"}`,
          });
          const rowHead = el("div", { class: "flex items-center justify-between gap-2" });
          rowHead.appendChild(el("span", {
            class: `rounded-full px-2 py-0.5 font-mono font-medium ${isOk
              ? "bg-emerald-100 text-emerald-800"
              : "bg-gray-200 text-gray-600"}`,
          }, r.phase ?? "—"));
          if (r.search_url) {
            const link = el("a", {
              href: r.search_url, target: "_blank",
              class: "text-gray-400 hover:text-indigo-600",
              title: "Open search URL",
            }, "↗");
            rowHead.appendChild(link);
          }
          row.appendChild(rowHead);
          row.appendChild(el("p", { class: "mt-1.5 break-all text-gray-700" }, r.query ?? ""));
          if (r.error) {
            row.appendChild(el("p", { class: "mt-1 text-red-600" }, r.error));
          }
          log.appendChild(row);
        });
        logCard.appendChild(log);
        out.appendChild(logCard);
      }

      resultsEl.appendChild(out);
    } catch (e) {
      setMeta("", "text-gray-400");
      resultsEl.appendChild(errorCard(e.message));
    } finally {
      execBtn.disabled = false;
    }
  });

  const body = el("div", {});
  const grid = el("div", { class: "mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2" });
  grid.appendChild(labeled("Company Name", companyIn));
  grid.appendChild(labeled("Country", countryIn));
  grid.appendChild(labeled("City / State", cityIn, true));
  grid.appendChild(labeled("Industry", industryIn, true));
  body.appendChild(grid);
  const addressRow = el("div", { class: "mt-4" });
  addressRow.appendChild(labeled("Full Address", addressIn, true));
  body.appendChild(addressRow);
  const phaseRow = el("div", { class: "mt-4" });
  phaseRow.appendChild(labeled("Search Phase", phaseSelect));
  body.appendChild(phaseRow);
  const btnWrap = el("div", { class: "mt-5" });
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

export async function render(root) {
  const page = el("div", { class: "max-w-3xl space-y-8" });

  // GMaps heading
  const gmapsHead = el("div", {});
  gmapsHead.appendChild(el("h2", { class: "text-base font-semibold text-gray-900" },
    "Google Maps"));
  gmapsHead.appendChild(el("p", { class: "mt-1 text-sm text-gray-500" },
    "Interactive place lookup — results are immediate, not tracked batch runs."));
  const gmapsCards = el("div", { class: "mt-4 space-y-6" });
  gmapsCards.appendChild(gmapsDiscoverSearchCard());
  gmapsCards.appendChild(gmapsDetailsCard());
  gmapsHead.appendChild(gmapsCards);
  page.appendChild(gmapsHead);

  page.appendChild(el("hr", { class: "border-gray-200" }));

  // GSearch heading
  const gsHead = el("div", {});
  gsHead.appendChild(el("h2", { class: "text-base font-semibold text-gray-900" },
    "Google Search"));
  gsHead.appendChild(el("p", { class: "mt-1 text-sm text-gray-500" },
    "Interactive single-entity search — not a tracked batch run."));
  const gsCards = el("div", { class: "mt-4" });
  gsCards.appendChild(gsearchCard());
  gsHead.appendChild(gsCards);
  page.appendChild(gsHead);

  root.replaceChildren(page);
}
