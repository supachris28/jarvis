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

/* Where vault notes open is a setting on each device (Status → This device): the Obsidian app, or Jarvis's own
   reader. "Auto" picks Obsidian on computers and Jarvis on phones and tablets, which have no Obsidian link handler. */
function noteSetting() { return store.get("jarvis.notes", "auto"); }
function isPhone() {
  return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent) || window.matchMedia("(pointer: coarse)").matches;
}
function notesInJarvis() {
  const setting = noteSetting();
  return setting === "jarvis" || (setting === "auto" && isPhone());
}
/* [[Sources/Email/…]] notes open in Jarvis's email reader everywhere; other notes follow the device setting */
function wikiHref(target) {
  if (/^Sources\/Email\//i.test(target)) return `#email?note=${encodeURIComponent(target)}`;
  if (notesInJarvis()) return `#note?path=${encodeURIComponent(target)}`;
  return `obsidian://open?vault=${encodeURIComponent(vaultName)}&file=${encodeURIComponent(target)}`;
}
/* Links made by the server (sources under answers, Vault changes, history) are obsidian:// links: on a device set
   to Jarvis they open in the reader instead. */
document.addEventListener("click", (event) => {
  const link = event.target.closest && event.target.closest('a[href^="obsidian://open"]');
  if (!link || !notesInJarvis()) return;
  const file = new URL(link.getAttribute("href")).searchParams.get("file");
  if (!file) return;
  event.preventDefault();
  location.hash = /^Sources\/Email\//i.test(file) ? `#email?note=${encodeURIComponent(file)}`
                                                   : `#note?path=${encodeURIComponent(file)}`;
}, true);

/* ---------- tiny, safe markdown ---------- */
function inline(text) {
  let s = esc(text);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_m, target, alias) => {
    const href = wikiHref(target.replace(/&amp;/g, "&"));
    return `<a href="${href}">${alias || target.split("/").pop()}</a>`;
  });
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}
/* Full Markdown (tables, code blocks, nested and numbered lists, italics) with marked + DOMPurify, which the Docker
   build vendors into /static/vendor. Without them (e.g. running from source) the small renderer below is used. */
let richReady = false;
function richMarkdown(text) {
  if (!richReady) {
    marked.use({ gfm: true, breaks: true, extensions: [{
      name: "wikilink", level: "inline",
      start(src) { const i = src.indexOf("[["); return i < 0 ? undefined : i; },
      tokenizer(src) {
        const m = /^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/.exec(src);
        return m ? { type: "wikilink", raw: m[0], target: m[1], alias: m[2] } : undefined;
      },
      renderer(token) {
        const href = wikiHref(token.target);
        return `<a href="${esc(href)}">${esc(token.alias || token.target.split("/").pop())}</a>`;
      },
    }] });
    DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A" && /^https?:/i.test(node.getAttribute("href") || "")) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener");
      }
    });
    richReady = true;
  }
  return DOMPurify.sanitize(marked.parse(String(text)), { ALLOWED_URI_REGEXP: /^(?:(?:https?|obsidian|mailto):|\/?#)/i });
}
function markdown(text) {
  if (window.marked && window.DOMPurify) {
    try { return richMarkdown(text); } catch { /* fall back to the simple renderer */ }
  }
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
/* ---------- shared to Jarvis (Android share menu) and home-screen shortcuts ----------
   The manifest's share_target opens /?share_text=…&share_url=…; shortcuts open /?ask=… . They're read once (kept
   through signing in) and removed from the address bar. */
const incoming = (() => {
  const q = new URLSearchParams(location.search);
  const found = { text: q.get("share_text") || "", url: q.get("share_url") || "", title: q.get("share_title") || "",
                  ask: q.get("ask") || "" };
  if (found.text || found.url || found.title || found.ask) {
    try { sessionStorage.setItem("jarvis.incoming", JSON.stringify(found)); } catch { /* private mode */ }
    history.replaceState(null, "", "/" + (found.ask || found.text || found.url ? "#chat" : location.hash));
    return found;
  }
  try { return JSON.parse(sessionStorage.getItem("jarvis.incoming") || "null"); } catch { return null; }
})();
function takeIncoming() {
  try { sessionStorage.removeItem("jarvis.incoming"); } catch { /* ignore */ }
  return incoming;
}
function sendPrompt(text) { setPrompt(text); $("#chat-form").requestSubmit(); }
function sharedText(item) {
  /* apps put the link in text, url or both — and often the title again at the start of the text */
  let text = (item.text || "").trim();
  if (item.title && !text.startsWith(item.title)) text = `${item.title}\n${text}`.trim();
  if (item.url && !text.includes(item.url)) text = `${text}\n${item.url}`.trim();
  return text.slice(0, 3000);
}
function showShared(item) {
  const text = sharedText(item);
  if (!text) return;
  const tracking = /https?:\/\/\S*(track|parcel|deliver|royalmail|evri|dpd|ups|fedex|amazon\.[a-z.]+\/(gp\/)?(your-?orders|progress-tracker))/i.test(text)
    || /\b[A-Z0-9]*\d[A-Z0-9]{9,}\b/.test(text);
  const card = document.createElement("div");
  card.className = "msg activity shared";
  card.innerHTML = `<div class="body"><p class="muted small">Shared with Jarvis</p><p class="shared-text"></p>
    <div class="shared-actions">
      <button type="button" data-share="calendar">📅 Add to calendar</button>
      <button type="button" data-share="remember" class="ghost">📝 Remember</button>
      ${tracking ? '<button type="button" data-share="track" class="ghost">📦 Track parcel</button>' : ""}
      <button type="button" data-share="ask" class="ghost">💬 Ask about it</button>
      <button type="button" data-share="dismiss" class="ghost">✕</button>
    </div></div>`;
  card.querySelector(".shared-text").textContent = text.length > 400 ? text.slice(0, 400) + "…" : text;
  card.addEventListener("click", (event) => {
    const action = event.target.closest("[data-share]")?.dataset.share;
    if (!action) return;
    card.remove();
    if (action === "calendar") sendPrompt(`Add to my calendar: ${text}`);
    if (action === "remember") sendPrompt(`Remember that ${text}`);
    if (action === "track") sendPrompt(`Track ${text}`);
    if (action === "ask") { setPrompt(`\n\n"""${text}"""`); $("#prompt").setSelectionRange(0, 0); }
  });
  $("#messages").appendChild(card);
  scrollChatToBottom();
}

/* the lights show one amber/red segment per part of Jarvis that is down */
let lightsMounted = false;
const STATUS_NAMES = { model: "model (PC)", obsidian: "vault", google: "Google", ntfy: "notifications", voice: "voice",
                       home: "Home Assistant", web: "internet search" };
function healthProblems(data) {
  const down = Object.entries(data.components || {})
    .filter(([, c]) => !c.ok && !/not configured|disabled|switched off|browser voice/i.test(c.detail || ""))
    .map(([key]) => STATUS_NAMES[key] || key);
  const jobs = (data.jobs || []).filter((j) => j.ok === false).map((j) => `${j.name} job`);
  return [...down, ...jobs];
}
async function checkHealth() {
  try {
    const response = await api("/api/status");
    if (!response.ok) return;
    Lights.setProblems(healthProblems(await response.json()));
    if (Lights.state === "offline") Lights.set("idle");
  } catch (error) {
    if (error.message !== "signed out") Lights.set("offline");
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
  if (!lightsMounted) { Lights.mount($("#visor")); lightsMounted = true; checkHealth(); setInterval(checkHealth, 600000); }
  route();
  await loadHistory();
  const item = takeIncoming();
  if (item && item.ask) sendPrompt(item.ask);
  else if (item) showShared(item);
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
  if (view === "email") {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    loadEmail(params.get("thread"), params.get("note"));
  }
  if (view === "note") loadNote(new URLSearchParams(location.hash.split("?")[1] || "").get("path"));
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
  if (role === "assistant" && text) addReportButton(el, trace, text);
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
$("#prompt").addEventListener("keydown", (e) => { if (e.key.length === 1 || e.key === "Backspace") Lights.ripple(); });
$("#prompt").addEventListener("blur", () => { if (Lights.state === "listening") Lights.set("idle"); });
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
  Lights.set("thinking");
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
        if (ev.type === "meta") {
          meta = ev;
          if (ev.route && !["chat", "blocked", "remember", "calendar-add"].includes(ev.route)) Lights.set("looking", ev.route);
        }
        if (ev.type === "trace") traceId = ev.id;
        if (ev.type === "sources") sources = ev.items || [];
        if (ev.type === "proposals") (ev.items || []).forEach((p) => bubble.appendChild(proposalCard(p)));
        if (ev.type === "actions") (ev.items || []).forEach((a) => bubble.appendChild(actionCard(a)));
        if (ev.type === "clear") { text = ""; body.innerHTML = ""; }
        if (ev.type === "status" && !text) body.innerHTML = `<p class="status">${esc(ev.text)}</p>`;  // replaced by the answer
        if (ev.type === "token") { Lights.token(); text += ev.text; body.innerHTML = markdown(text); bubble.scrollIntoView({ block: "end" }); }
      }
    }
    Lights.flash("done");
  } catch (error) {
    text += `\n\n(${error.message})`;
    body.innerHTML = markdown(text);
    Lights.flash("error");
  }
  bubble.classList.remove("pending");
  const foot = [];
  if (meta && meta.route && meta.route !== "chat") foot.push(`<span class="tag">${esc(meta.route)}</span>`);
  if (meta && meta.model === false) foot.push('<span class="tag warn">model offline</span>');
  sources.slice(0, 8).forEach((s) => foot.push(`<a class="src" href="${esc(s.url)}" ${(s.url || "").startsWith("http") ? 'target="_blank" rel="noopener"' : ""}>${esc(s.label)}</a>`));
  if (foot.length) bubble.insertAdjacentHTML("beforeend", `<div class="foot">${foot.join(" ")}</div>`);
  if (meta && meta.route === "web" && sources.length) linkCitations(body, sources);
  if (text.trim()) addSpeakButton(bubble, text);
  if (traceId) addDetailsButton(bubble, traceId);
  if (text.trim()) addReportButton(bubble, traceId, text, meta && meta.route);
  $("#send").disabled = false;
  if ((voiceOn() || (meta && meta.speak)) && text.trim()) speak(text, bubble.querySelector("button.speak"));  // "read me …" speaks
});

/* [1], [2] in a web answer → links to its sources. Only text nodes are touched, never attributes or code. */
function linkCitations(root, sources) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (/\[\d{1,2}\]/.test(node.nodeValue) && !node.parentElement.closest("a, code, pre")) nodes.push(node);
  }
  for (const node of nodes) {
    const parts = document.createDocumentFragment();
    let last = 0;
    node.nodeValue.replace(/\[(\d{1,2})\]/g, (match, n, offset) => {
      const source = sources.find((s) => s.label.startsWith(`[${n}]`));
      if (!source || !/^https?:/i.test(source.url || "")) return match;
      parts.append(node.nodeValue.slice(last, offset));
      const link = document.createElement("a");
      Object.assign(link, { className: "cite", href: source.url, target: "_blank", rel: "noopener", textContent: match });
      parts.append(link);
      last = offset + match.length;
      return match;
    });
    if (!last) continue;
    parts.append(node.nodeValue.slice(last));
    node.replaceWith(parts);
  }
}

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
  if (!button && !speakingNow() && Lights.state === "speaking") Lights.set("idle");
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
      utterance.onboundary = () => Lights.bump(1.1, Math.floor(Math.random() * 3) + 5);
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
  Lights.set("speaking");
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
    if (run === speechRun && Lights.state === "speaking") Lights.set("idle");
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
    ? `From email: ${p.email_url ? `<a href="${esc(p.email_url)}">${esc(p.email_subject)}</a>` : esc(p.email_subject)}`
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
      ${p.status === "pending" ? `<label class="cal-pick hidden" title="Which calendar">📅 <select name="calendar_id"></select></label><button data-act="add">Add to calendar</button><button class="ghost" data-act="edit">Edit</button><button class="ghost" data-act="dismiss">Dismiss</button>${p.sender && p.source === "llm" ? `<button class="ghost" data-act="mute" title="${esc(p.sender)}">Not from this sender</button>` : ""}`
        : `<span class="tag">${esc(p.status)}</span>`}
    </div>
    <div class="p-status small"></div>`;
  if (p.status === "pending") fillCalendarPicker(el.querySelector('[name="calendar_id"]'), p.calendar_id);
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
/* which of your calendars (ticked under Status → Calendars) an event goes into */
let calendarTargets = null;
function getCalendarTargets() {
  if (!calendarTargets) {
    calendarTargets = api("/api/calendars/targets").then((r) => r.json()).then((d) => d.calendars || [])
      .catch(() => { calendarTargets = null; return []; });
  }
  return calendarTargets;
}
async function fillCalendarPicker(select, current) {
  if (!select) return;
  const targets = await getCalendarTargets();
  if (targets.length < 2) return;  // only one place it can go: nothing to choose
  const chosen = targets.some((c) => c.id === current) ? current : (targets.find((c) => c.primary) || targets[0]).id;
  select.innerHTML = targets.map((c) => `<option value="${esc(c.id)}" ${c.id === chosen ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  select.closest(".cal-pick").classList.remove("hidden");
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
  const picker = card.querySelector('[name="calendar_id"]');
  if (picker && picker.value) body.calendar_id = picker.value;
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
  loadDeadlines();
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
/* ---------- renewals, deadlines and replies you're waiting for ---------- */
async function loadDeadlines() {
  const data = await (await api("/api/deadlines")).json();
  $("#deadline-list").innerHTML = data.deadlines.map((d) => `
    <div class="card deadline ${d.days <= 3 ? "soon" : ""}" data-deadline="${d.id}">
      <div><strong>${esc(d.icon)} ${esc(d.label)} ${esc(d.when)}</strong> <span class="muted">(${esc(d.away)})</span></div>
      <div>${esc(d.title)} <span class="muted">· ${esc(d.org)}</span></div>
      ${d.evidence ? `<div class="muted small">“${esc(d.evidence)}”</div>` : ""}
      <div class="row-actions">
        ${d.email_url ? `<a class="button ghost" href="${esc(d.email_url)}">Email</a>` : ""}
        <button class="ghost" data-deadline-status="done">Done</button>
        <button class="ghost" data-deadline-status="dismissed" title="Not a real deadline">Not this</button>
      </div></div>`).join("")
    || `<p class="muted">Nothing coming up. ${data.looked_back ? "" : "Jarvis reads the last year of email for renewals the first time Google is connected."}</p>`;
  $("#waiting-list").innerHTML = data.waiting.map((w) => `
    <div class="card deadline" data-thread="${esc(w.thread_id)}">
      <div><strong>${esc(w.to)}</strong> — ${esc(w.subject)}</div>
      <div class="muted small">You asked ${esc(w.sent)} · ${w.days} days ago, no reply yet</div>
      <div class="row-actions"><a class="button ghost" href="${esc(w.email_url)}">Email</a>
        <button class="ghost" data-waiting-dismiss title="Stop listing this one">Not waiting</button></div>
    </div>`).join("")
    || `<p class="muted">No unanswered questions in emails you sent (after ${data.followup_days} days).</p>`;
}
$("#deadline-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-deadline-status]");
  if (!button) return;
  const id = button.closest("[data-deadline]").dataset.deadline;
  await api(`/api/deadlines/${id}`, { method: "POST", body: JSON.stringify({ status: button.dataset.deadlineStatus }) });
  loadDeadlines();
});
$("#waiting-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-waiting-dismiss]");
  if (!button) return;
  await api(`/api/followups/${button.closest("[data-thread]").dataset.thread}/dismiss`, { method: "POST" });
  loadDeadlines();
});

/* ---------- reading an email inside Jarvis (Gmail web links can't open one email on Android) ---------- */
async function loadEmail(threadId, notePath) {
  $("#email-subject").textContent = "Loading…";
  $("#email-messages").innerHTML = "";
  if (!threadId && !notePath) return;
  const response = await api(threadId ? `/api/email/${encodeURIComponent(threadId)}`
                                      : `/api/email/note?path=${encodeURIComponent(notePath)}`);
  const data = await response.json();
  if (!response.ok) { $("#email-subject").textContent = data.error || "Couldn't load that email."; return; }
  $("#email-subject").textContent = data.subject || "(no subject)";
  $("#email-gmail").href = data.gmail_url;
  $("#email-messages").innerHTML = data.messages.map((m) => `
    <div class="card email-message">
      <div><strong>${esc(m.from)}</strong></div>
      <div class="muted small">${esc(m.when)}${m.to ? " · to " + esc(m.to) : ""}</div>
      <div class="email-body">${esc(m.body).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')}</div>
      ${m.attachments && m.attachments.length ? `<div class="muted small">📎 ${m.attachments.map(esc).join(", ")}</div>` : ""}
    </div>`).join("");
}
$("#email-back").addEventListener("click", () => { if (window.history.length > 1) window.history.back(); else location.hash = "#chat"; });

/* ---------- reading a vault note inside Jarvis ---------- */
function propertyValue(value) {
  if (Array.isArray(value)) return value.map(propertyValue).join(", ");
  if (value && typeof value === "object") return esc(JSON.stringify(value));
  return markdown(String(value ?? "")).replace(/^<p>([\s\S]*)<\/p>\s*$/, "$1");  // [[links]] in properties work too
}
async function loadNote(path) {
  $("#note-title").textContent = "Loading…";
  $("#note-path").textContent = "";
  $("#note-properties").innerHTML = "";
  $("#note-body").innerHTML = "";
  if (!path) return;
  const response = await api(`/api/vault/note?path=${encodeURIComponent(path)}`);
  const data = await response.json();
  if (!response.ok) { $("#note-title").textContent = data.error || "Couldn't open that note."; return; }
  $("#note-title").textContent = data.title;
  $("#note-path").textContent = data.path;
  $("#note-obsidian").href = data.obsidian_url;
  const props = Object.entries(data.properties || {}).filter(([, v]) => v !== null && v !== "");
  $("#note-properties").innerHTML = props.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${propertyValue(v)}</dd>`).join("");
  $("#note-properties").classList.toggle("hidden", !props.length);
  $("#note-body").innerHTML = markdown(data.body || "");
}
$("#note-back").addEventListener("click", () => { if (window.history.length > 1) window.history.back(); else location.hash = "#chat"; });

/* ---------- this device's settings (kept in this browser only) ---------- */
function showDeviceSettings() {
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  $("#install-mode").textContent = standalone
    ? "✅ Opened as an installed app."
    : "ℹ️ Opened in a browser tab — open Jarvis from its home-screen icon to check the installed app.";
  const select = $("#note-open");
  select.value = noteSetting();
  $("#note-open-hint").textContent = select.value === "auto"
    ? `This device looks like ${isPhone() ? "a phone or tablet, so notes open in Jarvis" : "a computer, so notes open in Obsidian"}.`
    : "";
}
$("#share-test").addEventListener("click", () => {
  location.href = "/?share_text=" + encodeURIComponent("Bowling Saturday 6pm at Hollywood Bowl — test share");
});
$("#note-open").addEventListener("change", (event) => { store.set("jarvis.notes", event.target.value); showDeviceSettings(); });

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
    ${d.status === "delivered" && d.delivered_text ? `<div class="small"><strong>Delivered ${esc(d.delivered_text)}</strong></div>` : ""}
    <div class="muted small">${d.expected_text && d.status !== "delivered" ? `Expected ${esc(d.expected_text)} · ` : ""}${esc(meta)}${d.checked_text ? ` · checked ${esc(d.checked_text)}` : ""}</div>
    ${d.poll_note ? `<div class="muted small">${esc(d.poll_note)}</div>` : ""}
    ${steps ? `<details><summary class="small">History</summary><ul class="small">${steps}</ul></details>` : ""}
    <div class="p-actions">
      ${d.tracking_url ? `<a class="button ghost" href="${esc(d.tracking_url)}" target="_blank" rel="noopener">Tracking page</a>` : ""}
      ${d.thread_id ? `<a class="button ghost" href="#email?thread=${esc(d.thread_id)}">Email</a>` : ""}
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
        <div>${h.icon} ${h.url ? `<a href="${esc(h.url)}"${/^\/?#/.test(h.url) ? "" : ' target="_blank" rel="noopener"'}>${esc(h.text)}</a>` : esc(h.text)}</div>
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
  showDeviceSettings();
  const data = await (await api("/api/status")).json();
  Lights.setProblems(healthProblems(data));
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
  calendarTargets = null;
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
  $("#notify-prefs").innerHTML = `<p class="muted small">First tick: send it to your phone. “in quiet hours”: don't hold
    it during quiet hours (${esc(data.quiet_hours || "none")}; held ones arrive together afterwards). “in chat”: also show
    it in the chat.</p>` + Object.entries(data.categories).map(([key, c]) => `
    <div class="row notify-pref" data-key="${esc(key)}">
      <label class="calendar-row"><span><input type="checkbox" data-field="on" ${c.on ? "checked" : ""}> ${esc(c.label)}</span></label>
      <span class="pref-options">
        <label class="small muted"><input type="checkbox" data-field="loud" ${c.quiet ? "" : "checked"} ${c.on ? "" : "disabled"}>
          in quiet hours</label>
        <label class="small muted"><input type="checkbox" data-field="chat" ${c.chat ? "checked" : ""}> in chat</label>
      </span>
    </div>`).join("");
}
$("#notify-prefs").addEventListener("change", async (event) => {
  const row = event.target.closest(".notify-pref");
  if (!row) return;
  const on = row.querySelector('[data-field="on"]').checked;
  const loud = row.querySelector('[data-field="loud"]');
  loud.disabled = !on;
  const chat = row.querySelector('[data-field="chat"]').checked;
  await api("/api/notifications/settings", { method: "POST",
    body: JSON.stringify({ [row.dataset.key]: { on, quiet: !loud.checked, chat } }) });
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

/* ---------- "that was wrong" ---------- */
function addReportButton(el, trace, answer, route = "") {
  let foot = el.querySelector(":scope > .foot");
  if (!foot) { foot = document.createElement("div"); foot.className = "foot"; el.appendChild(foot); }
  const button = document.createElement("button");
  button.className = "speak"; button.type = "button"; button.textContent = "👎";
  button.title = "This answer was wrong — report it";
  button.setAttribute("aria-label", "Report a wrong answer");
  button.addEventListener("click", () => {
    let form = el.querySelector(":scope > .report-form");
    if (form) { form.remove(); return; }
    form = document.createElement("form");
    form.className = "report-form card";
    form.innerHTML = `<label class="small"><span>What was wrong? <span class="muted">(optional)</span></span>
        <textarea rows="2" placeholder="e.g. included Wednesday's events; times were an hour out"></textarea></label>
      <div class="row-actions"><button type="submit">Report</button><button type="button" class="ghost">Cancel</button></div>`;
    form.querySelector("button.ghost").addEventListener("click", () => form.remove());
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const note = form.querySelector("textarea").value.trim();
      let asked = el.previousElementSibling;
      while (asked && !asked.classList.contains("user")) asked = asked.previousElementSibling;
      const prompt = asked ? asked.dataset.text || "" : "";
      const r = await api("/api/feedback", { method: "POST", body: JSON.stringify({ trace, answer, route, note, prompt }) });
      if (!r.ok) { form.insertAdjacentHTML("beforeend", '<p class="error small">Couldn\'t save the report.</p>'); return; }
      form.remove();
      button.textContent = "👎 Reported"; button.disabled = true;
    });
    el.appendChild(form);
    form.querySelector("textarea").focus();
  });
  foot.appendChild(button);
}
function reportHtml(f) {
  const status = { open: "", fixed: '<span class="tag">fixed</span>', dismissed: '<span class="tag">dismissed</span>' }[f.status] || "";
  return `<div class="card report ${esc(f.status)}" data-report="${f.id}">
    <div class="muted small">${when(f.ts)} · v${esc(f.version)}${f.route ? " · " + esc(f.route) : ""} ${status}</div>
    ${f.prompt ? `<p><strong>${esc(f.prompt)}</strong></p>` : ""}
    ${f.note ? `<p>📝 ${esc(f.note)}</p>` : ""}
    <details><summary class="small">Answer given</summary><div class="small">${markdown(f.answer)}</div></details>
    <div class="row-actions">
      ${f.trace ? `<a class="button ghost" href="#logs?trace=${esc(f.trace)}">Logs (${f.log_count})</a>` : ""}
      ${f.status === "open" ? '<button class="ghost" data-status="fixed">Fixed</button><button class="ghost" data-status="dismissed">Dismiss</button>'
                            : '<button class="ghost" data-status="open">Reopen</button>'}
    </div></div>`;
}
async function loadReports() {
  const data = await (await api(`/api/feedback?status=${$("#report-status").value}`)).json();
  const open = data.counts.open || 0;
  $("#log-list").innerHTML = `<p class="muted small">${open} open report(s). Download them and share the file when
      asking for fixes — each one includes what Jarvis did.</p>` + (data.items.map(reportHtml).join("")
    || '<p class="muted">Nothing reported. Tap 👎 under an answer that was wrong.</p>');
  $("#log-more").classList.add("hidden");
}
$("#log-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-status]");
  const card = event.target.closest("[data-report]");
  if (!button || !card) return;
  await api(`/api/feedback/${card.dataset.report}`, { method: "POST", body: JSON.stringify({ status: button.dataset.status }) });
  loadReports();
});

function setLogMode(mode) {
  logMode = mode;
  document.querySelectorAll("[data-logmode]").forEach((b) => b.classList.toggle("active", b.dataset.logmode === mode));
  const entries = mode === "entries", reports = mode === "reports";
  ["#log-level", "#log-source", "#log-search"].forEach((s) => $(s).classList.toggle("hidden", !entries));
  ["#trace-kind", "#problems-wrap"].forEach((s) => $(s).classList.toggle("hidden", entries || reports));
  ["#report-status", "#export-reports"].forEach((s) => $(s).classList.toggle("hidden", !reports));
}
document.querySelectorAll("[data-logmode]").forEach((b) => b.addEventListener("click", () => {
  setLogMode(b.dataset.logmode);
  if (location.hash.includes("trace=")) location.hash = "#logs"; else loadLogs();
}));
["#trace-kind", "#trace-problems", "#log-level", "#log-source", "#report-status"].forEach((s) => $(s).addEventListener("change", () => loadLogs()));
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
  if (logMode === "reports") { await loadReports(); return; }
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
$("#export-reports").addEventListener("click", () => download(`/api/feedback/export?status=${$("#report-status").value}`));
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
