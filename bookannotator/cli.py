"""Command line interface.

    bookannotator serve [--port 8765] [--data DIR] [--no-browser]
    bookannotator add path/to/*.txt [--author "Charles Dickens"]
    bookannotator list
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path


def main(argv=None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", type=Path, help="data folder (default: ~/BookAnnotator/books, "
                                                   "or $BOOKANNOTATOR_DATA)")
    ap = argparse.ArgumentParser(prog="bookannotator", description="Local annotation tool for literary texts.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", parents=[common], help="start the annotation app in your browser")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    add = sub.add_parser("add", parents=[common], help="import and preprocess .txt files")
    add.add_argument("files", nargs="+", type=Path)
    add.add_argument("--author", default="")
    sub.add_parser("list", parents=[common], help="list books")
    args = ap.parse_args(argv)

    # the data folder must be known before the store module is imported
    if args.data:
        os.environ["BOOKANNOTATOR_DATA"] = str(args.data.expanduser().resolve())
    from . import store

    if args.cmd == "serve":
        import uvicorn

        from .server import app
        url = f"http://127.0.0.1:{args.port}/"
        print(f"Book Annotator – data folder: {store.DATA_DIR}")
        print(f"Open {url}  (only reachable from this computer; Ctrl+C to stop)")
        if not args.no_browser:
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        # deliberately bound to the loopback interface: the app has no user accounts
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        return

    if args.cmd == "list":
        for b in store.list_books():
            print(f"{b['id']:20} {b['status']:10} {b.get('n_sentences', ''):>8}  {b['title']}")
        return

    from .server import run_pipeline
    for path in args.files:
        text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
        bid = store.unique_id(path.stem)
        store.create_book(bid, path.stem, args.author, text)
        print(f"{bid}: processing ...", end="", flush=True)
        run_pipeline(bid)
        meta = store.get_meta(bid)
        print(f" {meta['status']} ({meta.get('n_sentences', 0)} sentences)", file=sys.stdout)


if __name__ == "__main__":
    main()
