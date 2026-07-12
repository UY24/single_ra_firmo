// backend/app/static/js/companies.js — create company + companies table with stats.
import { api, el, fmtUsd, fmtNum } from "./api.js";
import { cell, emptyState, errorCard, head, loadingCard, pageIntro, shortDate } from "./ui.js";

function createForm(onCreated) {
  const message = el("p", {
    class: "form-message hidden",
    "aria-live": "polite",
  });
  const input = el("input", {
    id: "company-name",
    type: "text",
    placeholder: "e.g. Acme Industries",
    autocomplete: "organization",
    class: "control w-full px-3 py-2 text-sm",
  });
  const button = el("button", {
    type: "submit",
    class: "btn-primary disabled:opacity-50",
  }, "Add company");

  function setMessage(text, ok) {
    message.textContent = text;
    message.className = `form-message ${ok ? "text-green-700" : "text-red-600"}`;
    if (ok) message.removeAttribute("role");
    else message.setAttribute("role", "alert");
  }

  const form = el("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      const name = input.value.trim();
      if (!name) { setMessage("Company name is required.", false); return; }
      button.disabled = true;
      try {
        const company = await api("/companies", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        input.value = "";
        setMessage(`Created "${company.name ?? name}".`, true);
        onCreated();
      } catch (e) {
        setMessage(e.message, false); // 409 → "already exists", 400/503 → detail
      } finally {
        button.disabled = false;
      }
    },
  },
    el("div", { class: "create-company-fields" },
      el("label", { for: "company-name", class: "filter-label" }, "Company name"),
      input,
      button,
    ),
    message,
  );

  return el("section", { class: "create-company" },
    el("h2", { class: "section-title" }, "Create company"),
    form,
  );
}

function companiesTable(companies) {
  const rows = companies.map((c) =>
    el("tr", { class: "data-row" },
      cell(el("a", {
        class: "table-link",
        href: `#/runs?company_id=${encodeURIComponent(c.id)}`,
      }, c.name ?? "-"), "font-semibold text-slate-50"),
      cell(fmtNum(c.runs), "text-right"),
      cell(`${fmtNum(c.websites_found)} / ${fmtNum(c.websites_not_found)}`, "text-right"),
      cell(fmtUsd(c.total_cost_usd), "text-right"),
      cell(shortDate(c.created_at, { withTime: false }), "text-slate-400"),
    ),
  );

  return el("div", { class: "table-shell" },
    el("div", { class: "table-scroll" },
      el("table", { class: "data-table" },
        el("thead", {},
          el("tr", {},
            head("Name"), head("Runs", "text-right"),
            head("Found / not found", "text-right"),
            head("Cost", "text-right"), head("Created"),
          ),
        ),
        el("tbody", {}, ...rows),
      ),
    ),
  );
}

export async function render(root) {
  const listArea = el("div", { class: "company-directory" }, loadingCard());
  const view = el("div", { class: "core-view" },
    pageIntro(
      "Directory",
      "Companies",
      "Create and review the company workspaces used to organize pipeline runs.",
    ),
    createForm(() => refresh()),
    listArea,
  );
  root.replaceChildren(view);

  async function refresh() {
    listArea.replaceChildren(loadingCard());
    let stats;
    try {
      stats = await api("/companies/stats");
    } catch (e) {
      listArea.replaceChildren(errorCard(e.message));
      return;
    }
    const companies = stats.companies ?? [];
    if (companies.length === 0) {
      listArea.replaceChildren(
        emptyState("No companies yet", "Add a company above to get started."),
      );
      return;
    }
    listArea.replaceChildren(companiesTable(companies));
  }

  await refresh();
}
