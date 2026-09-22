"use strict";

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

let toastTimer;
function toast(message) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

const logEl = () => document.getElementById("job-log");
const terminalEl = () => document.getElementById("job-terminal");

function setLog(text) {
  const el = logEl();
  if (el) el.textContent = text;
}

function appendLog(chunk) {
  const el = logEl();
  if (!el) return;
  el.textContent += chunk;
  el.scrollTop = el.scrollHeight;
}

function openTerminal() {
  const overlay = terminalEl();
  if (overlay) overlay.hidden = false;
}

function closeTerminal() {
  const overlay = terminalEl();
  if (overlay) overlay.hidden = true;
}

document.getElementById("terminal-close")?.addEventListener("click", closeTerminal);
terminalEl()?.addEventListener("click", (event) => {
  if (event.target === terminalEl()) closeTerminal();
});

function showJobStatus(label) {
  const status = document.getElementById("job-status");
  const text = document.getElementById("job-status-label");
  if (text) text.textContent = label;
  if (status) status.hidden = false;
}

function hideJobStatus() {
  const status = document.getElementById("job-status");
  if (status) status.hidden = true;
}

async function swap(target, url) {
  const res = await fetch(url, { headers: { Accept: "text/html" } });
  if (res.ok) target.innerHTML = await res.text();
}

// Every bit of component markup is server-rendered, so a refresh is a fetch of
// the same partial the page was built from — there is no second copy here.
async function refresh() {
  const panel = document.getElementById("component-panel");
  const stack = document.getElementById("stack-grid");
  const phpPanel = document.getElementById("php-panel");
  if (panel) {
    const source = serviceLog()?.dataset.source;
    const query = source ? `?source=${encodeURIComponent(source)}` : "";
    await swap(panel, `/partials/component/${panel.dataset.id}${query}`);
  }
  if (stack) await swap(stack, "/partials/stack");
  if (phpPanel) await swap(phpPanel, "/partials/php");
  await refreshNav();
  bindServiceLog();
}

async function refreshNav() {
  const state = await api("/api/state").catch(() => null);
  if (!state) return;
  for (const item of state.components) {
    const link = document.querySelector(`[data-nav="${item.id}"]`);
    if (!link) continue;
    const dot = link.querySelector(".dot");
    if (dot) dot.className = `dot dot-${item.status}`;
    link.title = `${item.name} — ${item.status_text}`;
  }
}

function streamJob(jobId) {
  const title = document.getElementById("terminal-title");
  if (title) title.textContent = `Job #${jobId}`;
  showJobStatus(`Running job #${jobId}…`);
  const source = new EventSource(`/api/jobs/${jobId}/stream`);
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.log) appendLog(payload.log);
    if (payload.status !== "success" && payload.status !== "failed") return;
    source.close();
    hideJobStatus();
    toast(payload.status === "success" ? "Job finished" : "Job failed — read the log");
    refresh();
  };
  source.onerror = () => {
    source.close();
    hideJobStatus();
    refresh();
  };
}

// Actions that reach past this machine get a confirm; the rest just run.
function confirmed(act, label) {
  if (act === "remove") {
    return confirm(`Remove ${label}? Databases and site files are left alone.`);
  }
  if (act === "firewall-open") {
    return confirm(`Allow ${label} through UFW? Anything on the network can reach it.`);
  }
  return true;
}

async function runAction(button) {
  const { act, id, name } = button.dataset;
  if (!confirmed(act, name || id)) return;
  setLog("");
  const { job_id: jobId } = await api(`/api/components/${id}/${act}`, { method: "POST" });
  streamJob(jobId);
}

document.addEventListener("click", async (event) => {
  if (event.target.closest("#job-details-link")) {
    openTerminal();
    return;
  }
  if (event.target.closest("#log-play")) {
    setFollow(true);
    refreshServiceLog();
    return;
  }
  if (event.target.closest("#log-pause")) {
    setFollow(false);
    return;
  }

  const button = event.target.closest("button[data-act]");
  if (!button) return;
  event.preventDefault();
  button.disabled = true;
  try {
    await runAction(button);
  } catch (err) {
    toast(err.message);
    setLog(`Could not start the job: ${err.message}`);
  } finally {
    button.disabled = false;
  }
});

// ---- the service log on a component page -----------------------------------
// Not the SSE stream above: a job has an end, whereas access.log does not, so
// this polls a tail instead of following a job to completion.
const serviceLog = () => document.getElementById("service-log");

async function refreshServiceLog() {
  const pre = serviceLog();
  if (!pre) return;
  const { component, source } = pre.dataset;
  const query = new URLSearchParams({ source });
  const pinned = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  try {
    const data = await api(`/api/logs/${component}?${query}`);
    pre.textContent = data.error ? `Could not read this log: ${data.error}` : data.text;
  } catch (err) {
    pre.textContent = `Could not read this log: ${err.message}`;
  }
  if (pinned) pre.scrollTop = pre.scrollHeight;
}

let followTimer;
function setFollow(on) {
  clearInterval(followTimer);
  followTimer = on ? setInterval(refreshServiceLog, 3000) : undefined;
  const play = document.getElementById("log-play");
  const pause = document.getElementById("log-pause");
  play?.classList.toggle("is-on", on);
  pause?.classList.toggle("is-on", !on);
  play?.setAttribute("aria-pressed", String(on));
  pause?.setAttribute("aria-pressed", String(!on));
}

function bindServiceLog() {
  const pre = serviceLog();
  if (!pre) {
    setFollow(false);
    return;
  }
  pre.scrollTo(0, pre.scrollHeight);
  setFollow(true);
}

bindServiceLog();

// Dropping a tag is a plain DOM removal: the partial is re-fetched after the
// job, so the server's list is what survives.
document.addEventListener("click", (event) => {
  event.target.closest(".ext-drop")?.closest(".ext-tag")?.remove();
});

// Every tag still standing, plus whatever was typed into "add". The typed one
// has to carry the php- prefix — that is the package apt installs, and `imap`
// on its own is a different package entirely.
function chosenExtensions(form) {
  const names = [...form.querySelectorAll('input[name="ext"]')].map((input) => input.value);
  const typed = (form.elements.add?.value || "").trim().toLowerCase();
  if (!typed) return names;
  if (!/^php-[a-z0-9]+([-_][a-z0-9]+)*$/.test(typed)) {
    throw new Error(`Use the package name, like php-imap — not ${typed}`);
  }
  return [...new Set([...names, typed.slice(4)])];
}

function submittedOptions(form) {
  const options = {};
  for (const [name, value] of new FormData(form).entries()) {
    if (String(value).trim()) options[name] = value;
  }
  return options;
}

// Forms are inside partials that get replaced wholesale after a job, so these
// are delegated rather than bound to the elements that exist at load.
const FORMS = {
  "settings-form": async (form) => {
    const payload = Object.fromEntries(new FormData(form).entries());
    payload.port = Number(payload.port);
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    toast("Settings saved");
  },
  "php-extensions-form": async (form) => {
    setLog("");
    const body = JSON.stringify({ extensions: chosenExtensions(form) });
    const { job_id: jobId } = await api("/api/php/extensions", { method: "POST", body });
    streamJob(jobId);
  },
  "php-options-form": async (form) => {
    setLog("");
    const body = JSON.stringify({ options: submittedOptions(form) });
    const { job_id: jobId } = await api("/api/php/options", { method: "POST", body });
    streamJob(jobId);
  },
};

document.addEventListener("submit", async (event) => {
  const handler = FORMS[event.target.id];
  if (!handler) return;
  event.preventDefault();
  try {
    await handler(event.target);
  } catch (err) {
    toast(err.message);
  }
});
