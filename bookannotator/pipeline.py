"""Preprocessing: plain-text book -> paragraphs, sentences, tokens (POS, lemma),
named entities, quotation spans and speech-attribution candidates.

All positions are stored columnar (lists of ints/strings) to keep large novels
(~500k tokens) compact. Token offsets are Python string indices into source.txt.
"""
from __future__ import annotations

import re
from typing import Callable

import spacy

MODEL = "en_core_web_sm"
SENTENCES_PER_PAGE = 80

_nlp = None

# verbs that introduce or attribute direct speech ("said Joe", "she replied")
SPEECH_VERBS = {
    "say", "ask", "reply", "cry", "exclaim", "return", "answer", "whisper",
    "observe", "remark", "rejoin", "retort", "add", "continue", "murmur",
    "shout", "inquire", "enquire", "repeat", "call", "growl", "mutter", "sob",
    "scream", "declare", "interpose", "pursue", "resume", "urge", "suggest",
    "explain", "protest", "demand", "tell", "begin", "laugh", "sigh", "groan",
}
OPEN_QUOTES = {"“", "«"}
CLOSE_QUOTES = {"”", "»"}
TOGGLE_QUOTES = {'"', "''", "``"}
CHAPTER_RE = re.compile(
    r"^\s*(chapter|book|part|volume|stave|preface|prologue|epilogue)\b[\s\w.:,\-–—]*$",
    re.IGNORECASE,
)
PARA_RE = re.compile(r"\n[ \t\r]*\n")


def get_nlp():
    global _nlp
    if _nlp is None:
        _nlp = spacy.load(MODEL)
        _nlp.max_length = 5_000_000
    return _nlp


def split_paragraphs(text: str) -> list[tuple[int, int]]:
    """Paragraphs are separated by blank lines. Returns (start, end) offsets,
    trimmed of surrounding whitespace, skipping empty ones."""
    spans, pos = [], 0
    for m in list(PARA_RE.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        chunk = text[pos:end]
        lstrip = len(chunk) - len(chunk.lstrip())
        rstrip = len(chunk) - len(chunk.rstrip())
        s, e = pos + lstrip, end - rstrip
        if e > s:
            spans.append((s, e))
        if m:
            pos = m.end()
    return spans


def _quote_flags(tokens: list[str]) -> list[bool]:
    """Mark tokens inside direct-speech quotation marks (quote marks included).
    State is reset per paragraph by the caller."""
    flags, inside = [], False
    for t in tokens:
        if t in OPEN_QUOTES:
            inside = True
            flags.append(True)
        elif t in CLOSE_QUOTES:
            flags.append(True)
            inside = False
        elif t in TOGGLE_QUOTES:
            flags.append(True)
            inside = not inside
        else:
            flags.append(inside)
    return flags


def _subject_text(verb) -> str | None:
    for child in verb.children:
        if child.dep_ in ("nsubj", "nsubjpass"):
            span = [t for t in child.subtree if t.dep_ not in ("punct", "relcl", "acl", "prep")]
            if not span:
                return child.text
            return " ".join(t.text for t in span)
    return None


def process_text(
    text: str,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Run the full pipeline. Returns a dict with doc structure and auto layers."""
    nlp = get_nlp()
    paragraphs = split_paragraphs(text)
    n_par = len(paragraphs)

    tok_start, tok_end, tok_pos, tok_tag, tok_lemma, tok_quote = [], [], [], [], [], []
    sent_start_tok, sent_end_tok, sent_para, sent_attrib = [], [], [], []
    para_start_sent, chapters = [], []
    entities = []  # [start_tok, end_tok_exclusive, label]

    # newlines inside a paragraph are hard line wraps: replace them with spaces
    # so spaCy sees running text; offsets stay identical.
    para_texts = (text[s:e].replace("\n", " ").replace("\r", " ") for s, e in paragraphs)

    for p_idx, doc in enumerate(nlp.pipe(para_texts, batch_size=64)):
        p_off = paragraphs[p_idx][0]
        para_start_sent.append(len(sent_start_tok))
        if CHAPTER_RE.match(doc.text) and len(doc.text) < 80:
            chapters.append({"title": doc.text.strip(), "para": p_idx})

        # map spaCy token index -> our global token index (whitespace tokens dropped)
        local = {}
        para_tok_texts = []
        for t in doc:
            if t.is_space:
                continue
            local[t.i] = len(tok_start)
            tok_start.append(p_off + t.idx)
            tok_end.append(p_off + t.idx + len(t.text))
            tok_pos.append(t.pos_)
            tok_tag.append(t.tag_)
            tok_lemma.append(t.lemma_)
            para_tok_texts.append(t.text)
        tok_quote.extend(_quote_flags(para_tok_texts))

        for sent in doc.sents:
            ids = [local[t.i] for t in sent if t.i in local]
            if not ids:
                continue
            sent_start_tok.append(ids[0])
            sent_end_tok.append(ids[-1] + 1)
            sent_para.append(p_idx)
            # speech attribution candidates: subjects of speech verbs outside quotes
            cands = []
            for t in sent:
                if t.i in local and t.pos_ == "VERB" and t.lemma_.lower() in SPEECH_VERBS \
                        and not tok_quote[local[t.i]]:
                    subj = _subject_text(t)
                    if subj:
                        cands.append(subj)
            sent_attrib.append(cands)

        for ent in doc.ents:
            ids = [local[t.i] for t in ent if t.i in local]
            if ids:
                entities.append([ids[0], ids[-1] + 1, ent.label_])

        if progress and (p_idx % 50 == 0 or p_idx == n_par - 1):
            progress((p_idx + 1) / max(n_par, 1), f"paragraph {p_idx + 1}/{n_par}")

    quotes = _flags_to_spans(tok_quote)

    # pages: groups of whole paragraphs, ~SENTENCES_PER_PAGE each, new page at chapters
    chapter_paras = {c["para"] for c in chapters}
    pages, count = [], 0
    for p_idx in range(n_par):
        n_s = (para_start_sent[p_idx + 1] if p_idx + 1 < n_par else len(sent_start_tok)) - para_start_sent[p_idx]
        if not pages or count >= SENTENCES_PER_PAGE or (p_idx in chapter_paras and count > 0):
            pages.append(p_idx)
            count = 0
        count += n_s
    for c in chapters:
        c["page"] = max(i for i, p in enumerate(pages) if p <= c["para"])

    return {
        "doc": {
            "model": MODEL,
            "n_tokens": len(tok_start),
            "n_sentences": len(sent_start_tok),
            "n_paragraphs": n_par,
            "tokens": {"start": tok_start, "end": tok_end, "pos": tok_pos, "tag": tok_tag, "lemma": tok_lemma},
            "sentences": {"start_tok": sent_start_tok, "end_tok": sent_end_tok, "para": sent_para, "attrib": sent_attrib},
            "paragraphs": {"start": [s for s, _ in paragraphs], "end": [e for _, e in paragraphs], "start_sent": para_start_sent},
            "pages": pages,
            "chapters": chapters,
        },
        "layers": {
            "entities": {"spans": entities},
            "quotes": {"spans": quotes},
        },
    }


def _flags_to_spans(flags: list[bool]) -> list[list[int]]:
    spans, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            spans.append([start, i])
            start = None
    if start is not None:
        spans.append([start, len(flags)])
    return spans
