// backend/app/static/js/api.js
async function _doFetch(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

export async function api(path, opts = {}) {
  try {
    return await _doFetch(path, opts);
  } catch (e) {
    if (e instanceof TypeError) {
      // Network error — server may still be starting up (e.g. uvicorn --reload).
      // Retry once after a short delay before surfacing the error.
      await new Promise(r => setTimeout(r, 1500));
      return await _doFetch(path, opts);
    }
    throw e;
  }
}

export const defaultTerminal = (status) =>
  ["completed", "completed_with_errors", "failed"].includes(status?.status);

export function pollStatus(path, onUpdate, intervalMs = 2000, isTerminal = defaultTerminal) {
  let stopped = false;
  let timerId = null;
  async function tick() {
    if (stopped) return;
    try {
      const status = await api(path);
      if (stopped) return;
      onUpdate(status);
      if (stopped || isTerminal(status)) return;
    } catch (e) {
      if (!stopped) console.error(e);
    }
    if (stopped) return;
    timerId = setTimeout(() => {
      timerId = null;
      tick();
    }, intervalMs);
  }
  tick();
  return () => {
    stopped = true;
    if (timerId != null) {
      clearTimeout(timerId);
      timerId = null;
    }
  };
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...children);
  return node;
}

export const fmtUsd = (n) => n == null ? "—" : `$${Number(n).toFixed(4)}`;
export const fmtNum = (n) => n == null ? "—" : Number(n).toLocaleString();
