# Book Annotator

A local, layered annotation tool for literary texts. Load a plain-text novel;
it is segmented into sentences and tagged with spaCy (part of speech, lemmas,
named entities, quotations). On top of that you annotate **who speaks each
sentence** (characters and/or narrator), correct or review the automatic
layers, and add your own layers: semantic tags (PyMUSAS), word lists, or
free categories. The result is an exportable, annotated corpus.

**Your texts never leave your computer.** Book Annotator runs entirely on your
machine; there is no server, account or cloud service. See
[Privacy and security](#privacy-and-security).

Website: https://gi47huwi.github.io/BookAnnotator/

## Install

You need Python 3.10–3.12. The easiest way is [uv](https://docs.astral.sh/uv/):

```bash
uv tool install --python 3.12 "bookannotator @ git+https://github.com/gi47huwi/BookAnnotator"
```

With the optional PyMUSAS semantic-tagging layer:

```bash
uv tool install --python 3.12 "bookannotator[usas] @ git+https://github.com/gi47huwi/BookAnnotator"
```

Without uv:

```bash
python3.12 -m venv ~/.bookannotator
~/.bookannotator/bin/pip install "bookannotator[usas] @ git+https://github.com/gi47huwi/BookAnnotator"
~/.bookannotator/bin/bookannotator serve
```

Installation downloads the spaCy English model (and the PyMUSAS model) once.
After that the tool works offline.

## Start

```bash
bookannotator serve
```

Your browser opens `http://127.0.0.1:8765`. Upload a `.txt` file (UTF-8,
paragraphs separated by blank lines, e.g. from Project Gutenberg) and start
annotating. A novel of ~1 MB is processed in about 20 seconds.

| command | |
|---|---|
| `bookannotator serve` | start the app (`--port 8800`, `--no-browser`, `--data DIR`) |
| `bookannotator add *.txt --author "…"` | import and process several books at once |
| `bookannotator list` | list the books in your data folder |

Books and annotations are stored in `~/BookAnnotator/books/` (one folder per
book, plain JSON files). Choose another location with `--data DIR` or the
`BOOKANNOTATOR_DATA` environment variable. **Back this folder up.**

## How it works

### Layers

Every annotation lives in its own layer, like in an image editor: show/hide,
opacity, lock, drag to reorder. Click a layer to make it **active**: the tool
panel on the left and the keyboard switch to that layer's tools.

| layer | source | tools |
|---|---|---|
| Speech / speaker | manual, with suggestions | speakers on keys `0–9`, character list with aliases, suggestions from entities |
| Sentence boundaries | spaCy | split / merge, move boundaries, list of suspicious sentences |
| Part of speech | spaCy | UPOS tagset with descriptions, alternatives, free values |
| Named entities | spaCy | label palette, add / remove / relabel spans |
| Quotations | rule-based | mark / unmark direct speech |
| PyMUSAS semantic tags | optional | USAS tags incl. multi-word expressions, alternatives |
| Lexicon matcher | optional | marks terms from your word list (`term<TAB>category`, `river*`) |
| Manual span / tag layer | optional | your own categories |

### Annotation levels

Each layer has a level that says how far its content can be trusted:

| level | meaning |
|---|---|
| **Fixed** | automatic output taken as is; locked, unlock to correct errors |
| **Suggestion** | automatic output to be checked; correct a sentence, then confirm it (`Enter`) |
| **Manual** | annotated from scratch; mark sentences done (`Enter`) |

Suggestion and manual layers track progress per sentence (○ / ✓), and the
export records which layers were reviewed in each sentence.

### Speaker suggestions

No quotation → *Narrator*. Quotation with an attribution ("… said Joe")
matching a character's name or alias → that character (+ Narrator for the
narrative part). Fully quoted sentence → the previous speaker in the
paragraph. Add aliases such as `I` or `the man` to improve matching.

### Keyboard

| | |
|---|---|
| everywhere | `↑↓`/`jk` sentence · `[` `]` page · `Tab`/`⇧Tab` next/previous annotation of the active layer (the label filter limits it) · `⌘Z` undo |
| speech | `0–9` toggle speaker · `Enter` accept suggestion · `p` same as previous · `⌫` clear · `u`/`U` next/previous unannotated |
| span / tag layers | `←→` word (shift extends) · `1–9` label · `/` type a label · `p` repeat last label · `⌫` remove · `Enter` confirm sentence · `u`/`U` next/previous unreviewed |
| sentence boundaries | `s`/`m` split or move tool · `←→` move boundary · `i` next suspicious sentence |

### Export

- **Per book** (Export button): TSV in a CoNLL-like format, one token per line
  with lemma, tag, one column per layer (values or BIO spans) and speaker(s);
  sentence comments include the speaker and review status.
- **Whole corpus** (library page): JSONL, one sentence per line with speakers.

## Privacy and security

- All processing (spaCy, PyMUSAS) runs locally. Book Annotator makes no network
  requests after installation: no telemetry, no external fonts or scripts.
- The app only listens on `127.0.0.1` and has no option to open it to the
  network. It refuses requests whose `Host` is not local (DNS rebinding) and
  API requests sent by other websites (cross-site requests), and sets a strict
  Content Security Policy.
- Texts and annotations are plain files in your data folder; nothing is stored
  elsewhere apart from the last selected layer in your browser.
- Copyright: you are responsible for the rights to the texts you annotate. Keep
  the data folder out of public repositories (it is in `.gitignore`).

See [SECURITY.md](SECURITY.md) for the security model and how to report a problem.

## Citing

If you use Book Annotator in research, please cite it, see
[CITATION.cff](CITATION.cff) (GitHub shows a "Cite this repository" button).

## Development

```bash
git clone https://github.com/gi47huwi/BookAnnotator && cd BookAnnotator
uv venv --python 3.12 && uv pip install -e ".[usas]"
.venv/bin/uvicorn bookannotator.server:app --reload --port 8765
```

In a source checkout, data is stored in `./data/books/` (ignored by git).

| module | |
|---|---|
| `pipeline.py` | text → paragraphs, sentences, tokens, POS, entities, quotations, attribution candidates |
| `store.py` | file-based storage, layer stack, review state |
| `edits.py` | editing operations with undo, sentence-boundary heuristics |
| `annotators.py` | optional layers – add a `run_<id>` function and register it to add your own |
| `server.py` | FastAPI app and local-only security checks |
| `static/` | the browser UI (plain JavaScript, no build step) |

## Licence

MIT, see [LICENSE](LICENSE). spaCy models and PyMUSAS have their own licences.
