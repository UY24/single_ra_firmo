// backend/app/static/js/companies.js — create company + companies table with stats.
import { api, el, fmtUsd, fmtNum } from "./api.js";
import { errorCard, loadingCard, head, cell, shortDate } from "./ui.js";

function createForm(onCreated) {
  const message = el("p", { class: "mt-2 hidden text-sm" });
  const input = el("input", {
    type: "text",
    placeholder: "Company name",
    class: "control w-full px-3 py-2 text-sm",
  });
  const button = el("button", {
    type: "submit",
    class: "btn-primary disabled:opacity-50",
  }, "Add company");

  function setMessage(text, ok) {
    message.textContent = text;
    message.className = `mt-2 text-sm ${ok ? "text-green-700" : "text-red-600"}`;
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
    el("div", { class: "grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_auto]" }, input, button),
    message,
  );

  return el("div", { class: "panel" },
    el("h2", { class: "mb-3 section-title" }, "Create company"),
    form,
  );
}

function companiesTable(companies) {
  const rows = companies.map((c) =>
    el("tr", {
      class: "cursor-pointer hover:bg-indigo-50/40",
      onclick: () => { window.location.hash = `#/runs?company_id=${encodeURIComponent(c.id)}`; },
    },
      cell(c.name ?? "-", "font-semibold text-slate-50"),
      cell(fmtNum(c.runs), "text-right"),
      cell(`${fmtNum(c.websites_found)} / ${fmtNum(c.websites_not_found)}`, "text-right"),
      cell(fmtUsd(c.total_cost_usd), "text-right"),
      cell(shortDate(c.created_at, { withTime: false }), "text-slate-400"),
    ),
  );

  return el("div", { class: "table-shell" },
    el("div", { class: "table-scroll" },
      el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
        el("thead", {},
          el("tr", {},
            head("Name"), head("Runs", "text-right"),
            head("Found / not found", "text-right"),
            head("Cost", "text-right"), head("Created"),
          ),
        ),
        el("tbody", { class: "divide-y divide-gray-100" }, ...rows),
      ),
    ),
  );
}

export async function render(root) {
  const listArea = el("div", { class: "mt-6" }, loadingCard());
  root.replaceChildren(createForm(() => refresh()), listArea);

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
        el("div", { class: "panel p-10 text-center" },
          el("p", { class: "section-title" }, "No companies yet"),
          el("p", { class: "mt-1 section-copy" }, "Add one above to get started."),
        ),
      );
      return;
    }
    listArea.replaceChildren(companiesTable(companies));
  }

  await refresh();
}
