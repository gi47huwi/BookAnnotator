"""File-based project store.

data/books/<book_id>/
    source.txt          original text (never modified)
    meta.json           title, author, status, created
    doc.json            segmentation + token attributes (from pipeline, editable)
    layers.json         layer stack: order, visibility, opacity, lock, colour, labels, status
    layers/<id>.json    layer contents
    characters.json     speaker inventory for the speech layer

Layer kinds
    segmentation    sentence boundaries (stored in doc.json)
    token-attr      one value per token          {"values": [...], "detail": [...]?}
                    (the "pos" layer lives in doc.json tokens.pos)
    token-span      labelled token spans         {"spans": [[start, end_excl, label], ...]}
    sentence-label  speakers per sentence        {"key": "start_tok", "sentences": {start_tok: [ids]}}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

def _default_data_dir() -> Path:
    """Where books and annotations are stored.

    1. $BOOKANNOTATOR_DATA if set
    2. <checkout>/data/books when running from a source checkout
    3. ~/BookAnnotator/books for an installed package
    """
    env = os.environ.get("BOOKANNOTATOR_DATA")
    if env:
        return Path(env).expanduser().resolve()
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / "pyproject.toml").exists():
        return checkout / "data" / "books"
    return Path.home() / "BookAnnotator" / "books"


DATA_DIR = _default_data_dir()
NARRATOR = "narrator"
CORE_LAYERS = {"speech", "quotes", "entities", "pos", "sentences"}

UPOS = ["ADJ", "ADP", "ADV", "AUX", "CCONJ", "DET", "INTJ", "NOUN", "NUM", "PART", "PRON",
        "PROPN", "PUNCT", "SCONJ", "SYM", "VERB", "X"]
ENT_LABELS = ["PERSON", "GPE", "LOC", "FAC", "ORG", "NORP", "DATE", "TIME", "EVENT",
              "WORK_OF_ART", "LAW", "LANGUAGE", "MONEY", "QUANTITY", "PERCENT", "CARDINAL", "ORDINAL"]

# Annotation levels
#   fixed       automatic output taken as given; locked, editable after unlocking
#   suggestion  automatic output shown as suggestions; every sentence needs review
#   manual      annotated from scratch; sentences are marked done when finished
LEVELS = ("fixed", "suggestion", "manual")


def allowed_levels(layer: dict) -> tuple[str, ...]:
    if layer["kind"] == "sentence-label":
        return ("suggestion", "manual")
    if layer["kind"] == "segmentation":
        return ("fixed", "suggestion")
    if layer.get("source") == "manual":
        return ("manual",)
    return LEVELS


# style: how a span layer is drawn (highlight = background box, text = text colour,
# underline = coloured bar under the tokens)
DEFAULT_LAYERS = [
    {"id": "speech", "name": "Speech / speaker", "kind": "sentence-label", "source": "manual",
     "level": "suggestion", "visible": True, "opacity": 0.8, "locked": False, "color": "#d9480f"},
    {"id": "quotes", "name": "Quotations", "kind": "token-span", "source": "auto", "style": "text",
     "labels": ["Q"], "visible": True, "opacity": 0.7, "locked": True, "color": "#1c7ed6"},
    {"id": "entities", "name": "Named entities", "kind": "token-span", "source": "auto", "style": "highlight",
     "labels": ENT_LABELS, "visible": True, "opacity": 0.6, "locked": True, "color": "#2b8a3e"},
    {"id": "pos", "name": "Part of speech", "kind": "token-attr", "source": "auto",
     "labels": UPOS, "visible": False, "opacity": 0.8, "locked": True, "color": "#7048e8"},
    {"id": "sentences", "name": "Sentence boundaries", "kind": "segmentation", "source": "auto",
     "visible": True, "opacity": 0.5, "locked": True, "color": "#868e96"},
]
# properties the UI may change through PUT /layers
UI_PROPS = {"visible", "opacity", "locked", "color", "name", "labels", "style"}  # level: own endpoint

CHARACTER_COLORS = [
    "#e8590c", "#1971c2", "#2f9e44", "#c2255c", "#6741d9", "#0c8599",
    "#f08c00", "#5c940d", "#a61e4d", "#364fc7", "#087f5b", "#862e9c",
]

_lock = threading.RLock()


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    tmp.replace(path)


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def book_dir(book_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", book_id):
        raise ValueError(f"invalid book id: {book_id!r}")
    return DATA_DIR / book_id


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return s[:60] or "book"


def unique_id(base: str) -> str:
    base = slugify(base)
    cand, i = base, 2
    while (DATA_DIR / cand).exists():
        cand, i = f"{base}_{i}", i + 1
    return cand


# ---------------------------------------------------------------- books

def list_books() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    out = []
    for d in sorted(DATA_DIR.iterdir()):
        meta = _read_json(d / "meta.json")
        if meta:
            out.append(meta)
    return out


def get_meta(book_id: str) -> dict | None:
    return _read_json(book_dir(book_id) / "meta.json")


def save_meta(book_id: str, meta: dict) -> None:
    _write_json(book_dir(book_id) / "meta.json", meta)


def create_book(book_id: str, title: str, author: str, text: str) -> dict:
    d = book_dir(book_id)
    d.mkdir(parents=True, exist_ok=False)
    (d / "source.txt").write_text(text, encoding="utf-8")
    meta = {"id": book_id, "title": title, "author": author, "status": "queued",
            "progress": 0.0, "message": "", "created": time.time()}
    save_meta(book_id, meta)
    return meta


def delete_book(book_id: str) -> None:
    with _lock:
        _books.pop(book_id, None)
        shutil.rmtree(book_dir(book_id))


def source_text(book_id: str) -> str:
    return (book_dir(book_id) / "source.txt").read_text(encoding="utf-8")


def save_processed(book_id: str, result: dict) -> None:
    """Write pipeline output. Re-processing replaces the core auto layers and
    resets their 'edited' flag; manual layers and characters are kept."""
    d = book_dir(book_id)
    _write_json(d / "doc.json", result["doc"])
    spans = result["layers"]
    _write_json(d / "layers" / "entities.json", {"spans": spans["entities"]["spans"]})
    _write_json(d / "layers" / "quotes.json", {"spans": [[s, e, "Q"] for s, e in spans["quotes"]["spans"]]})
    layers = _read_json(d / "layers.json")
    if layers is None:
        layers = [dict(l) for l in DEFAULT_LAYERS]
    for l in layers:
        if l["id"] in CORE_LAYERS:
            l.pop("edited", None)
    _write_json(d / "layers.json", layers)
    if not (d / "layers" / "speech.json").exists():
        _write_json(d / "layers" / "speech.json", {"key": "start_tok", "sentences": {}})
    if not (d / "characters.json").exists():
        _write_json(d / "characters.json", [
            {"id": NARRATOR, "name": "Narrator", "aliases": [], "color": "#495057"}])
    with _lock:
        _books.pop(book_id, None)


# ---------------------------------------------------------------- layer stack

def get_layers(book_id: str) -> list[dict]:
    with _lock:
        layers = _read_json(book_dir(book_id) / "layers.json", [dict(l) for l in DEFAULT_LAYERS])
    defaults = {l["id"]: l for l in DEFAULT_LAYERS}
    for l in layers:  # fill fields added in later versions
        for k, v in defaults.get(l["id"], {}).items():
            l.setdefault(k, v)
        l.setdefault("status", "ready")
        l.setdefault("level", "fixed" if l.get("source") == "auto" else "manual")
    return layers


def _save_layers(book_id: str, layers: list[dict]) -> None:
    _write_json(book_dir(book_id) / "layers.json", layers)


def get_layer(book_id: str, lid: str) -> dict | None:
    return next((l for l in get_layers(book_id) if l["id"] == lid), None)


def merge_layer_ui(book_id: str, incoming: list[dict]) -> list[dict]:
    """Apply UI changes (order + UI_PROPS) without clobbering server-side fields
    like status/progress of running annotators."""
    with _lock:
        current = {l["id"]: l for l in get_layers(book_id)}
        out = []
        for inc in incoming:
            l = current.pop(inc.get("id"), None)
            if l is None:
                continue
            l.update({k: v for k, v in inc.items() if k in UI_PROPS})
            out.append(l)
        out.extend(current.values())  # layers the client didn't know about yet
        _save_layers(book_id, out)
        return out


def update_layer_meta(book_id: str, lid: str, **kw) -> dict | None:
    with _lock:
        layers = get_layers(book_id)
        for l in layers:
            if l["id"] == lid:
                l.update(kw)
                _save_layers(book_id, layers)
                return l
    return None


def add_layer_meta(book_id: str, layer: dict, position: int = 1) -> None:
    with _lock:
        layers = get_layers(book_id)
        layers.insert(min(position, len(layers)), layer)
        _save_layers(book_id, layers)


def remove_layer(book_id: str, lid: str) -> None:
    with _lock:
        layers = [l for l in get_layers(book_id) if l["id"] != lid]
        _save_layers(book_id, layers)
        for name in (f"{lid}.json", f"{lid}.review.json"):
            p = book_dir(book_id) / "layers" / name
            if p.exists():
                p.unlink()
        if book_id in _books:
            _books[book_id].drop(lid)


def unique_layer_id(book_id: str, base: str) -> str:
    used = {l["id"] for l in get_layers(book_id)}
    base = slugify(base).lower()
    cand, i = base, 2
    while cand in used:
        cand, i = f"{base}_{i}", i + 1
    return cand


# ---------------------------------------------------------------- in-memory book

class Book:
    """doc.json + layer contents cached in memory, with an undo history."""

    UNDO_LIMIT = 60

    def __init__(self, book_id: str):
        self.id = book_id
        self.dir = book_dir(book_id)
        self.doc = _read_json(self.dir / "doc.json")
        if self.doc is None:
            raise FileNotFoundError(book_id)
        self.text = source_text(book_id)
        self._content: dict[str, dict] = {}
        self._index: dict[str, list] = {}
        self.undo_stack: list[tuple[str, str, object]] = []  # (description, lid, snapshot)

    # -- generic content
    def content(self, lid: str) -> dict:
        if lid == "pos":
            return {"values": self.doc["tokens"]["pos"]}
        if lid not in self._content:
            default = {"key": "start_tok", "sentences": {}} if lid == "speech" else {}
            c = _read_json(self.dir / "layers" / f"{lid}.json", default)
            if lid == "speech":
                c = self._migrate_speech(c)
            if "spans" in c:  # older files stored unlabelled [start, end]
                c["spans"] = [sp if len(sp) > 2 else [sp[0], sp[1], "Q"] for sp in c["spans"]]
            self._content[lid] = c
        return self._content[lid]

    def save_content(self, lid: str, content: dict) -> None:
        if lid == "pos":
            self.doc["tokens"]["pos"] = content["values"]
            self.save_doc()
        else:
            self._content[lid] = content
            _write_json(self.dir / "layers" / f"{lid}.json", content)
        self._index.pop(lid, None)

    def save_doc(self) -> None:
        _write_json(self.dir / "doc.json", self.doc)

    def drop(self, lid: str) -> None:
        self._content.pop(lid, None)
        self._content.pop("review:" + lid, None)
        self._index.pop(lid, None)

    def span_index(self, lid: str) -> list:
        """token index -> span index (or None) for a token-span layer."""
        if lid not in self._index:
            arr = [None] * self.doc["n_tokens"]
            for k, sp in enumerate(self.content(lid).get("spans", [])):
                for i in range(sp[0], min(sp[1], len(arr))):
                    arr[i] = k
            self._index[lid] = arr
        return self._index[lid]

    # -- speech (keyed by sentence start token so that it survives split/merge)
    def _migrate_speech(self, c: dict) -> dict:
        if c.get("key") == "start_tok":
            return c
        st = self.doc["sentences"]["start_tok"]
        c = {"key": "start_tok",
             "sentences": {str(st[int(sid)]): v for sid, v in c.get("sentences", {}).items() if int(sid) < len(st)}}
        _write_json(self.dir / "layers" / "speech.json", c)
        return c

    def speakers(self, sid: int) -> list[str] | None:
        return self.content("speech")["sentences"].get(str(self.doc["sentences"]["start_tok"][sid]))

    def n_annotated(self) -> int:
        return len(self.content("speech")["sentences"])

    # -- review state: set of sentence start tokens reviewed per layer
    def reviewed(self, lid: str) -> set[int]:
        if lid == "speech":  # a speaker annotation is the review
            return {int(k) for k in self.content("speech")["sentences"]}
        key = "review:" + lid
        if key not in self._content:
            r = _read_json(self.dir / "layers" / f"{lid}.review.json", {"reviewed": []})
            self._content[key] = set(r["reviewed"])
        return self._content[key]

    def save_reviewed(self, lid: str, rev: set[int]) -> None:
        self._content["review:" + lid] = set(rev)
        _write_json(self.dir / "layers" / f"{lid}.review.json", {"reviewed": sorted(rev)})

    def n_reviewed(self, lid: str) -> int:
        return len(self.reviewed(lid) & set(self.doc["sentences"]["start_tok"]))

    def is_reviewed(self, lid: str, sid: int) -> bool:
        return self.doc["sentences"]["start_tok"][sid] in self.reviewed(lid)

    # -- undo
    def push_undo(self, description: str, lid: str, snapshot) -> None:
        self.undo_stack.append((description, lid, snapshot))
        del self.undo_stack[:-self.UNDO_LIMIT]


_books: dict[str, Book] = {}


def book(book_id: str) -> Book:
    with _lock:
        b = _books.get(book_id)
        if b is None:
            b = Book(book_id)
            _books[book_id] = b
            if len(_books) > 4:
                _books.pop(next(iter(_books)))
        return b


def count_annotated(book_id: str) -> int:
    """Cheap count for the library view (does not load the book)."""
    return len(_read_json(book_dir(book_id) / "layers" / "speech.json", {}).get("sentences", {}))


def lock():
    return _lock


# ---------------------------------------------------------------- characters

def get_characters(book_id: str) -> list[dict]:
    return _read_json(book_dir(book_id) / "characters.json", [])


def save_characters(book_id: str, chars: list[dict]) -> None:
    _write_json(book_dir(book_id) / "characters.json", chars)


def new_character_id(chars: list[dict]) -> str:
    used = {c["id"] for c in chars}
    i = 1
    while f"c{i}" in used:
        i += 1
    return f"c{i}"


def next_color(chars: list[dict]) -> str:
    return CHARACTER_COLORS[(len(chars) - 1) % len(CHARACTER_COLORS)]
