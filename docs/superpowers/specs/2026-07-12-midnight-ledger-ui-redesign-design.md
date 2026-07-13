# Midnight Ledger UI Redesign

**Date:** 2026-07-12  
**Status:** Approved design, pending implementation plan  
**Scope:** Entire Forage Console frontend, with Run Detail as the priority

## Objective

Replace the console's visually flat, card-heavy presentation with a coherent dark interface that makes outcomes easy to scan. Preserve every existing workflow, endpoint, polling path, file action, and pipeline-specific behavior.

The selected direction is **Midnight Ledger**: calm navy-black surfaces, emerald success/action accents, restrained borders, strong typography, and outcome-first information hierarchy.

## Problems to solve

- Most metrics have identical card treatment, so primary outcomes compete with operational metadata.
- The page contains too many bordered rectangles and nested panels.
- Dark theme tokens coexist with light-theme table dividers and hover colors.
- Page hierarchy, spacing, labels, buttons, and statuses vary between views.
- The fixed desktop sidebar has no deliberate compact/mobile navigation behavior.
- Run Detail exposes the right data but does not tell a clear result story.

## Design principles

1. **Outcome before telemetry.** Found, not found, and errored results lead; time, tokens, model, and batch state support them.
2. **Use structure before containers.** Prefer spacing, type, and hairline dividers. Reserve panels for forms, warnings, modal content, and grouped controls.
3. **One semantic color per meaning.** Emerald means success or primary action; blue means active/information; amber means warning; red means error/destructive.
4. **Dense but calm.** This is an operational tool, so tables remain information-rich without looking like a spreadsheet embedded in many cards.
5. **Progressive disclosure.** Secondary metadata remains present but visually quieter.

## Visual system

### Color tokens

- App background: `#080d16`
- Sidebar background: `#0b111c`
- Raised surface: `#0f1724`
- Soft surface: `#121d2c`
- Hairline border: `#1b2637`
- Strong border: `#2a3a50`
- Primary text: `#f3f7fd`
- Secondary text: `#a6b3c6`
- Muted text: `#718198`
- Emerald accent: `#65d6ad`
- Emerald soft background: `#10251f`
- Blue information: `#72a7ff`
- Amber warning: `#f2b95f`
- Red error: `#f07b84`

No decorative gradients, glass blur, large glows, or pure-black/pure-white fields. Shadows are limited to overlays and popovers.

### Typography

Use the existing system-font delivery with a cleaner scale; do not add a network font dependency.

- Page title: 24–28 px, semibold
- Section title: 15–16 px, semibold
- Outcome value: 42–52 px, bold with tabular numerals
- Metric value: 18–22 px, semibold with tabular numerals
- Body: 14 px
- Labels/kickers: 11–12 px, uppercase only for compact metadata
- IDs, filenames, and storage paths: monospace

### Surfaces and spacing

- Global content width: consistent maximum of approximately 1440 px.
- Standard section gap: 24 px desktop, 18 px mobile.
- Standard control height: 40–44 px.
- Default border radius: 10–12 px; status pills remain fully rounded.
- Replace repeated metric cards with open metric groups separated by hairlines.

## Application shell

### Desktop

- Keep the left navigation, reducing its visual weight.
- Brand block uses a small emerald `F` mark, product name, and quiet descriptor.
- Navigation items receive consistent inline SVG icons, label text, and one active treatment.
- The top header becomes a compact context bar rather than a second large navigation surface.

### Mobile and narrow screens

- Below the desktop breakpoint, the sidebar becomes an off-canvas drawer.
- A top-bar menu button opens it; Escape and backdrop click close it.
- Focus returns to the menu button after close.
- Content padding reduces without introducing horizontal page scroll.
- Data tables may scroll within their own region and retain visible column headers.

## Shared component changes

- `panel`: quiet grouped surface, not the default wrapper for every section.
- `section`: open layout with optional heading and divider.
- `metric-group`: primary/secondary metric layout without separate boxes per value.
- `status-badge` and `pill`: shared semantic palette and readable text labels.
- `table-shell`: one consistent dark table treatment with subtle row hover and keyboard-visible links/actions.
- Buttons: primary emerald, secondary outlined, ghost text action, destructive red.
- Empty/loading/error states: consistent icon-free text hierarchy; error regions use `role="alert"` where appropriate.

## Page designs

### Dashboard

- Add a concise page introduction and direct New Run action.
- Company summaries show company name, total found rate, runs, searches, tokens, and cost in a clearer hierarchy rather than a nine-cell mini-grid.
- Recent runs remains a table, with status and found outcome easier to scan.
- Cards are clickable with visible hover and focus feedback but no scaling animation.

### Companies

- Place company creation in a compact toolbar-like section.
- Use the shared dark table for the list.
- Improve form labeling, inline success/error messaging, focus visibility, and responsive stacking.

### New Run

- Preserve the current multi-step workflow and validation.
- Create a clear numbered step rail with completed/current/disabled states.
- Pipeline options use restrained selection rows instead of visually heavy cards.
- Keep the summary sticky on wide screens; make it a normal section on mobile.
- CSV schema, preview, and warnings use compact disclosure and table treatments.

### Runs

- Convert filters into a compact toolbar with labels and an explicit Clear action.
- Preserve URL-backed filtering.
- Run rows emphasize company, pipeline, outcome, status, and created time; cost/duration remain secondary.
- Provide a strong empty state with a New Run link.

### Operations

- Keep its operational density and all refresh, retry, cancel, delete, storage-copy, and download behavior.
- Apply shared table, status, action, and section styles.
- Reduce panels around each table while keeping destructive actions clearly separated.
- Retain horizontal table scrolling for very wide operational datasets.

### Tools

- Apply the same theme, controls, section hierarchy, focus treatment, and responsive spacing.
- No tool behavior changes.

## Run Detail design

Run Detail is the reference implementation for the new hierarchy.

### 1. Context header

- Human-readable pipeline name and run state.
- Run/upload identifier, timestamp when available, and status badge.
- Confidence, batch, and model appear as quiet metadata, not standalone metric cards.
- Active runs retain stop/finalizing behavior and polling.

### 2. Outcome summary

- Primary statement: `N of N websites found` with percentage when the denominator is meaningful.
- Adjacent secondary outcomes: Not found and Errors. Relationship retains Skipped.
- The outcome taxonomy is communicated with both text and color.
- For pipelines without the reporting summary, use Succeeded/Failed as the compatible fallback.

### 3. Execution metadata

- One compact divided row for total/processed, duration, average per row, input/output tokens, and batch job state.
- Hide values that do not apply instead of rendering meaningless placeholders.
- Long model names truncate visually but remain available through title text.

### 4. Cost

- Keep the existing LLM, SerpWow, search count, and total cost semantics.
- Present them as one open section with total visually emphasized, not as another large card.

### 5. Pipeline-specific details

- Relationship verdict remains separate from website outcome because it represents a distinct three-way decision.
- Warnings and technical errors appear directly below the relevant summary.
- Running progress is visible without dominating a completed run.

### 6. Files and actions

- Files become a compact list/table with filename, type, and View/Download actions.
- Preserve availability gating, modal viewing, CSV parsing, and download URLs.
- Stop and retry remain clearly destructive/recovery actions in their own lower-priority section.

## Data and behavior boundaries

- No backend schema or endpoint change is required for the visual redesign.
- Existing status objects remain the source of truth.
- Existing polling and routing remain unchanged.
- UI helpers may normalize display labels and derive percentages only from supplied counts.
- Missing fields render as omitted secondary items or an em dash where table alignment requires a value.
- Existing uncommitted changes in `app.css`, `operations.js`, and `run_detail.js` must be integrated, not overwritten.

## Accessibility and interaction

- Normal text contrast must meet WCAG AA (4.5:1 minimum).
- Every interactive element has a visible `:focus-visible` outline.
- Use buttons for actions and links for navigation/downloads.
- Tables retain semantic `table`, `th`, and `td` markup.
- Mobile drawer exposes `aria-expanded`, an accessible label, Escape handling, and backdrop behavior.
- Status is never communicated by color alone.
- Motion remains 150–220 ms and is disabled under `prefers-reduced-motion`.
- No emoji are introduced as UI icons; use a single inline SVG icon style.

## Verification

1. Run `node --check` against every modified JavaScript module.
2. Run the existing UI shell and relevant backend unittest modules with the mandatory `-t .` suite mode.
3. Add/extend DOM-stub rendering coverage for Run Detail across:
   - gsearch LLM completed,
   - gmaps heuristic completed,
   - relationship completed,
   - batch finalizing,
   - legacy non-reporting pipeline,
   - AI Mode completed and completed-with-errors.
4. Verify core pages at approximately 375, 768, 1024, and 1440 px widths.
5. Verify no horizontal page scroll, tables scroll locally, drawer keyboard behavior works, and focus states are visible.
6. Confirm View/Download, filter routing, polling cleanup, stop, retry, and operations actions retain their existing URLs and handlers.

## Non-goals

- Backend or pipeline behavior changes.
- New metrics, reporting fields, or database migrations.
- Replacing vanilla JavaScript with a framework.
- Adding chart libraries, remote fonts, or icon packages.
- Light theme support in this redesign.
