"""Optional annotation layers that can be added to a processed book.

Each annotator turns the book's existing tokens into a new layer, so its
output is always aligned with the (possibly edited) tokenisation.

To add one: write a function `run_<id>(book, options, progress) -> (content, labels)`
and register it in ANNOTATORS. An annotator that yields several layers declares
them in "outputs" and returns {output_key: (content, labels)}.
"""
from __future__ import annotations

import bisect
import importlib.util
import re
from collections import Counter
from typing import Callable

from .store import Book

Progress = Callable[[float, str], None]

USAS_MODEL = "en_dual_none_contextual"
USAS_INSTALL = ("VIRTUAL_ENV=.venv uv pip install pymusas "
                "https://github.com/UCREL/pymusas-models/releases/download/"
                "en_dual_none_contextual-0.3.3/en_dual_none_contextual-0.3.3-py3-none-any.whl")

CLIC_INSTALL = ("brew install icu4c pkgconf && PKG_CONFIG_PATH=$(brew --prefix icu4c)/lib/pkgconfig "
                "VIRTUAL_ENV=.venv uv pip install clictagger 'setuptools<81'   "
                "(Linux: apt-get install libicu-dev pkg-config first)")

# CLiC region classes -> output layers (one run, several layers; regions of
# different classes may overlap, so each class group gets its own layer)
CLIC_OUTPUTS = [
    {"key": "quotes", "name": "CLiC quotations", "color": "#1864ab", "style": "underline",
     "regions": {"quote.quote": "quote"},
     "description": "direct speech incl. quotation marks"},
    {"key": "suspensions", "name": "CLiC suspensions", "color": "#e67700", "style": "underline",
     "regions": {"quote.suspension.short": "short", "quote.suspension.long": "long"},
     "description": "narrator interruptions inside speech (\u201c…,\u201d said Joe, \u201c…\u201d)"},
    {"key": "embedded", "name": "CLiC embedded quotations", "color": "#5f3dc4", "style": "underline",
     "regions": {"quote.embedded": "embedded"},
     "description": "quotations inside quotations"},
    {"key": "structure", "name": "CLiC structure", "color": "#495057", "style": "underline",
     "regions": {"metadata.title": "title", "metadata.author": "author", "chapter.title": "chapter title"},
     "default": False,
     "description": "book title, author, chapter headings"},
]

ANNOTATORS = {
    "usas": {
        "name": "PyMUSAS semantic tags",
        "description": "USAS semantic field per token (e.g. W3 geographical terms, M4 water movement), "
                       "rule-based English tagger incl. multi-word expressions.",
        "kind": "token-attr", "source": "auto", "color": "#0c8599",
        "requires": ["pymusas", USAS_MODEL], "install": USAS_INSTALL,
        "options": [],
    },
    "clic": {
        "name": "CLiC tagger",
        "description": "Quotations, suspensions (narrator interruptions in speech), embedded quotations and "
                       "chapter structure from the CLiC tagger (Mahlberg lab, mahlberg-lab.github.io/clictagger). "
                       "Works best on cleaned texts: title and author on lines 1–2, headings like \u201cCHAPTER 1.\u201d.",
        "kind": "token-span", "source": "auto", "color": "#1864ab", "style": "underline",
        "requires": ["clictagger", "icu", "pkg_resources"], "install": CLIC_INSTALL,
        "outputs": CLIC_OUTPUTS,
        "options": [{"id": "regions", "label": "Layers to create", "type": "checks",
                     "choices": [{"value": o["key"], "label": o["name"], "hint": o["description"],
                                  "default": o.get("default", True)} for o in CLIC_OUTPUTS]}],
    },
    "lexicon": {
        "name": "Lexicon matcher",
        "description": "Marks tokens/phrases from a word list. One entry per line: `term` or "
                       "`term<TAB>category` (also `;` or `,`). `river*` matches prefixes; matching uses lemma or word form.",
        "kind": "token-span", "source": "auto", "color": "#e8590c", "style": "underline",
        "requires": [], "install": "",
        "options": [{"id": "name", "label": "Layer name", "type": "text", "default": "Lexicon"},
                    {"id": "entries", "label": "Word list", "type": "textarea", "file": True}],
    },
    "manual-span": {
        "name": "Manual span layer (empty)",
        "description": "An empty layer for your own labelled spans, e.g. motifs, places, sounds.",
        "kind": "token-span", "source": "manual", "color": "#ae3ec9", "style": "underline",
        "requires": [], "install": "",
        "options": [{"id": "name", "label": "Layer name", "type": "text", "default": "My spans"},
                    {"id": "labels", "label": "Labels (comma-separated)", "type": "text", "default": ""}],
    },
    "manual-attr": {
        "name": "Manual token tags (empty)",
        "description": "An empty interlinear layer with one free value per token.",
        "kind": "token-attr", "source": "manual", "color": "#5c940d",
        "requires": [], "install": "",
        "options": [{"id": "name", "label": "Layer name", "type": "text", "default": "My tags"},
                    {"id": "labels", "label": "Suggested values (comma-separated)", "type": "text", "default": ""}],
    },
}


def chars_to_tokens(b: Book, cs: int, ce: int) -> tuple[int, int] | None:
    """Character range [cs, ce) of source.txt -> token range [a, z) covering it."""
    T = b.doc["tokens"]
    a = bisect.bisect_right(T["end"], cs)          # first token ending after cs
    z = bisect.bisect_left(T["start"], ce)         # tokens starting before ce
    return (a, z) if a < z else None


def available(aid: str) -> tuple[bool, str]:
    missing = [m for m in ANNOTATORS[aid]["requires"] if importlib.util.find_spec(m) is None]
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, ""


def catalogue() -> list[dict]:
    out = []
    for aid, a in ANNOTATORS.items():
        ok, msg = available(aid)
        out.append({"id": aid, **{k: v for k, v in a.items() if k != "requires"},
                    "available": ok, "missing": msg})
    return out


def _token_texts(b: Book) -> list[str]:
    T, text = b.doc["tokens"], b.text
    return [text[s:e].replace("\n", " ") for s, e in zip(T["start"], T["end"])]


def _split_labels(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


# ------------------------------------------------------------------ PyMUSAS

def run_usas(b: Book, options: dict, progress: Progress):
    import spacy
    from spacy.tokens import Doc

    progress(0.0, "loading PyMUSAS")
    tagger = spacy.load(USAS_MODEL)
    words = _token_texts(b)
    T, S = b.doc["tokens"], b.doc["sentences"]
    n_sent = len(S["start_tok"])

    def docs():
        for sid in range(n_sent):
            a, z = S["start_tok"][sid], S["end_tok"][sid]
            yield Doc(tagger.vocab, words=words[a:z],
                      spaces=[T["start"][i + 1] > T["end"][i] if i + 1 < z else False for i in range(a, z)],
                      pos=T["pos"][a:z], lemmas=T["lemma"][a:z])

    values, detail = [""] * len(words), [""] * len(words)
    for sid, doc in enumerate(tagger.pipe(docs(), batch_size=256)):
        a = S["start_tok"][sid]
        for t in doc:
            tags = t._.pymusas_tags or []
            if tags and tags[0] != "PUNCT":
                values[a + t.i] = tags[0]
                mwe = t._.pymusas_mwe_indexes
                is_mwe = bool(mwe) and mwe[0][1] - mwe[0][0] > 1
                detail[a + t.i] = " ".join(tags) + (" [MWE]" if is_mwe else "")
        if sid % 500 == 0:
            progress(sid / n_sent, f"sentence {sid}/{n_sent}")
    labels = [v for v, _ in Counter(v for v in values if v).most_common(40)]
    return {"values": values, "detail": detail}, labels


# ------------------------------------------------------------------ lexicon

def _parse_lexicon(raw: str, default_label: str):
    entries = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\t|;|,", line, maxsplit=1)
        term = parts[0].strip().lower()
        label = parts[1].strip() if len(parts) > 1 and parts[1].strip() else default_label
        if term:
            entries.append((term.split(), label))
    return entries


def run_lexicon(b: Book, options: dict, progress: Progress):
    name = options.get("name") or "Lexicon"
    entries = _parse_lexicon(options.get("entries", ""), name)
    if not entries:
        raise ValueError("the word list is empty")
    words = [w.lower() for w in _token_texts(b)]
    lemmas = [l.lower() for l in b.doc["tokens"]["lemma"]]

    def tok_match(i: int, pat: str) -> bool:
        if pat.endswith("*"):
            return words[i].startswith(pat[:-1]) or lemmas[i].startswith(pat[:-1])
        return words[i] == pat or lemmas[i] == pat

    by_first: dict[str, list] = {}
    prefix = []
    for terms, label in sorted(entries, key=lambda e: -len(e[0])):  # longest first
        if terms[0].endswith("*"):
            prefix.append((terms, label))
        else:
            by_first.setdefault(terms[0], []).append((terms, label))

    spans, n, i = [], len(words), 0
    while i < n:
        cands = by_first.get(words[i], []) + by_first.get(lemmas[i], []) + prefix
        hit = None
        for terms, label in sorted(cands, key=lambda e: -len(e[0])):
            k = len(terms)
            if i + k <= n and all(tok_match(i + j, terms[j]) for j in range(k)):
                hit = (k, label)
                break
        if hit:
            spans.append([i, i + hit[0], hit[1]])
            i += hit[0]
        else:
            i += 1
        if i % 20000 == 0:
            progress(i / n, f"token {i}/{n}")
    labels = sorted({l for _, l in entries})
    return {"spans": spans}, labels


# ------------------------------------------------------------------ manual

def run_manual_span(b: Book, options: dict, progress: Progress):
    return {"spans": []}, _split_labels(options.get("labels", ""))


def run_manual_attr(b: Book, options: dict, progress: Progress):
    return {"values": [""] * b.doc["n_tokens"]}, _split_labels(options.get("labels", ""))


# ------------------------------------------------------------------ CLiC tagger

def run_clic(b: Book, options: dict, progress: Progress):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # clictagger imports the deprecated pkg_resources
        from clictagger.taggedtext import TaggedText

    progress(0.1, "running CLiC tagger")
    tt = TaggedText(b.text)  # offsets are character positions in source.txt, like our tokens
    progress(0.8, "mapping regions to tokens")
    out = {}
    for o in CLIC_OUTPUTS:
        spans = []
        for rclass, label in o["regions"].items():
            for r in tt.regions.get(rclass, []):
                ab = chars_to_tokens(b, r[0], r[1])
                if ab:
                    spans.append([ab[0], ab[1], label])
        spans.sort()
        clean, last_end = [], -1  # a span layer holds non-overlapping spans
        for sp in spans:
            if sp[0] >= last_end:
                clean.append(sp)
                last_end = sp[1]
        out[o["key"]] = ({"spans": clean}, list(o["regions"].values()))
    return out


RUNNERS = {"clic": run_clic, "usas": run_usas, "lexicon": run_lexicon,
           "manual-span": run_manual_span, "manual-attr": run_manual_attr}
