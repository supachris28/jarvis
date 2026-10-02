"use strict";
/* Jarvis's face: a visor of light segments in the header whose patterns mean something.
   Tap it to see the legend. Patterns:
     idle      slow blue wave, with the odd blink           — all well, waiting
     listening ripples as you type                           — taking in your message
     thinking  white-blue scanner sweeping back and forth    — working out what you asked
     looking   segments filling in the colour of the place   — searching notes (violet), email (red), calendar
               being searched                                   (green), Drive (yellow), home (orange), web (teal)
     talking   flickers with each word as the reply arrives  — writing the answer
     speaking  equaliser from the centre                     — reading aloud
     done      green sweep                                   — finished
     error     red double blink                              — something went wrong
     alert     amber/red lights on the right, one per part   — part of Jarvis needs attention (see Status)
               that is down
     offline   dim grey pulse                                — can't reach the server
*/
const Lights = (() => {
  const COLORS = {
    idle: [91, 141, 239], hearing: [110, 231, 183], thinking: [170, 200, 255], talking: [224, 231, 255], speaking: [125, 211, 252],
    done: [74, 222, 128], error: [248, 113, 113], alert: [251, 176, 34], bad: [249, 112, 102], offline: [120, 128, 140],
    vault: [167, 139, 250], gmail: [248, 113, 113], calendar: [52, 211, 153], drive: [250, 204, 21],
    home: [251, 146, 60], web: [45, 212, 191], brief: [252, 211, 77], bible: [253, 230, 138],
  };
  const MEANING = {
    hearing: "Hearing you — speak, then pause",
    idle: "All well — waiting for you",
    listening: "Listening — taking in what you type",
    thinking: "Thinking — working out what you asked",
    looking: "Looking things up",
    talking: "Writing the answer",
    speaking: "Reading aloud",
    done: "Done",
    error: "Something went wrong with that",
    offline: "Can't reach the Jarvis server",
  };
  const PLACES = { vault: "your notes", gmail: "your email", calendar: "your calendar", drive: "Google Drive",
                   home: "Home Assistant", web: "the web", brief: "your brief", bible: "the Bible" };
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  let canvas, ctx, button, cols = 14, rows = 3, width = 0, height = 0, dpr = 1;
  let state = "idle", place = "", since = performance.now(), problems = [], flashUntil = 0, flashState = "";
  const energy = [];      // per column, decays — token flicker, typing ripples, speech bumps
  let last = 0;

  function mount(el) {
    button = el;
    canvas = el.querySelector("canvas");
    ctx = canvas.getContext("2d");
    resize();
    window.addEventListener("resize", resize);
    el.addEventListener("click", toggleLegend);
    requestAnimationFrame(frame);
    describe();
  }
  function resize() {
    if (!canvas) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = canvas.clientWidth; height = canvas.clientHeight;
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    cols = Math.max(8, Math.floor(width / 13));
    energy.length = cols; energy.fill(0);
  }

  function set(next, where = "") {
    if (next === state && where === place) return;
    state = next; place = where; since = performance.now();
    describe();
  }
  function flash(kind) {           // done / error: shown briefly, then back to idle
    flashState = kind; flashUntil = performance.now() + (kind === "error" ? 1400 : 900);
    set("idle");
    since = performance.now();
    describe();
  }
  function bump(strength = 1, at = -1) {
    const i = at >= 0 ? at : Math.floor(Math.random() * cols);
    energy[i] = Math.min(1.4, (energy[i] || 0) + strength);
  }
  function token() {
    if (state !== "talking") set("talking");
    for (let n = 0; n < 2; n++) bump(0.8);
  }
  let loudness = 0;
  function voice(level) {         // microphone level 0..1 while hearing you
    loudness = Math.max(level, loudness * 0.7);
    const mid = Math.floor(cols / 2);
    bump(level * 0.9, mid + Math.round((Math.random() - 0.5) * cols * level));
  }
  function ripple() {              // a keypress: a ripple from the middle
    if (state === "idle" || state === "listening") set("listening");
    const mid = Math.floor(cols / 2);
    bump(0.9, mid); bump(0.6, mid - 1);
  }
  function setProblems(list) { problems = list; describe(); }

  function current() {
    if (performance.now() < flashUntil) return flashState;
    return state;
  }
  function describe() {
    if (!button) return;
    const now = current();
    let text = now === "looking" ? `Looking in ${PLACES[place] || place}` : MEANING[now] || MEANING.idle;
    if (problems.length && (now === "idle" || now === "listening")) text = `Needs attention: ${problems.join(", ")}`;
    button.setAttribute("aria-label", `Jarvis: ${text}. Tap for what the lights mean.`);
    button.title = text;
    const legend = document.getElementById("lights-legend");
    if (legend && !legend.classList.contains("hidden")) legend.querySelector(".now").textContent = text;
  }

  /* ---------- drawing ---------- */
  function color(name) { return COLORS[name] || COLORS.idle; }
  function frame(t) {
    requestAnimationFrame(frame);
    if (t - last < 33 || !ctx || document.hidden) return;   // ~30 fps, nothing while hidden
    last = t;
    if (performance.now() >= flashUntil && flashState) { flashState = ""; describe(); }
    const s = current();
    const age = (t - since) / 1000;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const gapX = 3, gapY = 2;
    const w = (width - gapX * (cols - 1)) / cols;
    const h = (height - gapY * (rows - 1)) / rows;
    const still = reduced.matches;
    for (let c = 0; c < cols; c++) {
      energy[c] = (energy[c] || 0) * 0.86;
      for (let r = 0; r < rows; r++) {
        let [level, rgb] = pattern(s, c, r, still ? 0 : t / 1000, age);
        const right = cols - 1 - c;
        if (problems.length && right < problems.length && (s === "idle" || s === "listening")) {
          rgb = color(problems.length > 1 ? "bad" : "alert");
          level = still ? 0.9 : 0.55 + 0.4 * (0.5 + 0.5 * Math.sin(t / 450 + right));
        }
        segment(c * (w + gapX), r * (h + gapY), w, h, rgb, Math.max(0.07, Math.min(1, level)));
      }
    }
  }
  function segment(x, y, w, h, rgb, level) {
    const [R, G, B] = rgb;
    ctx.shadowColor = `rgba(${R},${G},${B},${0.9 * level})`;
    ctx.shadowBlur = 8 * level;
    ctx.fillStyle = `rgba(${R},${G},${B},${0.15 + 0.85 * level})`;
    const radius = Math.min(h / 2, 4);
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y, w, h, radius); else ctx.rect(x, y, w, h);
    ctx.fill();
  }
  function pattern(s, c, r, t, age) {
    const x = c / Math.max(1, cols - 1);
    const e = energy[c] || 0;
    switch (s) {
      case "listening":
      case "idle": {
        const wave = 0.42 + 0.3 * Math.sin(t * 1.1 - x * 5 + r * 0.8) * (r === 1 ? 1 : 0.7);
        const blink = (t % 9) > 8.75 ? 0.25 : 1;            // a slow blink every ~9 s
        return [wave * blink + e * 0.6, color("idle")];
      }
      case "thinking": {
        const head = (Math.sin(t * 2.6) + 1) / 2;            // back and forth
        const d = Math.abs(x - head);
        return [Math.max(0.12, 1 - d * 4.5) + e * 0.3, color("thinking")];
      }
      case "looking": {
        const fill = (age * 0.9) % 1.25;                    // fills left→right, then starts again
        const lit = x <= fill ? 0.85 - (fill - x) * 0.35 : 0.12;
        return [lit + 0.1 * Math.sin(t * 8 + c), color(place)];
      }
      case "talking":
        return [0.25 + e * 0.75 + 0.08 * Math.sin(t * 3 + c), color("talking")];
      case "hearing": {
        const mid = (cols - 1) / 2;
        const d = Math.abs(c - mid) / Math.max(1, mid);
        const level = 0.3 + Math.max(0, loudness * 1.6 - d) * (r === 1 ? 1 : 0.75) + e * 0.4;
        loudness *= 0.995;
        return [level, color("hearing")];
      }
      case "speaking": {
        const mid = (cols - 1) / 2;
        const d = Math.abs(c - mid) / Math.max(1, mid);
        const level = 0.25 + 0.6 * Math.abs(Math.sin(t * 7.3 + d * 3.1)) * (1 - d * 0.6) + e * 0.5;
        return [level, color("speaking")];
      }
      case "done": {
        const sweep = age * 2.4;
        return [x <= sweep ? 0.9 - Math.max(0, age - 0.5) : 0.15, color("done")];
      }
      case "error":
        return [Math.floor(t * 4) % 2 ? 0.95 : 0.15, color("error")];
      case "offline":
        return [0.15 + 0.12 * Math.sin(t * 1.4), color("offline")];
      default:
        return [0.3, color("idle")];
    }
  }

  /* ---------- legend ---------- */
  function toggleLegend(event) {
    event.stopPropagation();
    const legend = document.getElementById("lights-legend");
    legend.classList.toggle("hidden");
    describe();
  }
  document.addEventListener("click", (event) => {
    const legend = document.getElementById("lights-legend");
    if (legend && !legend.classList.contains("hidden") && !event.target.closest("#lights-legend")) legend.classList.add("hidden");
  });

  return { mount, set, flash, token, bump, ripple, voice, setProblems, get state() { return state; } };
})();
