function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function response(payload) {
  return { ok: true, statusText: "OK", json: async () => payload };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

let nextTimerId = 0;
let timers = new Map();
let cleared = [];
globalThis.setTimeout = (callback) => {
  const id = ++nextTimerId;
  timers.set(id, callback);
  return id;
};
globalThis.clearTimeout = (id) => {
  cleared.push(id);
  timers.delete(id);
};

const { pollStatus } = await import("../app/static/js/api.js");

async function cleanupDuringFetch() {
  timers = new Map();
  cleared = [];
  const pending = deferred();
  const updates = [];
  globalThis.fetch = () => pending.promise;
  const stop = pollStatus("/slow", (status) => updates.push(status));
  stop();
  pending.resolve(response({ status: "running" }));
  await settle();
  assert(updates.length === 0, "cleanup during fetch still delivered onUpdate");
  assert(timers.size === 0, "cleanup during fetch still scheduled a timer");
}

async function cleanupClearsScheduledTimer() {
  timers = new Map();
  cleared = [];
  globalThis.fetch = async () => response({ status: "running" });
  const stop = pollStatus("/running", () => {});
  await settle();
  assert(timers.size === 1, "non-terminal poll did not schedule its next tick");
  const [timerId] = timers.keys();
  stop();
  assert(timers.size === 0, "cleanup did not clear scheduled timer");
  assert(cleared.includes(timerId), "cleanup cleared the wrong timer");
}

async function defaultTerminalUnchanged() {
  timers = new Map();
  const updates = [];
  globalThis.fetch = async () => response({ status: "completed" });
  pollStatus("/completed", (status) => updates.push(status));
  await settle();
  assert(updates.length === 1, "default terminal status was not delivered");
  assert(timers.size === 0, "default completed status did not terminate polling");
}

async function customPredicateContinuesCompletedBatch() {
  timers = new Map();
  const updates = [];
  globalThis.fetch = async () => response({ status: "completed", batch: "running" });
  const stop = pollStatus("/batch", (status) => updates.push(status), 25,
    (status) => status.status === "completed" && status.batch === "succeeded");
  await settle();
  assert(updates.length === 1, "custom predicate lost completed update");
  assert(timers.size === 1, "custom predicate stopped a running batch");
  stop();
}

await cleanupDuringFetch();
await cleanupClearsScheduledTimer();
await defaultTerminalUnchanged();
await customPredicateContinuesCompletedBatch();
