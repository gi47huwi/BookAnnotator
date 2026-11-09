"""FastAPI app: REST API + static single-page UI.

Start it with `bookannotator serve` (binds to 127.0.0.1 only).
Development:  uvicorn bookannotator.server:app --reload --port 8765

Security model: a single-user tool on the user's own computer. It has no
accounts, so it must never be reachable from other machines; see SECURITY.md.
"""
from __future__ import annotations

import bisect
import json
import re
import threading
import traceback
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import annotators, edits, store
from .store import NARRATOR, Book

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # a long novel is ~2 MB
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def ui_version() -> str:
    """Changes whenever a UI file changes, so open pages can ask for a reload."""
    return str(int(max(p.stat().st_mtime for p in STATIC.iterdir())))

app = FastAPI(title="Book Annotator", docs_url=None, redoc_url=None, openapi_url=None)


def _host_name(host_header: str) -> str:
    """'localhost:8765' -> 'localhost', '[::1]:8765' -> '::1'."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


@app.middleware("http")
async def local_only(request, call_next):
    """Only answer requests meant for this local app.

    * Host header must be localhost / 127.0.0.1 / ::1 – blocks DNS-rebinding,
      where a malicious website points its own domain at 127.0.0.1.
    * API calls coming from another website (cross-site or same-site, e.g. a
      page on localhost:3000) are refused, so a page open in the browser cannot
      upload, change, delete or read annotations. Requests without browser
      headers (curl, scripts on this machine) are allowed.
    """
    if _host_name(request.headers.get("host", "")) not in LOCAL_HOSTS:
        return PlainTextResponse("Forbidden: Book Annotator only accepts local requests.", status_code=403)
    if request.url.path.startswith("/api/"):
        site = request.headers.get("sec-fetch-site")
        if site and site not in ("same-origin", "none"):
            return PlainTextResponse("Forbidden: cross-site request.", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.headers.get('host', '')}":
            return PlainTextResponse("Forbidden: foreign origin.", status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["X-UI-Version"] = ui_version()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return response

# ------------------------------------------------------------------ processing

_queue_lock = threading.Lock()  # one heavy NLP job at a time


def run_pipeline(book_id: str) -> None:
    from .pipeline import process_text

    meta = store.get_meta(book_id)

    def progress(frac: float, msg: str) -> None:
        meta.update(status="processing", progress=round(frac, 3), message=msg)
        store.save_meta(book_id, meta)

    with _queue_lock:
        try:
            progress(0.0, "loading spaCy model")
            result = process_text(store.source_text(book_id), progress)
            store.save_processed(book_id, result)
            d = result["doc"]
            meta.update(status="ready", progress=1.0, message="",
                        n_tokens=d["n_tokens"], n_sentences=d["n_sentences"],
                        n_pages=len(d["pages"]), model=d["model"])
        except Exception as e:  # keep the error visible in the UI
            traceback.print_exc()
            meta.update(status="error", message=str(e))
        store.save_meta(book_id, meta)


def start_processing(book_id: str) -> None:
    threading.Thread(target=run_pipeline, args=(book_id,), daemon=True).start()


def run_annotator(book_id: str, targets: dict, aid: str, options: dict) -> None:
    """targets: {output_key: layer_id}; single-output annotators use key None."""
    def progress(frac: float, msg: str) -> None:
        for lid in targets.values():
            store.update_layer_meta(book_id, lid, progress=round(frac, 3), message=msg)

    with _queue_lock:
        try:
            b = store.book(book_id)
            result = annotators.RUNNERS[aid](b, options, progress)
            results = result if isinstance(result, dict) else {None: result}
            for key, lid in targets.items():
                content, labels = results[key]
                with store.lock():
                    b.save_content(lid, content)
                store.update_layer_meta(book_id, lid, status="ready", progress=1.0, message="", labels=labels)
        except Exception as e:
            traceback.print_exc()
            for lid in targets.values():
                store.update_layer_meta(book_id, lid, status="error", message=str(e))


@app.on_event("startup")
def resume_unfinished() -> None:
    for meta in store.list_books():
        if meta.get("status") in ("queued", "processing"):
            start_processing(meta["id"])
        elif meta.get("status") == "ready":
            for l in store.get_layers(meta["id"]):
                if l.get("status") == "running":
                    store.update_layer_meta(meta["id"], l["id"], status="error",
                                            message="interrupted by a server restart – delete and add again")


# ------------------------------------------------------------------ helpers

def _book(book_id: str) -> Book:
    try:
        return store.book(book_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "book not found or not processed")


def _layer(book_id: str, lid: str, editable: bool = False) -> dict:
    layer = store.get_layer(book_id, lid)
    if layer is None:
        raise HTTPException(404, f"no layer {lid}")
    if editable:
        if layer.get("locked"):
            raise HTTPException(423, f"layer “{layer['name']}” is locked – unlock it to edit")
        if layer.get("status", "ready") != "ready":
            raise HTTPException(409, "layer is still being computed")
    return layer


def _sent_range(b: Book, sid: int) -> tuple[int, int]:
    S = b.doc["sentences"]
    return S["start_tok"][sid], S["end_tok"][sid]


def _page_sent_range(b: Book, page: int) -> tuple[int, int]:
    doc = b.doc
    pages, P = doc["pages"], doc["paragraphs"]
    p0 = pages[page]
    p1 = pages[page + 1] if page + 1 < len(pages) else doc["n_paragraphs"]
    s0 = P["start_sent"][p0]
    s1 = P["start_sent"][p1] if p1 < doc["n_paragraphs"] else doc["n_sentences"]
    return s0, s1


def _page_of_sentence(b: Book, sid: int) -> int:
    para = b.doc["sentences"]["para"][sid]
    return max(bisect.bisect_right(b.doc["pages"], para) - 1, 0)


def _sentence_of_token(b: Book, tok: int) -> int:
    return bisect.bisect_right(b.doc["sentences"]["start_tok"], tok) - 1


_WORD = re.compile(r"\w")


def _norm(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s).strip().lower()


def _match_character(cand: str, chars: list[dict]) -> str | None:
    c = _norm(cand)
    if not c:
        return None
    words = c.split()
    for ch in chars:
        for name in [ch["name"], *ch.get("aliases", [])]:
            n = _norm(name)
            if not n:
                continue
            if c == n or (len(n.split()) == 1 and n in words and len(n) > 2) \
                    or (" " in n and n in c):
                return ch["id"]
    return None


def _suggest(b: Book, sid: int, chars: list[dict], prev: list[str] | None) -> dict:
    """Heuristic speaker suggestion for one sentence."""
    a, z = _sent_range(b, sid)
    qmap = b.span_index("quotes")
    text, T = b.text, b.doc["tokens"]
    n_quoted = sum(1 for i in range(a, z) if qmap[i] is not None)
    outside_words = sum(1 for i in range(a, z) if qmap[i] is None
                        and _WORD.search(text[T["start"][i]:T["end"][i]]))
    if n_quoted == 0:
        return {"speakers": [NARRATOR], "reason": "no quotation"}
    attrib = b.doc["sentences"]["attrib"][sid]
    who, reason = None, ""
    for cand in attrib:
        who = _match_character(cand, chars)
        if who:
            reason = f"attributed: “{cand}”"
            break
    if who is None and prev:
        others = [p for p in prev if p != NARRATOR]
        if others and outside_words == 0:
            who, reason = others[0], "continues previous speaker"
    if who is None:
        return {"speakers": [], "reason": "quotation, speaker unknown"
                + (f" (candidate: “{attrib[0]}”)" if attrib else "")}
    speakers = [who] + ([NARRATOR] if outside_words > 0 and who != NARRATOR else [])
    return {"speakers": speakers, "reason": reason}


def _speech_for_range(b: Book, s0: int, s1: int) -> dict:
    chars = store.get_characters(b.id)
    out, prev, prev_para = {}, None, None
    for sid in range(s0, s1):
        para = b.doc["sentences"]["para"][sid]
        if para != prev_para:
            prev = None
        manual = b.speakers(sid)
        sug = _suggest(b, sid, chars, prev)
        out[sid] = {"speakers": manual, "suggestion": sug}
        prev = manual or sug["speakers"] or prev
        prev_para = para
    return out


def _layers_with_stats(b: Book) -> list[dict]:
    layers = store.get_layers(b.id)
    for l in layers:
        if l.get("level") != "fixed" and l.get("status", "ready") == "ready":
            l["n_reviewed"] = b.n_reviewed(l["id"])
    return layers


def _review_counts(b: Book) -> dict:
    return {l["id"]: b.n_reviewed(l["id"]) for l in store.get_layers(b.id)
            if l.get("level") != "fixed" and l.get("status", "ready") == "ready"}


def _ready_layers(book_id: str, kind: str) -> list[dict]:
    return [l for l in store.get_layers(book_id) if l["kind"] == kind and l.get("status", "ready") == "ready"]


# ------------------------------------------------------------------ books

@app.get("/api/books")
def api_books():
    books = store.list_books()
    for m in books:
        if m.get("status") == "ready":
            m["n_annotated"] = store.count_annotated(m["id"])
    return books


@app.post("/api/books")
async def api_upload(file: UploadFile = File(...), title: str = Form(""), author: str = Form(""),
                     book_id: str = Form("")):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large (limit {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    if b"\x00" in raw[:4096]:
        raise HTTPException(415, "this does not look like a plain-text file")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    text = text.replace("\r\n", "\n")
    title = title.strip() or Path(file.filename or "book").stem
    bid = store.unique_id(book_id.strip() or title)
    meta = store.create_book(bid, title, author.strip(), text)
    start_processing(bid)
    return meta


@app.delete("/api/books/{book_id}")
def api_delete(book_id: str):
    store.delete_book(book_id)
    return {"ok": True}


@app.post("/api/books/{book_id}/reprocess")
def api_reprocess(book_id: str):
    meta = store.get_meta(book_id)
    meta.update(status="queued", progress=0.0)
    store.save_meta(book_id, meta)
    start_processing(book_id)
    return meta


@app.get("/api/books/{book_id}")
def api_book(book_id: str):
    meta = store.get_meta(book_id)
    if meta is None:
        raise HTTPException(404)
    if meta["status"] != "ready":
        return {"meta": meta}
    b = _book(book_id)
    return {
        "meta": meta,
        "n_pages": len(b.doc["pages"]),
        "n_sentences": b.doc["n_sentences"],
        "chapters": b.doc["chapters"],
        "layers": _layers_with_stats(b),
        "characters": store.get_characters(book_id),
        "n_annotated": b.n_annotated(),
        "can_undo": bool(b.undo_stack),
    }


@app.get("/api/books/{book_id}/page/{page}")
def api_page(book_id: str, page: int):
    b = _book(book_id)
    with store.lock():
        if not 0 <= page < len(b.doc["pages"]):
            raise HTTPException(404, "page out of range")
        s0, s1 = _page_sent_range(b, page)
        text, T, S = b.text, b.doc["tokens"], b.doc["sentences"]
        attr_layers = [(l["id"], b.content(l["id"]).get("values") or []) for l in _ready_layers(book_id, "token-attr")]
        detail_layers = {l["id"]: b.content(l["id"]).get("detail") for l in _ready_layers(book_id, "token-attr")}
        span_layers = [(l["id"], b.content(l["id"]).get("spans", []), b.span_index(l["id"]))
                       for l in _ready_layers(book_id, "token-span")]
        speech = _speech_for_range(b, s0, s1)
        review_layers = [l["id"] for l in store.get_layers(book_id)
                         if l.get("level") != "fixed" and l["id"] != "speech" and l.get("status", "ready") == "ready"]
        review_sets = {lid: b.reviewed(lid) for lid in review_layers}

        paragraphs, cur_para = [], None
        for sid in range(s0, s1):
            a, z = S["start_tok"][sid], S["end_tok"][sid]
            toks = []
            for i in range(a, z):
                attrs = {lid: (vals[i] if i < len(vals) else "") for lid, vals in attr_layers}
                details = {lid: d[i] for lid, d in detail_layers.items() if d and d[i]}
                spans = {}
                for lid, sp, idx in span_layers:
                    k = idx[i]
                    if k is not None:
                        spans[lid] = [k, sp[k][2], sp[k][0] == i, sp[k][1] == i + 1]
                toks.append({
                    "i": i,
                    "t": text[T["start"][i]:T["end"][i]].replace("\n", " "),
                    "ws": " " if i + 1 < z and T["start"][i + 1] > T["end"][i] else "",
                    "lemma": T["lemma"][i], "tag": T["tag"][i],
                    "a": attrs, "d": details, "s": spans,
                })
            para = S["para"][sid]
            if para != cur_para:
                paragraphs.append({"id": para, "sentences": []})
                cur_para = para
            rev = [lid for lid, rs in review_sets.items() if a in rs]
            if speech[sid]["speakers"]:
                rev.append("speech")
            paragraphs[-1]["sentences"].append({"id": sid, "start": a, "end": z, "tokens": toks, "rev": rev,
                                                "issue": edits.sentence_issue(b, sid), **speech[sid]})
        return {"page": page, "first_sentence": s0, "last_sentence": s1 - 1, "paragraphs": paragraphs,
                "n_sentences": b.doc["n_sentences"], "can_undo": bool(b.undo_stack)}


# ------------------------------------------------------------------ layer stack

class LayersIn(BaseModel):
    layers: list[dict]


@app.get("/api/books/{book_id}/layers")
def api_layers(book_id: str):
    return store.get_layers(book_id)


@app.put("/api/books/{book_id}/layers")
def api_save_layers(book_id: str, body: LayersIn):
    return store.merge_layer_ui(book_id, body.layers)


@app.get("/api/annotators")
def api_annotators():
    return annotators.catalogue()


class AddLayerIn(BaseModel):
    annotator: str
    options: dict = {}


def _choose_outputs(outputs: list[dict], wanted) -> list[dict]:
    """Accept a list of keys, or free text like "quotes, CLiC suspensions"
    (keys or display names, any case). Not given -> the default selection;
    an explicitly empty list (all boxes unticked) -> error."""
    if isinstance(wanted, list) and not wanted:
        raise HTTPException(400, "tick at least one layer to create")
    defaults = [o for o in outputs if o.get("default", True)]
    if not wanted:
        return defaults
    items = wanted if isinstance(wanted, list) else re.split(r"[,;\n]+", str(wanted))
    items = [str(x).strip().lower() for x in items if str(x).strip()]
    if not items:
        return defaults
    chosen = [o for o in outputs
              if any(x in (o["key"], o["name"].lower(), o["name"].lower().replace("clic ", "")) for x in items)]
    if not chosen:
        valid = ", ".join(f"{o['key']} ({o['name']})" for o in outputs)
        raise HTTPException(400, f"unknown layer choice “{', '.join(items)}” – choose from: {valid}")
    return chosen


@app.post("/api/books/{book_id}/layers")
def api_add_layer(book_id: str, body: AddLayerIn):
    _book(book_id)
    spec = annotators.ANNOTATORS.get(body.annotator)
    if spec is None:
        raise HTTPException(404, "unknown annotator")
    ok, msg = annotators.available(body.annotator)
    if not ok:
        raise HTTPException(400, f"{spec['name']} is not installed ({msg}). Install with: {spec['install']}")
    level = "manual" if spec["source"] == "manual" else body.options.get("level", "fixed")
    if level not in ("fixed", "suggestion"):
        level = "fixed"

    def new_layer(lid, name, color, style):
        layer = {"id": lid, "name": name, "kind": spec["kind"], "source": spec["source"],
                 "annotator": body.annotator, "visible": True, "opacity": 0.8,
                 "level": level, "locked": level == "fixed", "color": color,
                 "status": "running", "progress": 0.0, "message": "queued", "labels": []}
        if style:
            layer["style"] = style
        store.add_layer_meta(book_id, layer)
        return layer

    if "outputs" in spec:  # one run, several layers
        outs = _choose_outputs(spec["outputs"], body.options.get("regions"))
        targets, created = {}, []
        for o in outs:
            lid = store.unique_layer_id(book_id, f"{body.annotator}_{o['key']}")
            created.append(new_layer(lid, o["name"], o["color"], o.get("style")))
            targets[o["key"]] = lid
    else:
        name = (body.options.get("name") or spec["name"]).strip()
        lid = store.unique_layer_id(book_id, body.annotator if body.annotator == "usas" else name)
        created = [new_layer(lid, name, spec["color"], spec.get("style"))]
        targets = {None: lid}
    threading.Thread(target=run_annotator, args=(book_id, targets, body.annotator, body.options), daemon=True).start()
    return {"layers": created}


@app.delete("/api/books/{book_id}/layers/{lid}")
def api_delete_layer(book_id: str, lid: str):
    if lid in store.CORE_LAYERS:
        raise HTTPException(400, "core layers cannot be deleted")
    _layer(book_id, lid)
    store.remove_layer(book_id, lid)
    return {"ok": True}


# ------------------------------------------------------------------ layer editing

class SpanEdit(BaseModel):
    op: str                 # "set" | "clear"
    start: int
    end: int                # exclusive
    label: str = ""


class AttrEdit(BaseModel):
    tokens: list[int]
    value: str


class SegEdit(BaseModel):
    op: str                 # "split" | "merge" | "move"
    token: int | None = None
    sentence: int | None = None


def _edit_result(b: Book, **extra):
    return {"ok": True, "n_sentences": b.doc["n_sentences"], "n_annotated": b.n_annotated(),
            "can_undo": bool(b.undo_stack), "reviewed": _review_counts(b), **extra}


@app.post("/api/books/{book_id}/layers/{lid}/spans")
def api_edit_spans(book_id: str, lid: str, body: SpanEdit):
    layer = _layer(book_id, lid, editable=True)
    if layer["kind"] != "token-span":
        raise HTTPException(400, "not a span layer")
    b = _book(book_id)
    with store.lock():
        try:
            if body.op == "set":
                label = body.label.strip() or (layer.get("labels") or [layer["name"]])[0]
                edits.set_span(b, lid, body.start, body.end, label)
                if label not in (layer.get("labels") or []):
                    store.update_layer_meta(book_id, lid, labels=[*(layer.get("labels") or []), label])
            elif body.op == "clear":
                edits.clear_spans(b, lid, body.start, body.end)
            else:
                raise HTTPException(400, "op must be set or clear")
        except edits.EditError as e:
            raise HTTPException(400, str(e))
        return _edit_result(b)


@app.post("/api/books/{book_id}/layers/{lid}/attrs")
def api_edit_attrs(book_id: str, lid: str, body: AttrEdit):
    layer = _layer(book_id, lid, editable=True)
    if layer["kind"] != "token-attr":
        raise HTTPException(400, "not a token attribute layer")
    b = _book(book_id)
    with store.lock():
        try:
            edits.set_attr(b, lid, body.tokens, body.value.strip())
        except edits.EditError as e:
            raise HTTPException(400, str(e))
        return _edit_result(b)


@app.post("/api/books/{book_id}/layers/sentences/edit")
def api_edit_sentences(book_id: str, body: SegEdit):
    _layer(book_id, "sentences", editable=True)
    b = _book(book_id)
    with store.lock():
        try:
            if body.op == "split" and body.token is not None:
                sid = edits.split_sentence(b, body.token)
            elif body.op == "merge" and body.sentence is not None:
                edits.merge_sentences(b, body.sentence)
                sid = body.sentence - 1
            elif body.op == "move" and body.sentence is not None and body.token is not None:
                edits.move_boundary(b, body.sentence, body.token)
                sid = body.sentence
            else:
                raise HTTPException(400, "split needs token, merge needs sentence")
        except edits.EditError as e:
            raise HTTPException(400, str(e))
        return _edit_result(b, sentence=sid)


@app.get("/api/books/{book_id}/layers/sentences/issues")
def api_sentence_issues(book_id: str, limit: int = 200, after: int = -1):
    """Sentences whose boundaries look suspicious (heuristics), book-wide."""
    b = _book(book_id)
    S, T, text = b.doc["sentences"], b.doc["tokens"], b.text
    out = []
    for sid in range(max(after + 1, 0), b.doc["n_sentences"]):
        reason = edits.sentence_issue(b, sid)
        if reason:
            a, z = S["start_tok"][sid], S["end_tok"][sid]
            snippet = text[T["start"][a]:T["end"][z - 1]].replace("\n", " ")
            out.append({"sentence": sid, "page": _page_of_sentence(b, sid), "reason": reason,
                        "text": snippet[:70] + ("…" if len(snippet) > 70 else ""),
                        "reviewed": b.is_reviewed("sentences", sid)})
            if len(out) >= limit:
                break
    return out


class LevelIn(BaseModel):
    level: str
    clear: bool = False


@app.put("/api/books/{book_id}/layers/{lid}/level")
def api_set_level(book_id: str, lid: str, body: LevelIn):
    _layer(book_id, lid)
    b = _book(book_id)
    with store.lock():
        try:
            layer = edits.set_level(b, lid, body.level, body.clear)
        except edits.EditError as e:
            raise HTTPException(400, str(e))
        return _edit_result(b, layer=layer)


class ReviewIn(BaseModel):
    sentences: list[int] = []
    page: int | None = None
    reviewed: bool = True


@app.post("/api/books/{book_id}/layers/{lid}/review")
def api_review(book_id: str, lid: str, body: ReviewIn):
    """Mark sentences (or a whole page) as reviewed / done for a layer."""
    layer = _layer(book_id, lid, editable=True)
    if layer.get("level") == "fixed" or lid == "speech":
        raise HTTPException(400, "this layer has no review state")
    b = _book(book_id)
    with store.lock():
        sids = list(body.sentences)
        if body.page is not None:
            s0, s1 = _page_sent_range(b, body.page)
            sids = list(range(s0, s1))
        n = edits.set_reviewed(b, lid, sids, body.reviewed)
        return _edit_result(b, changed=n)


@app.get("/api/books/{book_id}/layers/{lid}/next-unreviewed")
def api_next_unreviewed(book_id: str, lid: str, after: int = -1, direction: int = 1):
    b = _book(book_id)
    rev = b.reviewed(lid)
    st = b.doc["sentences"]["start_tok"]
    n = b.doc["n_sentences"]
    step = -1 if direction < 0 else 1
    for k in range(1, n + 1):
        sid = (after + step * k) % n
        if st[sid] not in rev:
            return {"sentence": sid, "page": _page_of_sentence(b, sid)}
    return {"sentence": None, "page": None}


@app.get("/api/books/{book_id}/layers/{lid}/next-item")
def api_next_item(book_id: str, lid: str, after_tok: int = -1, after_sent: int = -1,
                  direction: int = 1, label: str = ""):
    """Next/previous annotation item of a layer, book-wide, wrapping around.

    token-span      next span (start after `after_tok`)
    token-attr      next token with a value (punctuation skipped)
    sentence-label  next sentence containing a quotation (direct speech)
    segmentation    next sentence boundary
    `label` restricts to labels/values containing it (case-insensitive).
    On suggestion-level layers items in confirmed sentences are skipped."""
    layer = _layer(book_id, lid)
    b = _book(book_id)
    step = -1 if direction < 0 else 1
    S = b.doc["sentences"]
    st = S["start_tok"]
    n_tok, n_sent = b.doc["n_tokens"], b.doc["n_sentences"]
    needle = label.strip().lower()
    skip_reviewed = layer.get("level") == "suggestion" and lid != "speech"
    rev = b.reviewed(lid) if skip_reviewed else set()

    def open_sentence(sid: int) -> bool:
        return not skip_reviewed or st[sid] not in rev

    def found(sid: int, a: int, z: int, lab: str, wrapped: bool):
        return {"found": True, "sentence": sid, "page": _page_of_sentence(b, sid),
                "start": a, "end": z, "label": lab, "wrapped": wrapped}

    if layer["kind"] == "token-span":
        spans = b.content(lid).get("spans", [])
        idx = list(range(len(spans)))
        starts = [sp[0] for sp in spans]
        k0 = bisect.bisect_right(starts, after_tok) if step > 0 else bisect.bisect_left(starts, after_tok) - 1
        order = [(k0 + step * j) % len(idx) for j in range(len(idx))] if idx else []
        for j, k in enumerate(order):
            a, z, lab = spans[k][0], spans[k][1], spans[k][2]
            if needle and needle not in str(lab).lower():
                continue
            sid = edits.sentence_of_token(b, a)
            if open_sentence(sid):
                wrapped = (a <= after_tok) if step > 0 else (a >= after_tok)
                return found(sid, a, z, lab, wrapped)
    elif layer["kind"] == "token-attr":
        values = b.content(lid).get("values") or []
        pos = b.doc["tokens"]["pos"]
        for j in range(1, n_tok + 1):
            i = (after_tok + step * j) % n_tok
            v = values[i] if i < len(values) else ""
            if not v or pos[i] == "PUNCT" or (needle and needle not in v.lower()):
                continue
            sid = edits.sentence_of_token(b, i)
            if open_sentence(sid):
                return found(sid, i, i + 1, v, (i <= after_tok) if step > 0 else (i >= after_tok))
    elif layer["kind"] == "sentence-label":
        qmap = b.span_index("quotes")
        for j in range(1, n_sent + 1):
            sid = (after_sent + step * j) % n_sent
            a, z = S["start_tok"][sid], S["end_tok"][sid]
            if any(qmap[i] is not None for i in range(a, z)):
                return found(sid, a, z, "", (sid <= after_sent) if step > 0 else (sid >= after_sent))
    elif layer["kind"] == "segmentation":
        for j in range(1, n_sent + 1):
            sid = (after_sent + step * j) % n_sent
            if sid > 0 and S["para"][sid] == S["para"][sid - 1] and open_sentence(sid):
                return found(sid, st[sid], st[sid] + 1, "", (sid <= after_sent) if step > 0 else (sid >= after_sent))
    return {"found": False}


@app.post("/api/books/{book_id}/undo")
def api_undo(book_id: str):
    b = _book(book_id)
    with store.lock():
        r = edits.undo(b)
        if r is None:
            raise HTTPException(400, "nothing to undo")
        return _edit_result(b, **r)


# ------------------------------------------------------------------ speech layer

class SpeechIn(BaseModel):
    updates: dict[int, list[str]]


@app.put("/api/books/{book_id}/speech")
def api_speech(book_id: str, body: SpeechIn):
    _layer(book_id, "speech", editable=True)
    b = _book(book_id)
    with store.lock():
        edits.set_speech(b, body.updates)
        return _edit_result(b)


class AcceptIn(BaseModel):
    page: int
    only_narrator: bool = False


@app.post("/api/books/{book_id}/speech/accept")
def api_accept(book_id: str, body: AcceptIn):
    """Accept suggestions for all unannotated sentences on a page."""
    _layer(book_id, "speech", editable=True)
    b = _book(book_id)
    with store.lock():
        s0, s1 = _page_sent_range(b, body.page)
        updates = {}
        for sid, v in _speech_for_range(b, s0, s1).items():
            sug = v["suggestion"]["speakers"]
            if v["speakers"] is None and sug and (not body.only_narrator or sug == [NARRATOR]):
                updates[sid] = sug
        if updates:
            edits.set_speech(b, updates)
        return _edit_result(b, accepted=len(updates))


@app.get("/api/books/{book_id}/next-unannotated")
def api_next(book_id: str, after: int = -1, direction: int = 1):
    """Next (direction=1) or previous (direction=-1) unannotated sentence, wrapping around."""
    b = _book(book_id)
    ann = b.content("speech")["sentences"]
    st = b.doc["sentences"]["start_tok"]
    n = b.doc["n_sentences"]
    step = -1 if direction < 0 else 1
    for k in range(1, n + 1):
        sid = (after + step * k) % n
        if str(st[sid]) not in ann:
            return {"sentence": sid, "page": _page_of_sentence(b, sid)}
    return {"sentence": None, "page": None}


@app.get("/api/books/{book_id}/locate/{sid}")
def api_locate(book_id: str, sid: int):
    return {"sentence": sid, "page": _page_of_sentence(_book(book_id), sid)}


# ------------------------------------------------------------------ characters

class CharacterIn(BaseModel):
    name: str
    aliases: list[str] = []
    color: str | None = None


@app.get("/api/books/{book_id}/characters")
def api_chars(book_id: str):
    return store.get_characters(book_id)


@app.post("/api/books/{book_id}/characters")
def api_add_char(book_id: str, body: CharacterIn):
    chars = store.get_characters(book_id)
    ch = {"id": store.new_character_id(chars), "name": body.name.strip(),
          "aliases": [a.strip() for a in body.aliases if a.strip()],
          "color": body.color or store.next_color(chars + [{}])}
    chars.append(ch)
    store.save_characters(book_id, chars)
    return ch


@app.patch("/api/books/{book_id}/characters/{cid}")
def api_edit_char(book_id: str, cid: str, body: CharacterIn):
    chars = store.get_characters(book_id)
    for ch in chars:
        if ch["id"] == cid:
            ch["name"] = body.name.strip() or ch["name"]
            ch["aliases"] = [a.strip() for a in body.aliases if a.strip()]
            if body.color:
                ch["color"] = body.color
            store.save_characters(book_id, chars)
            return ch
    raise HTTPException(404)


@app.delete("/api/books/{book_id}/characters/{cid}")
def api_del_char(book_id: str, cid: str):
    if cid == NARRATOR:
        raise HTTPException(400, "the narrator cannot be deleted")
    chars = [c for c in store.get_characters(book_id) if c["id"] != cid]
    store.save_characters(book_id, chars)
    b = _book(book_id)
    with store.lock():
        sp = b.content("speech")
        st = b.doc["sentences"]["start_tok"]
        sid_of = {s: i for i, s in enumerate(st)}
        updates = {sid_of[int(k)]: [x for x in v if x != cid]
                   for k, v in sp["sentences"].items() if cid in v and int(k) in sid_of}
        if updates:
            edits.set_speech(b, updates)
    return {"ok": True}


@app.get("/api/books/{book_id}/character-candidates")
def api_char_candidates(book_id: str, limit: int = 40):
    """Most frequent PERSON entities."""
    b = _book(book_id)
    text, T = b.text, b.doc["tokens"]
    cnt = Counter()
    for a, z, label in b.content("entities").get("spans", []):
        if label == "PERSON":
            name = text[T["start"][a]:T["end"][z - 1]].replace("\n", " ")
            name = re.sub(r"['’]s$", "", name).strip()
            cnt[name] += 1
    known = {_norm(n) for ch in store.get_characters(book_id) for n in [ch["name"], *ch.get("aliases", [])]}
    out = [{"name": n, "count": c} for n, c in cnt.most_common(limit * 2) if _norm(n) not in known]
    return out[:limit]


# ------------------------------------------------------------------ export

def _speaker_names(b: Book, sid: int, chars: dict) -> list[str] | None:
    sp = b.speakers(sid)
    return [chars.get(x, x) for x in sp] if sp else None


@app.get("/api/books/{book_id}/export.tsv")
def api_export_tsv(book_id: str):
    """One token per line, CoNLL-like; one column per layer; sentences separated by blank lines."""
    b = _book(book_id)
    layers = [l for l in store.get_layers(book_id) if l.get("status", "ready") == "ready"]
    attr = [(l["id"], b.content(l["id"]).get("values") or []) for l in layers if l["kind"] == "token-attr"]
    span = [(l["id"], b.content(l["id"]).get("spans", []), b.span_index(l["id"]))
            for l in layers if l["kind"] == "token-span"]
    chars = {c["id"]: c["name"] for c in store.get_characters(book_id)}
    review_layers = [l["id"] for l in layers if l.get("level") != "fixed"]

    def gen():
        cols = ["sent_id", "tok_id", "token", "lemma", "tag"] + [l for l, _ in attr] + [l for l, _, _ in span] + ["speaker"]
        yield "# columns: " + "\t".join(cols) + "\n"
        text, T, S = b.text, b.doc["tokens"], b.doc["sentences"]
        for sid in range(b.doc["n_sentences"]):
            a, z = S["start_tok"][sid], S["end_tok"][sid]
            speakers = _speaker_names(b, sid, chars)
            spk = "|".join(speakers) if speakers else "_"
            rev = [l for l in review_layers if b.is_reviewed(l, sid)]
            yield (f"# sent_id = {sid}\n# paragraph = {S['para'][sid]}\n# speaker = {spk}\n"
                   f"# reviewed = {','.join(rev) if rev else '_'}\n")
            for i in range(a, z):
                row = [str(sid), str(i - a + 1), text[T["start"][i]:T["end"][i]].replace("\n", " "),
                       T["lemma"][i], T["tag"][i]]
                row += [(vals[i] if i < len(vals) and vals[i] else "_") for _, vals in attr]
                for _, sp, idx in span:
                    k = idx[i]
                    row.append("O" if k is None else ("B-" if sp[k][0] == i else "I-") + sp[k][2])
                row.append(spk)
                yield "\t".join(row) + "\n"
            yield "\n"
    return StreamingResponse(gen(), media_type="text/tab-separated-values",
                             headers={"Content-Disposition": f'attachment; filename="{book_id}.tsv"'})


@app.get("/api/export/corpus.jsonl")
def api_export_corpus():
    """All ready books, one JSON object per sentence."""
    def gen():
        for meta in store.list_books():
            if meta.get("status") != "ready":
                continue
            b = store.book(meta["id"])
            chars = {c["id"]: c["name"] for c in store.get_characters(meta["id"])}
            text, T, S = b.text, b.doc["tokens"], b.doc["sentences"]
            for sid in range(b.doc["n_sentences"]):
                a, z = S["start_tok"][sid], S["end_tok"][sid]
                yield json.dumps({
                    "book": meta["id"], "title": meta["title"], "sent_id": sid, "paragraph": S["para"][sid],
                    "text": text[T["start"][a]:T["end"][z - 1]].replace("\n", " "),
                    "speakers": _speaker_names(b, sid, chars),
                }, ensure_ascii=False) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="corpus.jsonl"'})


# ------------------------------------------------------------------ UI

@app.middleware("http")
async def no_cache_static(request, call_next):
    # local tool: always serve the current UI files
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
