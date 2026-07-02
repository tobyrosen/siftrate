# Changelog

All notable changes to siftrate are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and siftrate adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-02

First public release.

### Added
- Single-file, stdlib-only rating/labeling server (`siftrate.py`).
- Config-driven items with a 1–N scale (optional per-value captions),
  multi-select flags, and a free-text note — mix and match per config.
- Mobile-first, tap-to-score web UI with a live progress bar.
- Atomic, resumable results: scores merge into one JSON file, rewritten with a
  temp-file-and-rename so a crash mid-save can't corrupt it; reopening the page
  restores where you left off.
- `--token` auth gating every route (bearer header, `?token=` query, or cookie)
  for when you expose siftrate past localhost.
- `pip` / `pipx` / `uvx` install with a `siftrate` console command; still
  runnable as a single copied file.

### Security
- Binds `127.0.0.1` by default. Widen with `--host`; siftrate warns when bound
  past localhost without a `--token`.
- 32 MB cap on save payloads and strict request validation. Only `http(s)` item
  URLs render as links. No telemetry and no outbound network calls.
