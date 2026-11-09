"""Editing operations on layers. Every operation records an undo snapshot.

Snapshots are dicts with a "type":
    content   layer content (spans / values / speech)
    seg       sentence arrays + paragraph index + speech
    review    reviewed-sentence set of one layer
    level     layer level + lock state + (optionally) content
"""
from __future__ import annotations

import bisect
import copy

from . import store
from .store import Book


class EditError(ValueError):
    pass


def _mark_edited(b: Book, lid: str) -> None:
    layer = store.get_layer(b.id, lid)
    if layer and layer.get("source") == "auto" and not layer.get("edited"):
        store.update_layer_meta(b.id, lid, edited=True)


def _content_snap(b: Book, lid: str) -> dict:
    return {"type": "content", "content": copy.deepcopy(b.content(lid))}


def _seg_snap(b: Book) -> dict:
    return {"type": "seg", "sentences": copy.deepcopy(b.doc["sentences"]),
            "para_start_sent": list(b.doc["paragraphs"]["start_sent"]),
            "speech": copy.deepcopy(b.content("speech"))}


# ------------------------------------------------------------ token spans

def set_span(b: Book, lid: str, start: int, end: int, label: str) -> None:
    """Replace any spans overlapping [start, end) with one labelled span."""
    if not 0 <= start < end <= b.doc["n_tokens"]:
        raise EditError("invalid token range")
    b.push_undo(f"label {label}", lid, _content_snap(b, lid))
    c = b.content(lid)
    spans = [sp for sp in c.get("spans", []) if sp[1] <= start or sp[0] >= end]
    spans.append([start, end, label])
    spans.sort()
    b.save_content(lid, {**c, "spans": spans})
    _mark_edited(b, lid)


def clear_spans(b: Book, lid: str, start: int, end: int) -> int:
    c = b.content(lid)
    spans = c.get("spans", [])
    keep = [sp for sp in spans if sp[1] <= start or sp[0] >= end]
    if len(keep) == len(spans):
        return 0
    b.push_undo("remove spans", lid, _content_snap(b, lid))
    b.save_content(lid, {**c, "spans": keep})
    _mark_edited(b, lid)
    return len(spans) - len(keep)


# ------------------------------------------------------------ token attributes

def set_attr(b: Book, lid: str, tokens: list[int], value: str) -> None:
    n = b.doc["n_tokens"]
    if not tokens or any(not 0 <= i < n for i in tokens):
        raise EditError("invalid tokens")
    b.push_undo(f"set {value or 'empty'}", lid, _content_snap(b, lid))
    c = b.content(lid)
    values = list(c.get("values") or [""] * n)
    detail = list(c["detail"]) if c.get("detail") else None
    for i in tokens:
        values[i] = value
        if detail is not None:
            detail[i] = value  # manual value replaces the tagger's alternatives
    new = {**c, "values": values}
    if detail is not None:
        new["detail"] = detail
    b.save_content(lid, new)
    _mark_edited(b, lid)


# ------------------------------------------------------------ segmentation

def _rebuild_para_index(b: Book) -> None:
    sent_para = b.doc["sentences"]["para"]
    n_par = b.doc["n_paragraphs"]
    start_sent = [None] * n_par
    for sid, p in enumerate(sent_para):
        if start_sent[p] is None:
            start_sent[p] = sid
    nxt = len(sent_para)
    for p in range(n_par - 1, -1, -1):
        if start_sent[p] is None:
            start_sent[p] = nxt
        nxt = start_sent[p]
    b.doc["paragraphs"]["start_sent"] = start_sent
    b.doc["n_sentences"] = len(sent_para)


def sentence_of_token(b: Book, tok: int) -> int:
    st = b.doc["sentences"]["start_tok"]
    sid = bisect.bisect_right(st, tok) - 1
    if sid < 0 or tok >= b.doc["sentences"]["end_tok"][sid]:
        raise EditError("token outside any sentence")
    return sid


def split_sentence(b: Book, tok: int) -> int:
    """Start a new sentence at token `tok`. Returns the new sentence id."""
    S = b.doc["sentences"]
    sid = sentence_of_token(b, tok)
    if S["start_tok"][sid] == tok:
        raise EditError("token already starts a sentence")
    b.push_undo("split sentence", "sentences", _seg_snap(b))
    S["start_tok"].insert(sid + 1, tok)
    S["end_tok"].insert(sid + 1, S["end_tok"][sid])
    S["end_tok"][sid] = tok
    S["para"].insert(sid + 1, S["para"][sid])
    S["attrib"].insert(sid + 1, [])
    _rebuild_para_index(b)
    b.save_doc()
    _mark_edited(b, "sentences")
    return sid + 1


def merge_sentences(b: Book, sid: int) -> None:
    """Merge sentence `sid` into the previous sentence (same paragraph only)."""
    S = b.doc["sentences"]
    if not 0 < sid < len(S["start_tok"]):
        raise EditError("no previous sentence")
    if S["para"][sid] != S["para"][sid - 1]:
        raise EditError("sentences are in different paragraphs")
    b.push_undo("merge sentences", "sentences", _seg_snap(b))
    removed_start = S["start_tok"][sid]
    S["end_tok"][sid - 1] = S["end_tok"][sid]
    S["attrib"][sid - 1] = S["attrib"][sid - 1] + S["attrib"][sid]
    for k in ("start_tok", "end_tok", "para", "attrib"):
        del S[k][sid]
    _rebuild_para_index(b)
    b.save_doc()
    sp = b.content("speech")
    if str(removed_start) in sp["sentences"]:
        sp = copy.deepcopy(sp)
        del sp["sentences"][str(removed_start)]
        b.save_content("speech", sp)
    _mark_edited(b, "sentences")


def move_boundary(b: Book, sid: int, tok: int) -> None:
    """Move the start of sentence `sid` (= end of sentence sid-1) to token `tok`."""
    S = b.doc["sentences"]
    if not 0 < sid < len(S["start_tok"]):
        raise EditError("the first sentence of the book has no boundary to move")
    if S["para"][sid] != S["para"][sid - 1]:
        raise EditError("paragraph boundaries cannot be moved")
    if not S["start_tok"][sid - 1] < tok < S["end_tok"][sid]:
        raise EditError("a boundary cannot move past another boundary")
    if tok == S["start_tok"][sid]:
        return
    b.push_undo("move boundary", "sentences", _seg_snap(b))
    old = S["start_tok"][sid]
    S["start_tok"][sid] = tok
    S["end_tok"][sid - 1] = tok
    # keep the speakers attached to the (moved) sentence
    sp = b.content("speech")
    if str(old) in sp["sentences"]:
        sp = copy.deepcopy(sp)
        sp["sentences"][str(tok)] = sp["sentences"].pop(str(old))
        b.save_content("speech", sp)
    for lid in _review_layers(b):
        rev = b.reviewed(lid)
        if old in rev:
            b.save_reviewed(lid, (rev - {old}) | {tok})
    b.save_doc()
    _mark_edited(b, "sentences")


def _review_layers(b: Book) -> list[str]:
    return [l["id"] for l in store.get_layers(b.id) if l.get("level") != "fixed" and l["id"] != "speech"]


SENT_END = {".", "!", "?", "…", ";", ":", "—", "-", "--"}
CLOSERS = {'"', "”", "’", "'", ")", "]", "''"}
ABBREV = {"mr", "mrs", "dr", "st", "messrs", "mme", "esq", "capt", "col", "gen", "rev", "no", "vol", "ch"}


def sentence_issue(b: Book, sid: int) -> str | None:
    """Heuristic hint that a sentence boundary may be wrong."""
    S, T, text = b.doc["sentences"], b.doc["tokens"], b.text
    a, z = S["start_tok"][sid], S["end_tok"][sid]
    words = [text[T["start"][i]:T["end"][i]] for i in range(a, z)]
    alpha = [w for w in words if any(ch.isalpha() for ch in w)]
    same_para_next = sid + 1 < len(S["para"]) and S["para"][sid + 1] == S["para"][sid]
    same_para_prev = sid > 0 and S["para"][sid - 1] == S["para"][sid]
    if len(words) > 100:
        return f"very long ({len(words)} tokens)"
    core = [w for w in words if w not in CLOSERS]
    last = core[-1] if core else ""
    if same_para_next and last.rstrip(".").lower() in ABBREV:
        return f"ends with abbreviation “{last}”"
    if same_para_next and last not in SENT_END and not last.endswith("."):
        return "no final punctuation"
    if same_para_prev and alpha and alpha[0][0].islower():
        return "starts lowercase"
    if not alpha and (same_para_prev or same_para_next):
        return "no words (stray punctuation?)"
    return None


# ------------------------------------------------------------ speech

def set_speech(b: Book, updates: dict[int, list[str]]) -> None:
    b.push_undo("speakers", "speech", _content_snap(b, "speech"))
    sp = copy.deepcopy(b.content("speech"))
    st = b.doc["sentences"]["start_tok"]
    for sid, speakers in updates.items():
        key = str(st[sid])
        if speakers:
            sp["sentences"][key] = speakers
        else:
            sp["sentences"].pop(key, None)
    b.save_content("speech", sp)


# ------------------------------------------------------------ review & level

def set_reviewed(b: Book, lid: str, sids: list[int], value: bool) -> int:
    st = b.doc["sentences"]["start_tok"]
    keys = {st[s] for s in sids if 0 <= s < len(st)}
    rev = b.reviewed(lid)
    new = (rev | keys) if value else (rev - keys)
    if new == rev:
        return 0
    b.push_undo("confirm" if value else "unconfirm", lid, {"type": "review", "reviewed": set(rev)})
    b.save_reviewed(lid, new)
    return len(keys)


def set_level(b: Book, lid: str, level: str, clear: bool = False) -> dict:
    layer = store.get_layer(b.id, lid)
    if level not in store.allowed_levels(layer):
        raise EditError(f"level “{level}” is not available for {layer['name']}")
    snap = {"type": "level", "level": layer.get("level"), "locked": layer.get("locked"), "content": None}
    if clear and level == "manual" and layer["kind"] in ("token-span", "token-attr"):
        snap["content"] = copy.deepcopy(b.content(lid))
        empty = {"spans": []} if layer["kind"] == "token-span" else {"values": [""] * b.doc["n_tokens"]}
        b.save_content(lid, empty)
    b.push_undo(f"level {level}", lid, snap)
    return store.update_layer_meta(b.id, lid, level=level, locked=(level == "fixed"))


# ------------------------------------------------------------ undo

def undo(b: Book) -> dict | None:
    if not b.undo_stack:
        return None
    desc, lid, snap = b.undo_stack.pop()
    t = snap["type"]
    if t == "seg":
        b.doc["sentences"] = snap["sentences"]
        b.doc["paragraphs"]["start_sent"] = snap["para_start_sent"]
        b.doc["n_sentences"] = len(snap["sentences"]["start_tok"])
        b.save_doc()
        b.save_content("speech", snap["speech"])
    elif t == "content":
        b.save_content(lid, snap["content"])
    elif t == "review":
        b.save_reviewed(lid, snap["reviewed"])
    elif t == "level":
        if snap["content"] is not None:
            b.save_content(lid, snap["content"])
        store.update_layer_meta(b.id, lid, level=snap["level"], locked=snap["locked"])
    return {"undone": desc, "layer": lid}
