"use strict";
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const SLOTS = ["breakfast", "lunch", "dinner"];
let tab = "plan";
let busy = false;
let renderSeq = 0;        // a slow render must never paint over the tab the user moved to

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, method = "GET", body, timeoutMs = 60000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let r;
  try {
    r = await fetch("api/" + path, {
      method, signal: ctl.signal, headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error(e.name === "AbortError" ? "That took too long. Try again." : "Could not reach the add-on.");
  } finally { clearTimeout(timer); }
  if (!r.ok) {
    let msg = r.statusText || "HTTP " + r.status;
    try {
      const d = (await r.json()).detail;
      msg = typeof d === "string" ? d : Array.isArray(d) ? d.map(x => x.msg).join("; ") : msg;
    } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); if (ms) toast.t = setTimeout(() => t.classList.remove("show"), ms);
}

// Run one action at a time. keep: on failure, leave the page as it is so typed text / parsed receipts survive.
async function act(fn, okMsg, { keep = false } = {}) {
  if (busy) { toast("Still working on the last thing…"); return; }
  busy = true;
  document.body.classList.add("busy");
  let ok = false;
  try { await fn(); ok = true; if (okMsg) toast(okMsg); } catch (e) { toast(e.message); }
  busy = false;
  document.body.classList.remove("busy");
  if (ok || !keep) render();
}

const localISO = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const day = d => new Date(d + "T12:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
// "7/9 in pantry" badge (CookTrace-style); green when everything is on hand
const matchChip = m => m && m.total ? `<span class="chip ${m.have === m.total ? "ok" : ""}" title="${m.missing.length ? "Missing: " + esc(m.missing.join(", ")) : "All on hand"}">🥫 ${m.have}/${m.total}</span>` : "";
const daysLeft = d => Math.round((new Date(d + "T12:00") - new Date(localISO(new Date()) + "T12:00")) / 864e5);
const expiryText = d => { const n = daysLeft(d); return n < 0 ? `expired ${-n}d ago` : n === 0 ? "expires today" : `${n}d left`; };
const num = v => Math.round(Number(v) || 0);
const macros = m => m.calories ? `${num(m.calories)} kcal · P${num(m.protein)} C${num(m.carbs)} F${num(m.fat)} per serving` : "";
const qtyText = i => i.qty ? `${esc(+(+i.qty).toFixed(2))} ${esc(i.unit || "")}` : "";

// ---------------------------------------------------------------- Claude sign-in (Claude Code CLI)
async function claudeCard() {
  const s = await api("claude");
  if (s.signed_in) return `<div class="card row sp"><span class="muted">Claude connected (subscription)</span>
    <button class="btn small" id="cl-out">Disconnect</button></div>`;
  if (!s.cli) return "";
  const l = s.login;
  if (l && !l.done && l.url) return `<div class="card"><h3>Connect Claude</h3>
    <p>1. <a href="${esc(l.url)}" target="_blank" rel="noopener">Open this sign-in link</a> and approve.</p>
    <p>2. Paste the code it shows:</p>
    <div class="row"><input id="cl-code" placeholder="Code" style="flex:1"><button class="btn primary" id="cl-send">Connect</button>
    <button class="btn" id="cl-cancel">Cancel</button></div></div>`;
  if (l && !l.done) return `<div class="card muted">Starting Claude sign-in…</div>`;
  return `<div class="card"><h3>Connect Claude</h3><p class="muted">${s.token ? "Using the token from the Configuration tab. " : ""}
    Sign in with your Claude subscription to generate plans (no API fees).${l && l.result ? " Last attempt: " + esc(l.result) : ""}</p>
    <button class="btn primary" id="cl-in">Sign in to Claude</button></div>`;
}

async function fillClaude(seq) {
  try {
    const html = await claudeCard();
    const slot = $("#claude-slot");
    if (seq !== renderSeq || !slot) return;
    slot.innerHTML = html;
    const on = (id, fn) => { const b = $(id); if (b) b.onclick = fn; };
    on("#cl-in", () => act(() => api("claude/login", "POST")));
    on("#cl-send", () => act(() => api("claude/code", "POST", { code: $("#cl-code").value }), "Checking…", { keep: true }));
    on("#cl-cancel", () => act(() => api("claude/cancel", "POST")));
    on("#cl-out", () => confirm("Disconnect Claude?") && act(() => api("claude/logout", "POST")));
    // While a sign-in is in progress, keep the card fresh (the link takes a few seconds to appear).
    if (/Starting Claude sign-in/.test(html)) setTimeout(() => seq === renderSeq && fillClaude(seq), 2000);
  } catch (e) {}
}

// ---------------------------------------------------------------- Plan
let planNotes = "";

function mealRow(d, s) {
  const m = d.meals[s], people = d.people[s];
  return `<div class="meal">
    <div style="flex:1;min-width:0"><div class="slot">${s} · <label class="muted">👥 <input type="number" min="1" max="30" value="${people}"
        class="people" data-mhc="${d.date}|${s}" aria-label="People for ${s}"></label></div>
      ${m ? `<div>${esc(m.name)} ${matchChip(m.match)}</div><div class="macros">${macros(m)}</div>
        <details><summary class="muted">Recipe for ${m.headcount}</summary>
          <ul>${m.ingredients.map(i => `<li>${qtyText(i)} ${esc(i.name)}</li>`).join("")}</ul>
          <ol>${m.steps.map(x => `<li>${esc(x)}</li>`).join("")}</ol>
          ${m.saved ? "" : `<button class="btn small" data-keep="${m.recipe_id}">Save to my recipes</button>`}</details>`
        : '<div class="muted">Nothing planned</div>'}</div>
    <div class="row">${m ? (m.cooked ? '<span class="chip ok">✓ Cooked</span>' : `<button class="btn small" data-cook="${d.date}|${s}" title="Take the ingredients out of the pantry">🍳 Cooked</button>`) : ""}
      <button class="btn small" data-regen="${d.date}|${s}">${m ? "Swap" : "Add"}</button>
      ${m ? `<button class="btn small danger" data-clear="${d.date}|${s}" aria-label="Clear">✕</button>` : ""}</div></div>`;
}

async function renderPlan() {
  const seq = ++renderSeq;
  const [days, st, gen] = await Promise.all([api("plan"), api("state"), api("plan/generate")]);
  if (seq !== renderSeq) return;
  $("#date").textContent = day(st.today);
  const running = gen.status === "running";
  view.innerHTML = `
    <div class="card"><div class="row">
      <input id="notes" placeholder="Notes for this week (optional)" value="${esc(planNotes)}" style="flex:1">
      <button class="btn primary" id="gen" ${running ? "disabled" : ""}>${running ? "Generating…" : "Generate week"}</button></div>
      ${running ? '<p class="muted">Claude is planning the week. This takes a few minutes; you can leave this page.</p>' : ""}
      ${gen.status === "error" ? `<p class="muted">Last generation failed: ${esc(gen.message)}</p>` : ""}</div>
    <div id="claude-slot"></div>
    ${days.map(d => `<div class="card"><div class="row sp"><h3>${day(d.date)}</h3>
      <label class="row muted">Whole day 👥 <input type="number" min="1" max="30" value="${d.people.dinner}" class="people" data-hc="${d.date}"></label></div>
      ${SLOTS.map(s => mealRow(d, s)).join("")}</div>`).join("")}`;
  fillClaude(seq);
  if (running) pollGeneration(seq);
  $("#notes").oninput = e => { planNotes = e.target.value; };
  $("#gen").onclick = () => act(async () => {
    await api("plan/generate", "POST", { notes: planNotes });
    toast("Generating. This takes a few minutes; you can leave this page.", 6000);
  }, null, { keep: true });
  // Headcount saves are quick and need no full re-render; chain them so a tap on Generate never loses one.
  const saveHc = body => { hcSave = hcSave.then(() => api("plan/headcount", "POST", body)).then(() => {}, e => toast(e.message)); };
  view.querySelectorAll("[data-hc]").forEach(i => i.onchange = () => {
    saveHc({ date: i.dataset.hc, headcount: +i.value });
    view.querySelectorAll(`[data-mhc^="${i.dataset.hc}|"]`).forEach(x => { x.value = i.value; });
  });
  view.querySelectorAll("[data-mhc]").forEach(i => i.onchange = () => {
    const [date, slot] = i.dataset.mhc.split("|");
    saveHc({ date, slot, headcount: +i.value });
  });
  view.querySelectorAll("[data-regen]").forEach(b => b.onclick = () => {
    const [date, slot] = b.dataset.regen.split("|");
    const hint = prompt("Any preference? (optional)");
    if (hint === null) return;
    act(async () => { await hcSave; await api("plan/meal", "POST", { date, slot, hint }, 300000); }, "Meal updated, grocery list too");
  });
  view.querySelectorAll("[data-cook]").forEach(b => b.onclick = () => {
    const [date, slot] = b.dataset.cook.split("|");
    act(async () => {
      const r = await api("plan/cooked", "POST", { date, slot });
      toast(r.already ? "Already marked cooked." : r.changed.length ? "Used: " + r.changed.map(c => `${c.name} (${c.left} left)`).join(", ") : "Marked cooked. Nothing in the pantry matched.", 6000);
    });
  });
  view.querySelectorAll("[data-keep]").forEach(b => b.onclick = () => act(() => api(`recipes/${b.dataset.keep}/save`, "POST"), "Saved to my recipes"));
  view.querySelectorAll("[data-clear]").forEach(b => b.onclick = () => {
    const [date, slot] = b.dataset.clear.split("|");
    act(() => api(`plan/meal?date=${date}&slot=${slot}`, "DELETE"));
  });
}
let hcSave = Promise.resolve();

function pollGeneration(seq) {
  setTimeout(async () => {
    if (seq !== renderSeq || tab !== "plan") return;
    try {
      const g = await api("plan/generate");
      if (g.status === "running") return pollGeneration(seq);
      toast(g.status === "done" ? g.message + (g.skipped ? ` (${g.skipped} meal(s) left as they were)` : "") : "Generation failed: " + g.message, 6000);
      render();
    } catch (e) { pollGeneration(seq); }
  }, 4000);
}

// ---------------------------------------------------------------- Recipes
let recipeQuery = "", recipeSort = "", searching = false;

const next7 = () => Array.from({ length: 7 }, (_, n) => { const d = new Date(); d.setDate(d.getDate() + n); return localISO(d); });

async function renderRecipes(live = false) {
  const seq = ++renderSeq;
  const [rs, lib] = await Promise.all([api(`recipes?q=${encodeURIComponent(recipeQuery)}&live=${live}&sort=${recipeSort}`, "GET", null, 90000), api("library")]);
  if (seq !== renderSeq) return;
  const ra = lib.recipeapi;
  const dayOpts = next7().map(d => `<option value="${d}">${day(d)}</option>`).join("");
  view.innerHTML = `
    <div class="card"><div class="row"><input id="rq" placeholder="Search recipes" value="${esc(recipeQuery)}" style="flex:1">
      <button class="btn" id="rs">Search</button><button class="btn" id="rl" title="Also search TheMealDB and Recipe API">Online</button>
      <button class="btn ${recipeSort ? "primary" : ""}" id="rm" title="Recipes you can make from the pantry first">🥫 Can make</button></div>
      <div class="macros">${lib.total} recipes stored${ra.enabled ? ` · Recipe API sync: ${ra.stored} pulled, ${ra.used}/${ra.cap} calls this month` : ""}</div></div>
    <div class="card"><h3>Import a recipe</h3>
      <input id="url" placeholder="Recipe URL"><div style="height:8px"></div>
      <textarea id="paste" placeholder="…or paste the recipe text"></textarea><div style="height:8px"></div>
      <button class="btn primary" id="imp">Import</button></div>
    ${rs.map(r => `<div class="card"><div class="row sp"><h3>${esc(r.name)}</h3><span class="row">${matchChip(r.match)}<span class="chip">${esc(r.meal_type || "")} · ${esc(r.servings)} serv</span></span></div>
      <div class="macros">${macros(r)}</div>
      <details><summary class="muted">Ingredients &amp; steps</summary>
        <ul>${r.ingredients.map(i => `<li>${qtyText(i)} ${esc(i.name)}</li>`).join("")}</ul>
        <ol>${r.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol></details>
      <div class="row" style="margin-top:8px">
        <select data-pday="${r.id}" aria-label="Day" style="width:auto">${dayOpts}</select>
        <select data-pslot="${r.id}" aria-label="Meal" style="width:auto">${SLOTS.map(s => `<option ${s === (r.meal_type === "breakfast" ? "breakfast" : "dinner") ? "selected" : ""}>${s}</option>`).join("")}</select>
        <button class="btn small" data-plan="${r.id}">Add to plan</button></div>
      <div class="row" style="margin-top:8px">${r.saved ? `<button class="btn small" data-fav="${r.id}">${r.favorite ? "★ Favorite" : "☆ Favorite"}</button>
      <button class="btn small danger" data-del="${r.id}">Delete</button>` : `<button class="btn small primary" data-save="${r.id}">Save to my recipes</button>
      <span class="chip">${esc(r.origin)}</span>`}</div></div>`).join("") || `<p class="muted">${recipeQuery ? "No matches. Try Online." : "No recipes yet."}</p>`}`;
  const go = live => {
    if (searching) return;
    searching = true;
    recipeQuery = $("#rq").value.trim();
    if (live) toast("Searching online…");
    renderRecipes(live).catch(e => toast(e.message)).finally(() => { searching = false; });
  };
  $("#rs").onclick = () => go(false);
  $("#rl").onclick = () => go(true);
  $("#rm").onclick = () => { recipeSort = recipeSort ? "" : "match"; go(false); };
  $("#rq").onkeydown = e => { if (e.key === "Enter") go(false); };
  view.querySelectorAll("[data-save]").forEach(b => b.onclick = () => act(() => api(`recipes/${b.dataset.save}/save`, "POST"), "Saved"));
  view.querySelectorAll("[data-plan]").forEach(b => b.onclick = () => {
    const id = b.dataset.plan, date = $(`[data-pday="${id}"]`).value, slot = $(`[data-pslot="${id}"]`).value;
    act(() => api("plan/meal", "POST", { date, slot, recipe_id: +id }), `Added to ${day(date)} ${slot}`);
  });
  $("#imp").onclick = () => act(async () => {
    const url = $("#url").value.trim(), text = $("#paste").value.trim();
    toast("Importing…", 0); await api("recipes/import", "POST", { url: url || null, text: text || null }, 300000);
  }, "Recipe saved", { keep: true });
  view.querySelectorAll("[data-del]").forEach(b => b.onclick = () => confirm("Delete recipe?") && act(() => api("recipes/" + b.dataset.del, "DELETE")));
  view.querySelectorAll("[data-fav]").forEach(b => b.onclick = () => act(async () => {
    const r = rs.find(x => x.id == b.dataset.fav); await api("recipes/" + r.id, "PUT", { ...r, favorite: r.favorite ? 0 : 1 });
  }));
}

// ---------------------------------------------------------------- Grocery
async function renderGrocery() {
  const seq = ++renderSeq;
  const [groups, sugg] = await Promise.all([api("grocery"), api("grocery/suggestions")]);
  if (seq !== renderSeq) return;
  view.innerHTML = `
    <div class="card row">
      <button class="btn" id="rb">Rebuild from plan</button>
      <button class="btn primary" id="sync">Send to Home Assistant list</button></div>
    <div class="card row"><input id="gn" placeholder="Add item" style="flex:1"><button class="btn" id="ga">Add</button></div>
    ${sugg.length ? `<div class="card"><h3>Probably due again</h3><div class="row">${sugg.map((s, n) =>
      `<button class="btn small" data-sg="${n}" title="Usually every ${s.every_days} days, last bought ${esc(s.last)}">+ ${esc(s.name)}</button>`).join("")}</div></div>` : ""}
    ${groups.map(g => `<div class="card"><h3>${esc(g.label)}</h3>
      ${g.aisles.map(a => `<div class="aisle">${esc(a.aisle)}</div>${a.items.map(i => `
        <div class="item ${i.checked ? "done" : ""}"><input type="checkbox" data-chk="${i.id}" ${i.checked ? "checked" : ""} aria-label="Bought ${esc(i.name)}">
        <div class="nm">${esc(i.name)} <span class="muted">${qtyText(i)}</span>
        ${i.note && i.note !== "manual" ? `<div class="macros">${esc(i.note)}</div>` : ""}</div>
        ${i.stocked ? '<span class="chip ok">in pantry</span>' : `<button class="btn small" data-pan="${i.id}" title="Bought: add to pantry">🥫</button>`}
        <button class="btn small danger" data-gd="${i.id}" aria-label="Remove">✕</button></div>`).join("")}`).join("")}</div>`).join("") || '<p class="muted">List is empty. Generate a week first.</p>'}`;
  $("#rb").onclick = () => act(() => api("grocery/rebuild", "POST"), "Rebuilt");
  $("#sync").onclick = () => act(async () => { const r = await api("grocery/sync", "POST"); toast(`Sent ${r.synced} items`); });
  view.querySelectorAll("[data-sg]").forEach(b => b.onclick = () => {
    const s = sugg[+b.dataset.sg]; act(() => api("grocery", "POST", { name: s.name, store: s.store }), "Added " + s.name);
  });
  $("#ga").onclick = () => { const n = $("#gn").value.trim(); n && act(() => api("grocery", "POST", { name: n }), null, { keep: true }); };
  $("#gn").onkeydown = e => { if (e.key === "Enter") $("#ga").click(); };
  // Ticking in the store must be instant and never dropped: optimistic, outside the one-action lock.
  view.querySelectorAll("[data-chk]").forEach(c => c.onchange = async () => {
    const row = c.closest(".item"); row.classList.toggle("done", c.checked);
    try { await api(`grocery/${c.dataset.chk}/check?checked=${c.checked}`, "POST"); }
    catch (e) { c.checked = !c.checked; row.classList.toggle("done", c.checked); toast(e.message); }
  });
  view.querySelectorAll("[data-pan]").forEach(b => b.onclick = () => act(() => api(`grocery/${b.dataset.pan}/check?checked=true&to_pantry=true`, "POST"), "Added to pantry"));
  view.querySelectorAll("[data-gd]").forEach(b => b.onclick = () => act(() => api("grocery/" + b.dataset.gd, "DELETE")));
}

// ---------------------------------------------------------------- Pantry + barcode scanning
// iOS Safari has no BarcodeDetector: load the zxing-wasm polyfill (Sec-ant/barcode-detector), pinned + SRI.
async function ensureDetector() {
  if ("BarcodeDetector" in window) return true;
  try {
    await new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/barcode-detector@3.0.8/dist/iife/polyfill.js";
      s.integrity = "sha384-AQNiZ1PD/YkyuIQblEWrXvn0Byx6RIgisRdpUXjbSsLYEMsNhJxT6M3LHmS7xBZZ";
      s.crossOrigin = "anonymous";
      s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    });
  } catch (e) {}
  return "BarcodeDetector" in window;
}

// Pick (or take) a photo and downsize it to a ~1568px JPEG (what Claude reads best). Must be called from a tap.
function pickPhoto() {
  return new Promise(resolve => {
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = "image/*"; inp.capture = "environment"; inp.style.display = "none";
    document.body.appendChild(inp);              // detached inputs can be garbage-collected on iOS
    const done = v => { inp.remove(); resolve(v); };
    inp.addEventListener("cancel", () => done(null));
    inp.onchange = () => {
      const f = inp.files[0]; if (!f) return done(null);
      const img = new Image();
      img.onload = () => {
        const k = Math.min(1, 1568 / Math.max(img.width, img.height));
        const c = document.createElement("canvas"); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(img.src);
        done(c.toDataURL("image/jpeg", 0.85).split(",")[1]);
      };
      img.onerror = () => { URL.revokeObjectURL(img.src); toast("Could not read that photo. Try a JPEG."); done(null); };
      img.src = URL.createObjectURL(f);
    };
    inp.click();
  });
}

async function startScanner() {
  const ov = document.createElement("div"); ov.id = "scanner";
  ov.innerHTML = `<video playsinline muted></video><div id="sc-panel"></div>
    <div class="bar"><input id="mcode" inputmode="numeric" placeholder="Type barcode" style="flex:1">
    <button class="btn" id="mok">Go</button><button class="btn danger" id="mx">Done</button></div>`;
  document.body.appendChild(ov);
  const video = $("video", ov), panel = $("#sc-panel", ov);
  let stream = null, stop = false, last = "", lastSeen = 0, inFlight = false, paused = false;
  const close = () => { stop = true; stream && stream.getTracks().forEach(t => t.stop()); ov.remove(); render(); };

  // Unknown barcode: an in-overlay panel, so the photo button gets a real tap (file pickers need one).
  const askUnknown = bc => new Promise(resolve => {
    paused = true;
    panel.innerHTML = `<div class="card"><b>No database knows ${esc(bc)}.</b>
      <div class="row" style="margin-top:8px"><input id="un-name" placeholder="What is it?" style="flex:1"><button class="btn primary" id="un-save">Save</button></div>
      <div class="row" style="margin-top:8px"><button class="btn" id="un-photo" style="flex:1">📸 Snap the front</button><button class="btn" id="un-skip">Skip</button></div></div>`;
    const finish = () => { panel.innerHTML = ""; paused = false; resolve(); };
    $("#un-skip", panel).onclick = finish;
    $("#un-save", panel).onclick = async () => {
      const name = $("#un-name", panel).value.trim(); if (!name) return;
      try { const t = await api("pantry/teach", "POST", { barcode: bc, name }); toast("Added " + t.item.name); } catch (e) { toast(e.message); }
      finish();
    };
    $("#un-photo", panel).onclick = async () => {
      const image = await pickPhoto(); if (!image) return;
      toast("Identifying…", 0);
      try { const t = await api("pantry/photo", "POST", { image, barcode: bc }, 200000); toast("Added " + t.item.name); } catch (e) { toast(e.message); }
      finish();
    };
  });

  const hit = async code => {
    if (inFlight || paused) return;
    inFlight = true;
    try {
      navigator.vibrate && navigator.vibrate(60);
      const r = await api("pantry/scan", "POST", { barcode: code });
      if (r.status === "unknown") {
        if (!r.barcode) toast("That does not look like a product barcode.");
        else await askUnknown(r.barcode);
      } else toast((r.status === "bumped" ? "+1 " : "Added ") + r.item.name + (r.status === "bumped" ? ` (now ${r.item.qty})` : ""));
    } catch (e) { toast(e.message); }
    finally { inFlight = false; }
  };

  $("#mx", ov).onclick = close;
  $("#mok", ov).onclick = () => {
    const v = $("#mcode", ov).value.replace(/\D/g, "");
    if (v.length < 6 || v.length > 14) return toast("Enter the digits under the barcode.");
    $("#mcode", ov).value = ""; hit(v);
  };
  $("#mcode", ov).onkeydown = e => { if (e.key === "Enter") $("#mok", ov).click(); };   // USB/Bluetooth scanners type + Enter

  try {
    const s = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
    if (stop) { s.getTracks().forEach(t => t.stop()); return; }   // Done was tapped while the permission prompt was up
    stream = s; video.srcObject = s; await video.play();
  } catch (e) {
    toast("Camera unavailable (it needs HTTPS). Type the barcode or use a scanner.", 8000);
    return;
  }
  if (!(await ensureDetector())) return toast("Scanner failed to load. Type the barcode instead.", 8000);
  let det;
  try { det = new BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "code_128"] }); }
  catch (e) { return toast("This browser cannot read barcodes. Type them instead.", 8000); }
  let errors = 0;
  while (!stop) {
    try {
      const f = await det.detect(video);
      errors = 0;
      if (f.length) {
        const c = f[0].rawValue; lastSeen = Date.now();
        if (c !== last) { last = c; hit(c); }          // a code re-arms only after it has left the frame
      } else if (last && Date.now() - lastSeen > 1500) last = "";
    } catch (e) {
      if (++errors === 5) toast("Scanning stopped working. Type the barcode instead.", 8000);
    }
    await new Promise(r => setTimeout(r, 250));
  }
}

async function importReceipt() {
  const image = await pickPhoto(); if (!image) return;
  toast("Reading receipt…", 0);
  const r = await api("pantry/receipt", "POST", { image }, 300000);
  const items = r.items || [];
  if (!items.length) return toast("No food items found on that receipt.");
  toast(`Found ${items.length} items`);
  ++renderSeq;
  view.innerHTML = `<div class="card"><h3>Receipt: ${items.length} items</h3><p class="muted">Untick anything you don't want in the pantry.</p>
    ${items.map((it, n) => `<label class="item"><input type="checkbox" data-ri="${n}" checked>
      <span class="nm">${esc(it.name)} <span class="muted">× ${esc(it.qty || 1)} ${esc(it.pack_size || "")}</span></span></label>`).join("")}
    <div class="row" style="margin-top:10px"><button class="btn primary" id="radd">Add to pantry</button><button class="btn" id="rcan">Cancel</button></div></div>`;
  $("#rcan").onclick = render;
  $("#radd").onclick = () => act(async () => {
    const keep = items.filter((_, n) => $(`[data-ri="${n}"]`).checked);
    const res = await api("pantry/bulk", "POST", { items: keep, store: r.store || "" }); toast(`Added ${res.added.length} items`);
  }, null, { keep: true });
}

async function renderPantry() {
  const seq = ++renderSeq;
  const items = await api("pantry");
  if (seq !== renderSeq) return;
  const soon = items.filter(i => i.qty > 0 && i.expires && daysLeft(i.expires) <= 5).sort((a, b) => a.expires < b.expires ? -1 : 1);
  view.innerHTML = `
    ${soon.length ? `<div class="card warn"><h3>Use soon</h3>${soon.map(i => `<div class="item"><span class="nm">${esc(i.name)}</span>
      <span class="chip ${daysLeft(i.expires) < 0 ? "bad" : ""}">${expiryText(i.expires)}</span></div>`).join("")}
      <p class="macros">The weekly plan puts these into the first few days.</p></div>` : ""}
    <div class="card row"><button class="btn primary" id="scan" style="flex:1">📷 Scan barcodes</button>
      <button class="btn" id="add">Add manually</button></div>
    <div class="card row"><button class="btn" id="rcpt" style="flex:1">🧾 Receipt photo</button>
      <button class="btn" id="pho" style="flex:1">📸 Item photo</button></div>
    <p class="muted">Scan an item to add it, scan again to add another. Items the grocery list needs are skipped when you have them.</p>
    ${items.map(i => `<div class="card ${i.qty > 0 ? "" : "out"}"><div class="row sp"><div><div>${esc(i.name)}${i.qty > 0 ? "" : ' <span class="chip">out</span>'}</div>
      <div class="macros">${esc([i.brand, i.pack_size, i.category].filter(Boolean).join(" · "))}</div></div>
      <div class="row"><button class="btn small" data-exp="${i.id}" title="Set best-before date">📅 ${i.expires ? expiryText(i.expires) : ""}</button>
      <button class="btn small" data-m="${i.id}" aria-label="One less">−</button><b>${esc(+(+i.qty).toFixed(2))}${i.unit ? " " + esc(i.unit) : ""}</b>
      <button class="btn small" data-p="${i.id}" aria-label="One more">+</button><button class="btn small danger" data-d="${i.id}" aria-label="Remove">✕</button></div></div></div>`).join("") || '<p class="muted">Pantry is empty.</p>'}`;
  view.querySelectorAll("[data-exp]").forEach(b => b.onclick = () => {
    const i = items.find(x => x.id == b.dataset.exp);
    const v = prompt("Best before: a date (YYYY-MM-DD), or days from today (3, 7, 30). Leave blank to clear.", i.expires || "");
    if (v === null) return;
    let exp = v.trim();
    if (/^\d{1,3}$/.test(exp)) { const d = new Date(); d.setDate(d.getDate() + +exp); exp = localISO(d); }
    if (exp && !/^\d{4}-\d{2}-\d{2}$/.test(exp)) return toast("Use YYYY-MM-DD or a number of days.");
    act(() => api("pantry/" + i.id, "PUT", { name: i.name, qty: i.qty, unit: i.unit, expires: exp || null }));
  });
  $("#scan").onclick = () => startScanner();
  $("#rcpt").onclick = () => { if (busy) return toast("Still working on the last thing…"); importReceipt().catch(e => toast(e.message)); };
  $("#pho").onclick = async () => {
    const image = await pickPhoto(); if (!image) return;
    toast("Identifying…", 0);
    act(async () => { const t = await api("pantry/photo", "POST", { image }, 200000); toast("Added " + t.item.name); });
  };
  $("#add").onclick = () => { const name = prompt("Item name"); name && name.trim() && act(() => api("pantry", "POST", { name: name.trim() })); };
  const bump = (id, d) => act(async () => {
    const i = items.find(x => x.id == id), qty = Math.max(0, +(i.qty + d).toFixed(2));
    qty === 0 ? await api("pantry/" + id, "DELETE") : await api("pantry/" + id, "PUT", { name: i.name, qty, unit: i.unit, expires: i.expires });
  });
  view.querySelectorAll("[data-m]").forEach(b => b.onclick = () => bump(b.dataset.m, -1));
  view.querySelectorAll("[data-p]").forEach(b => b.onclick = () => bump(b.dataset.p, 1));
  view.querySelectorAll("[data-d]").forEach(b => b.onclick = () => act(() => api("pantry/" + b.dataset.d, "DELETE")));
}

// ---------------------------------------------------------------- Shell
const renderers = { plan: renderPlan, recipes: renderRecipes, grocery: renderGrocery, pantry: renderPantry };
async function render() {
  if ($("#scanner")) return;
  try { await renderers[tab](); } catch (e) { view.innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  tab = b.dataset.tab;
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  render();
});
render();
