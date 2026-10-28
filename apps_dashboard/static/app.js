"use strict";
// Apps dashboard: render /api/apps.  Everything from the server is set as text, never as HTML.

const REFRESH_MS = 30000;
const PENDING_MS = 3000;  // ask again this soon while a scan or screenshots are on their way
const PREVIEWS_KEY = "apps-dashboard.previews";  // ids of the cards switched to show their screenshot
const CACHE_KEY = "apps-dashboard.scan";
let data = null;
let cached = false;
let pendingTimer = null;
let viewer = "local";
let timer = null;
let busy = false;
let pendingTries = 0;
const savedPreviews = readSaved(PREVIEWS_KEY);
let previewed = new Set(Array.isArray(savedPreviews) ? savedPreviews : []);
let searchText = "";
let statusFilter = "";
// The card order and the hidden cards (app ids): saved on the server (PUT /api/layout).
let layout = { order: [], hidden: [], updated: null };
let saving = null;      // the save in flight
let saveAgain = false;  // the layout changed again while it was being saved
let saveError = null;
let drag = null;        // the card being dragged
let justDragged = false;

function readSaved(key) {
  try { return JSON.parse(localStorage.getItem(key) || "null"); }
  catch (err) { return null; }
}

function writeSaved(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); }
  catch (err) { /* blocked or full: keep the current page working */ }
}

function restoreScan() {
  const saved = readSaved(CACHE_KEY);
  if (!saved || !Array.isArray(saved.apps) || !Array.isArray(saved.errors) || !saved.host) return;
  data = saved;
  cached = true;
  viewer = saved.viewer || "local";
  if (saved.layout) layout = saved.layout;
  render();
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function age(seconds) {
  if (seconds === null || seconds === undefined) return "?";
  const s = Math.max(0, Math.round(seconds));
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m`;
  return `${s}s`;
}

function since(iso) {
  return iso ? (Date.now() - Date.parse(iso)) / 1000 : null;
}

function probeText(probe) {
  if (!probe) return "";
  if (probe.code) return `${probe.code}${probe.ms !== null && probe.ms !== undefined ? ` · ${probe.ms} ms` : ""}`;
  return probe.error || "";
}

function probeOk(probe) {
  return probe && probe.code && probe.code < 500;
}

function shortUrl(url) {
  return url.replace(/^https?:\/\//, "").replace(/\/$/, "");
}

function chip(text, cls) {
  return el("span", { class: cls ? `chip ${cls}` : "chip", text });
}

// The tailnet URL to open the app at: the first one under this node's current name.
function mainUrl(app) {
  const route = app.routes.find((r) => r.url && !r.stale);
  return route ? route.url : null;
}

// Where Open goes: the tailnet URL, else (only for a browser on this Mac) the loopback one.
function openUrl(app) {
  return mainUrl(app) || (viewer === "local" && app.local ? app.local.url : null);
}

function linkRow(app) {
  const row = el("div", { class: "links" });
  const idle = app.status === "idle";  // on demand and not running (a published URL answers 502): nothing wrong
  // Entries under the node's old name do not resolve: they are listed under Details only.
  for (const route of app.routes.filter((r) => !r.stale)) {
    if (!route.url) {
      row.append(el("span", { class: "open" }, `tailnet tcp :${route.https_port} → ${route.target}`));
      continue;
    }
    const ok = idle || !route.probe || probeOk(route.probe);
    row.append(el("a", { class: `open primary${ok ? "" : " bad"}`, href: route.url, target: "_blank", rel: "noopener" },
      shortUrl(route.url), el("span", { class: "code", text: idle && route.probe ? "not running" : probeText(route.probe) })));
  }
  if (app.local) {
    const ok = idle || probeOk(app.local.probe);
    const label = `127.0.0.1:${app.local.port}`;
    const code = el("span", { class: "code", text: idle ? "not running" : probeText(app.local.probe) });
    // 127.0.0.1 on a phone is the phone: only link it for a browser on this Mac.
    row.append(viewer === "local"
      ? el("a", { class: `open${ok ? "" : " bad"}`, href: app.local.url, target: "_blank", rel: "noopener", title: "local only" },
        label, code)
      : el("span", { class: `open${ok ? "" : " bad"}`, title: "only reachable on this Mac" }, label, code));
  }
  return row;
}

function launchdChips(ld) {
  const chips = [];
  chips.push(chip(ld.state || (ld.loaded ? "loaded" : "not loaded"), ld.loaded ? "" : "bad"));
  if (ld.keep_alive) chips.push(chip("KeepAlive"));
  if (ld.run_at_load) chips.push(chip("RunAtLoad"));
  if (ld.disabled) chips.push(chip("Disabled", "bad"));
  if (ld.runs) chips.push(chip(`runs ${ld.runs}`));
  if (ld.last_exit && ld.last_exit !== "0") chips.push(chip(`last exit ${ld.last_exit}`, ld.pid ? "" : "bad"));
  return chips;
}

function codeFacts(code) {
  const parts = [];
  const path = el("span", { class: "mono", text: code.display });
  parts.push(path);
  if (code.git) {
    parts.push(el("br"));
    parts.push(chip(code.head ? `${code.branch || "?"}@${code.head}` : `${code.branch || "?"} · no commits`));
    parts.push(code.dirty ? chip(`${code.dirty} uncommitted`, "bad") : chip("clean", "ok"));
    if (code.ahead) parts.push(chip(`${code.ahead} ahead`, "bad"));
    if (code.behind) parts.push(chip(`${code.behind} behind`));
    if (code.upstream && !code.ahead && !code.behind) parts.push(chip(`= ${code.upstream}`, "ok"));
    if (code.remote) {
      parts.push(" ");
      parts.push(el("a", { href: code.remote, target: "_blank", rel: "noopener", text: code.remote.replace(/^https:\/\//, "") }));
    }
  } else if (code.blocked) {
    parts.push(" ", el("span", { class: "chip", text: "no access", title: "macOS does not let the dashboard's LaunchAgent read this folder" }));
  } else {
    parts.push(" ", chip("not git"));
  }
  return parts;
}

// The screenshot the server took of the app (see previews.py), linked to the app.
function previewBox(app) {
  const url = openUrl(app);
  const note = el("span", { class: "preview-note", text: app.preview.error ? `No preview: ${app.preview.error}` : "Taking a screenshot…" });
  const box = el(url ? "a" : "div", { class: "preview", href: url, target: url && "_blank", rel: url && "noopener",
    title: url ? shortUrl(url) : null }, note);
  if (!app.preview.error) {
    const img = el("img", { src: app.preview.src, alt: `${app.name} (screenshot)`, width: 800, height: 500, decoding: "async" });
    img.addEventListener("error", () => { img.remove(); note.textContent = "No preview"; });
    box.append(img);
  }
  return box;
}

function diagnosticBody(app) {
  const facts = el("dl", { class: "facts" });
  const add = (term, ...value) => facts.append(el("dt", { text: term }), el("dd", {}, ...value));
  const proc = app.process;
  if (proc) {
    add("process", `pid ${proc.pid} · up ${age(proc.uptime_s)}`,
      proc.started_from ? el("div", { class: "sub", text: `started from ${proc.started_from}` }) : null);
  } else if (app.local) {
    add("process", el("span", { class: "sub", text: `nothing listening on ${app.local.port}` }));
  }
  if (app.launchd) add("launchd", el("span", { class: "mono", text: app.launchd.label }));
  else if (proc) add("launchd", el("span", { class: "sub", text: "none" }));
  if (app.codebase) add("code", ...codeFacts(app.codebase));

  const more = el("dl", { class: "facts" });
  const addMore = (term, value) => { if (value) more.append(el("dt", { text: term }), el("dd", { class: "mono", text: value })); };
  if (proc) {
    addMore("command", proc.command);
    addMore("cwd", proc.cwd);
    addMore("started", proc.started && new Date(proc.started).toLocaleString());
    addMore("listens", (proc.listen || []).join(", "));
  }
  for (const route of app.routes) {
    addMore(`serve :${route.https_port}${route.path || ""}`, `${route.kind} → ${route.target}` +
      `${route.funnel ? " (Funnel)" : ""}${route.stale && route.url ? ` · old node name ${shortUrl(route.url)}, does not resolve` : ""}`);
  }
  if (app.launchd) {
    more.append(el("dt", { text: "launchd" }), el("dd", {}, launchdChips(app.launchd)));
    addMore("plist", app.launchd.plist);
    addMore("program", app.launchd.program);
    addMore("logs", (app.launchd.logs || []).join("\n"));
  }
  if (app.codebase && app.codebase.source) addMore("code from", app.codebase.source);
  if (app.codebase && app.codebase.runs_from) addMore("runs a copy in", app.codebase.runs_from);
  addMore("app id", app.id);
  const details = el("details", { "data-disclosure": `more:${app.id}` },
    el("summary", { text: "More details", "data-control": "more", "aria-label": `More details for ${app.name}` }), more);
  const tails = app.launchd && app.launchd.log_tail;
  if (tails) {
    for (const [path, lines] of Object.entries(tails)) {
      if (lines && lines.length) details.append(el("div", { class: "sub mono", text: path }), el("pre", { text: lines.join("\n") }));
    }
  }

  return el("div", { class: "diagnostic-body" }, linkRow(app), facts, details);
}

function matchesFilters(app) {
  const text = [app.name, app.id, app.local && app.local.port, ...app.routes.map((r) => r.https_port)].join(" ").toLowerCase();
  return (!searchText || text.includes(searchText)) && (!statusFilter || app.status === statusFilter);
}

function card(app) {
  const key = app.id;
  const url = openUrl(app);
  const menu = el("details", { class: "organize", "data-disclosure": `organize:${key}` },
    el("summary", { text: "···", "data-control": "organize", "aria-label": `Organize ${app.name}` }),
    el("div", { class: "organize-actions" },
      app.preview ? el("button", { type: "button", class: "view", "data-app": key,
        "data-control": "preview", "aria-label": `${previewed.has(key) ? "Hide" : "Show"} preview for ${app.name}` },
        previewed.has(key) ? "Hide preview" : "Show preview") : null,
      el("button", { type: "button", class: "move", "data-app": key, "data-direction": "-1", "data-control": "earlier",
        "aria-label": `Move ${app.name} earlier` }, "Move earlier"),
      el("button", { type: "button", class: "move", "data-app": key, "data-direction": "1", "data-control": "later",
        "aria-label": `Move ${app.name} later` }, "Move later")));
  const hide = el("button", { type: "button", class: "hide", "data-slot": key, "data-control": "hide",
    title: "Hide (Show all apps brings it back)", "aria-label": `Hide ${app.name}` }, "×");
  // The title link starts a drag too (draggable=false keeps the browser's own link drag out of the way).
  const head = el("div", { class: "head" }, el("div", { class: "card-identity" },
    el("h3", { class: "name", title: app.description || null }, url && !app.self
      ? el("a", { href: url, target: "_blank", rel: "noopener", draggable: "false", "data-control": "title",
        "aria-label": `Open ${app.name}` }, app.name)
      : app.name),
    el("div", { class: "card-state" },
      el("span", { class: `status ${app.status}` }, el("span", { class: `dot ${app.status}`, "aria-hidden": "true" }), app.status),
      !app.registered ? el("span", { class: "badge", text: "Not registered", title: "found listening; apps-dashboard register names it" }) : null)),
    el("div", { class: "card-tools" }, menu, hide));
  const warnings = el("ul", { class: "notes collapsed-notes" },
    app.problems.map((text) => el("li", { class: "problem", text })),
    app.warnings.map((text) => el("li", { class: "warning", text })),
    app.status === "idle" ? el("li", { class: "idle-note", text: "On demand · not running" }) : null);
  const diagnostics = el("details", { class: "diagnostics", "data-disclosure": `diagnostics:${key}` },
    el("summary", { "data-control": "diagnostics", "aria-label": `Diagnostics for ${app.name}` }, el("span", { text: "Diagnostics" })),
    diagnosticBody(app));
  return el("article", { class: `card ${app.status}`, id: `app-${key}`, "data-key": key, "data-slot": key },
    head, el("div", { class: "launch-row" }, el("span", { class: "endpoint mono", text: url ? shortUrl(url) : app.local ? `127.0.0.1:${app.local.port}` : "" }),
      app.self ? el("span", { class: "badge self-mark", text: "This page" })
        : url ? el("a", { class: "open-app", href: url, target: "_blank", rel: "noopener", "data-control": "open",
          "aria-label": `Open ${app.name}` }, "Open ↗") : null),
    warnings.childNodes.length ? warnings : null,
    app.preview && previewed.has(key) ? previewBox(app) : null,
    diagnostics);
}

function section(title, apps) {
  if (!apps.length) return null;
  return el("section", {}, el("h2", { text: title }), el("div", { class: "grid" }, apps.map(card)));
}

function onTailnet(app) {
  return app.on_tailnet === undefined ? app.routes.length > 0 : app.on_tailnet;
}

// The page's sections: the apps on the tailnet, then the ones only on this Mac.
function sections(apps) {
  return [arrange(apps.filter(onTailnet)), arrange(apps.filter((a) => !onTailnet(a)))];
}

// The saved order first, then the cards it does not name yet, in the server's order.
function arrange(apps) {
  const index = new Map(layout.order.map((slot, i) => [slot, i]));
  return apps.map((app, i) => [app, index.has(app.id) ? index.get(app.id) : Infinity, i])
    .sort((a, b) => a[1] - b[1] || a[2] - b[2]).map((x) => x[0]);
}

function render() {
  if (!data || (drag && drag.moving)) return;
  const active = document.activeElement;
  const activeCard = active && active.closest && active.closest("[data-key]");
  const focus = activeCard && active.dataset.control ? { key: activeCard.dataset.key, control: active.dataset.control } : null;
  const main = document.getElementById("main");
  const open = new Set([...main.querySelectorAll("details[open][data-disclosure]")].map((d) => d.dataset.disclosure));
  const host = data.host || {};
  const hostNode = document.getElementById("host");
  hostNode.textContent = host.dns || host.name;
  hostNode.title = `this page on ${host.dns || host.name}`;
  const hiddenSlots = new Set(layout.hidden);
  const hidden = data.apps.filter((a) => hiddenSlots.has(a.id)).length;
  const visible = data.apps.filter((a) => !hiddenSlots.has(a.id));
  const counts = {};
  for (const app of data.apps) counts[app.status] = (counts[app.status] || 0) + 1;
  const summary = document.getElementById("summary");
  summary.replaceChildren(...["up", "degraded", "down", "idle"].filter((s) => counts[s])
    .flatMap((s, i) => [i ? " · " : "", el("b", { text: String(counts[s]) }), ` ${s}`]));
  const showAll = document.getElementById("showall");
  showAll.disabled = !hidden;
  showAll.textContent = hidden ? `Show all apps (${hidden})` : "Show all apps";
  showAll.setAttribute("aria-label", `Show all apps (${hidden} hidden)`);
  const errors = document.getElementById("errors");
  const errorList = saveError ? [...data.errors, saveError] : data.errors;
  errors.hidden = !errorList.length;
  errors.replaceChildren(...errorList.map((e) => el("div", { text: e })));
  const shown = visible.filter(matchesFilters);
  const [tailnet, local] = sections(shown);
  main.replaceChildren(...[section("On the tailnet", tailnet), section("Only on this Mac", local)].filter(Boolean));
  if (!data.apps.length) main.append(el("p", { class: "empty", text: "Nothing is served from this Mac." }));
  else if (!visible.length) main.append(el("p", { class: "empty", text: "Every app is hidden: Show all apps brings them back." }));
  else if (!shown.length) main.append(el("p", { class: "empty", text: "No apps match these filters." }));
  document.getElementById("filter-count").textContent = searchText || statusFilter ? `${shown.length} matching ${shown.length === 1 ? "app" : "apps"}` : "";
  for (const disclosure of main.querySelectorAll("details[data-disclosure]")) disclosure.open = open.has(disclosure.dataset.disclosure);
  for (const grid of main.querySelectorAll(".grid")) {
    const cards = [...grid.children];
    cards.forEach((node, i) => { node.querySelector("button.move[data-direction='-1']").disabled = i === 0;
      node.querySelector("button.move[data-direction='1']").disabled = i === cards.length - 1; });
  }
  if (focus) {
    const node = document.getElementById(`app-${focus.key}`);
    const target = node && [...node.querySelectorAll("[data-control]")].find((n) => n.dataset.control === focus.control);
    if (target && !target.disabled) target.focus({ preventScroll: true });
  }
  document.getElementById("foot").textContent =
    `apps-dashboard ${data.version || ""} · this page on ${host.dns || host.name} · registry ${data.registry || "?"} · scan took ${data.took_ms} ms · ` +
    `register apps with ./apps-dashboard register (see README)${data.previews ? "" : " · previews off"}`;
  tick();
}

// "scanned 5s ago" (and "cached", "updating…").
function tick() {
  if (!data) return;
  document.getElementById("scanned").textContent =
    `${cached ? "cached · " : ""}scanned ${age(since(data.scanned))} ago${data.pending ? " · updating…" : ""}`;
}

async function load(fresh) {
  if (busy) return;
  busy = true;
  clearTimeout(pendingTimer);
  const button = document.getElementById("refresh");
  button.disabled = true;
  try {
    const resp = await fetch(`/api/apps${fresh ? "?fresh=1" : ""}`, { cache: "no-store" });
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
    data = body;
    cached = false;
    viewer = body.viewer || "local";
    if (body.layout && !saving && !(Date.parse(body.layout.updated) < Date.parse(layout.updated))) layout = body.layout;
    writeSaved(CACHE_KEY, Object.assign({}, body, { layout }));
    render();
    if ((body.pending || body.previews_pending) && pendingTries < 30) {
      pendingTries += 1;
      pendingTimer = setTimeout(() => load(false), PENDING_MS);
    } else {
      pendingTries = 0;
    }
  } catch (err) {
    const errors = document.getElementById("errors");
    errors.hidden = false;
    errors.replaceChildren(el("div", { text: `Could not load: ${err.message}` }));
  } finally {
    busy = false;
    button.disabled = false;
  }
}

// Save the layout; changes made while a save is in flight go in the next one.
function saveLayout() {
  if (saving) {
    saveAgain = true;
    return saving;
  }
  saving = (async () => {
    try {
      do {
        saveAgain = false;
        // The default mode ("cors") sends this page's real Origin. With mode "same-origin" and the page's
        // no-referrer policy, Safari sends Origin: null, which the server refuses.
        const resp = await fetch("/api/layout", { method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ order: layout.order, hidden: layout.hidden }) });
        const body = await resp.json();
        if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
        if (!saveAgain) layout = body;
        if (data) writeSaved(CACHE_KEY, Object.assign({}, data, { layout }));
      } while (saveAgain);
      if (saveError) { saveError = null; render(); }
    } catch (err) {
      saveError = `Could not save the order and hidden apps: ${err.message}`;
      render();
    } finally {
      saving = null;
    }
  })();
  return saving;
}

function changeLayout(change) {
  layout = Object.assign({}, layout, change);
  if (data) writeSaved(CACHE_KEY, Object.assign({}, data, { layout }));
  render();
  saveLayout();
}

// The new order after a drag: every card where the page would put it (hidden ones too), the shown ones
// as they now stand; saved slots of apps that are not there now (stopped and unregistered) stay after the
// one they followed.
function mergedOrder(shownSlots) {
  const shown = new Set(shownSlots);
  const next = shownSlots[Symbol.iterator]();
  const out = sections(data.apps).flat().map((a) => a.id).map((slot) => (shown.has(slot) ? next.next().value : slot));
  for (const rest of next) out.push(rest);  // the apps changed during the drag
  let previous = null;
  for (const slot of layout.order) {
    if (!out.includes(slot)) out.splice(previous === null ? 0 : out.indexOf(previous) + 1, 0, slot);
    previous = slot;
  }
  return out;
}

// Drag a card (mouse only; touch scrolls) to move it within its section: anywhere but its controls (the
// title link aside: a click on it still opens the app), its Diagnostics and its preview.
const DRAG_PX = 5;
document.getElementById("main").addEventListener("pointerdown", (event) => {
  if (event.pointerType !== "mouse" || event.button !== 0) return;
  const node = event.target.closest("article.card");
  const control = event.target.closest("a, button, summary, input, select, .organize, .diagnostics, .preview");
  if (!node || (control && control.dataset.control !== "title")) return;
  drag = { card: node, grid: node.parentElement, x: event.clientX, y: event.clientY, id: event.pointerId, moving: false };
});
window.addEventListener("pointermove", (event) => {
  if (!drag || event.pointerId !== drag.id) return;
  if (!drag.moving) {
    if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < DRAG_PX) return;
    drag.moving = true;
    drag.before = [...drag.grid.children].map((n) => n.dataset.slot).join("\n");
    drag.card.classList.add("dragging");
    document.body.classList.add("dragging-on");
  }
  event.preventDefault();
  const over = document.elementFromPoint(event.clientX, event.clientY);
  const target = over && over.closest("article");
  if (!target || target === drag.card || target.parentElement !== drag.grid) return;
  const cards = [...drag.grid.children];
  const forward = cards.indexOf(drag.card) < cards.indexOf(target);
  drag.grid.insertBefore(drag.card, forward ? target.nextSibling : target);
});
function endDrag(event) {
  if (!drag || event.pointerId !== drag.id) return;
  const done = drag;
  drag = null;
  if (!done.moving) return;
  justDragged = true;
  setTimeout(() => { justDragged = false; }, 0);
  done.card.classList.remove("dragging");
  document.body.classList.remove("dragging-on");
  if ([...done.grid.children].map((n) => n.dataset.slot).join("\n") !== done.before) {
    changeLayout({ order: mergedOrder([...document.querySelectorAll("#main article[data-slot]")].map((n) => n.dataset.slot)) });
  } else {
    render();  // a refresh may have waited for the drag
  }
}
window.addEventListener("pointerup", endDrag);
window.addEventListener("pointercancel", endDrag);
window.addEventListener("click", (event) => {  // the click that ends a drag does nothing
  if (justDragged) {
    event.stopPropagation();
    event.preventDefault();
  }
}, true);

function schedule() {
  clearInterval(timer);
  timer = setInterval(() => { if (document.visibilityState === "visible") load(false); }, REFRESH_MS);
}

document.getElementById("refresh").addEventListener("click", () => load(true));
document.getElementById("showall").addEventListener("click", () => changeLayout({ hidden: [] }));
function announce(text) { document.getElementById("announcement").textContent = text; }
function focusOrganize(key) {
  const node = document.getElementById(`app-${key}`);
  if (node) node.querySelector(".organize > summary").focus();
}
document.getElementById("app-search").addEventListener("input", (event) => {
  searchText = event.target.value.trim().toLowerCase(); render();
});
document.getElementById("status-filter").addEventListener("change", (event) => {
  statusFilter = event.target.value; render();
});
document.getElementById("main").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const menu = button.closest(".organize");
  if (menu) menu.open = false;
  if (button.classList.contains("hide")) {
    changeLayout({ hidden: [...new Set([...layout.hidden, button.dataset.slot])] });
    announce("App hidden. Show all apps restores it.");
    document.getElementById("showall").focus();
  } else if (button.classList.contains("view")) {
    const id = button.dataset.app;
    if (previewed.has(id)) previewed.delete(id); else previewed.add(id);
    writeSaved(PREVIEWS_KEY, [...previewed]); render(); focusOrganize(id);
  } else if (button.classList.contains("move")) {
    const node = button.closest("article");
    const cards = [...node.parentElement.children];
    const i = cards.indexOf(node), next = i + Number(button.dataset.direction);
    if (next < 0 || next >= cards.length) return;
    node.parentElement.insertBefore(node, next > i ? cards[next].nextSibling : cards[next]);
    changeLayout({ order: mergedOrder([...document.querySelectorAll("#main article[data-slot]")].map((n) => n.dataset.slot)) });
    announce(`App moved ${next > i ? "later" : "earlier"}.`); focusOrganize(button.dataset.app);
  }
});
document.addEventListener("click", (event) => {
  for (const menu of document.querySelectorAll(".organize[open]")) if (!menu.contains(event.target)) menu.open = false;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") for (const menu of document.querySelectorAll(".organize[open]")) {
    menu.open = false; menu.querySelector("summary").focus();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "r" && !event.metaKey && !event.ctrlKey && !event.altKey && event.target === document.body) load(true);
});
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(false); });
setInterval(tick, 5000);
schedule();
restoreScan();
load(false);
