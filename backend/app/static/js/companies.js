// backend/app/static/js/companies.js — create company + companies table with stats.
import { api, el, fmtUsd, fmtNum } from "./api.js";
import { errorCard, loadingCard, head, cell, shortDate } from "./ui.js";

function createForm(onCreated) {
  const message = el("p", { class: "mt-2 hidden text-sm" });
  const input = el("input", {
    type: "text",
    placeholder: "Company name",
    class: "w-64 rounded-lg border border-gray-300 px-3 py-2 text-sm " +
           "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500",
  });
  const button = el("button", {
    type: "submit",
    class: "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white " +
           "hover:bg-indigo-500 disabled:opacity-50",
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
        setMessage(`Created “${company.name ?? name}”.`, true);
        onCreated();
      } catch (e) {
        setMessage(e.message, false); // 409 → "already exists", 400/503 → detail
      } finally {
        button.disabled = false;
      }
    },
  },
    el("div", { class: "flex items-center gap-3" }, input, button),
    message,
  );

  return el("div", { class: "rounded-xl border border-gray-200 bg-white p-5 shadow-sm" },
    el("h2", { class: "mb-3 text-sm font-semibold text-gray-700" }, "Create company"),
    form,
  );
}

function companiesTable(companies) {
  const rows = companies.map((c) =>
    el("tr", {
      class: "cursor-pointer hover:bg-indigo-50/40",
      onclick: () => { window.location.hash = `#/runs?company_id=${encodeURIComponent(c.id)}`; },
    },
      cell(c.name ?? "—", "font-medium text-gray-900"),
      cell(fmtNum(c.runs), "text-right"),
      cell(`${fmtNum(c.websites_found)} / ${fmtNum(c.websites_not_found)}`, "text-right"),
      cell(fmtUsd(c.total_cost_usd), "text-right"),
      cell(shortDate(c.created_at, { withTime: false }), "text-gray-400"),
    ),
  );

  return el("div", { class: "overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm" },
    el("table", { class: "min-w-full divide-y divide-gray-200 text-sm" },
      el("thead", { class: "bg-gray-50" },
        el("tr", {},
          head("Name"), head("Runs", "text-right"),
          head("Found / not found", "text-right"),
          head("Cost", "text-right"), head("Created"),
        ),
      ),
      el("tbody", { class: "divide-y divide-gray-100" }, ...rows),
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
        el("div", { class: "rounded-xl border border-gray-200 bg-white p-10 text-center shadow-sm" },
          el("p", { class: "text-sm font-medium text-gray-900" }, "No companies yet"),
          el("p", { class: "mt-1 text-sm text-gray-500" }, "Add one above to get started."),
        ),
      );
      return;
    }
    listArea.replaceChildren(companiesTable(companies));
  }

  await refresh();
}
