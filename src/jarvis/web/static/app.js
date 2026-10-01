"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (path, options = {}) => {
  const opts = { credentials: "same-origin", ...options };
  opts.headers = { "X-Jarvis": "1", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  const response = await fetch(path, opts);
  if (response.status === 401 && path !== "/api/login") { showLogin(); throw new Error("signed out"); }
  return response;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "never";
let vaultName = "Jarvis";
let ttsProvider = "browser";
let lastActivityId = 0;
const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
};

/* ---------- tiny, safe markdown ---------- */
function inline(text) {
  let s = esc(text);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_m, target, alias) => {
    const href = `obsidian://open?vault=${encodeURIComponent(vaultName)}&file=${encodeURIComponent(target.replace(/&amp;/g, "&"))}`;
    return `<a href="${href}">${alias || target.split("/").pop()}</a>`;
  });
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}
function markdown(text) {
  const out = [];
  let list = null;
  for (const line of String(text).split("\n")) {
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) { if (!list) { list = []; } list.push(`<li>${inline(item[1])}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    const heading = line.match(/^#{1,4}\s+(.*)$/);
    const quote = line.match(/^>\s?(.*)$/);
    if (heading) out.push(`<h4>${inline(heading[1])}</h4>`);
    else if (quote) out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("");
}

/* ---------- auth ---------- */
function showLogin(session) {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  if (session) {
    $("#code-row").classList.toggle("hidden", !session.totp);
    $("#login-hint").classList.toggle("hidden", session.password_set);
  }
}
async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  const session = await (await fetch("/api/session", { credentials: "same-origin" })).json();
  if (!session.authenticated) return showLogin(session);
  vaultName = session.vault || vaultName;
  ttsProvider = session.tts || "browser";
  updateVoiceButton();
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  route();
  loadHistory();
}
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  const response = await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("#password").value, code: $("#code").value }) });
  if (response.ok) { $("#password").value = ""; $("#code").value = ""; boot(); }
  else $("#login-error").textContent = (await response.json()).error || "Sign-in failed";
});

/* ---------- navigation ---------- */
function route() {
  let view = (location.hash.replace("#", "").split("?")[0]) || "chat";
  if (view === "events") view = "plan";
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== `view-${view}`));
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  if (view === "status") loadStatus();
  if (view === "notifications") loadNotifications();
  if (view === "changes") loadChanges();
  if (view === "plan") loadPlan();
  if (view === "logs") loadLogs();
  if (view === "chat") {
    $("#prompt").focus();
    if (chatNeedsScroll) requestAnimationFrame(scrollChatToBottom);  // messages added while this tab was hidden
  }
}
/* A hidden tab can't scroll (it has no layout), so history loaded or messages added while another tab is open
   are scrolled to when the chat is next shown. */
let chatNeedsScroll = true;
function chatVisible() { return !$("#view-chat").classList.contains("hidden"); }
function scrollChatToBottom() {
  if (!chatVisible()) { chatNeedsScroll = true; return; }
  const messages = $("#messages");
  const last = messages.lastElementChild;
  if (last) last.scrollIntoView({ block: "end" });  // in case the page, not the list, is what scrolls
  messages.scrollTop = messages.scrollHeight;        // …and right to the end, including the padding
  chatNeedsScroll = false;
}
window.addEventListener("hashchange", route);

/* ---------- chat ---------- */
function addMessage(role, text = "", trace = "") {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="body">${role === "user" ? `<p>${esc(text)}</p>` : markdown(text)}</div>`;
  if (role === "assistant" && text) addSpeakButton(el, text);
  if (trace && role !== "user") addDetailsButton(el, trace);
  if (role === "user") { el.dataset.text = text; el.title = "Tap to edit and resend"; }
  $("#messages").appendChild(el);
  if (chatVisible()) el.scrollIntoView({ block: "end" }); else chatNeedsScroll = true;
  return el;
}
function addSpeakButton(el, text) {
  let foot = el.querySelector(".foot");
  if (!foot) { foot = document.createElement("div"); foot.className = "foot"; el.appendChild(foot); }
  const button = document.createElement("button");
  button.className = "speak"; button.type = "button"; button.textContent = "▶ Speak"; button.title = "Read this reply aloud";
  button.addEventListener("click", () => {
    if (speakingButton === button) { stopSpeaking(); return; }  // ■ Stop
    unlockAudio(); speak(text, button);
  });
  foot.prepend(button);
}
async function loadHistory() {
  if ($("#messages").childElementCount) return;
  const rows = await (await api("/api/chat/history")).json();
  rows.forEach((r) => {
    addMessage(r.role, r.content, r.trace);
    if (r.role === "activity") lastActivityId = Math.max(lastActivityId, r.id);
  });
  chatNeedsScroll = true;
  requestAnimationFrame(() => requestAnimationFrame(scrollChatToBottom));  // after the messages have laid out
  setInterval(pollActivity, 20000);
}
async function pollActivity() {
  if (document.hidden || $("#app").classList.contains("hidden")) return;
  try {
    const rows = await (await api(`/api/activity?after=${lastActivityId}`)).json();
    rows.forEach((r) => { addMessage("activity", r.content); lastActivityId = Math.max(lastActivityId, r.id); });
  } catch { /* offline */ }
}
/* ---------- recall earlier messages: ↑/↓ in the box, or tap one ---------- */
let recallIndex = -1, recallDraft = "";
function sentMessages() { return [...document.querySelectorAll("#messages .msg.user")].map((m) => m.dataset.text || "").filter(Boolean); }
function setPrompt(text) {
  const box = $("#prompt");
  box.value = text;
  box.dispatchEvent(new Event("input"));  // resize
  box.focus();
  box.setSelectionRange(text.length, text.length);
}
$("#prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); recallIndex = -1; $("#chat-form").requestSubmit(); return; }
  if ((event.key !== "ArrowUp" && event.key !== "ArrowDown") || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
  const box = event.target;
  const before = box.value.slice(0, box.selectionStart), after = box.value.slice(box.selectionEnd);
  // only when the caret is on the first line (↑) or last line (↓), so moving around a long draft still works
  if (event.key === "ArrowUp" && before.includes("\n")) return;
  if (event.key === "ArrowDown" && (after.includes("\n") || recallIndex < 0)) return;
  const sent = sentMessages();
  if (!sent.length) return;
  event.preventDefault();
  if (event.key === "ArrowUp") {
    if (recallIndex < 0) { recallDraft = box.value; recallIndex = sent.length; }
    recallIndex = Math.max(0, recallIndex - 1);
    setPrompt(sent[recallIndex]);
  } else {
    recallIndex += 1;
    if (recallIndex >= sent.length) { recallIndex = -1; setPrompt(recallDraft); } else setPrompt(sent[recallIndex]);
  }
});
$("#messages").addEventListener("click", (event) => {
  const message = event.target.closest(".msg.user");
  if (!message || !message.dataset.text || event.target.closest("a, button")) return;
  if (String(window.getSelection() || "").trim()) return;  // selecting text to copy, not editing
  recallIndex = -1;
  setPrompt(message.dataset.text);
});
$("#prompt").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px"; });
$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("#prompt").value.trim();
  if (!message) return;
  recallIndex = -1;
  $("#prompt").value = ""; $("#prompt").style.height = "auto";
  $("#send").disabled = true;
  stopSpeaking();
  unlockAudio();  // lets a "read me …" reply play on phones even with voice off
  addMessage("user", message);
  const bubble = addMessage("assistant", "");
  bubble.classList.add("pending");
  const body = bubble.querySelector(".body");
  let text = "", meta = null, sources = [], traceId = "";
  try {
    const response = await api("/api/chat", { method: "POST", body: JSON.stringify({ message }) });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl); buffer = buffer.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "meta") meta = ev;
        if (ev.type === "trace") traceId = ev.id;
        if (ev.type === "sources") sources = ev.items || [];
        if (ev.type === "proposals") (ev.items || []).forEach((p) => bubble.appendChild(proposalCard(p)));
        if (ev.type === "actions") (ev.items || []).forEach((a) => bubble.appendChild(actionCard(a)));
        if (ev.type === "clear") { text = ""; body.innerHTML = ""; }
        if (ev.type === "status" && !text) body.innerHTML = `<p class="status">${esc(ev.text)}</p>`;  // replaced by the answer
        if (ev.type === "token") { text += ev.text; body.innerHTML = markdown(text); bubble.scrollIntoView({ block: "end" }); }
      }
    }
  } catch (error) {
    text += `\n\n(${error.message})`;
    body.innerHTML = markdown(text);
  }
  bubble.classList.remove("pending");
  const foot = [];
  if (meta && meta.route && meta.route !== "chat") foot.push(`<span class="tag">${esc(meta.route)}</span>`);
  if (meta && meta.model === false) foot.push('<span class="tag warn">model offline</span>');
  sources.slice(0, 8).forEach((s) => foot.push(`<a class="src" href="${esc(s.url)}" ${(s.url || "").startsWith("http") ? 'target="_blank" rel="noopener"' : ""}>${esc(s.label)}</a>`));
  if (foot.length) bubble.insertAdjacentHTML("beforeend", `<div class="foot">${foot.join(" ")}</div>`);
  if (meta && meta.route === "web" && sources.length) {  // make [1], [2] citations clickable
    body.innerHTML = body.innerHTML.replace(/\[(\d{1,2})\]/g, (m, n) => {
      const source = sources.find((s) => s.label.startsWith(`[${n}]`));
      return source ? `<a class="cite" href="${esc(source.url)}" target="_blank" rel="noopener">[${n}]</a>` : m;
    });
  }
  if (text.trim()) addSpeakButton(bubble, text);
  if (traceId) addDetailsButton(bubble, traceId);
  $("#send").disabled = false;
  if ((voiceOn() || (meta && meta.speak)) && text.trim()) speak(text, bubble.querySelector("button.speak"));  // "read me …" speaks
});

/* ---------- voice ---------- */
const audio = new Audio();
const SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
let speechRun = 0;
function voiceOn() { return store.get("jarvis.voice", "off") === "on"; }
function updateVoiceButton() {
  const on = voiceOn();
  $("#voice-toggle").textContent = on ? "🔊 Voice on" : "🔈 Voice off";
  $("#voice-toggle").setAttribute("aria-pressed", String(on));
}
$("#stop-speech").addEventListener("click", stopSpeaking);
$("#voice-toggle").addEventListener("click", () => {
  store.set("jarvis.voice", voiceOn() ? "off" : "on");
  updateVoiceButton();
  if (voiceOn()) unlockAudio(); else stopSpeaking();
});
function unlockAudio() {  // mobile browsers only allow audio started from a tap
  audio.src = SILENCE;
  audio.play().catch(() => {});
}
function stopSpeaking() {
  speechRun += 1;
  audio.pause();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  setSpeaking(null);
}
/* one "Stop" control: a floating button while anything is speaking, and the reply's own ▶ Speak turns into ■ Stop */
let speakingButton = null;
function setSpeaking(button) {
  if (speakingButton && speakingButton !== button) speakingButton.textContent = "▶ Speak";
  speakingButton = button;
  if (button) button.textContent = "■ Stop";
  $("#stop-speech").classList.toggle("hidden", !button && !speakingNow());
}
function speakingNow() { return !audio.paused || ("speechSynthesis" in window && speechSynthesis.speaking); }
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && speakingNow()) stopSpeaking(); });
function plainText(text) {  // one paragraph → plain text
  return String(text)
    .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, "$2")
    .replace(/\[\[([^\]]+)\]\]/g, (_m, t) => t.split("/").pop())
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/\[\d{1,2}\]/g, "")                 // citation markers
    .replace(/[⁰¹²³⁴⁵⁶⁷⁸⁹]+/g, "")                // verse numbers
    .replace(/[*_`#>|]/g, "")
    .replace(/→/g, " to ").replace(/—/g, ", ")
    .replace(/\s+/g, " ").replace(/\s+([.,!?;:])/g, "$1").trim();
}
/* Paragraphs, headings and list items become separate parts with a pause between them. Each part ends with
   punctuation so the voice lets the sentence fall, and long paragraphs are split at sentence ends. */
function speechParts(text, size = 280) {
  const blocks = String(text).replace(/```[\s\S]*?```/g, "\n\n(code omitted)\n\n")
    .split(/\n\s*\n|\n(?=\s*(?:[-*+]|\d+[.)])\s)|\n(?=#{1,6}\s)|\n(?=\S)/);
  const parts = [];
  for (const block of blocks) {
    let line = plainText(block.replace(/^\s*(?:[-*+]|\d+[.)])\s+/, ""));
    if (!line) continue;
    if (!/[.!?:;…]$/.test(line)) line += ".";
    const sentences = line.match(/[^.!?]+[.!?]*\s*/g) || [line];
    let current = "";
    for (const sentence of sentences) {
      if ((current + sentence).length > size && current) { parts.push({ text: current.trim(), pause: 0 }); current = ""; }
      current += sentence;
    }
    if (current.trim()) parts.push({ text: current.trim(), pause: PARAGRAPH_PAUSE_MS });
  }
  return parts;
}
const PARAGRAPH_PAUSE_MS = 550;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function browserSpeak(parts, run) {
  if (!("speechSynthesis" in window)) return;
  const voices = speechSynthesis.getVoices();
  const voice = voices.find((v) => v.lang === "en-GB" && /male|daniel|arthur|george/i.test(v.name))
    || voices.find((v) => v.lang === "en-GB") || null;
  for (const part of parts) {
    if (run !== speechRun) return;
    await new Promise((resolve) => {
      const utterance = new SpeechSynthesisUtterance(part.text);
      utterance.voice = voice; utterance.lang = "en-GB";
      utterance.onend = resolve; utterance.onerror = resolve;
      speechSynthesis.speak(utterance);
    });
    if (part.pause) await wait(part.pause);
  }
}
async function speak(text, button = null) {
  stopSpeaking();
  const run = speechRun;
  const parts = speechParts(text);
  if (!parts.length) return;
  setSpeaking(button);
  $("#stop-speech").classList.remove("hidden");
  try {
    if (ttsProvider === "browser") { await browserSpeak(parts, run); return; }
    let next = fetchAudio(parts[0].text);
    for (let i = 0; i < parts.length; i++) {
      const url = await next;
      next = i + 1 < parts.length ? fetchAudio(parts[i + 1].text) : null;   // prefetch while playing
      if (run !== speechRun) return;
      if (!url) { await browserSpeak(parts.slice(i), run); return; }
      await new Promise((resolve) => {
        audio.onended = resolve; audio.onerror = resolve;
        audio.src = url;
        audio.play().catch(resolve);
      });
      URL.revokeObjectURL(url);
      if (run !== speechRun) return;
      if (parts[i].pause && i + 1 < parts.length) await wait(parts[i].pause);
    }
  } finally {
    if (run === speechRun) setSpeaking(null), $("#stop-speech").classList.add("hidden");
  }
}
async function fetchAudio(text) {
  try {
    const response = await api("/api/tts", { method: "POST", body: JSON.stringify({ text }) });
    if (!response.ok) return null;
    return URL.createObjectURL(await response.blob());
  } catch { return null; }
}

/* ---------- calendar proposals ---------- */
function addDays(isoDate, days) {
  const d = new Date(isoDate + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
function proposalCard(p) {
  if (p.kind === "note") return noteCard(p);
  const el = document.createElement("div");
  el.className = "proposal card";
  el.dataset.id = p.id;
  const source = p.email_subject
    ? `From email: ${p.gmail_url ? `<a href="${esc(p.gmail_url)}" target="_blank" rel="noopener">${esc(p.email_subject)}</a>` : esc(p.email_subject)}`
    : "From chat";
  const how = { ics: "calendar invite", jsonld: "booking details", llm: `read by AI · ${Math.round(p.confidence * 100)}% sure`, chat: "your message" }[p.source] || p.source;
  const endShown = p.all_day ? addDays(p.end.slice(0, 10), -1) : p.end.slice(0, 16);
  el.innerHTML = `
    <div class="p-title">${esc(p.title)}</div>
    <div>${esc(p.when)}${p.location ? " · " + esc(p.location) : ""}${p.calendar_name ? ` · <span class="tag">📅 ${esc(p.calendar_name)}</span>` : ""}</div>
    <div class="muted small">${source} · ${esc(how)}</div>
    ${p.notes ? `<div class="small">${esc(p.notes)}</div>` : ""}
    <div class="p-edit hidden">
      <label>Title <input name="title" value="${esc(p.title)}"></label>
      <label class="check"><input type="checkbox" name="all_day" ${p.all_day ? "checked" : ""}> All day</label>
      <label>Start <input name="start" type="${p.all_day ? "date" : "datetime-local"}" value="${esc(p.all_day ? p.start.slice(0, 10) : p.start.slice(0, 16))}"></label>
      <label>End <input name="end" type="${p.all_day ? "date" : "datetime-local"}" value="${esc(endShown)}"></label>
      <label>Location <input name="location" value="${esc(p.location)}"></label>
    </div>
    <div class="p-actions">
      ${p.status === "pending" ? `<button data-act="add">Add to calendar</button><button class="ghost" data-act="edit">Edit</button><button class="ghost" data-act="dismiss">Dismiss</button>${p.sender && p.source === "llm" ? `<button class="ghost" data-act="mute" title="${esc(p.sender)}">Not from this sender</button>` : ""}`
        : `<span class="tag">${esc(p.status)}</span>`}
    </div>
    <div class="p-status small"></div>`;
  el.querySelector('[name="all_day"]').addEventListener("change", (e) => {
    const allDay = e.target.checked;
    for (const name of ["start", "end"]) {
      const input = el.querySelector(`[name="${name}"]`);
      const value = input.value;
      input.type = allDay ? "date" : "datetime-local";
      input.value = allDay ? value.slice(0, 10) : (value.length === 10 ? value + (name === "start" ? "T09:00" : "T10:00") : value);
    }
  });
  return el;
}
function noteCard(p) {  // add a detail (e.g. a booking reference) to an event that's already in the calendar
  const el = document.createElement("div");
  el.className = "proposal card note";
  el.dataset.id = p.id;
  el.innerHTML = `
    <div class="p-title">📝 Add to “${esc(p.title)}”</div>
    <div class="muted small">${esc(p.when)}${p.location ? " · " + esc(p.location) : ""}</div>
    <div class="note-text">${esc(p.notes)}</div>
    <div class="p-edit hidden"></div>
    <div class="p-actions">
      ${p.status === "pending" ? `<button data-act="add">Add note</button><button class="ghost" data-act="dismiss">Dismiss</button>`
        : `<span class="tag">${esc(p.status === "added" ? "added" : p.status)}</span>`}
    </div>
    <div class="p-status small"></div>`;
  return el;
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest(".proposal [data-act]");
  if (!button) return;
  const card = button.closest(".proposal");
  const id = card.dataset.id;
  const status = card.querySelector(".p-status");
  if (button.dataset.act === "edit") { card.querySelector(".p-edit").classList.toggle("hidden"); return; }
  if (button.dataset.act === "dismiss" || button.dataset.act === "mute") {
    const mute = button.dataset.act === "mute";
    const result = await (await api(`/api/events/${id}/dismiss`, { method: "POST", body: JSON.stringify({ mute }) })).json();
    card.querySelector(".p-actions").innerHTML = '<span class="tag">dismissed</span>';
    if (result.muted) {
      status.textContent = `No more event suggestions from ${result.sender}. You can undo this at the bottom of the Plan tab.`;
      if (!$("#view-plan").classList.contains("hidden")) setTimeout(loadPlan, 2500);
    }
    return;
  }
  const edit = card.querySelector(".p-edit");
  let body = {};
  if (!edit.classList.contains("hidden")) {
    const val = (n) => edit.querySelector(`[name="${n}"]`);
    const allDay = val("all_day").checked;
    body = { title: val("title").value, all_day: allDay, location: val("location").value, start: val("start").value,
             end: allDay ? (val("end").value ? addDays(val("end").value, 1) : "") : val("end").value };
  }
  button.disabled = true;
  status.textContent = "Adding…";
  const response = await api(`/api/events/${id}/add`, { method: "POST", body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) { status.innerHTML = `<span class="error">${esc(result.error)}</span>`; button.disabled = false; return; }
  card.querySelector(".p-actions").innerHTML = `<span class="tag ok">added</span> ${result.html_link ? `<a href="${esc(result.html_link)}" target="_blank" rel="noopener">Open in Calendar</a>` : ""}`;
  status.textContent = result.when;
  card.querySelector(".p-edit").classList.add("hidden");
});
/* ---------- reminders & home actions ---------- */
function actionCard(a) {
  const el = document.createElement("div");
  el.className = `action card ${a.kind}`;
  el.dataset.id = a.id;
  const icon = a.kind === "ha" ? "🏠" : "⏰";
  const buttons = a.status === "proposed"
    ? `<button data-act="confirm">Confirm</button><button class="ghost" data-act="cancel">Cancel</button>`
    : a.status === "scheduled" ? `<button class="ghost" data-act="cancel">Cancel</button>` : `<span class="tag">${esc(a.status)}</span>`;
  el.innerHTML = `
    <div class="p-title">${icon} ${esc(a.text)}</div>
    <div class="muted small">${a.due ? esc(a.when) : "right away"}${a.status === "proposed" ? " · waiting for your OK" : ""}${a.result ? " · " + esc(a.result) : ""}</div>
    ${a.kind === "ha" && a.payload.entity_ids ? `<div class="muted small">${esc(a.payload.entity_ids.join(", "))}</div>` : ""}
    <div class="p-actions">${buttons}</div><div class="p-status small"></div>`;
  return el;
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest(".action [data-act]");
  if (!button) return;
  const card = button.closest(".action");
  const status = card.querySelector(".p-status");
  button.disabled = true;
  const act = button.dataset.act;
  const response = await api(`/api/scheduled/${card.dataset.id}/${act}`, { method: "POST" });
  const result = await response.json();
  if (!response.ok) { status.innerHTML = `<span class="error">${esc(result.error)}</span>`; button.disabled = false; return; }
  if (act === "cancel") { card.querySelector(".p-actions").innerHTML = '<span class="tag">cancelled</span>'; return; }
  card.replaceWith(actionCard(result));
});
$("#show-brief").addEventListener("click", async (event) => {
  event.target.disabled = true;
  const box = $("#brief");
  box.classList.remove("hidden");
  box.innerHTML = '<p class="muted">Putting your brief together…</p>';
  const data = await (await api("/api/brief", { method: "POST" })).json();
  box.innerHTML = markdown(data.text);
  addSpeakButton(box, data.text);
  event.target.disabled = false;
});
async function loadPlan() {
  const data = await (await api("/api/scheduled")).json();
  const proposals = $("#action-proposals"); proposals.innerHTML = "";
  data.open.filter((a) => a.status === "proposed").forEach((a) => proposals.appendChild(actionCard(a)));
  const list = $("#scheduled-list"); list.innerHTML = "";
  data.open.filter((a) => a.status === "scheduled").forEach((a) => list.appendChild(actionCard(a)));
  if (!list.childElementCount) list.innerHTML = '<p class="muted">Nothing scheduled. Try “remind me to call Mum tomorrow at 6pm” or “turn off the hall light at 11pm”.</p>';
  renderHistory(data.history || []);
  loadDeliveries();
  await loadEvents();
}
async function loadEvents() {
  const data = await (await api("/api/events?status=pending")).json();
  $("#events-warning").classList.toggle("hidden", data.can_add);
  $("#events-queue").textContent = data.queue ? `${data.queue} email(s) waiting to be read by the model.` : "";
  const list = $("#event-list");
  list.innerHTML = "";
  data.items.forEach((p) => list.appendChild(proposalCard(p)));
  if (!data.items.length && !$("#action-proposals").childElementCount) list.innerHTML = '<p class="muted">Nothing waiting. Events found in your email and home actions you ask for appear here.</p>';
  await loadMuted();
}
/* ---------- deliveries ---------- */
function deliveryCard(d) {
  const el = document.createElement("div");
  el.className = `card delivery ${d.status}`;
  el.dataset.id = d.id;
  const meta = [d.carrier, d.tracking_number, d.retailer && d.retailer !== d.name ? d.retailer : ""].filter(Boolean).join(" · ");
  const steps = (d.history || []).slice().reverse().map((h) =>
    `<li><span class="muted small">${esc(when(h.ts))}</span> ${esc(h.text)} <span class="muted small">(${esc(h.via)})</span></li>`).join("");
  el.innerHTML = `
    <div class="p-title">${d.icon} ${esc(d.name)} <span class="tag ${d.status === "delivered" ? "ok" : ["attempted", "delayed"].includes(d.status) ? "warn" : ""}">${esc(d.label)}</span></div>
    <div class="small">${esc(d.status_text)}</div>
    <div class="muted small">${d.expected_text && d.status !== "delivered" ? `Expected ${esc(d.expected_text)} · ` : ""}${esc(meta)}${d.checked_text ? ` · checked ${esc(d.checked_text)}` : ""}</div>
    ${d.poll_note ? `<div class="muted small">${esc(d.poll_note)}</div>` : ""}
    ${steps ? `<details><summary class="small">History</summary><ul class="small">${steps}</ul></details>` : ""}
    <div class="p-actions">
      ${d.tracking_url ? `<a class="button ghost" href="${esc(d.tracking_url)}" target="_blank" rel="noopener">Tracking page</a>` : ""}
      ${d.tracking_url && d.status !== "delivered" ? '<button class="ghost" data-dact="check">Check now</button>' : ""}
      <button class="ghost" data-dact="archive">${d.status === "delivered" ? "Done" : "Stop tracking"}</button>
    </div><div class="p-status small"></div>`;
  return el;
}
async function loadDeliveries() {
  const data = await (await api("/api/deliveries")).json();
  const list = $("#delivery-list");
  list.innerHTML = "";
  data.items.forEach((d) => list.appendChild(deliveryCard(d)));
  if (!data.items.length) list.innerHTML = '<p class="muted">No deliveries on the go. Dispatch emails are picked up automatically.</p>';
}
$("#delivery-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-dact]");
  if (!button) return;
  const card = button.closest(".delivery");
  button.disabled = true;
  if (button.dataset.dact === "archive") { await api(`/api/deliveries/${card.dataset.id}/archive`, { method: "POST" }); card.remove(); return; }
  card.querySelector(".p-status").textContent = "Checking…";
  const result = await (await api(`/api/deliveries/${card.dataset.id}/check`, { method: "POST" })).json();
  if (result.item) card.replaceWith(deliveryCard(result.item));
  else card.querySelector(".p-status").textContent = result.detail || "Couldn't check.";
});
$("#delivery-lookback").addEventListener("click", async (event) => {
  const button = event.target;
  button.disabled = true; button.textContent = "Looking…";
  const result = await (await api("/api/deliveries/look-back", { method: "POST", body: JSON.stringify({ days: 30 }) })).json();
  button.disabled = false; button.textContent = "Look back";
  if (result.error) { alert(result.error); return; }
  await loadDeliveries();
  alert(`Read ${result.about_parcels} delivery email(s) from the last 30 days — ${result.new} new parcel(s) found.`);
});
$("#delivery-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#delivery-input");
  if (!input.value.trim()) return;
  const response = await api("/api/deliveries", { method: "POST", body: JSON.stringify({ text: input.value }) });
  if (!response.ok) { alert((await response.json()).error); return; }
  input.value = "";
  loadDeliveries();
});

/* one timeline, newest first: reminders, home actions and calendar decisions together */
const HISTORY_TONE = { done: "ok", "added to calendar": "ok", failed: "bad" };
function renderHistory(items) {
  const box = $("#history");
  const shown = box.dataset.all === "1" ? items : items.slice(0, 15);
  box.innerHTML = shown.map((h) => `
    <div class="row history-item">
      <div><span class="muted small">${esc(h.at)}</span>
        <div>${h.icon} ${h.url ? `<a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.text)}</a>` : esc(h.text)}</div>
        ${h.detail ? `<div class="muted small">${esc(h.detail)}</div>` : ""}</div>
      <span class="tag ${HISTORY_TONE[h.status] || ""}">${esc(h.status)}</span>
    </div>`).join("") || '<p class="muted">Nothing yet. Reminders, home actions and calendar additions show up here.</p>';
  if (items.length > shown.length) {
    box.insertAdjacentHTML("beforeend", `<button class="ghost" id="history-more">Show all ${items.length}</button>`);
    $("#history-more").addEventListener("click", () => { box.dataset.all = "1"; renderHistory(items); });
  }
}
async function loadMuted() {
  const data = await (await api("/api/events/senders")).json();
  $("#muted-box").classList.toggle("hidden", !data.muted.length);
  $("#muted-senders").innerHTML = data.muted.map((m) =>
    `<div class="row"><div>${esc(m.sender)}<div class="muted small">${m.dismissed} dismissed</div></div>` +
    `<button class="ghost" data-unmute="${esc(m.sender)}">Unmute</button></div>`).join("");
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-unmute]");
  if (!button) return;
  button.disabled = true;
  await api("/api/events/senders/unmute", { method: "POST", body: JSON.stringify({ sender: button.dataset.unmute }) });
  loadMuted();
});
$("#scan-events").addEventListener("click", async (event) => {
  event.target.disabled = true;
  await api("/api/jobs/gmail/run", { method: "POST" });
  await api("/api/jobs/events/run", { method: "POST" });
  setTimeout(() => { event.target.disabled = false; loadPlan(); }, 4000);
});

/* ---------- status ---------- */
async function loadStatus() {
  const data = await (await api("/api/status")).json();
  const names = { model: "Model (PC)", obsidian: "Vault", google: "Google", ntfy: "Notifications", voice: "Voice", home: "Home Assistant", web: "Internet search" };
  const google = data.components.google || {};
  $("#google-connect").classList.toggle("hidden", !!google.ok);  // only needed until Google is connected
  $("#components").innerHTML = Object.entries(data.components).map(([key, c]) =>
    `<div class="card state ${c.ok ? "ok" : "bad"}${key === "google" && c.ok ? " clickable" : ""}" data-component="${esc(key)}"
      ${key === "google" && c.ok ? 'title="Tap to reconnect (if permissions changed)"' : ""}>
      <strong>${esc(names[key] || key)}</strong><span>${esc(c.detail)}${key === "google" && c.ok ? " · tap to reconnect" : ""}</span>
      ${key === "google" && c.ok ? `<div class="reconnect hidden"><p class="muted small">Reconnect if you've changed what
        Jarvis may access, or Google stopped working.</p><a class="button ghost" href="/auth/google/start">Reconnect Google</a></div>` : ""}
    </div>`).join("") +
    `<div class="card state"><strong>Knowledge</strong><span>${data.counts.people} people · ${data.counts.threads} threads · ${data.counts.events} events · ${data.vault_outbox} queued</span></div>` +
    `<div class="card state"><strong>Events from email</strong><span>${data.counts.event_proposals} waiting for you · ${data.counts.event_scan_queue} emails to read</span></div>`;
  $("#jobs").innerHTML = data.jobs.map((j) => `
    <div class="row">
      <div><strong>${esc(j.name)}</strong> <span class="muted">${esc(j.description)} · every ${Math.round(j.interval / 60) || 1} min</span>
        <div class="muted small ${j.ok === false ? "error" : ""}">${j.running ? "running…" : `${when(j.last_run)} — ${esc(j.result)}`}</div></div>
      <span class="row-actions">${j.trace ? `<a class="button ghost" href="#logs?trace=${esc(j.trace)}">Logs</a>` : ""}
      <button class="ghost" data-job="${esc(j.name)}">Run</button></span>
    </div>`).join("");
}
$("#calendar-box").addEventListener("toggle", async (event) => {
  if (!event.target.open) return;
  const list = $("#calendar-list");
  list.innerHTML = '<p class="muted small">Loading…</p>';
  const response = await api("/api/calendars");
  const data = await response.json();
  if (!response.ok) { list.innerHTML = `<p class="error small">${esc(data.error || "Couldn't load calendars")}</p>`; return; }
  list.innerHTML = data.calendars.map((c) => `
    <label class="row calendar-row"><span><input type="checkbox" value="${esc(c.primary ? "primary" : c.id)}" ${c.enabled ? "checked" : ""}>
      ${esc(c.name || c.id)}</span>
      <span>${c.primary ? '<span class="tag">main</span>' : ""}${c.hidden ? '<span class="tag">hidden</span>' : ""}${c.writable ? "" : '<span class="tag">read-only</span>'}</span></label>`).join("");
});
$("#calendar-save").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll("#calendar-list input:checked")].map((i) => i.value);
  const response = await api("/api/calendars", { method: "POST", body: JSON.stringify({ ids }) });
  $("#calendar-status").textContent = response.ok ? `Saved — ${ids.length} calendar(s). Reading them now.` : "Couldn't save.";
});
$("#components").addEventListener("click", (event) => {
  if (event.target.closest("a")) return;
  const card = event.target.closest('[data-component="google"].clickable');
  if (card) card.querySelector(".reconnect").classList.toggle("hidden");
});
$("#jobs").addEventListener("click", async (event) => {
  const name = event.target.dataset.job;
  if (!name) return;
  event.target.disabled = true;
  await api(`/api/jobs/${name}/run`, { method: "POST" });
  setTimeout(loadStatus, 2500);
});
$("#refresh-status").addEventListener("click", loadStatus);
$("#test-notify").addEventListener("click", async () => {
  const r = await (await api("/api/notifications/test", { method: "POST" })).json();
  alert(`Test notification: ${r.status}`);
});
$("#clear-chat").addEventListener("click", async () => {
  await api("/api/chat/clear", { method: "POST" });
  $("#messages").innerHTML = "";
});
$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); location.reload(); });

/* ---------- notifications & vault ---------- */
async function loadNotifyPrefs() {
  const data = await (await api("/api/notifications/settings")).json();
  $("#notify-prefs").innerHTML = `<p class="muted small">Quiet hours: ${esc(data.quiet_hours || "none")}. Held
    notifications arrive together afterwards.</p>` + Object.entries(data.categories).map(([key, c]) => `
    <div class="row notify-pref" data-key="${esc(key)}">
      <label class="calendar-row"><span><input type="checkbox" data-field="on" ${c.on ? "checked" : ""}> ${esc(c.label)}</span></label>
      <label class="small muted"><input type="checkbox" data-field="loud" ${c.quiet ? "" : "checked"} ${c.on ? "" : "disabled"}>
        send in quiet hours</label>
    </div>`).join("");
}
$("#notify-prefs").addEventListener("change", async (event) => {
  const row = event.target.closest(".notify-pref");
  if (!row) return;
  const on = row.querySelector('[data-field="on"]').checked;
  const loud = row.querySelector('[data-field="loud"]');
  loud.disabled = !on;
  await api("/api/notifications/settings", { method: "POST",
    body: JSON.stringify({ [row.dataset.key]: { on, quiet: !loud.checked } }) });
  $("#notify-saved").textContent = "Saved.";
});
$("#notify-settings").addEventListener("toggle", (event) => { if (event.target.open) loadNotifyPrefs(); });
async function loadNotifications() {
  const rows = await (await api("/api/notifications")).json();
  $("#notification-list").innerHTML = rows.map((n) => `
    <div class="row"><div><strong>${esc(n.title)}</strong> <span class="tag">${esc(n.status)}</span>
      <div class="small">${markdown(n.message)}</div><div class="muted small">${when(n.ts)}${n.error ? " · " + esc(n.error) : ""}</div></div></div>`).join("")
    || '<p class="muted">Nothing yet.</p>';
}
async function loadChanges() {
  const data = await (await api("/api/vault/changes")).json();
  $("#outbox").textContent = data.pending ? `${data.pending} note(s) waiting to be written (Obsidian offline or busy).` : "All notes written.";
  $("#change-list").innerHTML = data.changes.map((c) => `
    <div class="row"><div><a href="${esc(c.url)}">${esc(c.path)}</a>
      <div class="muted small">${when(c.ts)} · ${esc(c.actor)}${c.created ? " · created" : ""}</div></div>
      <button class="ghost" data-revert="${c.id}">Revert</button></div>`).join("") || '<p class="muted">No changes yet.</p>';
}
$("#change-list").addEventListener("click", async (event) => {
  const id = event.target.dataset.revert;
  if (!id || !confirm("Restore this note to how it was before this change?")) return;
  const r = await api(`/api/vault/revert/${id}`, { method: "POST" });
  if (!r.ok) alert((await r.json()).error);
  loadChanges();
});

/* ---------- diagnostics ---------- */
const LEVEL_ICON = { debug: "·", info: "i", warning: "!", error: "✗" };
const STATUS_ICON = { ok: "✓", warning: "!", error: "✗", running: "…" };
let logMode = "traces";
let logBefore = null;
let verboseUntil = 0;

function fmtTime(ts) { return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
function fmtMs(ms) { return ms == null ? "" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`; }
function pretty(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }

function entryHtml(e, traceStart) {
  const offset = traceStart ? `+${fmtMs((e.ts - traceStart) * 1000)}` : fmtTime(e.ts);
  const hasBody = (e.data && Object.keys(e.data).length) || e.error || (!traceStart && e.trace);
  const summary = `<span class="lvl">${LEVEL_ICON[e.level] || ""}</span><span class="when">${esc(offset)}</span>
    <span class="src">${esc(e.source)}</span><span class="msg">${esc(e.message)}</span>${e.duration_ms != null ? `<span class="dur">${fmtMs(e.duration_ms)}</span>` : ""}`;
  if (!hasBody) return `<div class="log-entry lvl-${e.level}"><div class="log-summary">${summary}</div></div>`;
  return `<details class="log-entry lvl-${e.level}"><summary class="log-summary">${summary}</summary>
    <div class="log-body">
      ${!traceStart && e.trace ? `<p class="small"><a href="#logs?trace=${esc(e.trace)}">Show everything from this ${esc(e.source)} run</a></p>` : ""}
      ${e.data && Object.keys(e.data).length ? `<pre>${esc(pretty(e.data))}</pre>` : ""}
      ${e.error ? `<pre class="error">${esc(e.error)}</pre>` : ""}
    </div></details>`;
}
async function traceEntries(id) {
  const data = await (await api(`/api/diag/logs?trace=${encodeURIComponent(id)}&limit=1000`)).json();
  const start = data.trace ? data.trace.ts : (data.items[0] ? data.items[0].ts : 0);
  if (!data.items.length) return '<p class="muted small">Nothing was logged for this run. Turn on <strong>Verbose</strong> in Logs to record more detail next time.</p>';
  return data.items.map((e) => entryHtml(e, start)).join("");
}
function traceHtml(t, open = false) {
  const kind = { chat: "chat", job: "job", request: "action", client: "browser" }[t.kind] || t.kind;
  return `<details class="trace st-${t.status}" data-trace="${esc(t.id)}" ${open ? "open" : ""}>
    <summary class="log-summary"><span class="lvl">${STATUS_ICON[t.status] || ""}</span><span class="when">${esc(fmtTime(t.ts))}</span>
      <span class="src">${esc(kind)}</span><span class="msg">${esc(t.name)}</span>
      ${t.issues ? `<span class="tag warn">${t.issues} issue${t.issues > 1 ? "s" : ""}</span>` : ""}<span class="dur">${fmtMs(t.duration_ms)}</span></summary>
    <div class="log-body trace-body"><p class="muted small">Loading…</p></div></details>`;
}
document.addEventListener("toggle", async (event) => {
  const el = event.target;
  if (!(el instanceof HTMLDetailsElement) || !el.open || !el.dataset.trace || el.dataset.loaded) return;
  el.dataset.loaded = "1";
  el.querySelector(".trace-body").innerHTML = await traceEntries(el.dataset.trace)
    + `<p class="small"><button class="ghost small-button" data-export-trace="${esc(el.dataset.trace)}">Download this run</button></p>`;
}, true);

function addDetailsButton(el, traceId) {
  let foot = el.querySelector(":scope > .foot");
  if (!foot) { foot = document.createElement("div"); foot.className = "foot"; el.appendChild(foot); }
  const button = document.createElement("button");
  button.className = "speak"; button.type = "button"; button.textContent = "🔍 Details";
  button.title = "What Jarvis did for this reply";
  button.addEventListener("click", async () => {
    let panel = el.querySelector(":scope > .details-panel");
    if (panel) { panel.remove(); return; }
    panel = document.createElement("div");
    panel.className = "details-panel card";
    panel.innerHTML = '<p class="muted small">Loading…</p>';
    el.appendChild(panel);
    panel.innerHTML = await traceEntries(traceId) + `<p class="small"><a href="#logs?trace=${esc(traceId)}">Open in Logs</a></p>`;
  });
  foot.appendChild(button);
}

function setLogMode(mode) {
  logMode = mode;
  document.querySelectorAll("[data-logmode]").forEach((b) => b.classList.toggle("active", b.dataset.logmode === mode));
  const entries = mode === "entries";
  ["#log-level", "#log-source", "#log-search"].forEach((s) => $(s).classList.toggle("hidden", !entries));
  ["#trace-kind", "#problems-wrap"].forEach((s) => $(s).classList.toggle("hidden", entries));
}
document.querySelectorAll("[data-logmode]").forEach((b) => b.addEventListener("click", () => {
  setLogMode(b.dataset.logmode);
  if (location.hash.includes("trace=")) location.hash = "#logs"; else loadLogs();
}));
["#trace-kind", "#trace-problems", "#log-level", "#log-source"].forEach((s) => $(s).addEventListener("change", () => loadLogs()));
let searchTimer = null;
$("#log-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadLogs(), 350); });

async function loadLogMeta() {
  const meta = await (await api("/api/diag/meta")).json();
  verboseUntil = meta.verbose_until;
  const c = meta.last_24h;
  $("#log-meta").textContent = `Last 24 h: ${c.error || 0} error(s), ${c.warning || 0} warning(s), ${c.info || 0} info. Kept for ${meta.retention_days} days.`
    + (verboseUntil * 1000 > Date.now() ? " Verbose logging is on — it records message contents, so switch it off when done." : "");
  $("#verbose-toggle").textContent = verboseUntil * 1000 > Date.now()
    ? `Verbose until ${new Date(verboseUntil * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Verbose: off";
  $("#verbose-toggle").classList.toggle("on", verboseUntil * 1000 > Date.now());
  const select = $("#log-source");
  const current = select.value;
  select.innerHTML = '<option value="">All sources</option>' + meta.sources.map((s) => `<option>${esc(s)}</option>`).join("");
  select.value = current;
}
async function loadLogs(more = false) {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const list = $("#log-list");
  loadLogMeta();
  if (params.get("trace")) {
    const id = params.get("trace");
    const data = await (await api(`/api/diag/logs?trace=${encodeURIComponent(id)}&limit=1`)).json();
    const t = data.trace || { id, ts: Date.now() / 1000, kind: "run", name: id, status: "ok", issues: 0 };
    list.innerHTML = `<p class="small"><a href="#logs">← All activity</a></p>` + traceHtml(t, false);
    list.querySelector("details").open = true;
    $("#log-more").classList.add("hidden");
    return;
  }
  if (logMode === "traces") {
    const q = new URLSearchParams({ kind: $("#trace-kind").value, problems: $("#trace-problems").checked ? "1" : "" });
    const traces = await (await api(`/api/diag/traces?${q}`)).json();
    list.innerHTML = traces.map((t) => traceHtml(t)).join("") || '<p class="muted">Nothing recorded yet.</p>';
    $("#log-more").classList.add("hidden");
    return;
  }
  if (!more) logBefore = null;
  const q = new URLSearchParams({ level: $("#log-level").value, source: $("#log-source").value, q: $("#log-search").value });
  if (logBefore) q.set("before", logBefore);
  const data = await (await api(`/api/diag/logs?${q}`)).json();
  const html = data.items.map((e) => entryHtml(e)).join("");
  list.innerHTML = more ? list.innerHTML + html : (html || '<p class="muted">No matching entries.</p>');
  logBefore = data.next_before;
  $("#log-more").classList.toggle("hidden", !logBefore);
}
$("#log-more").addEventListener("click", () => loadLogs(true));
$("#verbose-toggle").addEventListener("click", async () => {
  const on = verboseUntil * 1000 > Date.now();
  await api("/api/diag/verbose", { method: "POST", body: JSON.stringify({ minutes: on ? 0 : 60 }) });
  loadLogMeta();
});
async function download(url) {
  const response = await api(url);
  const blob = await response.blob();
  const name = (response.headers.get("content-disposition") || "").match(/filename="([^"]+)"/)?.[1] || "jarvis-logs.json";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
$("#export-logs").addEventListener("click", () => download("/api/diag/export?hours=24"));
document.addEventListener("click", (event) => {
  const id = event.target.dataset && event.target.dataset.exportTrace;
  if (id) download(`/api/diag/export?trace=${encodeURIComponent(id)}`);
});

/* report browser-side errors so they show up in Logs */
let reported = 0;
function reportClientError(message, stack) {
  if (reported++ > 5 || $("#app").classList.contains("hidden")) return;
  fetch("/api/diag/client", { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Jarvis": "1" },
    body: JSON.stringify({ message: String(message).slice(0, 500), stack: String(stack || "").slice(0, 4000), url: location.href }) }).catch(() => {});
}
window.addEventListener("error", (e) => reportClientError(e.message, e.error && e.error.stack));
window.addEventListener("unhandledrejection", (e) => { if (String(e.reason && e.reason.message) !== "signed out") reportClientError(e.reason && e.reason.message || e.reason, e.reason && e.reason.stack); });

boot();
