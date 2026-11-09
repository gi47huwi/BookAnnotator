// Book Annotator – single-page UI (no build step)

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const ENT_COLORS = {
  PERSON: "#d6336c", GPE: "#2b8a3e", LOC: "#2f9e44", FAC: "#5c940d", ORG: "#1971c2", NORP: "#7048e8",
  DATE: "#e67700", TIME: "#e67700", MONEY: "#0c8599", QUANTITY: "#0c8599", CARDINAL: "#868e96",
  ORDINAL: "#868e96", WORK_OF_ART: "#ae3ec9", EVENT: "#f76707", LAW: "#495057", LANGUAGE: "#495057", PERCENT: "#0c8599",
};
const ENT_DESC = {
  PERSON: "people, incl. fictional", GPE: "countries, cities, states", LOC: "non-political locations", FAC: "buildings, bridges, roads",
  ORG: "organisations", NORP: "nationalities, religious/political groups", DATE: "dates, periods", TIME: "times of day",
  EVENT: "named events", WORK_OF_ART: "titles of works", LAW: "named laws", LANGUAGE: "languages", MONEY: "monetary values",
  QUANTITY: "measurements", PERCENT: "percentages", CARDINAL: "other numerals", ORDINAL: "first, second…",
};
const UPOS_DESC = {
  ADJ: "adjective", ADP: "adposition", ADV: "adverb", AUX: "auxiliary", CCONJ: "coordinating conj.", DET: "determiner",
  INTJ: "interjection", NOUN: "noun", NUM: "numeral", PART: "particle", PRON: "pronoun", PROPN: "proper noun",
  PUNCT: "punctuation", SCONJ: "subordinating conj.", SYM: "symbol", VERB: "verb", X: "other",
};
const USAS_FIELDS = {
  A: "General & abstract terms", B: "The body & the individual", C: "Arts & crafts", E: "Emotion",
  F: "Food & farming", G: "Government & public", H: "Architecture, housing & the home", I: "Money & commerce",
  K: "Entertainment, sports & games", L: "Life & living things", M: "Movement, location, travel & transport",
  N: "Numbers & measurement", O: "Substances, materials, objects & equipment", P: "Education",
  Q: "Language & communication", S: "Social actions, states & processes", T: "Time", W: "World & environment",
  X: "Psychological actions, states & processes", Y: "Science & technology", Z: "Names & grammar",
};
const LEVEL_INFO = {
  fixed: ["Fixed", "Automatic output is taken as is. Locked – unlock to correct errors."],
  suggestion: ["Suggestion", "Automatic output is only a suggestion. Check each sentence, correct it, then confirm."],
  manual: ["Manual", "Annotated from scratch. Mark each sentence done when finished."],
};
const NARRATOR = "narrator";
const CORE = new Set(["speech", "quotes", "entities", "pos", "sentences"]);

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers = { "Content-Type": "application/json" };
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    toast(msg, 3500);
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 1800) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

// ------------------------------------------------------------------ state
const S = {
  book: null,          // {meta, layers, characters, chapters, n_pages, n_sentences, n_annotated, can_undo}
  page: 0,
  data: null,          // page payload
  sents: new Map(),    // sid -> sentence
  paraOf: new Map(),   // sid -> paragraph id
  order: [],           // sids on page
  toks: new Map(),     // token index -> {tok, sid}
  sel: new Set(),      // selected sentences
  cur: null,
  anchor: null,
  tsel: null,          // selected token range {a, b} (inclusive)
  tanchor: null,
  bsel: null,          // sentence whose start boundary is selected (move tool)
  segTool: "split",
  filter: "",
  candidates: null,
  issues: null,
  activeLayer: "speech",
  lastLabels: {},      // layer id -> last label/value applied (for p)
};

const bid = () => encodeURIComponent(S.book.meta.id);
const layer = (id) => S.book.layers.find((l) => l.id === id);
const activeLayer = () => layer(S.activeLayer) || S.book.layers[0];
const levelOf = (l) => l.level || (l.source === "auto" ? "fixed" : "manual");
const ready = (l) => (l.status || "ready") === "ready";
/** layers whose sentences carry a reviewed/done state (speech: annotated = reviewed) */
const tracksReview = (l) => levelOf(l) !== "fixed";
const isSuggestion = (l, s) => levelOf(l) === "suggestion" && !s.rev.includes(l.id);

function allowedLevels(l) {
  if (l.kind === "sentence-label") return ["suggestion", "manual"];
  if (l.kind === "segmentation") return ["fixed", "suggestion"];
  if (l.source === "manual") return ["manual"];
  return ["fixed", "suggestion", "manual"];
}

/** What clicks and keys do right now, derived from the active layer. */
function mode() {
  const l = activeLayer();
  if (!l || !ready(l) || l.locked) return "view";
  return { "sentence-label": "speech", "token-span": "span", "token-attr": "attr", segmentation: "seg" }[l.kind] || "view";
}

// ------------------------------------------------------------------ router
window.addEventListener("hashchange", route);
route();

async function route() {
  const m = location.hash.match(/^#\/book\/([^/]+)(?:\/(\d+))?/);
  if (m) await openBook(decodeURIComponent(m[1]), Number(m[2] || 0));
  else { S.book = null; showLibrary(); }
}

// ------------------------------------------------------------------ library
let pollTimer = null;

async function showLibrary() {
  $("#workspace").hidden = true;
  $("#library").hidden = false;
  $("#crumbs").innerHTML = "";
  $("#topbar-right").innerHTML = "";
  await renderBooks();
}

async function renderBooks() {
  const books = await api("/api/books");
  const el = $("#books");
  if (!books.length) {
    el.innerHTML = `<div class="empty">No books yet. Upload a .txt file to start your corpus.</div>`;
  } else {
    el.innerHTML = books.map((b) => {
      const rd = b.status === "ready";
      const pct = rd ? Math.round((100 * (b.n_annotated || 0)) / (b.n_sentences || 1)) : Math.round(100 * (b.progress || 0));
      const stats = rd
        ? `${b.n_sentences.toLocaleString()} sentences · ${b.n_tokens.toLocaleString()} tokens · ${pct}% speakers annotated`
        : b.status === "error" ? `Error: ${esc(b.message)}` : `${b.status} ${pct}% ${esc(b.message || "")}`;
      return `<div class="book ${b.status}">
        <div><h3>${esc(b.title)}</h3><div class="stats">${esc(b.author || "")}</div></div>
        <div class="stats">${stats}</div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <div class="row">
          ${rd ? `<a class="btn primary" href="#/book/${encodeURIComponent(b.id)}">Annotate</a>
                  <a class="btn" href="/api/books/${encodeURIComponent(b.id)}/export.tsv">TSV</a>` : ""}
          ${b.status === "error" ? `<button class="btn" data-reprocess="${esc(b.id)}">Retry</button>` : ""}
          <button class="btn ghost danger" data-del="${esc(b.id)}" style="margin-left:auto">Delete</button>
        </div></div>`;
    }).join("");
  }
  clearTimeout(pollTimer);
  if (books.some((b) => b.status === "queued" || b.status === "processing") && !S.book) {
    pollTimer = setTimeout(renderBooks, 1500);
  }
}

$("#books").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del && confirm(`Delete “${del.dataset.del}” and all its annotations? This cannot be undone.`)) {
    await api(`/api/books/${del.dataset.del}`, { method: "DELETE" });
    renderBooks();
  }
  const rp = e.target.closest("[data-reprocess]");
  if (rp) { await api(`/api/books/${rp.dataset.reprocess}/reprocess`, { method: "POST" }); renderBooks(); }
});

const fileInput = $("#file");
const drop = $("#drop");
fileInput.addEventListener("change", () => {
  const f = fileInput.files[0];
  $("#drop-label").innerHTML = f ? `<strong>${esc(f.name)}</strong> · ${(f.size / 1e6).toFixed(1)} MB` : "";
  if (f && !$("#up-title").value) $("#up-title").value = f.name.replace(/\.txt$/i, "");
});
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  fileInput.files = e.dataTransfer.files;
  fileInput.dispatchEvent(new Event("change"));
});
$("#upload").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = fileInput.files[0];
  if (!f) return toast("Choose a .txt file first");
  const fd = new FormData();
  fd.append("file", f);
  fd.append("title", $("#up-title").value);
  fd.append("author", $("#up-author").value);
  await api("/api/books", { method: "POST", body: fd });
  e.target.reset();
  $("#drop-label").innerHTML = "<strong>Choose a .txt file</strong> or drop it here";
  toast("Uploaded – processing with spaCy…");
  renderBooks();
});

// ------------------------------------------------------------------ book
async function openBook(id, page) {
  if (!S.book || S.book.meta.id !== id) {
    const b = await api(`/api/books/${encodeURIComponent(id)}`);
    if (b.meta.status !== "ready") { toast("Book is still processing"); location.hash = "#/"; return; }
    S.book = b;
    S.activeLayer = "speech";
    try { const saved = localStorage.getItem("active-layer:" + id); if (b.layers.some((l) => l.id === saved)) S.activeLayer = saved; } catch {}
    S.data = null;
    S.candidates = S.issues = null;
    $("#add-layer").hidden = true;
    renderLayers();
    pollLayers();
  }
  $("#library").hidden = true;
  $("#workspace").hidden = false;
  renderTopbar();
  await loadPage(page);
}

function renderTopbar() {
  const b = S.book;
  $("#crumbs").innerHTML = `<span class="muted">/</span><span class="title">${esc(b.meta.title)}</span>`;
  const chapters = b.chapters.map((c) => `<option value="${c.page}">${esc(c.title)}</option>`).join("");
  $("#topbar-right").innerHTML = `
    <button class="btn" id="undo" title="Undo last edit (⌘Z)">↶ Undo</button>
    <div class="progress" id="progress"><span id="prog-txt"></span><div class="bar"><i id="prog-bar"></i></div></div>
    <div class="pagenav">
      ${chapters ? `<select id="chap"><option value="">Chapter…</option>${chapters}</select>` : ""}
      <button class="btn" id="prev" title="Previous page ([)">‹</button>
      <input id="pageno" type="number" min="1" max="${b.n_pages}"> <span class="muted">/ ${b.n_pages}</span>
      <button class="btn" id="next" title="Next page (])">›</button>
    </div>
    <a class="btn" href="/api/books/${bid()}/export.tsv">Export</a>`;
  $("#undo").onclick = undo;
  $("#prev").onclick = () => gotoPage(S.page - 1);
  $("#next").onclick = () => gotoPage(S.page + 1);
  $("#pageno").onchange = (e) => gotoPage(Number(e.target.value) - 1);
  if ($("#chap")) $("#chap").onchange = (e) => { if (e.target.value !== "") gotoPage(Number(e.target.value)); e.target.value = ""; };
  updateProgress();
}

/** Done count for the active layer: speakers for speech, reviewed sentences otherwise. */
function layerDone(l) {
  if (l.id === "speech") return S.book.n_annotated;
  return l.n_reviewed || 0;
}

function updateProgress() {
  const b = S.book;
  if (!$("#prog-txt")) return;
  const l = activeLayer();
  const show = tracksReview(l);
  $("#progress").style.visibility = show ? "visible" : "hidden";
  if (show) {
    const done = layerDone(l);
    const what = l.id === "speech" ? "speakers" : levelOf(l) === "manual" ? "done" : "reviewed";
    $("#prog-txt").textContent = `${l.name}: ${done.toLocaleString()} / ${b.n_sentences.toLocaleString()} ${what}`;
    $("#prog-bar").style.width = (100 * done) / b.n_sentences + "%";
  }
  $("#undo").disabled = !b.can_undo;
}

/** Apply counters returned by every edit endpoint. */
function applyResult(r) {
  if (r.n_annotated !== undefined) S.book.n_annotated = r.n_annotated;
  if (r.n_sentences !== undefined) S.book.n_sentences = r.n_sentences;
  if (r.can_undo !== undefined) S.book.can_undo = r.can_undo;
  if (r.reviewed) for (const l of S.book.layers) l.n_reviewed = r.reviewed[l.id];
  updateProgress();
}

function gotoPage(p, cursor = "first") {
  if (p < 0 || p >= S.book.n_pages) return;
  S.pendingCursor = cursor;
  location.hash = `#/book/${bid()}/${p}`;
}

async function loadPage(page, { keepScroll = false } = {}) {
  const wrap = $("#reader-wrap");
  const scroll = wrap.scrollTop;
  if (!keepScroll) $("#reader").innerHTML = `<p class="loading">Loading…</p>`;
  const data = await api(`/api/books/${bid()}/page/${page}`);
  const pageChanged = S.page !== page || !S.data;
  S.page = page;
  S.data = data;
  S.book.can_undo = data.can_undo;
  S.sents.clear();
  S.toks.clear();
  S.paraOf.clear();
  S.order = [];
  for (const p of data.paragraphs) for (const s of p.sentences) {
    S.sents.set(s.id, s);
    S.paraOf.set(s.id, p.id);
    S.order.push(s.id);
    for (const t of s.tokens) S.toks.set(t.i, { tok: t, sid: s.id });
  }
  if (pageChanged || !S.sents.has(S.cur)) {
    const want = S.pendingCursor;
    S.pendingCursor = null;
    const sid = typeof want === "number" && S.sents.has(want) ? want
      : want === "last" ? S.order[S.order.length - 1] : S.order[0];
    S.sel = new Set([sid]);
    S.cur = S.anchor = sid;
    S.tsel = null;
    S.bsel = null;
    if (mode() === "span" || mode() === "attr") selectFirstTokenOfCursor();
  }
  if (S.pendingItem) { applyItemSelection(S.pendingItem); S.pendingItem = null; }
  if (S.tsel && !(S.toks.has(S.tsel.a) && S.toks.has(S.tsel.b))) S.tsel = null;
  if (S.bsel !== null && !S.sents.has(S.bsel)) S.bsel = null;
  renderReader();
  $("#pageno").value = page + 1;
  updateProgress();
  if (keepScroll) wrap.scrollTop = scroll;
  else if (pageChanged) { wrap.scrollTop = 0; scrollToCursor(); }
  renderInspector();
  renderTools();
}

async function reloadPage() { await loadPage(S.page, { keepScroll: true }); }

// ------------------------------------------------------------------ rendering
function charById(id) { return S.book.characters.find((c) => c.id === id); }

function chipsHtml(ids, ghost) {
  return ids.map((id) => {
    const c = charById(id);
    return `<span class="chip${ghost ? " ghost" : ""}" style="--c:${c ? c.color : "#888"}">${esc(c ? c.name : id)}</span>`;
  }).join("");
}

function spanColor(l, label) {
  return l.id === "entities" ? ENT_COLORS[label] || "#868e96" : l.color;
}

/** Visible layers grouped by how they are drawn, in stack order (top first). */
function renderPlan() {
  const vis = S.book.layers.filter((l) => l.visible && ready(l));
  const spans = vis.filter((l) => l.kind === "token-span");
  const hl = spans.find((l) => l.style === "highlight");
  const act = activeLayer();
  return {
    attrs: vis.filter((l) => l.kind === "token-attr"),
    highlight: hl,
    text: spans.filter((l) => l.style === "text"),
    underline: spans.filter((l) => l !== hl && l.style !== "text"),
    speech: layer("speech"),
    showSpeech: vis.some((l) => l.id === "speech"),
    seg: layer("sentences"),
    // review markers for the active layer (not for speech: its chips show the state)
    review: act && ready(act) && act.id !== "speech" && tracksReview(act) ? act : null,
    issues: act?.id === "sentences",
  };
}

function inTsel(i) { return S.tsel && i >= S.tsel.a && i <= S.tsel.b; }

function sentenceHtml(s, prevKey, plan) {
  const sp = plan.speech;
  const speechManual = levelOf(sp) === "manual";
  const ann = s.speakers && s.speakers.length;
  const ids = ann ? s.speakers : speechManual ? [] : s.suggestion.speakers;
  const key = (ann ? "a:" : "g:") + ids.join(",");
  let chips = "";
  if (plan.showSpeech) {
    if (ann) chips = key !== prevKey ? chipsHtml(ids, false) : "";
    else if (ids.length) chips = key !== prevKey ? chipsHtml(ids, true) : "";
    else if (!speechManual) chips = `<span class="chip ghost unknown">?</span>`;
  }
  if (plan.issues && s.issue) chips = `<span class="issue" title="${esc(s.issue)}">⚠</span>` + chips;

  const style = [];
  let cls = "s";
  if (ann && plan.showSpeech) {
    cls += " ann";
    style.push(`--sc:${charById(ids[0])?.color || "#888"}`);
    if (ids.length > 1) { cls += " multi"; style.push(`--sc2:${charById(ids[1])?.color || "#888"}`); }
  }
  if (S.sel.has(s.id)) cls += " sel";
  if (S.cur === s.id) cls += " cur";
  if (plan.review && !s.rev.includes(plan.review.id)) cls += " unrev";

  const hl = plan.highlight;
  const hlSug = hl && isSuggestion(hl, s);
  const segSug = plan.seg && isSuggestion(plan.seg, s);
  let html = "", hlOpen = false;
  s.tokens.forEach((t, k) => {
    const next = s.tokens[k + 1];
    const h = hl && t.s[hl.id];
    if (h && (h[2] || !hlOpen)) {
      if (hlOpen) html += `</span>`;
      html += `<span class="hl${hlSug ? " sug" : ""}" style="--ec:${spanColor(hl, h[1])};--op:var(--op-${hl.id})">`;
      hlOpen = true;
    }
    const shadows = [];
    let ulLabels = "";
    plan.underline.forEach((l, n) => {
      const x = t.s[l.id];
      if (!x) return;
      const sug = isSuggestion(l, s);
      shadows.push(`0 ${2 + n * 3}px 0 0 color-mix(in srgb, ${l.color} calc(var(--op-${l.id}) * ${sug ? 40 : 100}%), transparent)`);
      if (x[3]) ulLabels += `<span class="ul-l${sug ? " sug" : ""}" style="color:${l.color}">${esc(x[1])}</span>`;
    });
    const cont = next && t.ws && plan.underline.some((l) => t.s[l.id] && next.s[l.id] && t.s[l.id][0] === next.s[l.id][0]);
    const textLayers = plan.text.filter((l) => t.s[l.id]);
    let tcls = "tk";
    if (textLayers.length) tcls += isSuggestion(textLayers[0], s) ? " tx sug" : " tx";
    if (inTsel(t.i)) tcls += " tsel";
    if (t.i === s.start) {
      tcls += " first";
      if (segSug) tcls += " bsug";
      if (S.bsel === s.id) tcls += " bsel";
    }
    const tstyle = [];
    if (shadows.length) tstyle.push(`box-shadow:${shadows.join(",")}`);
    if (textLayers.length) tstyle.push(`--tc:${textLayers[0].color};--top:var(--op-${textLayers[0].id})`);
    let il = "";
    for (const l of plan.attrs) {
      const v = t.a[l.id] || "";
      const show = v && !(l.id === "pos" && v === "PUNCT");
      il += `<span class="ia${isSuggestion(l, s) ? " sug" : ""}" style="color:${l.color};opacity:var(--op-${l.id})" title="${esc(t.d[l.id] || v)}">${show ? esc(v) : "&nbsp;"}</span>`;
    }
    html += `<span class="${tcls}" data-i="${t.i}" style="${tstyle.join(";")}"><span class="w">${esc(t.t)}${cont ? " " : ""}</span>${il}</span>${ulLabels}`;
    if (hlOpen && (!h || h[3])) {
      if (h) html += `<span class="el" style="color:${spanColor(hl, h[1])}">${esc(h[1])}</span>`;
      html += `</span>`;
      hlOpen = false;
    }
    if (!cont) html += t.ws;
  });
  if (hlOpen) html += `</span>`;
  if (plan.review) {
    const done = s.rev.includes(plan.review.id);
    const word = levelOf(plan.review) === "manual" ? "done" : "reviewed";
    html += `<span class="rv${done ? " ok" : ""}" data-rv="${s.id}" title="${done ? `${word} – click to undo` : `not ${word} – click or Enter to confirm`}">${done ? "✓" : "○"}</span>`;
  }
  return { html: `<span class="${cls}" data-sid="${s.id}" style="${style.join(";")}"><span class="chips">${chips}</span>${html}</span> `, key };
}

function renderReader() {
  const plan = renderPlan();
  let out = "", prevKey = null;
  for (const p of S.data.paragraphs) {
    const text1 = p.sentences.length === 1 ? p.sentences[0].tokens.map((t) => t.t).join(" ") : "";
    const isChapter = /^(chapter|book|part|volume|stave)\b/i.test(text1) && text1.length < 80;
    out += `<p data-para="${p.id}"${isChapter ? ` class="chapter"` : ""}>`;
    for (const s of p.sentences) {
      const r = sentenceHtml(s, prevKey, plan);
      out += r.html;
      prevKey = r.key;
    }
    out += `</p>`;
    prevKey = null;
  }
  const reader = $("#reader");
  reader.innerHTML = out;
  reader.classList.toggle("il", plan.attrs.length > 0);
  reader.dataset.mode = mode();
  reader.dataset.segtool = S.segTool;
  applyLayerStyles();
  renderPager();
}

function renderPager() {
  const p = S.page, n = S.book.n_pages;
  $("#pager-bottom").innerHTML =
    `<button class="btn" ${p === 0 ? "disabled" : ""} id="pb-prev">‹ Previous page</button>
     <span class="muted small">page ${p + 1} of ${n} · sentences ${S.data.first_sentence + 1}–${S.data.last_sentence + 1}</span>
     <button class="btn" ${p >= n - 1 ? "disabled" : ""} id="pb-next">Next page ›</button>`;
  $("#pb-prev").onclick = () => gotoPage(p - 1);
  $("#pb-next").onclick = () => gotoPage(p + 1);
}

function refreshSelectionClasses() {
  for (const el of $("#reader").querySelectorAll(".s")) {
    const sid = Number(el.dataset.sid);
    el.classList.toggle("sel", S.sel.has(sid));
    el.classList.toggle("cur", S.cur === sid);
  }
  for (const el of $("#reader").querySelectorAll(".tk")) el.classList.toggle("tsel", inTsel(Number(el.dataset.i)));
  for (const el of $("#reader").querySelectorAll(".tk.first")) {
    el.classList.toggle("bsel", S.bsel !== null && S.toks.get(Number(el.dataset.i))?.sid === S.bsel);
  }
  renderInspector();
  renderTools();
}

function scrollToCursor() {
  const el = S.tsel ? $(`.tk[data-i="${S.tsel.b}"]`) : $(`.s[data-sid="${S.cur}"]`);
  if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// ------------------------------------------------------------------ layers panel
const EYE = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>`;
const LOCK = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>`;
const UNLOCK = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/></svg>`;
const KIND_LABEL = { "sentence-label": "sentences", "token-span": "spans", "token-attr": "token tags", segmentation: "segmentation" };

function levelSub(l) {
  const lv = levelOf(l);
  if (lv === "fixed") return `fixed${l.edited ? " · edited" : ""}`;
  const pct = Math.floor((100 * layerDone(l)) / S.book.n_sentences);
  return `${lv} · ${pct}%`;
}

function renderLayers() {
  try { localStorage.setItem("active-layer:" + S.book.meta.id, S.activeLayer); } catch {}
  const L = S.book.layers;
  $("#layers").innerHTML = L.map((l) => {
    const st = l.status || "ready";
    const sub = st === "running" ? `computing ${Math.round(100 * (l.progress || 0))}%`
      : st === "error" ? "error" : `${levelSub(l)} · ${KIND_LABEL[l.kind] || l.kind}`;
    return `
    <li class="layer${l.id === S.activeLayer ? " active" : ""}${l.visible ? "" : " hidden"} ${st} lv-${levelOf(l)}" data-layer="${l.id}" draggable="true">
      <button class="ic eye${l.visible ? "" : " off"}" title="Show / hide">${EYE}</button>
      <span class="thumb" style="--c:${l.color};--o:${l.opacity}"></span>
      <span class="ln">${esc(l.name)}<small>${sub}</small>
        ${st === "running" ? `<span class="mini-bar"><i style="width:${100 * (l.progress || 0)}%"></i></span>` : ""}</span>
      <button class="ic lock${l.locked ? "" : " off unlocked"}" title="${l.locked ? "Locked – click to enable editing" : "Editable – click to lock"}">${l.locked ? LOCK : UNLOCK}</button>
    </li>`;
  }).join("");
  applyLayerStyles();
  renderLayerProps();
}

function renderLayerProps() {
  const a = activeLayer();
  const el = $("#layer-props");
  if (!a || !S.book) { el.innerHTML = ""; return; }
  const st = a.status || "ready";
  const lv = levelOf(a);
  let html = `<label>Opacity <input type="range" id="op" min="0" max="1" step="0.05" value="${a.opacity}"> <span id="opv">${Math.round(a.opacity * 100)}%</span></label>`;
  if (st === "running") html += `<p>Computing… ${esc(a.message || "")}</p>`;
  else if (st === "error") html += `<p class="err">${esc(a.message)}</p>`;
  else {
    const levels = allowedLevels(a);
    html += `<div class="levels">${["fixed", "suggestion", "manual"].map((x) =>
      `<button class="lvl${x === lv ? " on" : ""}" data-level="${x}" ${levels.includes(x) ? "" : "disabled"} title="${esc(LEVEL_INFO[x][1])}">${LEVEL_INFO[x][0]}</button>`).join("")}</div>
      <p class="hint">${esc(LEVEL_INFO[lv][1])}${a.source === "auto" ? ` Source: ${esc(a.annotator || "spaCy")}${a.edited ? ", manually edited" : ""}.` : ""}</p>`;
  }
  if (!CORE.has(a.id)) html += `<button class="btn ghost danger small-btn" id="del-layer">Delete layer</button>`;
  el.innerHTML = html;

  $("#op").oninput = (e) => {
    a.opacity = Number(e.target.value);
    $("#opv").textContent = Math.round(a.opacity * 100) + "%";
    $(`.layer[data-layer="${a.id}"] .thumb`).style.setProperty("--o", a.opacity);
    applyLayerStyles();
    saveLayers();
  };
  for (const b of el.querySelectorAll("[data-level]")) b.onclick = () => setLevel(a, b.dataset.level);
  if ($("#del-layer")) $("#del-layer").onclick = async () => {
    if (!confirm(`Delete layer “${a.name}”? This cannot be undone.`)) return;
    await api(`/api/books/${bid()}/layers/${a.id}`, { method: "DELETE" });
    S.book.layers = S.book.layers.filter((l) => l.id !== a.id);
    S.activeLayer = "speech";
    syncSelectionToMode();
    renderLayers();
    reloadPage();
  };
}

async function setLevel(l, level) {
  if (level === levelOf(l)) return;
  let clear = false;
  if (level === "manual" && l.source === "auto" && (l.kind === "token-span" || l.kind === "token-attr")) {
    clear = confirm(`Annotate “${l.name}” manually.\n\nOK: remove the automatic annotations and start from scratch.\nCancel: keep them as a starting point.\n\n(Either way you can undo with ⌘Z.)`);
  }
  const r = await api(`/api/books/${bid()}/layers/${l.id}/level`, { method: "PUT", json: { level, clear } });
  switchLayer(l.id);
  Object.assign(l, { level: r.layer.level, locked: r.layer.locked });
  applyResult(r);
  syncSelectionToMode();
  l.visible = true;
  renderLayers();
  saveLayers();
  await reloadPage();
  toast(`${l.name}: ${LEVEL_INFO[level][0]}`);
}

async function setLocked(l, locked) {
  if (!locked) {
    switchLayer(l.id); // ends editing on the previously active layer
    l.visible = true;
  }
  l.locked = locked;
  syncSelectionToMode();
  renderLayers();
  saveLayers();
  renderReader();
  renderTools();
  renderInspector();
  updateProgress();
}

function applyLayerStyles() {
  const root = document.documentElement.style;
  for (const l of S.book.layers) {
    root.setProperty("--op-" + l.id, l.opacity);
    root.setProperty("--c-" + l.id, l.color);
  }
  const sl = layer("sentences");
  $("#reader").classList.toggle("show-sentences", !!sl?.visible);
}

let saveLayersT;
function saveLayers() {
  clearTimeout(saveLayersT);
  saveLayersT = setTimeout(() => api(`/api/books/${bid()}/layers`, {
    method: "PUT",
    json: { layers: S.book.layers.map(({ id, visible, opacity, locked, color, name, labels, style }) => ({ id, visible, opacity, locked, color, name, labels, style })) },
  }), 300);
}

/** Make `lid` the active layer. Leaving a layer ends its editing session:
 *  a fixed layer that was unlocked for editing is locked again, and the
 *  selection made with the old layer's tools is dropped. */
function switchLayer(lid) {
  const prev = activeLayer();
  if (prev && prev.id !== lid) {
    if (levelOf(prev) === "fixed" && !prev.locked && ready(prev)) {
      prev.locked = true;
      saveLayers();
      toast(`${prev.name}: editing finished, locked again`);
    }
    S.filter = "";
    S.bsel = null;
    S.tsel = null;
    S.tanchor = null;
    if (S.cur !== null) { S.sel = new Set([S.cur]); S.anchor = S.cur; }
  }
  S.activeLayer = lid;
  syncSelectionToMode();
}

/** Keep only the kind of selection the current mode works with:
 *  words for span/tag layers, a boundary for segmentation, sentences otherwise. */
function syncSelectionToMode() {
  const m = mode();
  const tokenLayer = ["token-span", "token-attr"].includes(activeLayer()?.kind);
  if (m === "span" || m === "attr") { if (!S.tsel) selectFirstTokenOfCursor(); }
  else if (!tokenLayer) { S.tsel = null; S.tanchor = null; } // read-only token layers keep a browsed item
  if (activeLayer()?.kind !== "segmentation") S.bsel = null;
}

function activate(lid) {
  switchLayer(lid);
  renderLayers();
  renderReader();
  renderTools();
  renderInspector();
  updateProgress();
}

$("#layers").addEventListener("click", (e) => {
  const li = e.target.closest(".layer");
  if (!li) return;
  const l = layer(li.dataset.layer);
  if (e.target.closest(".eye")) { l.visible = !l.visible; renderLayers(); saveLayers(); renderReader(); return; }
  if (e.target.closest(".lock")) return setLocked(l, !l.locked);
  activate(l.id);
});

let dragId = null;
$("#layers").addEventListener("dragstart", (e) => { dragId = e.target.closest(".layer")?.dataset.layer; });
$("#layers").addEventListener("dragover", (e) => e.preventDefault());
$("#layers").addEventListener("drop", (e) => {
  e.preventDefault();
  const target = e.target.closest(".layer")?.dataset.layer;
  if (!dragId || !target || dragId === target) return;
  const L = S.book.layers;
  const [moved] = L.splice(L.findIndex((l) => l.id === dragId), 1);
  L.splice(L.findIndex((l) => l.id === target), 0, moved);
  renderLayers();
  saveLayers();
  renderReader();
});

// poll while annotators run
let layerPoll = null;
async function pollLayers() {
  clearTimeout(layerPoll);
  if (!S.book) return;
  if (!S.book.layers.some((l) => l.status === "running")) return;
  layerPoll = setTimeout(async () => {
    const fresh = await api(`/api/books/${bid()}/layers`);
    const wasRunning = new Set(S.book.layers.filter((l) => l.status === "running").map((l) => l.id));
    for (const f of fresh) {
      const l = layer(f.id);
      if (l) Object.assign(l, { status: f.status, progress: f.progress, message: f.message, labels: f.labels });
      else S.book.layers.push(f);
    }
    renderLayers();
    const done = fresh.find((f) => wasRunning.has(f.id) && f.status !== "running");
    if (done) {
      toast(done.status === "ready" ? `Layer “${done.name}” is ready` : `Layer “${done.name}” failed`);
      await reloadPage();
    }
    pollLayers();
  }, 1000);
}

// ------------------------------------------------------------------ add layer
$("#add-layer-btn").addEventListener("click", async () => {
  const box = $("#add-layer");
  if (!box.hidden) { box.hidden = true; return; }
  const cat = await api("/api/annotators");
  box.hidden = false;
  const levelSelect = `<label>Annotation level <select name="level">
      <option value="fixed">Fixed – take the output as is</option>
      <option value="suggestion">Suggestion – review every sentence</option></select></label>`;
  box.innerHTML = `<div class="al-head"><b>Add layer</b><button class="x" id="al-close">×</button></div>` + cat.map((a) => `
    <div class="annot${a.available ? "" : " na"}" data-aid="${a.id}">
      <div class="an-name"><span class="dot" style="background:${a.color}"></span>${esc(a.name)}
        <small>${a.source === "auto" ? "automatic" : "manual"} · ${KIND_LABEL[a.kind]}</small></div>
      <div class="an-desc">${esc(a.description)}</div>
      ${a.available ? "" : `<div class="an-missing">Not installed (${esc(a.missing)}). Run:<code>${esc(a.install)}</code></div>`}
      ${a.available ? `<form class="an-form">${a.options.map((o) => o.type === "textarea"
        ? `<label>${esc(o.label)}${o.file ? ` <input type="file" accept=".txt,.tsv,.csv" data-fill="${o.id}">` : ""}</label><textarea name="${o.id}" rows="5" placeholder="river\nflood*\tWATER_MOVEMENT"></textarea>`
        : `<label>${esc(o.label)}<input name="${o.id}" value="${esc(o.default || "")}"></label>`).join("")}
        ${a.source === "auto" ? levelSelect : ""}
        <button class="btn primary small-btn">${a.source === "auto" ? "Run" : "Create"}</button></form>` : ""}
    </div>`).join("");
  $("#al-close").onclick = () => (box.hidden = true);
  for (const f of box.querySelectorAll("input[type=file][data-fill]")) {
    f.onchange = async () => { f.closest("form").querySelector(`textarea[name=${f.dataset.fill}]`).value = await f.files[0].text(); };
  }
  for (const form of box.querySelectorAll(".an-form")) {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const aid = form.closest(".annot").dataset.aid;
      const options = Object.fromEntries([...new FormData(form)].filter(([, v]) => typeof v === "string"));
      const l = await api(`/api/books/${bid()}/layers`, { method: "POST", json: { annotator: aid, options } });
      S.book.layers.splice(1, 0, l);
      box.hidden = true;
      activate(l.id);
      if (l.status === "running") pollLayers();
    };
  }
});

// ================================================================== TOOL PANELS
// The left panel shows the tools of the active layer.

function renderTools() {
  if (!S.book || !S.data) return;
  const l = activeLayer();
  const el = $("#tools");
  const keepFilter = document.activeElement?.id === "tool-filter";
  let body = "";
  if (!ready(l)) body = `<p class="hint">This layer is still being computed or failed.</p>`;
  else if (l.kind === "sentence-label") body = speechTools(l);
  else if (l.kind === "segmentation") body = segTools(l);
  else if (l.kind === "token-attr") body = attrTools(l);
  else if (l.kind === "token-span") body = spanTools(l);
  el.innerHTML = toolHead(l) + body;
  if (keepFilter) { const f = $("#tool-filter"); f.focus(); f.setSelectionRange(f.value.length, f.value.length); }
  highlightCharacters();
}

function toolHead(l) {
  const lv = levelOf(l);
  const done = layerDone(l);
  const prog = tracksReview(l) && ready(l)
    ? `<div class="th-prog"><div class="bar"><i style="width:${(100 * done) / S.book.n_sentences}%"></i></div>
       <span>${done.toLocaleString()} / ${S.book.n_sentences.toLocaleString()} ${l.id === "speech" ? "annotated" : lv === "manual" ? "done" : "reviewed"}</span></div>` : "";
  const banner = l.locked && ready(l)
    ? `<div class="banner">${lv === "fixed" ? "Fixed automatic layer – read only." : "Layer is locked."}
        <button class="btn small-btn inline" data-act="unlock">Enable editing</button></div>`
    : lv === "fixed" && ready(l)
      ? `<div class="banner edit">Correcting a fixed layer: changes are saved, but sentences are not tracked as reviewed
          (no <kbd>Enter</kbd> / <kbd>u</kbd>). Use level <b>Suggestion</b> to review sentence by sentence.
          <button class="btn small-btn inline" data-act="lock">Done editing</button></div>` : "";
  return `<div class="tool-head">
      <div class="th-name"><span class="dot" style="background:${l.color}"></span>${esc(l.name)}
        <span class="badge lv-${lv}" title="${esc(LEVEL_INFO[lv][1])}">${LEVEL_INFO[lv][0]}</span></div>
      ${prog}</div>${banner}`;
}

function reviewSection(l) {
  if (!tracksReview(l) || l.id === "speech" || l.locked) return "";
  const manual = levelOf(l) === "manual";
  return `<section class="tool-sec"><h2>${manual ? "Progress" : "Review"}</h2>
    <div class="actions">
      <button class="btn small-btn primary" data-act="confirm">${manual ? "Mark sentence done" : "Confirm sentence"} <kbd>Enter</kbd></button>
      <button class="btn small-btn" data-act="confirm-page">${manual ? "Mark page done" : "Confirm whole page"}</button>
      <div class="btnpair"><button class="btn small-btn" data-act="prev-unrev" title="Previous ${manual ? "open" : "unreviewed"} sentence">‹ Previous <kbd>U</kbd></button>
      <button class="btn small-btn" data-act="next-unrev" title="Next ${manual ? "open" : "unreviewed"} sentence">Next › <kbd>u</kbd></button></div>
    </div></section>`;
}

function helpSection(rows) {
  return `<details class="help"><summary>Keyboard</summary><dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></details>`;
}
/** Enter / u / U only exist on layers with per-sentence review (suggestion, manual). */
function reviewKeys(l) {
  if (!tracksReview(l)) return [];
  const manual = levelOf(l) === "manual";
  return [["<kbd>Enter</kbd>", manual ? "mark sentence done" : "confirm sentence"],
    ["<kbd>u</kbd> / <kbd>U</kbd>", `next / previous ${manual ? "open" : "unreviewed"}`]];
}
const K_TAB = [["<kbd>Tab</kbd> / <kbd>⇧Tab</kbd>", "next / previous annotation (filter limits labels)"]];
const K_NAV = [["<kbd>↑</kbd><kbd>↓</kbd> <kbd>j</kbd><kbd>k</kbd>", "sentence"], ["<kbd>[</kbd> <kbd>]</kbd>", "page"], ["<kbd>⌘Z</kbd>", "undo"]];

// ---------- speech
function speechTools(l) {
  const suggestions = levelOf(l) === "suggestion";
  return `
  <section class="tool-sec">
    <h2 title="Select sentences, then press the speaker's key or click the name">Speakers</h2>
    <ul class="characters">${S.book.characters.map((c, i) => `
      <li class="char" data-cid="${c.id}" style="--c:${c.color}" title="Click: toggle on selected sentences · double-click: edit">
        <span class="key">${i <= 9 ? i : ""}</span>
        <span class="sw"><input type="color" value="${c.color}" data-color="${c.id}"></span>
        <span class="nm">${esc(c.name)}${c.aliases?.length ? `<small>${esc(c.aliases.join(", "))}</small>` : ""}</span>
        ${c.id !== NARRATOR ? `<button class="x" data-delchar="${c.id}" title="Delete">×</button>` : ""}
      </li>`).join("")}</ul>
    <form id="add-char" class="add-char"><input id="new-char" placeholder="Add character…" autocomplete="off"></form>
    <button class="btn ghost small-btn" data-act="find-cands">Suggest characters from text</button>
    ${S.candidates ? `<ul class="candidates">${S.candidates.length
      ? S.candidates.map((c) => `<li data-cand="${esc(c.name)}" title="Add as character"><b>${esc(c.name)}</b><span>${c.count}×</span></li>`).join("")
      : `<li class="muted">No PERSON entities found</li>`}</ul>` : ""}
  </section>
  <section class="tool-sec"><h2>Annotate</h2>${browseHtml(l)}<div class="actions">
    ${suggestions ? `<button class="btn small-btn" data-act="accept-narr" title="Mark every unannotated sentence without quotation as narrator">Accept narrator suggestions</button>
    <button class="btn small-btn" data-act="accept-all">Accept all suggestions</button>` : ""}
    <button class="btn small-btn" data-act="repeat" title="Give the selected sentences the speakers of the previous annotated sentence">Same as previous <kbd>p</kbd></button>
    <div class="btnpair"><button class="btn small-btn" data-act="prev-unann" title="Previous unannotated sentence">‹ Previous <kbd>U</kbd></button>
    <button class="btn small-btn" data-act="next-unann" title="Next unannotated sentence">Next › <kbd>u</kbd></button></div>
  </div></section>
  ${helpSection([...K_NAV.slice(0, 1), ["<kbd>0</kbd>–<kbd>9</kbd>", "toggle speaker"],
    ...(suggestions ? [["<kbd>Enter</kbd>", "accept suggestion, next"]] : [["<kbd>Enter</kbd>", "next sentence"]]),
    ["<kbd>p</kbd>", "same speaker as previous"], ["<kbd>⌫</kbd>", "clear speakers"],
    ["<kbd>u</kbd> / <kbd>U</kbd>", "next / previous unannotated"], ["<kbd>Tab</kbd> / <kbd>⇧Tab</kbd>", "next / previous direct speech"], ...K_NAV.slice(1)])}`;
}

function highlightCharacters() {
  const s = S.sents.get(S.cur);
  const on = new Set(s?.speakers || []);
  for (const li of document.querySelectorAll(".char")) li.classList.toggle("on", on.has(li.dataset.cid));
}

// ---------- segmentation
function segTools(l) {
  const s = S.sents.get(S.bsel ?? S.cur);
  const boundary = S.segTool === "move" && S.bsel !== null
    ? `<div class="selinfo">Boundary before sentence <b>#${S.bsel}</b>
        <div class="btnrow"><button class="btn small-btn inline" data-act="bmove" data-d="-1">← earlier</button>
        <button class="btn small-btn inline" data-act="bmove" data-d="1">later →</button></div></div>`
    : S.segTool === "move" ? `<p class="hint">Click a word: the nearest sentence boundary moves in front of it. Then fine-tune with <kbd>←</kbd><kbd>→</kbd>.</p>`
      : `<p class="hint">Click a word to <b>start a new sentence</b> there. Click the first word of a sentence to <b>merge</b> it with the previous one.</p>`;
  const issues = S.issues;
  return `
  <section class="tool-sec"><h2>Tool</h2>${browseHtml(l)}
    <div class="seg-tools">
      <button class="tool${S.segTool === "split" ? " on" : ""}" data-act="segtool" data-tool="split"><b>✂ Split / merge</b><small>key s</small></button>
      <button class="tool${S.segTool === "move" ? " on" : ""}" data-act="segtool" data-tool="move"><b>↔ Move boundary</b><small>key m</small></button>
    </div>
    ${l.locked ? "" : boundary}
    ${s ? `<p class="muted small">Sentence #${s.id}: ${s.tokens.length} tokens${s.issue ? ` · <span class="warn">⚠ ${esc(s.issue)}</span>` : ""}</p>` : ""}
  </section>
  ${reviewSection(l)}
  <section class="tool-sec"><h2>Check boundaries</h2>
    <p class="hint">Sentences that look mis-segmented (very long, lowercase start, no final punctuation…). <kbd>i</kbd> jumps to the next one.</p>
    <button class="btn small-btn" data-act="issues">${issues ? "Refresh list" : "Find suspicious sentences"}</button>
    ${issues ? `<ul class="issue-list">${issues.length ? issues.map((x) => `
      <li data-jump="${x.sentence}" data-page="${x.page}" class="${x.reviewed ? "done" : ""}${x.sentence === S.cur ? " here" : ""}">
        <span class="ir">${x.reviewed ? "✓ " : ""}#${x.sentence} · ${esc(x.reason)}</span><span class="it">${esc(x.text)}</span></li>`).join("")
      : `<li class="muted">Nothing suspicious found.</li>`}</ul>` : ""}
  </section>
  ${helpSection([["click", S.segTool === "move" ? "move nearest boundary here" : "split / merge"], ["<kbd>s</kbd> <kbd>m</kbd>", "split / move tool"],
    ["<kbd>←</kbd><kbd>→</kbd>", "move selected boundary"], ...reviewKeys(l), ...K_TAB,
    ["<kbd>i</kbd>", "next suspicious sentence"], ...K_NAV])}`;
}

// ---------- shared: labels for token layers
function labelDesc(l, label) {
  if (l.id === "pos") return UPOS_DESC[label] || "";
  if (l.id === "entities") return ENT_DESC[label] || "";
  if (l.annotator === "usas") return USAS_FIELDS[label?.[0]] || "";
  return "";
}

function pageCounts(l) {
  const cnt = new Map();
  for (const p of S.data.paragraphs) for (const s of p.sentences) for (const t of s.tokens) {
    if (l.kind === "token-attr") { const v = t.a[l.id]; if (v) cnt.set(v, (cnt.get(v) || 0) + 1); }
    else { const x = t.s[l.id]; if (x && x[2]) cnt.set(x[1], (cnt.get(x[1]) || 0) + 1); }
  }
  return cnt;
}

function paletteLabels(l) {
  const labels = [...(l.labels || [])];
  for (const v of pageCounts(l).keys()) if (!labels.includes(v)) labels.push(v);
  return labels;
}

function paletteHtml(l, current) {
  const cnt = pageCounts(l);
  const base = l.labels || [];
  const f = S.filter.toLowerCase();
  const labels = paletteLabels(l).filter((x) => !f || x.toLowerCase().includes(f) || labelDesc(l, x).toLowerCase().includes(f));
  const showFilter = paletteLabels(l).length > 12;
  return `${showFilter ? `<input id="tool-filter" class="filter" placeholder="Filter ${paletteLabels(l).length} labels…" value="${esc(S.filter)}" autocomplete="off">` : ""}
    <ul class="palette">${labels.map((x) => {
      const k = base.indexOf(x);
      return `<li class="pal${x === current ? " on" : ""}" data-label="${esc(x)}" title="${esc(labelDesc(l, x))}">
        <span class="pk">${k >= 0 && k < 9 ? k + 1 : ""}</span>
        <span class="pc" style="background:${l.kind === "token-span" ? spanColor(l, x) : l.color}"></span>
        <span class="pn">${esc(x)}<small>${esc(labelDesc(l, x))}</small></span>
        <span class="pcount">${cnt.get(x) || ""}</span></li>`;
    }).join("")}</ul>`;
}

function selectedTokens() {
  if (!S.tsel) return [];
  const out = [];
  for (let i = S.tsel.a; i <= S.tsel.b; i++) { const x = S.toks.get(i); if (x) out.push(x.tok); }
  return out;
}

function tselText() {
  const s = selectedTokens().map((t) => t.t).join(" ");
  return s.length > 50 ? s.slice(0, 50) + "…" : s;
}

// ---------- token attributes (POS, USAS, own tags)
function attrTools(l) {
  const toks = selectedTokens();
  const t = toks[0];
  const cur = t ? t.a[l.id] || "" : "";
  let sel = `<p class="hint">Click a word (shift-click to extend).</p>`;
  if (t) {
    const alts = (t.d[l.id] || "").split(/\s+/).filter((x) => x && !x.startsWith("[") && x !== cur);
    sel = `<div class="selinfo">
      <div class="sel-word">${esc(tselText())}</div>
      ${toks.length === 1 ? `<div class="muted small">lemma <b>${esc(t.lemma)}</b> · tag ${esc(t.tag)}</div>
        <div>current: <b class="curval">${esc(cur || "—")}</b> <span class="muted small">${esc(labelDesc(l, cur))}</span></div>
        ${alts.length ? `<div class="alts">alternatives: ${alts.map((x) => `<button class="lab" data-label="${esc(x)}" title="${esc(labelDesc(l, x))}">${esc(x)}</button>`).join("")}</div>` : ""}`
        : `<div class="muted small">${toks.length} words</div>`}
    </div>`;
  }
  return `
  <section class="tool-sec"><h2>Selection</h2>${sel}${browseHtml(l)}
    ${l.locked ? "" : `<form id="attr-form" class="inline-form"><input id="attr-val" placeholder="type a value… (/)" autocomplete="off">
      <button class="btn small-btn inline" ${t ? "" : "disabled"}>Set</button></form>
      <button class="btn ghost small-btn" data-act="clear" ${t ? "" : "disabled"}>Clear value <kbd>⌫</kbd></button>
      <button class="btn ghost small-btn" data-act="repeat" ${t && S.lastLabels[l.id] !== undefined ? "" : "disabled"}>Repeat last value${S.lastLabels[l.id] ? ` “${esc(S.lastLabels[l.id])}”` : ""} <kbd>p</kbd></button>`}
  </section>
  <section class="tool-sec"><h2>${l.id === "pos" ? "Tagset (UPOS)" : "Values"}</h2>${paletteHtml(l, cur)}</section>
  ${reviewSection(l)}
  ${helpSection([["<kbd>←</kbd><kbd>→</kbd>", "word (<kbd>shift</kbd> extends)"], ["<kbd>1</kbd>–<kbd>9</kbd>", "set value"],
    ["<kbd>/</kbd>", "type a value"], ["<kbd>⌫</kbd>", "clear value"], ["<kbd>p</kbd>", "repeat last label"], ...reviewKeys(l), ...K_TAB, ...K_NAV])}`;
}

// ---------- token spans (entities, quotes, lexicon, own spans)
function pageSpans(l) {
  const out = [];
  let cur = null;
  for (const p of S.data.paragraphs) for (const s of p.sentences) for (const t of s.tokens) {
    const x = t.s[l.id];
    if (x && x[2]) { cur = { label: x[1], a: t.i, b: t.i, text: t.t }; out.push(cur); }
    else if (x && cur && x[0] === S.toks.get(cur.a).tok.s[l.id]?.[0]) { cur.b = t.i; cur.text += " " + t.t; }
    if (x && x[3]) cur = null;
  }
  return out;
}

function spanTools(l) {
  const toks = selectedTokens();
  const curSpan = toks.map((t) => t.s[l.id]).find(Boolean);
  const current = curSpan?.[1];
  const sel = toks.length
    ? `<div class="selinfo"><div class="sel-word">${esc(tselText())}</div>
        <div>span: <b class="curval">${current ? esc(current) : "—"}</b> <span class="muted small">${esc(labelDesc(l, current))}</span></div></div>`
    : `<p class="hint">Click a word, shift-click to extend the selection.</p>`;
  const spans = pageSpans(l);
  const groups = new Map();
  for (const sp of spans) { if (!groups.has(sp.label)) groups.set(sp.label, []); groups.get(sp.label).push(sp); }
  return `
  <section class="tool-sec"><h2>Selection</h2>${sel}${browseHtml(l)}
    ${l.locked ? "" : `<form id="lab-form" class="inline-form"><input id="lab-new" placeholder="new label… (/)" autocomplete="off">
      <button class="btn small-btn inline" ${toks.length ? "" : "disabled"}>Set</button></form>
      <button class="btn ghost small-btn" data-act="clear" ${curSpan ? "" : "disabled"}>Remove span <kbd>⌫</kbd></button>
      <button class="btn ghost small-btn" data-act="repeat" ${toks.length && S.lastLabels[l.id] ? "" : "disabled"}>Repeat last label${S.lastLabels[l.id] ? ` “${esc(S.lastLabels[l.id])}”` : ""} <kbd>p</kbd></button>`}
  </section>
  <section class="tool-sec"><h2>Labels</h2>${paletteHtml(l, current)}</section>
  ${reviewSection(l)}
  <section class="tool-sec"><h2>On this page (${spans.length})</h2>
    <div class="page-spans">${[...groups].map(([lab, list]) => `
      <div class="ps-group"><span class="ps-lab" style="color:${spanColor(l, lab)}">${esc(lab)}</span>
        ${list.map((sp) => `<button class="ps" data-span="${sp.a}:${sp.b}">${esc(sp.text)}</button>`).join("")}</div>`).join("")
      || `<p class="muted small">No spans on this page.</p>`}</div>
  </section>
  ${helpSection([["<kbd>←</kbd><kbd>→</kbd>", "word (<kbd>shift</kbd> extends)"], ["<kbd>1</kbd>–<kbd>9</kbd>", "label selection"],
    ["<kbd>/</kbd>", "type a new label"], ["<kbd>⌫</kbd>", "remove span"], ["<kbd>p</kbd>", "repeat last label"], ...reviewKeys(l), ...K_TAB, ...K_NAV])}`;
}

// ---------- tool panel events
const tools = $("#tools");
tools.addEventListener("click", async (e) => {
  const act = e.target.closest("[data-act]");
  if (act) {
    const a = act.dataset.act;
    const l = activeLayer();
    if (a === "unlock") return setLocked(l, false);
    if (a === "lock") return setLocked(l, true);
    if (a === "confirm") return confirmSentences();
    if (a === "confirm-page") return confirmPage();
    if (a === "next-item") return nextItem(1);
    if (a === "prev-item") return nextItem(-1);
    if (a === "next-unrev") return nextUnreviewed(1);
    if (a === "prev-unrev") return nextUnreviewed(-1);
    if (a === "repeat") return repeatPrevious();
    if (a === "accept-narr") return acceptPage(true);
    if (a === "accept-all") return acceptPage(false);
    if (a === "next-unann") return nextUnannotated(1);
    if (a === "prev-unann") return nextUnannotated(-1);
    if (a === "clear") return clearTokens();
    if (a === "segtool") { S.segTool = act.dataset.tool; S.bsel = null; renderReader(); renderTools(); return; }
    if (a === "bmove") return nudgeBoundary(Number(act.dataset.d));
    if (a === "issues") { S.issues = await api(`/api/books/${bid()}/layers/sentences/issues`); renderTools(); return; }
    if (a === "find-cands") { S.candidates = await api(`/api/books/${bid()}/character-candidates`); renderTools(); return; }
  }
  const lab = e.target.closest("[data-label]");
  if (lab) return applyLabel(lab.dataset.label);
  const ps = e.target.closest("[data-span]");
  if (ps) {
    const [a, b] = ps.dataset.span.split(":").map(Number);
    S.tanchor = a;
    S.tsel = { a, b };
    S.cur = S.toks.get(a).sid;
    S.sel = new Set([S.cur]);
    refreshSelectionClasses();
    scrollToCursor();
    return;
  }
  const jump = e.target.closest("[data-jump]");
  if (jump) return jumpTo(Number(jump.dataset.jump), Number(jump.dataset.page));
  const cand = e.target.closest("[data-cand]");
  if (cand) { S.candidates = S.candidates.filter((c) => c.name !== cand.dataset.cand); return addCharacter(cand.dataset.cand); }
  if (e.target.closest("input")) return;
  const del = e.target.closest("[data-delchar]");
  if (del) {
    const c = charById(del.dataset.delchar);
    if (!confirm(`Delete ${c.name}? Their speaker labels will be removed.`)) return;
    await api(`/api/books/${bid()}/characters/${c.id}`, { method: "DELETE" });
    S.book.characters = S.book.characters.filter((x) => x.id !== c.id);
    await reloadPage();
    return;
  }
  const li = e.target.closest(".char");
  if (li && e.detail === 1) toggleSpeaker(li.dataset.cid);
});

tools.addEventListener("dblclick", async (e) => {
  const li = e.target.closest(".char");
  if (!li) return;
  const c = charById(li.dataset.cid);
  const name = prompt("Name", c.name);
  if (name === null) return;
  const aliases = prompt("Aliases used in attributions (comma-separated, e.g. “Mr. Pocket, Herbert”)", (c.aliases || []).join(", "));
  if (aliases === null) return;
  const upd = await api(`/api/books/${bid()}/characters/${c.id}`, {
    method: "PATCH", json: { name, aliases: aliases.split(","), color: c.color },
  });
  Object.assign(c, upd);
  await reloadPage();
});

tools.addEventListener("change", async (e) => {
  const id = e.target.dataset.color;
  if (!id) return;
  const c = charById(id);
  c.color = e.target.value;
  await api(`/api/books/${bid()}/characters/${id}`, { method: "PATCH", json: { name: c.name, aliases: c.aliases || [], color: c.color } });
  renderReader();
  renderTools();
});

tools.addEventListener("input", (e) => {
  if (e.target.id === "tool-filter") { S.filter = e.target.value; renderTools(); }
});

tools.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (e.target.id === "add-char") {
    const v = $("#new-char").value.trim();
    if (v) await addCharacter(v);
  } else if (e.target.id === "lab-form") {
    const v = $("#lab-new").value.trim();
    if (v) await applyLabel(v);
  } else if (e.target.id === "attr-form") {
    await applyLabel($("#attr-val").value.trim());
  }
});

async function jumpTo(sid, page) {
  if (page === S.page) { selectSentence(sid); return; }
  gotoPage(page, sid); // gotoPage sets the cursor target
}

// ------------------------------------------------------------------ speech editing
async function addCharacter(name, aliases = []) {
  const c = await api(`/api/books/${bid()}/characters`, { method: "POST", json: { name, aliases } });
  S.book.characters.push(c);
  await reloadPage();
  return c;
}

function speechEditable() {
  const l = layer("speech");
  if (l.locked) { toast("Speech layer is locked – unlock it in the Layers panel"); return false; }
  if (!l.visible) { l.visible = true; renderLayers(); saveLayers(); }
  return true;
}

async function saveSpeech(updates) {
  applyResult(await api(`/api/books/${bid()}/speech`, { method: "PUT", json: { updates } }));
  await reloadPage();
}

function toggleSpeaker(cid) {
  if (!S.sel.size || !speechEditable()) return;
  const sids = [...S.sel];
  const all = sids.every((sid) => (S.sents.get(sid).speakers || []).includes(cid));
  const updates = {};
  for (const sid of sids) {
    const cur = S.sents.get(sid).speakers || [];
    updates[sid] = all ? cur.filter((x) => x !== cid) : cur.includes(cid) ? cur : [...cur, cid];
  }
  saveSpeech(updates);
}

async function acceptSelection() {
  if (!speechEditable()) return;
  const updates = {};
  if (levelOf(layer("speech")) === "suggestion") {
    for (const sid of S.sel) {
      const s = S.sents.get(sid);
      if (!s.speakers && s.suggestion.speakers.length) updates[sid] = s.suggestion.speakers;
    }
  }
  if (Object.keys(updates).length) await saveSpeech(updates);
  moveSentence(1, false);
}

function clearSpeakers() {
  if (!speechEditable()) return;
  const updates = {};
  for (const sid of S.sel) updates[sid] = [];
  saveSpeech(updates);
}

async function acceptPage(onlyNarrator) {
  if (!speechEditable()) return;
  const r = await api(`/api/books/${bid()}/speech/accept`, { method: "POST", json: { page: S.page, only_narrator: onlyNarrator } });
  applyResult(r);
  await reloadPage();
  toast(`Accepted ${r.accepted} suggestion${r.accepted === 1 ? "" : "s"}`);
}

async function nextUnannotated(dir = 1) {
  const r = await api(`/api/books/${bid()}/next-unannotated?after=${S.cur ?? -1}&direction=${dir}`);
  if (r.sentence === null) return toast("Everything is annotated 🎉");
  jumpTo(r.sentence, r.page);
}

/** p: apply the previous annotation again – the speakers of the nearest annotated
 *  sentence before the selection, or the last label/value used on a token layer. */
async function repeatPrevious() {
  const m = mode();
  if (m === "speech") {
    if (!speechEditable()) return;
    const first = Math.min(...[...S.sel].map((sid) => S.order.indexOf(sid)));
    let prev = null;
    for (let i = first - 1; i >= 0; i--) {
      const s = S.sents.get(S.order[i]);
      if (s.speakers && s.speakers.length) { prev = s; break; }
    }
    if (!prev) return toast("No annotated sentence before this one on the page");
    const updates = {};
    for (const sid of S.sel) updates[sid] = [...prev.speakers];
    await saveSpeech(updates);
    toast(`Speaker: ${prev.speakers.map((id) => charById(id)?.name || id).join(" + ")}`);
    return;
  }
  if (m === "span" || m === "attr") {
    const last = S.lastLabels[activeLayer().id];
    if (last === undefined) return toast("No previous label on this layer yet");
    return applyLabel(last);
  }
  toast(activeLayer().locked ? "Layer is locked" : "Nothing to repeat on this layer");
}

// ------------------------------------------------------------------ item navigation (Tab)
const ITEM_NOUN = { entities: ["entity", "entities"], quotes: ["quotation", "quotations"] };
function itemNoun(l, plural = false) {
  const n = ITEM_NOUN[l.id] || ({
    "token-span": ["span", "spans"], "token-attr": ["tagged word", "tagged words"],
    "sentence-label": ["direct speech", "direct speech"], segmentation: ["boundary", "boundaries"],
  })[l.kind] || ["item", "items"];
  return plural ? n[1] : n[0];
}

function applyItemSelection(r) {
  const l = activeLayer();
  S.cur = r.sentence;
  S.sel = new Set([r.sentence]);
  S.anchor = r.sentence;
  if (l.kind === "token-span" || l.kind === "token-attr") {
    S.tsel = { a: r.start, b: r.end - 1 };
    S.tanchor = r.start;
  } else if (l.kind === "segmentation") {
    S.bsel = r.sentence;
  }
}

/** Tab / Shift+Tab: next / previous annotation of the active layer (book-wide).
 *  The panel's label filter restricts which labels are visited. */
async function nextItem(dir = 1) {
  const l = activeLayer();
  if (!ready(l)) return;
  const s = S.sents.get(S.cur);
  const afterTok = S.tsel ? S.tsel.a : s ? (dir > 0 ? s.start - 1 : s.start) : -1;
  const q = new URLSearchParams({ after_tok: afterTok, after_sent: S.cur ?? -1, direction: dir, label: S.filter || "" });
  const r = await api(`/api/books/${bid()}/layers/${l.id}/next-item?${q}`);
  if (!r.found) {
    return toast(`No ${S.filter ? `“${S.filter}” ` : ""}${itemNoun(l, true)} found${levelOf(l) === "suggestion" ? " in unconfirmed sentences" : ""}`);
  }
  if (r.wrapped) toast(dir > 0 ? "Reached the end – continued from the beginning" : "Reached the beginning – continued from the end");
  if (r.page !== S.page) {
    S.pendingItem = r;
    gotoPage(r.page, r.sentence);
    return;
  }
  applyItemSelection(r);
  refreshSelectionClasses();
  scrollToCursor();
}

function browseHtml(l) {
  const filt = S.filter && (l.kind === "token-span" || l.kind === "token-attr") ? ` “${esc(S.filter)}”` : "";
  return `<div class="btnpair browse">
      <button class="btn small-btn" data-act="prev-item" title="Previous ${itemNoun(l)}${filt} (Shift+Tab)">‹ <kbd>⇧Tab</kbd></button>
      <button class="btn small-btn" data-act="next-item" title="Next ${itemNoun(l)}${filt} (Tab)">Next ${itemNoun(l)}${filt} <kbd>Tab</kbd></button>
    </div>`;
}

// ------------------------------------------------------------------ review
async function review(sids, reviewed = true) {
  const l = activeLayer();
  if (l.locked) return toast("Layer is locked");
  const r = await api(`/api/books/${bid()}/layers/${l.id}/review`, { method: "POST", json: { sentences: sids, reviewed } });
  applyResult(r);
  renderLayers();
}

async function confirmSentences() {
  const l = activeLayer();
  if (!tracksReview(l) || l.id === "speech") return;
  await review([...S.sel]);
  const last = Math.max(...[...S.sel].map((s) => S.order.indexOf(s)));
  const nxt = S.order[last + 1];
  if (nxt === undefined) { await reloadPage(); return gotoPage(S.page + 1); }
  S.sel = new Set([nxt]);
  S.cur = S.anchor = nxt;
  if (mode() === "span" || mode() === "attr") selectFirstTokenOfCursor();
  await reloadPage();
  scrollToCursor();
}

async function confirmPage() {
  const l = activeLayer();
  const r = await api(`/api/books/${bid()}/layers/${l.id}/review`, { method: "POST", json: { page: S.page } });
  applyResult(r);
  renderLayers();
  await reloadPage();
  toast(`${r.changed} sentence${r.changed === 1 ? "" : "s"} ${levelOf(l) === "manual" ? "marked done" : "confirmed"}`);
}

async function nextUnreviewed(dir = 1) {
  const l = activeLayer();
  if (l.id === "speech") return nextUnannotated(dir);
  const r = await api(`/api/books/${bid()}/layers/${l.id}/next-unreviewed?after=${S.cur ?? -1}&direction=${dir}`);
  if (r.sentence === null) return toast("All sentences are reviewed 🎉");
  jumpTo(r.sentence, r.page);
}

// ------------------------------------------------------------------ token-layer editing
async function applyLabel(label) {
  const a = activeLayer();
  if (mode() !== "span" && mode() !== "attr") return toast(a.locked ? "Layer is locked – enable editing first" : "Not editable");
  if (!S.tsel) return toast("Select words first");
  if (label) S.lastLabels[a.id] = label;
  if (a.kind === "token-span") {
    if (!label) return;
    const r = await api(`/api/books/${bid()}/layers/${a.id}/spans`, {
      method: "POST", json: { op: "set", start: S.tsel.a, end: S.tsel.b + 1, label },
    });
    if (!(a.labels || []).includes(label)) a.labels = [...(a.labels || []), label];
    applyResult(r);
  } else {
    const tokens = [];
    for (let i = S.tsel.a; i <= S.tsel.b; i++) tokens.push(i);
    const r = await api(`/api/books/${bid()}/layers/${a.id}/attrs`, { method: "POST", json: { tokens, value: label } });
    if (label && !(a.labels || []).includes(label)) a.labels = [...(a.labels || []), label];
    applyResult(r);
  }
  markEdited(a);
  await reloadPage();
}

async function clearTokens() {
  const a = activeLayer();
  if (!S.tsel || (mode() !== "span" && mode() !== "attr")) return;
  if (a.kind === "token-span") {
    applyResult(await api(`/api/books/${bid()}/layers/${a.id}/spans`, {
      method: "POST", json: { op: "clear", start: S.tsel.a, end: S.tsel.b + 1 },
    }));
    markEdited(a);
    await reloadPage();
  } else await applyLabel("");
}

function markEdited(a) {
  if (a.source === "auto" && !a.edited) { a.edited = true; renderLayers(); }
}

// ------------------------------------------------------------------ segmentation editing
async function segEdit(body, msg) {
  const r = await api(`/api/books/${bid()}/layers/sentences/edit`, { method: "POST", json: body });
  applyResult(r);
  markEdited(layer("sentences"));
  S.cur = r.sentence;
  S.sel = new Set([r.sentence]);
  if (body.op === "move") S.bsel = r.sentence;
  await reloadPage();
  if (msg) toast(msg);
}

function segmentClick(tokIdx) {
  const { sid } = S.toks.get(tokIdx);
  const s = S.sents.get(sid);
  if (S.segTool === "split") {
    if (tokIdx === s.start) return segEdit({ op: "merge", sentence: sid }, "Sentences merged");
    return segEdit({ op: "split", token: tokIdx }, "Sentence split");
  }
  // move tool: move the nearest boundary of this sentence to the clicked word
  const para = S.paraOf.get(sid);
  const prevSame = S.sents.has(sid - 1) && S.paraOf.get(sid - 1) === para;
  const nextSame = S.sents.has(sid + 1) && S.paraOf.get(sid + 1) === para;
  if (tokIdx === s.start) { S.bsel = prevSame ? sid : null; refreshSelectionClasses(); return; }
  const toStart = tokIdx - s.start, toEnd = s.end - tokIdx;
  if (prevSame && (toStart <= toEnd || !nextSame)) return segEdit({ op: "move", sentence: sid, token: tokIdx });
  if (nextSame) return segEdit({ op: "move", sentence: sid + 1, token: tokIdx });
  toast("This sentence has no movable boundary (paragraph edges are fixed)");
}

function nudgeBoundary(d) {
  if (S.bsel === null) return toast("Select a boundary first: click a sentence's first word");
  const s = S.sents.get(S.bsel);
  segEdit({ op: "move", sentence: S.bsel, token: s.start + d });
}

async function nextIssue() {
  if (!S.issues) S.issues = await api(`/api/books/${bid()}/layers/sentences/issues`);
  const nxt = S.issues.find((x) => x.sentence > (S.cur ?? -1) && !x.reviewed) || S.issues.find((x) => !x.reviewed);
  if (!nxt) return toast("No open suspicious sentences");
  jumpTo(nxt.sentence, nxt.page);
}

async function undo() {
  const r = await api(`/api/books/${bid()}/undo`, { method: "POST" });
  applyResult(r);
  if (r.layer && r.undone?.startsWith("level")) {
    const fresh = await api(`/api/books/${bid()}/layers`);
    for (const f of fresh) { const l = layer(f.id); if (l) Object.assign(l, { level: f.level, locked: f.locked }); }
    syncSelectionToMode();
  }
  renderLayers();
  await reloadPage();
  toast(`Undone: ${r.undone}`);
}

// ------------------------------------------------------------------ selection
function selectSentence(sid, extend = false) {
  if (extend && S.anchor !== null) {
    const a = S.order.indexOf(S.anchor), b = S.order.indexOf(sid);
    S.sel = new Set(S.order.slice(Math.min(a, b), Math.max(a, b) + 1));
  } else {
    S.sel = new Set([sid]);
    S.anchor = sid;
  }
  S.cur = sid;
  if (mode() === "span" || mode() === "attr") selectFirstTokenOfCursor();
  refreshSelectionClasses();
  scrollToCursor();
}

/** Word selection when entering a sentence: on span layers the first span in it
 *  (the first thing to check), otherwise the first word. */
function selectFirstTokenOfCursor() {
  const s = S.sents.get(S.cur);
  if (!s) return;
  const l = activeLayer();
  if (l.kind === "token-span") {
    const first = s.tokens.find((t) => t.s[l.id]);
    if (first) {
      const id = first.s[l.id][0];
      const span = s.tokens.filter((t) => t.s[l.id]?.[0] === id);
      S.tsel = { a: span[0].i, b: span[span.length - 1].i };
      S.tanchor = span[0].i;
      return;
    }
  }
  S.tsel = { a: s.start, b: s.start };
  S.tanchor = s.start;
}

function selectToken(i, extend = false) {
  if (extend && S.tanchor !== null && S.toks.has(S.tanchor)) S.tsel = { a: Math.min(S.tanchor, i), b: Math.max(S.tanchor, i) };
  else { S.tsel = { a: i, b: i }; S.tanchor = i; }
  const sid = S.toks.get(i).sid;
  S.cur = sid;
  S.sel = new Set([sid]);
  refreshSelectionClasses();
  scrollToCursor();
}

function moveToken(d, extend) {
  if (!S.tsel) return selectFirstTokenOfCursor();
  const head = extend ? (S.tsel.a === S.tanchor ? S.tsel.b : S.tsel.a) : d > 0 ? S.tsel.b : S.tsel.a;
  const j = head + d;
  if (!S.toks.has(j)) return;
  if (extend) {
    S.tsel = { a: Math.min(S.tanchor, j), b: Math.max(S.tanchor, j) };
    S.cur = S.toks.get(j).sid;
    refreshSelectionClasses();
    scrollToCursor();
  } else selectToken(j);
}

function moveSentence(d, extend) {
  const i = S.order.indexOf(S.cur);
  const j = i + d;
  if (j < 0) return gotoPage(S.page - 1, "last");
  if (j >= S.order.length) return gotoPage(S.page + 1, "first");
  selectSentence(S.order[j], extend);
}

/** The word under the pointer; for clicks on inter-word spaces, the following word. */
function tokenFromEvent(e) {
  const direct = e.target.closest(".tk");
  if (direct) return direct;
  const r = document.caretRangeFromPoint?.(e.clientX, e.clientY);
  const node = r?.startContainer;
  if (!node) return null;
  const el = node.nodeType === 3 ? node.parentElement : node;
  const tk = el.closest(".tk");
  if (tk) return tk;
  let sib = node.nodeType === 3 ? node.nextSibling : null;
  while (sib && !(sib.classList && sib.classList.contains("tk"))) sib = sib.nextSibling;
  return sib || null;
}

$("#reader").addEventListener("click", (e) => {
  const rv = e.target.closest("[data-rv]");
  if (rv) {
    const sid = Number(rv.dataset.rv);
    return review([sid], !rv.classList.contains("ok")).then(reloadPage);
  }
  const m = mode();
  const tk = m === "view" || m === "speech" ? null : tokenFromEvent(e);
  if (m === "seg" && tk) return segmentClick(Number(tk.dataset.i));
  if ((m === "span" || m === "attr") && tk) return selectToken(Number(tk.dataset.i), e.shiftKey);
  const el = e.target.closest(".s");
  if (!el) return;
  const sid = Number(el.dataset.sid);
  if (e.metaKey || e.ctrlKey) {
    S.sel.has(sid) ? S.sel.delete(sid) : S.sel.add(sid);
    S.cur = sid;
    refreshSelectionClasses();
  } else selectSentence(sid, e.shiftKey);
});

document.addEventListener("keydown", (e) => {
  if (!S.book || $("#workspace").hidden) return;
  if (e.key === "Tab" && !e.metaKey && !e.ctrlKey && !e.altKey
      && (e.target.id === "tool-filter" || !e.target.closest("input, textarea, select, button, a"))) {
    e.preventDefault();
    if (e.target.id === "tool-filter") e.target.blur();
    return nextItem(e.shiftKey ? -1 : 1);
  }
  if (e.target.closest("input, textarea, select")) {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  const k = e.key;
  const m = mode();
  const l = activeLayer();
  if ((e.metaKey || e.ctrlKey) && k.toLowerCase() === "z") { e.preventDefault(); return undo(); }
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (k === "]") return gotoPage(S.page + 1);
  if (k === "[") return gotoPage(S.page - 1);
  if (k === "ArrowDown" || k === "j" || k === "J") { e.preventDefault(); return moveSentence(1, e.shiftKey); }
  if (k === "ArrowUp" || k === "k" || k === "K") { e.preventDefault(); return moveSentence(-1, e.shiftKey); }

  if (m === "speech") {
    if (k === "Enter") { e.preventDefault(); return acceptSelection(); }
    if (k === "Backspace" || k === "Delete") { e.preventDefault(); return clearSpeakers(); }
    if (k === "u" || k === "U") return nextUnannotated(e.shiftKey || k === "U" ? -1 : 1);
    if (k === "p") return repeatPrevious();
    if (/^[0-9]$/.test(k)) { const c = S.book.characters[Number(k)]; if (c) toggleSpeaker(c.id); return; }
  }
  if (m === "span" || m === "attr") {
    if (k === "ArrowRight" || k === "l") { e.preventDefault(); return moveToken(1, e.shiftKey); }
    if (k === "ArrowLeft" || k === "h") { e.preventDefault(); return moveToken(-1, e.shiftKey); }
    if (k === "Backspace" || k === "Delete") { e.preventDefault(); return clearTokens(); }
    if (k === "/") { e.preventDefault(); ($("#lab-new") || $("#attr-val"))?.focus(); return; }
    if (/^[1-9]$/.test(k)) { const lab = (l.labels || [])[Number(k) - 1]; if (lab !== undefined) applyLabel(lab); return; }
    if (k === "Escape" && S.tsel) { S.tsel = { a: S.tsel.b, b: S.tsel.b }; S.tanchor = S.tsel.b; refreshSelectionClasses(); return; }
  }
  if (m === "seg") {
    if (k === "s" || k === "m") { S.segTool = k === "s" ? "split" : "move"; S.bsel = null; renderReader(); renderTools(); return; }
    if (k === "ArrowLeft" || k === "ArrowRight") { e.preventDefault(); return nudgeBoundary(k === "ArrowLeft" ? -1 : 1); }
  }
  if (l.id === "sentences" && k === "i") return nextIssue(); // also while read-only
  if (m !== "view" && m !== "speech") {
    if ((k === "Enter" || k === "u" || k === "U") && !tracksReview(l)) {
      if (k === "Enter") e.preventDefault();
      return toast(`${l.name} is at level Fixed: no per-sentence review. Switch it to Suggestion or Manual to use Enter / u.`, 3500);
    }
    if (k === "Enter") { e.preventDefault(); return confirmSentences(); }
    if (k === "u" || k === "U") return nextUnreviewed(e.shiftKey || k === "U" ? -1 : 1);
  }
  if ((m === "span" || m === "attr") && k === "p") return repeatPrevious();
  if (k === "Escape") { S.sel = new Set([S.cur]); refreshSelectionClasses(); }
});

// ------------------------------------------------------------------ inspector
function usasTitle(v) {
  if (!v) return "";
  return v.split(/\s+/).filter((t) => /^[A-Z]/.test(t)).map((t) => `${t}: ${USAS_FIELDS[t[0]] || ""}`).join("\n");
}

function renderInspector() {
  const s = S.sents.get(S.cur);
  $("#insp-id").textContent = s ? `#${s.id}${S.sel.size > 1 ? ` · ${S.sel.size} selected` : ""}` : "";
  if (!s) { $("#inspector").innerHTML = ""; return; }
  const names = (ids) => ids.map((id) => esc(charById(id)?.name || id)).join(" + ");
  const sug = s.suggestion;
  const L = S.book.layers.filter(ready);
  const attrs = L.filter((l) => l.kind === "token-attr");
  const spans = L.filter((l) => l.kind === "token-span");
  const revLayers = L.filter((l) => tracksReview(l) && l.id !== "speech");
  $("#inspector").innerHTML = `
    <div class="sugg">
      <div><b>Speaker:</b> ${s.speakers ? names(s.speakers) : `<span class="muted">not annotated</span>`}</div>
      ${levelOf(layer("speech")) === "suggestion" ? `<div><b>Suggestion:</b> ${sug.speakers.length ? names(sug.speakers) : "—"}
        <div class="muted">${esc(sug.reason)}</div></div>` : ""}
      ${revLayers.length ? `<div class="revs">${revLayers.map((l) => `<span class="${s.rev.includes(l.id) ? "ok" : ""}">${s.rev.includes(l.id) ? "✓" : "○"} ${esc(l.name)}</span>`).join("")}</div>` : ""}
      ${s.issue ? `<div class="warn">⚠ ${esc(s.issue)}</div>` : ""}
    </div>
    <div class="tbl-wrap"><table><tr><th>token</th><th>lemma</th>${attrs.map((l) => `<th style="color:${l.color}" title="${esc(l.name)}">${esc(l.id)}</th>`).join("")}
      ${spans.map((l) => `<th style="color:${l.color}" title="${esc(l.name)}">${esc(l.id)}</th>`).join("")}</tr>
    ${s.tokens.map((t) => `<tr class="${inTsel(t.i) ? "tsel" : ""}" data-i="${t.i}"><td>${esc(t.t)}</td><td>${esc(t.lemma)}</td>
      ${attrs.map((l) => `<td title="${esc((t.d[l.id] || "") + (l.annotator === "usas" ? "\n" + usasTitle(t.d[l.id] || t.a[l.id]) : ""))}">${esc(t.a[l.id] || "")}</td>`).join("")}
      ${spans.map((l) => { const x = t.s[l.id]; return `<td>${x ? (x[2] ? "B-" : "I-") + esc(x[1]) : ""}</td>`; }).join("")}</tr>`).join("")}
    </table></div>`;
}
$("#inspector").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-i]");
  if (tr && (mode() === "span" || mode() === "attr")) selectToken(Number(tr.dataset.i), e.shiftKey);
});
