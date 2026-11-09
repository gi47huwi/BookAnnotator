# Security

## Security model

Book Annotator is a **single-user desktop tool**. It runs a small web server on
your own computer and has no user accounts or passwords, so its protection is
that nobody else can reach it:

| threat | protection |
|---|---|
| other computers on the network | the server binds to `127.0.0.1` only; `bookannotator serve` has no option to change that |
| a malicious website sending requests to `localhost` (CSRF) | API requests with a foreign `Origin` or a cross-site / same-site `Sec-Fetch-Site` header are refused (403) |
| DNS rebinding (a website pointing its domain at 127.0.0.1 to read your data) | requests whose `Host` is not `localhost`, `127.0.0.1` or `::1` are refused (403) |
| injected content in uploaded texts | text is always inserted as escaped text; strict Content Security Policy (`script-src 'self'`), no framing |
| oversized or binary uploads | 50 MB limit, binary files are rejected |
| data leaving the machine | no telemetry, no external scripts, fonts or APIs; models are installed once with the package |

## Please do not

- expose the app to a network (e.g. `uvicorn … --host 0.0.0.0`, port forwarding,
  tunnels, reverse proxies). Anyone who reaches it can read, change and delete
  all books and annotations;
- commit your data folder to a public repository: it contains the full texts.

## Your data

Books and annotations are plain JSON files in your data folder
(`~/BookAnnotator/books/` by default). Protect it like any other research data:
disk encryption (FileVault, BitLocker), backups, and access rights according to
your institution's rules and the copyright status of the texts.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Security" tab → "Report a
vulnerability") instead of opening a public issue.
