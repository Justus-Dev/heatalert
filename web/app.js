/* HeatWatch family app + demo console. Plain JS, polls /api/state. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let S = null;                                   // latest /api/state
let viewer = new URLSearchParams(location.search).get("as"); // contact id, or null = family overview
let view = "today";
let showRaw = false;
let polling = null;
let journalTimer = null;
let seeded = false;                             // first state seen for this viewer
const seen = new Set();
let unread = 0;
let restoredDismissed = false;
let scenarios = [];
let pick = { scenario: null, weather: null, speed: null };
let setupRendered = false;
let settingsDirty = false;

// ---------------------------------------------------------------- icons

const ICON = {
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
  bell: '<svg viewBox="0 0 24 24"><path d="M6 9a6 6 0 0 1 12 0v4l1.5 3h-15L6 13z"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>',
  alert: '<svg viewBox="0 0 24 24"><path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17.2v.3"/></svg>',
  plug: '<svg viewBox="0 0 24 24"><path d="M9 3v5M15 3v5M6.5 8h11v3a5.5 5.5 0 0 1-11 0zM12 16.5V21"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8v.2"/></svg>',
  door: '<svg viewBox="0 0 24 24"><path d="M6 21V4.5A1.5 1.5 0 0 1 7.5 3h9A1.5 1.5 0 0 1 18 4.5V21M4 21h16M14.5 12.5v.5"/></svg>',
  moon: '<svg viewBox="0 0 24 24"><path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z"/></svg>',
  walk: '<svg viewBox="0 0 24 24"><circle cx="13" cy="4.5" r="1.8"/><path d="m9 21 2.5-6 2.5 2.5V21M8 11.5l2.5-3.5 3 1 2 3 2.5 1"/></svg>',
};

// ---------------------------------------------------------------- helpers

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, credentials: "same-origin", ...opts });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new Error("access code required"); }
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : res.statusText);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// Times are ISO strings in the household's timezone: show the wall-clock part as-is.
const hm = iso => (iso ? iso.slice(11, 16) : "–");
const minutesBetween = (a, b) => (Date.parse(b) - Date.parse(a)) / 60000;
function dur(min) {
  min = Math.max(0, Math.round(min));
  const h = Math.floor(min / 60), m = min % 60;
  if (h && m) return `${h} h ${m} min`;
  return h ? `${h} h` : `${m} min`;
}
function short(min) {
  min = Math.max(0, Math.round(min));
  const h = Math.floor(min / 60), m = min % 60;
  if (h && m) return `${h}h ${m}m`;
  return h ? `${h}h` : `${m}m`;
}
function ago(iso) {
  const m = minutesBetween(iso, S.clock.now);
  return m < 1 ? "just now" : `${dur(m)} ago`;
}
function dayLabel(iso) {
  const d = new Date(iso.slice(0, 10) + "T12:00:00");
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}
function initials(name) {
  return name.replace(/^(Mr|Mrs|Ms|Dr)\.?\s+/i, "").split(/\s+/).map(p => p[0]).join("").slice(0, 2).toUpperCase();
}
const person = () => S.settings.person_name;
const contactById = id => S.settings.contacts.find(c => c.id === id);
const me = () => (viewer ? contactById(viewer) : null);
const relevant = n => !viewer || n.contact_id === viewer;

function toast(title, body, kind = "") {
  const host = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const icon = kind === "alert" ? ICON.alert : kind === "all_clear" ? ICON.check : ICON.bell;
  el.innerHTML = `<div class="t-icon">${icon}</div><div><b>${esc(title)}</b>${esc(body)}</div>`;
  host.prepend(el);
  while (host.children.length > 2) host.lastChild.remove();
  setTimeout(() => { el.classList.add("leave"); setTimeout(() => el.remove(), 220); }, 6000);
}

// ---------------------------------------------------------------- auth & boot

function showLogin() {
  clearInterval(polling); polling = null;
  for (const id of ["today", "messages", "settings", "setup"]) $(`#view-${id}`).hidden = true;
  $("#view-login").hidden = false;
  $(".tabs").hidden = true;
  $("#login-code").focus();
}
$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  const err = $("#login-error");
  err.hidden = true;
  try {
    await post("/api/login", { code: $("#login-code").value });
    $("#view-login").hidden = true;
    $(".tabs").hidden = false;
    start();
  } catch (ex) {
    err.textContent = ex.message === "wrong access code" ? "That code is not right." : ex.message;
    err.hidden = false;
  }
});

async function boot() {
  try {
    const a = await api("/api/auth");
    if (a.required && !a.ok) return showLogin();
  } catch (_) { /* server down: polling shows it */ }
  start();
}
function start() {
  loadScenarios();
  refresh().then(() => {
    const want = location.hash.slice(1);
    if (S && S.settings.setup_complete && ["today", "messages", "settings"].includes(want)) show(want);
  });
  clearInterval(polling); polling = setInterval(refresh, 700);
  clearInterval(journalTimer); journalTimer = setInterval(refreshJournal, 1500);
}

// ---------------------------------------------------------------- navigation

function show(next) {
  if (next === "settings" && viewer) next = "today";
  view = next;
  $$(".tab").forEach(b => b.classList.toggle("active", b.dataset.view === next));
  for (const v of ["today", "messages", "settings"]) $(`#view-${v}`).hidden = v !== next;
  if (next === "messages") { unread = 0; renderBadge(); renderMessages(); }
  if (next === "settings") renderSettings(true);
  $("#scroll").scrollTop = 0;
}
$$(".tab").forEach(b => b.addEventListener("click", () => S && S.settings.setup_complete && show(b.dataset.view)));
$("#show-raw").addEventListener("change", e => { showRaw = e.target.checked; renderTimeline(); });

function setViewer(id) {
  viewer = id || null;
  const url = new URL(location.href);
  if (viewer) url.searchParams.set("as", viewer); else url.searchParams.delete("as");
  history.replaceState(null, "", url);
  seeded = false; seen.clear(); unread = 0;
  $("#view-settings").dataset.rendered = "";
  show("today");
  render();
}

// ---------------------------------------------------------------- polling

async function refresh() {
  try {
    S = await api("/api/state");
  } catch (e) {
    return;
  }
  render();
}

function render() {
  if (!S) return;
  const s = S.settings;
  $('[data-action="indoor_motion"]').textContent = `${s.person_name} moves`;
  $("#tab-settings").hidden = !!viewer;
  renderViewerChip();

  if (!s.setup_complete && !viewer) {
    for (const v of ["today", "messages", "settings"]) $(`#view-${v}`).hidden = true;
    $("#view-setup").hidden = false;
    if (!setupRendered) renderSetup();
  } else {
    $("#view-setup").hidden = true;
    if (setupRendered) { setupRendered = false; show("today"); }
    if ($(`#view-${view}`).hidden) $(`#view-${view}`).hidden = false;
    renderGreeting();
    renderRestored();
    renderHero();
    renderEscalation();
    renderDegraded();
    renderHeat();
    renderTimeline();
    trackMessages();
    if (view === "messages") renderMessages();
    if (view === "settings") renderSettings(false);
  }
  renderConsole();
}

// ---------------------------------------------------------------- header & greeting

function renderViewerChip() {
  const chip = $("#viewer-chip");
  const c = me();
  chip.hidden = false;
  chip.innerHTML = c
    ? `<span>${esc(c.name)}</span><span class="avatar">${esc(initials(c.name))}</span>`
    : `<span>Family</span><span class="avatar">${esc(initials(person()))}</span>`;
}

function renderGreeting() {
  const c = me();
  const s = S.settings;
  $("#greet-title").textContent = c ? `Hi ${c.name}` : `Looking after ${s.person_name}`;
  const clock = S.clock;
  const mode = clock.mode === "demo"
    ? `<span class="mini demo">${clock.paused ? "Paused · " : ""}Demo ×${clock.scale}</span>`
    : "";
  const role = c
    ? `Contact ${s.contacts.findIndex(x => x.id === c.id) + 1} for ${esc(s.person_name)}`
    : esc(s.location_name);
  $("#greet-sub").innerHTML = `<span>${dayLabel(clock.now)} · ${hm(clock.now)}</span><span>·</span><span>${role}</span>${mode}`;
}

function renderRestored() {
  const host = $("#restored");
  if (!S.restored_from || restoredDismissed) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="notice info">${ICON.info}<div style="flex:1"><b>Restored after a restart</b>
    <span class="fine">HeatWatch reloaded its state from ${hm(S.restored_from)}. Nothing was lost.</span></div>
    <button class="link" id="restored-x" aria-label="Dismiss">Dismiss</button></div>`;
  $("#restored-x").addEventListener("click", () => { restoredDismissed = true; renderRestored(); });
}

// ---------------------------------------------------------------- hero

function heroModel() {
  const p = S.presence, inc = S.incident, i = S.inactivity, name = person();
  const open = inc && inc.stage !== "resolved";
  const m = { tone: "green", chip: "At home", pulse: false, title: `${name} is at home`, sub: "", foot: "", footIcon: ICON.clock, ring: "progress" };
  m.sub = p.last_activity
    ? `Last movement ${hm(p.last_activity)} · ${ago(p.last_activity)}`
    : "No movement seen yet today";
  m.foot = i.paused ? i.paused : `Check-in at ${hm(i.due_at)} if there is no movement`;
  if (!i.paused && i.progress >= 0.75) { m.tone = "amber"; m.chip = "Quiet for a while"; }

  if (open && inc.stage === "checkin") {
    Object.assign(m, { tone: "amber", chip: "Checking in", pulse: true, title: `Checking on ${name}`,
      sub: `No movement since ${hm(i.reference)}. A gentle tone is playing on the chime.`,
      foot: `If ${name} moves, nobody is alerted`, footIcon: ICON.bell, ring: "check" });
  } else if (open && inc.stage === "contact") {
    Object.assign(m, { tone: "red", chip: "Needs attention", pulse: true, title: `Please check on ${name}`,
      sub: inc.checkin_skipped ? `The chime is offline, so ${name} could not be asked first.` : `No reaction to the check-in tone at ${hm(inc.opened_at)}.`,
      foot: `${inc.current_contact_id === viewer ? "You were" : inc.current_contact} alerted at ${hm(inc.stage_since)}`, footIcon: ICON.bell, ring: "full" });
  } else if (open && inc.stage === "unanswered") {
    Object.assign(m, { tone: "red", chip: "Urgent", pulse: true, title: `Nobody has reached ${name}`,
      sub: "Call now. If there is no answer, call emergency services (112).",
      foot: "All contacts were alerted", footIcon: ICON.alert, ring: "full" });
  } else if (open && inc.stage === "acknowledged") {
    Object.assign(m, { tone: "amber", chip: "Being handled", title: `${inc.acknowledged_by} is on it`,
      sub: `Since ${hm(inc.acknowledged_at)}. Close the alert once ${name} is OK.`,
      foot: inc.ack_reminded
        ? (inc.acknowledged_by_id === viewer ? "HeatWatch asked again. Please confirm, or tap “Still on it”" : "HeatWatch asked again and is waiting for a confirmation")
        : `HeatWatch asks again after ${S.settings.ack_followup_min} min`,
      footIcon: ICON.bell, ring: "check" });
  } else if (p.status === "away") {
    Object.assign(m, { tone: "blue", chip: "Out", title: `${name} is out`, sub: `Left at ${hm(p.last_departure || p.since)}`,
      foot: "No inactivity checks while out", footIcon: ICON.walk, ring: "off" });
  } else if (p.status === "door") {
    Object.assign(m, { tone: "gray", chip: "Front door", title: "Front door in use",
      sub: "Working out whether someone came in or went out…", foot: "This takes up to 10 minutes", footIcon: ICON.door, ring: "off" });
  } else if (i.paused && i.paused.startsWith("Night rest")) {
    Object.assign(m, { tone: "violet", chip: "Night rest", title: `${name} is resting`, foot: `${i.paused}. No check-ins at night.`, footIcon: ICON.moon, ring: "off" });
  } else if (i.paused === "Indoor camera offline") {
    Object.assign(m, { tone: "amber", chip: "Limited", title: "Camera offline",
      sub: "HeatWatch cannot see movement right now.", foot: "Inactivity checks are paused", footIcon: ICON.plug, ring: "off" });
  }
  return { ...m, open };
}

function ringSvg(model) {
  const i = S.inactivity;
  const C = 2 * Math.PI * 30;
  let frac = Math.min(1, i.progress), cls = "", big = short(i.elapsed_min), small = `of ${short(i.threshold_min)}`;
  if (model.ring === "off") { frac = 0; cls = "off"; big = "–"; small = "paused"; }
  else if (model.ring === "full") { frac = 1; cls = "full"; }
  else if (model.ring === "check") { frac = 1; cls = "mid"; }
  else if (frac >= 1) cls = "full";
  else if (frac >= 0.6) cls = "mid";
  return `<div class="ring ${cls}" aria-label="Quiet for ${esc(dur(i.elapsed_min))} of ${esc(dur(i.threshold_min))}">
    <svg viewBox="0 0 76 76"><circle class="bg" cx="38" cy="38" r="30"/>
    <circle class="fg" cx="38" cy="38" r="30" stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - frac)}"/></svg>
    <div class="ring-label"><b>${esc(big)}</b><span>${esc(small)}</span></div></div>`;
}

function renderHero() {
  const m = heroModel();
  const inc = S.incident;
  let actions = "";
  if (m.open && inc.stage !== "checkin") {
    if (viewer) {
      const mine = inc.acknowledged_by_id === viewer;
      actions = `<div class="hero-actions">
        <button class="btn ${mine ? "ghost" : "danger"}" data-act="ack">${mine ? "Still on it" : "I'm on it"}</button>
        <button class="btn ${mine ? "primary" : "outline"}" data-act="resolve">${esc(person())} is OK</button></div>`;
    } else {
      actions = `<div class="hero-note">Contacts respond from their own HeatWatch view.</div>`;
    }
  } else if (m.open && viewer) {
    actions = `<div class="hero-note">You'll get a message if ${esc(person())} doesn't respond to the tone.</div>`;
  }
  const hero = $("#hero");
  hero.innerHTML = `
    <div class="hero-top"><span class="chip tone-${m.tone} ${m.pulse ? "pulse" : ""}"><i class="dot"></i>${esc(m.chip)}</span></div>
    <div class="hero-main"><div><h2>${esc(m.title)}</h2>${m.sub ? `<p>${esc(m.sub)}</p>` : ""}</div>${ringSvg(m)}</div>
    <div class="hero-foot">${m.footIcon}<span>${esc(m.foot)}</span></div>
    ${actions}`;
  $$("[data-act]", hero).forEach(b => b.addEventListener("click", () => {
    const path = b.dataset.act === "ack" ? "/api/incident/ack" : "/api/incident/resolve";
    act(path, { contact_id: viewer });
  }));
}

async function act(path, data) {
  try { S = await post(path, data); render(); } catch (e) { toast("That did not work", e.message, "alert"); }
}

// ---------------------------------------------------------------- escalation

function firstMessageTime(incId, contactId, prefix) {
  const n = [...S.notifications].reverse().find(n =>
    n.incident_id === incId && (contactId == null || n.contact_id === contactId) && n.title.startsWith(prefix));
  return n ? n.at : null;
}

function escalationSteps(inc) {
  const s = S.settings;
  const steps = [
    { key: "chime", label: inc.checkin_skipped ? "Chime offline: tone skipped" : `Gentle tone on the chime${inc.chime_plays.length > 1 ? ` (${inc.chime_plays.length}×)` : ""}`, when: inc.checkin_skipped ? null : inc.opened_at },
    { key: "wait", label: `Wait ${s.checkin_window_min} min for movement`, when: null },
  ];
  s.contacts.forEach((c, i) => steps.push({
    key: `c${i}`, cid: c.id,
    label: `Alert ${c.name}${c.has_key ? " (has a key)" : ""}${inc.acknowledged_by_id === c.id ? " · on it" : ""}`,
    when: firstMessageTime(inc.id, c.id, "Please check on"),
  }));
  steps.push({ key: "all", label: "Tell everyone nobody has responded", when: firstMessageTime(inc.id, null, "URGENT") });

  const keys = steps.map(x => x.key);
  const idxOf = cid => s.contacts.findIndex(c => c.id === cid);
  let cur = -1;
  if (inc.stage === "checkin") cur = keys.indexOf("wait");
  else if (inc.stage === "contact") cur = keys.indexOf(`c${idxOf(inc.current_contact_id)}`);
  else if (inc.stage === "unanswered") cur = keys.indexOf("all");
  else if (inc.stage === "acknowledged") cur = keys.indexOf(`c${idxOf(inc.acknowledged_by_id)}`);

  return steps.map((st, i) => {
    let state = "";
    const reached = st.when != null;
    if (inc.checkin_skipped && (st.key === "chime" || st.key === "wait")) state = "skip";
    else if (inc.stage === "resolved") state = st.key === "chime" || st.key === "wait" || reached ? "done" : "skip";
    else if (i === cur) state = "now";
    else if (i < cur) state = "done";
    return { ...st, state };
  });
}

function renderEscalation() {
  const host = $("#escalation");
  const inc = S.incident;
  if (!inc) { host.innerHTML = ""; return; }
  if (inc.stage === "resolved") {
    const title = inc.family_alerted ? "Alert resolved" : "Check-in answered";
    const sub = inc.family_alerted ? inc.resolution : `${person()} moved after the tone. Nobody was alerted.`;
    host.innerHTML = `<div class="card"><div class="resolved-line"><div class="ok-mark">${ICON.check}</div>
      <div><b>${esc(title)} · ${hm(inc.resolved_at)}</b><span>${esc(sub)}</span></div></div></div>`;
    return;
  }
  const steps = escalationSteps(inc).map((st, i) => `
    <li class="${st.state}"><span class="mark">${st.state === "done" ? ICON.check : i + 1}</span>
    <span class="label">${esc(st.label)}</span><span class="when">${st.when ? hm(st.when) : ""}</span></li>`).join("");

  // Privacy: the image is visible only to the contact it was shared with.
  let snap = "";
  if (inc.snapshot_id) {
    const shared = S.notifications.find(n => n.snapshot_id === inc.snapshot_id);
    const to = shared ? contactById(shared.contact_id) : null;
    snap = viewer && shared && shared.contact_id === viewer
      ? `<div class="snapshot"><img src="/api/snapshots/${esc(inc.snapshot_id)}" alt="Last camera snapshot" onerror="this.remove()"><span>Last snapshot, shared only with you because ${esc(person())} agreed to it. (Emulator placeholder image.)</span></div>`
      : `<p class="fine" style="margin-top:12px">The last snapshot was shared only with ${esc(to ? to.name : "the alerted contact")}, as ${esc(person())} agreed.</p>`;
  }
  const log = inc.log.map(l => `<li><span class="t">${hm(l.at)}</span><span class="m"></span><span class="det">${esc(l.text)}</span></li>`).join("");
  host.innerHTML = `<div class="card">
      <div class="card-head"><h2>Escalation</h2><span class="fine">${esc(dur(inc.threshold_min))} limit${inc.heat_level !== "none" ? " · heat" : ""}</span></div>
      <ol class="steps">${steps}</ol>${snap}
      <details class="log-details"><summary>Incident log</summary><ol class="timeline">${log}</ol></details>
    </div>`;
}

// ---------------------------------------------------------------- degraded, heat, timeline

function renderDegraded() {
  const host = $("#degraded");
  if (!S.degraded.length) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="notice">${ICON.plug}<div><b>Limited protection</b><ul>
    ${S.degraded.map(d => `<li><b>${esc(d.name)} is offline.</b> ${esc(d.impact)}</li>`).join("")}</ul></div></div>`;
}

function renderHeat() {
  const h = S.heat, w = h.weather;
  const card = $("#heat");
  card.className = `card ${h.level === "none" ? "" : "heat-on"}`;
  const feels = w && w.apparent_max_c != null ? `${Math.round(w.apparent_max_c)}°` : "–";
  const tmax = w && w.temp_max_c != null ? `feels like · ${w.temp_max_c.toFixed(1)} °C max` : "no forecast yet";
  const t = h.thresholds_min;
  const lvl = (k, label) => `<div class="${h.level === k ? "on" : ""}"><b>${short(t[k])}</b>${label}</div>`;
  const tone = h.level === "extreme" ? "red" : h.level === "heat" ? "accent" : "green";
  const indoor = S.devices.find(d => d.role === "climate_sensor");
  const notes = [
    ...h.reasons,
    w ? w.source : "Weather loading…",
    indoor && !h.indoor_hot ? `${indoor.name}: below ${indoor.temperature_max_c ?? "?"} °C` : null,
    h.weather_error,
  ].filter(Boolean);
  card.innerHTML = `
    <div class="heat-head"><div class="heat-icon">${ICON.sun}</div>
      <div><div class="heat-temp">${feels}</div><div class="heat-sub">${esc(tmax)}</div></div>
      <span class="chip tone-${tone}">${esc(h.level === "none" ? "No heat warning" : h.label)}</span></div>
    <div class="levels">${lvl("none", "normal")}${lvl("heat", "heat")}${lvl("extreme", "extreme")}</div>
    <div class="heat-notes">${notes.map(n => `<div class="fine">${esc(n)}</div>`).join("")}</div>`;
}

function renderTimeline() {
  if (!S) return;
  const items = S.timeline.filter(e => showRaw || e.kind !== "ring");
  $("#timeline").innerHTML = items.length
    ? items.map(e => `<li class="k-${e.kind}"><span class="t">${hm(e.at)}</span><span class="m"></span>
        <div><div class="ttl">${esc(e.title)}</div>${e.detail ? `<div class="det">${esc(e.detail)}</div>` : ""}</div></li>`).join("")
    : `<li class="empty">Nothing yet today.</li>`;
}

$("#btn-summary").addEventListener("click", async e => {
  const btn = e.currentTarget;
  btn.disabled = true; btn.textContent = "Writing…";
  try {
    const r = await post("/api/summary");
    const box = $("#summary");
    box.hidden = false;
    const ai = r.author !== "template";
    box.innerHTML = `${esc(r.text)}<div class="author ${ai ? "ai" : ""}">${ai ? `Written by Claude on Amazon Bedrock` : "Template text · Amazon Bedrock not configured"}</div>`;
  } catch (err) { toast("Summary failed", err.message, "alert"); }
  btn.disabled = false; btn.textContent = "Summarize again";
});

// ---------------------------------------------------------------- messages

function trackMessages() {
  for (const m of [...S.notifications].reverse()) {
    if (seen.has(m.id)) continue;
    seen.add(m.id);
    if (!seeded || !relevant(m)) continue;
    if (view !== "messages") unread += 1;
    toast(viewer ? m.title : `To ${m.contact_name}: ${m.title}`, m.body, m.category);
    if (window.Notification && Notification.permission === "granted") {
      try { new Notification(`HeatWatch: ${m.title}`, { body: m.body }); } catch (_) {}
    }
  }
  seeded = true;
  renderBadge();
}
function renderBadge() {
  const b = $("#badge");
  b.hidden = unread === 0;
  b.textContent = unread;
}
const CATEGORY = { alert: ["Alert", "red"], all_clear: ["All clear", "green"], nudge: ["Care hint", "accent"], info: ["Info", "blue"] };

function renderMessages() {
  if (!S) return;
  const msgs = S.notifications.filter(relevant);
  $("#messages-sub").textContent = viewer ? `Messages for ${me() ? me().name : "you"}` : "Everything HeatWatch sent to the family";
  $("#messages").innerHTML = msgs.length ? msgs.map(m => {
    const [cat, tone] = CATEGORY[m.category] || ["Message", "gray"];
    const c = contactById(m.contact_id);
    const ai = m.author !== "template";
    const author = ai ? "Written by Claude on Amazon Bedrock" : m.category === "nudge" ? "Template text · Bedrock not configured" : "Fixed safety template";
    return `<article class="msg">
      <div class="meta"><span class="chip tone-${tone}">${cat}</span><span>${viewer ? "" : `To ${esc(m.contact_name)}${c ? ` · ${esc(c.relation)}` : ""} · `}${hm(m.at)}</span></div>
      <h4>${esc(m.title)}</h4><p>${esc(m.body)}</p>
      ${m.snapshot_id && viewer === m.contact_id ? `<div class="snapshot"><img src="/api/snapshots/${esc(m.snapshot_id)}" alt="Last snapshot" onerror="this.remove()"><span>Last snapshot, shared with you with consent</span></div>` : ""}
      <div class="author ${ai ? "ai" : ""}">${author}</div>
    </article>`;
  }).join("") : `<div class="card empty">No messages yet. HeatWatch only writes when it matters.</div>`;
}
$("#btn-notify-perm").addEventListener("click", async () => {
  if (!window.Notification) return toast("Not supported", "This browser has no desktop notifications.");
  const p = await Notification.requestPermission();
  toast("Desktop notifications", p === "granted" ? "On. New messages will pop up." : "Permission was not granted.");
});

// ---------------------------------------------------------------- setup

function renderSetup() {
  setupRendered = true;
  const s = S.settings;
  const host = $("#view-setup");
  host.innerHTML = "";
  host.appendChild($("#tpl-setup").content.cloneNode(true));
  const f = $("#setup-form");
  for (const k of ["person_name", "person_nickname", "location_name", "latitude", "longitude", "quiet_start", "quiet_end"]) f.elements[k].value = s[k];
  f.elements.share_snapshot.checked = s.consent.share_snapshot;
  $("#setup-contacts").innerHTML = s.contacts.map((c, i) => `
    <div class="contact-edit" data-contact="${esc(c.id)}">
      <div class="num">Contact ${i + 1} · ${i === 0 ? "alerted first" : "if contact " + i + " does not respond"}</div>
      <div class="two"><label>Name<input data-f="name" value="${esc(c.name)}" required></label>
      <label>Relation<input data-f="relation" value="${esc(c.relation)}" required></label></div>
      <label>Phone<input data-f="phone" value="${esc(c.phone)}" inputmode="tel"></label>
      <label class="check"><input type="checkbox" data-f="has_key" ${c.has_key ? "checked" : ""}><span>Has a key to the flat</span></label>
    </div>`).join("");
  const syncName = () => $$("[data-name]", f).forEach(el => (el.textContent = f.elements.person_name.value || "them"));
  f.elements.person_name.addEventListener("input", syncName);
  syncName();
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const err = $("#setup-error");
    err.hidden = true;
    if (!f.reportValidity()) return;
    const contacts = $$("[data-contact]", f).map(el => ({
      id: el.dataset.contact,
      name: $('[data-f="name"]', el).value.trim(),
      relation: $('[data-f="relation"]', el).value.trim(),
      phone: $('[data-f="phone"]', el).value.trim(),
      has_key: $('[data-f="has_key"]', el).checked,
    }));
    try {
      await post("/api/setup", {
        person_name: f.elements.person_name.value.trim(),
        person_nickname: f.elements.person_nickname.value.trim(),
        location_name: f.elements.location_name.value.trim(),
        latitude: Number(f.elements.latitude.value),
        longitude: Number(f.elements.longitude.value),
        contacts,
        quiet_start: f.elements.quiet_start.value,
        quiet_end: f.elements.quiet_end.value,
        consent_given: f.elements.consent_given.checked,
        consent_given_by: f.elements.consent_given_by.value,
        share_snapshot: f.elements.share_snapshot.checked,
      });
      toast("HeatWatch is on", `Looking after ${f.elements.person_name.value.trim()}.`, "all_clear");
      await refresh();
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    }
  });
}

// ---------------------------------------------------------------- settings (family overview only)

const ROLE_LABELS = {
  entrance_camera: "Entrance / doorbell", door_sensor: "Door sensor", indoor_camera: "Indoor camera",
  garden_camera: "Garden camera", chime: "Chime", climate_sensor: "Climate sensor", ignore: "Not used",
};

async function renderSettings(force) {
  const host = $("#view-settings");
  if (!force && (settingsDirty || host.contains(document.activeElement))) return;
  if (!force && host.dataset.rendered) {
    const ai = $("#ai-status");
    if (ai) ai.textContent = S.ai.status;
    return;
  }
  host.dataset.rendered = "1";
  settingsDirty = false;
  const s = S.settings;
  let auth = { required: false };
  try { auth = await api("/api/auth"); } catch (_) {}
  const num = (name, label, min = 1) => `<label>${label}<input type="number" min="${min}" name="${name}" value="${esc(s[name])}"></label>`;
  host.innerHTML = `
    <div class="greet"><h1>Settings</h1><p>${esc(s.person_name)} · ${esc(s.location_name)}</p></div>

    <div class="group-title">Privacy</div>
    <div class="group">
      <div class="row"><span class="k">Consent</span><span class="v">${esc(s.consent.given_by)}${s.consent.given_at ? `<br>${esc(s.consent.given_at.slice(0, 10))}` : ""}</span></div>
      <div class="row"><span class="k">Emergency snapshot</span><span class="v">${s.consent.share_snapshot ? "Shared with alerted contact" : "Off"}</span></div>
      <div class="row"><span class="k">Event history</span><span class="v">Deleted after 24 h</span></div>
      <div class="row"><button class="link" id="btn-redo">Redo setup and consent</button></div>
    </div>

    <div class="group-title">When to check in</div>
    <form class="group form-pad" id="rules-form">
      <p class="fine" style="padding-top:12px">Minutes without movement at home before the chime check-in.</p>
      <div class="three">${num("threshold_normal_min", "Normal", 15)}${num("threshold_heat_min", "Heat", 15)}${num("threshold_extreme_min", "Extreme", 15)}</div>
      <div class="two">
        <label>Heat from (feels like °C)<input type="number" step="0.5" name="heat_apparent_c" value="${s.heat_apparent_c}"></label>
        <label>Extreme from (°C)<input type="number" step="0.5" name="extreme_apparent_c" value="${s.extreme_apparent_c}"></label>
      </div>
      <div class="three">${num("checkin_window_min", "After tone")}${num("contact_ack_window_min", "Per contact")}${num("ack_followup_min", "Follow-up")}</div>
      <p class="fine" style="margin-top:6px">Minutes to wait after the tone, for each contact, and before asking again after “I'm on it”.</p>
      <div class="two">
        <label>Night rest from<input type="time" name="quiet_start" value="${s.quiet_start}"></label>
        <label>until<input type="time" name="quiet_end" value="${s.quiet_end}"></label>
      </div>
      <label class="check"><input type="checkbox" name="nudges_enabled" ${s.nudges_enabled ? "checked" : ""}><span>On heat days, suggest a call if nobody came or went</span></label>
      <label>Care hint time<input type="time" name="nudge_time" value="${s.nudge_time}"></label>
      <div class="saved" id="rules-msg"></div>
      <button class="btn primary block" type="submit">Save</button>
    </form>

    <div class="group-title">Contacts, in order</div>
    <div class="group">
      ${s.contacts.map((c, i) => `<div class="row"><span class="k">${i + 1}. ${esc(c.name)}<span class="sub">${esc(c.relation)}${c.has_key ? " · has a key" : ""} · ${esc(c.phone)}</span></span>
        <a class="link" href="?as=${esc(c.id)}">Open view</a></div>`).join("")}
    </div>

    <div class="group-title">Ring devices</div>
    <div class="group">
      ${S.devices.map(d => `<div class="row"><span class="k">${esc(d.name)}<span class="sub">${esc(d.kind.replace("_", " "))} · ${d.offline ? "offline" : "online"}${d.battery != null ? ` · ${d.battery}%` : ""}</span></span>
        <select data-device="${esc(d.name)}">${Object.entries(ROLE_LABELS).map(([k, v]) => `<option value="${k}" ${d.role === k ? "selected" : ""}>${v}</option>`).join("")}</select></div>`).join("") || '<div class="row"><span class="v">No devices discovered.</span></div>'}
    </div>

    <div class="group-title">Writing assistant</div>
    <div class="group"><div class="row"><span class="k">Status<span class="sub" id="ai-status">${esc(S.ai.status)}</span></span></div>
      <div class="row"><span class="fine">Used only for the care hint and the day summary. Alerts always use fixed templates.</span></div></div>
    ${auth.required ? `<div class="group-title">Access</div><div class="group"><div class="row"><button class="link" id="btn-logout">Sign out</button></div></div>` : ""}
    <div style="height:8px"></div>`;

  $("#btn-redo").addEventListener("click", async () => { await post("/api/setup/reset"); host.dataset.rendered = ""; await refresh(); });
  const logout = $("#btn-logout");
  if (logout) logout.addEventListener("click", async () => { await post("/api/logout"); showLogin(); });
  const form = $("#rules-form");
  form.addEventListener("input", () => { settingsDirty = true; $("#rules-msg").textContent = ""; });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const patch = {};
    for (const [k, v] of new FormData(form).entries()) patch[k] = ["quiet_start", "quiet_end", "nudge_time"].includes(k) ? v : Number(v);
    patch.nudges_enabled = form.elements.nudges_enabled.checked;
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
      $("#rules-msg").style.color = "";
      $("#rules-msg").textContent = "Saved.";
      settingsDirty = false;
    } catch (ex) { $("#rules-msg").style.color = "var(--red)"; $("#rules-msg").textContent = ex.message; }
  });
  $$("[data-device]", host).forEach(sel => sel.addEventListener("change", async () => {
    const roles = { ...S.settings.device_roles, [sel.dataset.device]: sel.value };
    try { await api("/api/settings", { method: "PUT", body: JSON.stringify({ device_roles: roles }) }); toast("Saved", `${sel.dataset.device}: ${sel.options[sel.selectedIndex].text}`, "all_clear"); }
    catch (ex) { toast("Could not save", ex.message, "alert"); }
  }));
}

// ---------------------------------------------------------------- demo console

async function loadScenarios() {
  try { scenarios = await api("/api/demo/scenarios"); } catch (_) { scenarios = []; }
  const order = Object.keys(SCENARIO_TEXT);
  const rank = id => (order.includes(id) ? order.indexOf(id) : order.length);
  scenarios.sort((a, b) => rank(a.id) - rank(b.id));
  if (!pick.scenario && scenarios.length) choose(scenarios[0].id);
  drawScenarios();
}
function choose(id) {
  const sc = scenarios.find(s => s.id === id);
  if (!sc) return;
  pick = { scenario: id, weather: sc.weather, speed: sc.speed };
  drawScenarios();
}
const SCENARIO_TEXT = {
  heat_day_emergency: ["Emergency on a heat day", "Shopping trip, then no movement after 11:05. Replay it with both weathers."],
  heat_day_false_alarm: ["Nap: false alarm caught", "The chime check-in is answered, nobody is bothered. Care hint at 17:00."],
  out_for_the_day: ["Out for the day", "Hours without movement inside, but the door shows Margaret left: no alarm."],
  device_trouble: ["Devices fail", "Door sensor and chime go offline. HeatWatch stays on the safe side and says so."],
};
function drawScenarios() {
  $("#scenario-list").innerHTML = scenarios.map(sc => {
    const [t, d] = SCENARIO_TEXT[sc.id] || [sc.name, sc.description];
    return `<button class="scenario ${pick.scenario === sc.id ? "sel" : ""}" data-sc="${esc(sc.id)}"><b>${esc(t)}</b><span>${esc(d)} · ${esc(sc.start)}–${esc(sc.end)}</span></button>`;
  }).join("");
  $$("[data-sc]").forEach(b => b.addEventListener("click", () => choose(b.dataset.sc)));
  $$("#weather-seg button").forEach(b => b.classList.toggle("sel", b.dataset.weather === pick.weather));
  const live = S && S.clock.mode === "demo" ? S.clock.scale : pick.speed;
  $$("#speed-seg button").forEach(b => b.classList.toggle("sel", Number(b.dataset.speed) === live));
}
$$("#weather-seg button").forEach(b => b.addEventListener("click", () => { pick.weather = b.dataset.weather; drawScenarios(); }));
$$("#speed-seg button").forEach(b => b.addEventListener("click", async () => {
  pick.speed = Number(b.dataset.speed);
  if (S && S.clock.mode === "demo") {
    try { await post("/api/demo/speed", { speed: pick.speed }); await refresh(); } catch (e) { toast("Speed", e.message, "alert"); }
  }
  drawScenarios();
}));
$("#btn-start").addEventListener("click", async () => {
  if (!pick.scenario) return;
  const btn = $("#btn-start");
  btn.disabled = true;
  try {
    S = await post("/api/demo/start", pick);
    seeded = false; seen.clear(); unread = 0; restoredDismissed = true;
    show("today");
    render();
  } catch (e) { toast("Cannot start", e.message, "alert"); }
  btn.disabled = false;
});
$("#btn-pause").addEventListener("click", async () => {
  if (!S || S.clock.mode !== "demo") return;
  await post(S.clock.paused ? "/api/demo/resume" : "/api/demo/pause");
  refresh();
});
$("#btn-live").addEventListener("click", async () => { S = await post("/api/demo/live"); render(); });
$$("#manual-buttons button").forEach(b => b.addEventListener("click", async () => {
  try { const r = await post("/api/demo/inject", { action: b.dataset.action }); b.blur(); toast("Emulator", r.label); }
  catch (e) { toast("Emulator", e.message, "alert"); }
}));

function renderConsole() {
  const d = S.demo, r = S.ring;
  // whose phone
  const seg = $("#viewer-seg");
  const opts = [{ id: "", label: "Family overview" }, ...S.settings.contacts.map(c => ({ id: c.id, label: c.name }))];
  const sig = opts.map(o => o.id + o.label).join("|") + "#" + (viewer || "");
  if (seg.dataset.sig !== sig) {
    seg.dataset.sig = sig;
    seg.innerHTML = opts.map(o => `<button data-viewer="${esc(o.id)}" class="${(viewer || "") === o.id ? "sel" : ""}">${esc(o.label)}</button>`).join("");
    $$("[data-viewer]", seg).forEach(b => b.addEventListener("click", () => setViewer(b.dataset.viewer)));
  }
  drawScenarios();

  $("#btn-pause").textContent = S.clock.paused ? "Resume" : "Pause";
  $("#btn-pause").disabled = S.clock.mode !== "demo";
  const lines = [];
  if (S.clock.mode === "demo" && S.demo_label) {
    lines.push(S.demo_label);
    if (d.scenario) lines.push(`steps fired ${d.steps_fired} · pending ${d.steps_pending}${d.next_step_at ? ` · next ${hm(d.next_step_at)}` : ""}${d.finished ? " · finished" : ""}`);
  } else {
    lines.push("Live mode: real time, live forecast.");
  }
  if (d.last_error) lines.push(`! ${d.last_error}`);
  $("#demo-status").textContent = lines.join("\n");

  $("#ring-kv").innerHTML = `
    <dt>Ring API</dt><dd><span class="status-dot ${r.ready ? "" : "bad"}"></span>${esc(r.base_url)} ${r.sandbox ? "(ring-sandbox)" : "(production)"}</dd>
    <dt>Account</dt><dd>${esc(r.account ? r.account.id : "–")}</dd>
    <dt>Devices</dt><dd>${S.devices.length} via GET /v1/devices</dd>
    <dt>Webhooks</dt><dd>${r.webhooks_verified} verified · ${r.webhooks_rejected} rejected</dd>
    ${r.last_error ? `<dt>Last error</dt><dd>${esc(r.last_error)}</dd>` : ""}`;
  $("#api-calls").innerHTML = r.calls.map(c => `<li><span class="m">${esc(c.method)}</span><span class="s${String(c.status || 5)[0]}">${c.status ?? "ERR"}</span><span class="p">${esc(decodeURIComponent(c.path))}</span></li>`).join("")
    || `<li><span></span><span></span><span class="p">No calls yet</span></li>`;
}

async function refreshJournal() {
  try {
    const j = await api("/api/demo/journal");
    $("#journal").innerHTML = j.slice().reverse().map(e => {
      if (e.kind === "chime.play") return `<li><span class="m">CHIME</span><span class="s2">♪</span><span class="p">${esc(e.event)} · …${esc(e.device_id.slice(-6))}</span></li>`;
      if (e.kind === "webhook") return `<li><span class="m">POST</span><span class="s${String(e.status || 5)[0]}">${e.status ?? "ERR"}</span><span class="p">signed webhook → ${esc(new URL(e.url).pathname)} · ${esc(e.request_id.slice(0, 8))}</span></li>`;
      return `<li><span class="m">${esc(e.kind)}</span><span></span><span class="p">${esc(JSON.stringify(e))}</span></li>`;
    }).join("") || `<li><span></span><span></span><span class="p">Nothing delivered yet</span></li>`;
  } catch (_) { /* emulator offline or signed out: keep last view */ }
}

// ---------------------------------------------------------------- boot
boot();
