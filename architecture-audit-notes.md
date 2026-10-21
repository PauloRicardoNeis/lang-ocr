# Architecture Audit Notes

## Scope
- Repository: `Lang`
- Audit date: 2026-04-05
- Focus: system structure, runtime relationships, maintainability, and refactor opportunities.

## Verified Runtime Topology

### Applications and entry points
- Root launcher `open_jp_catalog.cmd` starts `python JP/lang_jp.py browse`.
- Root launcher `open_jp_capture_tray.cmd` starts the Rust daemon in `JP/desktop_capture_daemon/`.
- Root launcher `open_jp_capture_logs.cmd` tails the daemon log via `JP/desktop_capture_daemon/tail_capture_log.ps1`.
- Main Python CLI entry point is `JP/lang_jp.py:5095`.
- Main Python web server entry point is `JP/lang_jp_web.py:2273`.
- Main OCR worker entry point is `JP/capture_worker.py:1281`.
- Main Rust tray/daemon entry point is `JP/desktop_capture_daemon/src/main.rs:1526`.

### High-level runtime graph
- Rust tray daemon captures the primary monitor, dedupes frames, writes spool PNGs, and inserts `capture_jobs`.
- Python worker claims `capture_jobs`, runs OCR plus Sudachi-based analysis, creates `sources`, `source_analysis_segments`, `source_analysis_candidates`, and optional source translations.
- Python CLI is the main mutation surface for database operations, review commands, flashcard generation, Anki sync, and kanji metadata refresh.
- Python web UI is primarily a read-side browser over SQLite, plus one write-like path that shells out to the CLI to run Anki sync commands.
- SQLite is the main contract between all local processes.

### Cross-process dependency direction
- `desktop_capture_daemon` depends on:
  - the SQLite schema for `capture_jobs` and `sources`
  - a Python 3.12 runtime
  - `capture_worker.py` path resolution and environment variables
- `capture_worker.py` depends on:
  - `lang_jp.py` exported functions and constants
  - `ocr_backends.py`
  - the database schema
- `lang_jp_web.py` depends on:
  - direct SQL reads from the same SQLite database
  - subprocess invocation of `lang_jp.py` for Anki sync actions
- `lang_jp.py` depends on:
  - `schema.sql`
  - `lang_jp_web.py` for `browse`
  - local AnkiConnect and external HTTP utilities for translation and kanji metadata

## Main Modules

### `JP/lang_jp.py`
- Size: 4641 lines.
- Role: central application module and CLI.
- Verified responsibilities:
  - SQLite connection setup and runtime schema upgrades (`db_connect`, `ensure_runtime_schema`)
  - source CRUD and capture-job queue helpers
  - source analysis promotion
  - term, form, segment, and kanji linking
  - derived stats maintenance
  - romaji generation and flashcard payload construction
  - AnkiConnect integration and deck reprioritization
  - parser construction for all CLI commands
  - browser handoff to `lang_jp_web.py`
- Architectural note:
  - This file is the system's effective application layer, repository layer, integration layer, and CLI layer at once.

### `JP/lang_jp_web.py`
- Size: 2082 lines.
- Role: local HTTP UI.
- Verified responsibilities:
  - HTTP server using `ThreadingHTTPServer`
  - all page rendering through inline HTML/CSS strings
  - direct SQL queries for dashboard, term/kanji/source/card pages
  - local polling endpoint for live source refresh
  - image streaming from file paths stored in the database
  - subprocess bridge back into `lang_jp.py` for sync actions
- Architectural note:
  - The UI is not a separate frontend app. It is server-rendered HTML with a small inline JS enhancement layer and no asset/build pipeline.

### `JP/capture_worker.py`
- Size: 1221 lines.
- Role: OCR worker plus local diagnostics.
- Verified responsibilities:
  - continuous or one-shot queue processing modes
  - EasyOCR-based region selection and reruns
  - Sudachi tokenization into term/subterm/kanji candidates
  - source creation and optional auto-promotion of candidates
  - English source translation through Google GTX
  - diagnostic logging and memory snapshots
- Architectural note:
  - The worker is nominally separate, but its domain coupling is high because it imports many internals directly from `lang_jp.py`.

### `JP/ocr_backends.py`
- Size: 353 lines.
- Role: shared OCR helpers.
- Verified responsibilities:
  - OCR block data structures
  - reading-order sorting
  - OCR region grouping
  - `EasyOCR` and `manga-ocr` backends
- Architectural note:
  - This is one of the few genuinely shared modules with a clean purpose and a bounded interface.

### `JP/single_image_ocr.py`
- Size: 248 lines.
- Role: manual/diagnostic OCR tool outside the passive daemon path.
- Verified responsibilities:
  - full-image/manual/overlay-box region selection
  - backend selection and OCR output writing
  - debug crop/image generation

### `JP/desktop_capture_daemon/src/main.rs`
- Size: 1447 lines.
- Role: Windows tray application and passive capture scheduler.
- Verified responsibilities:
  - tray UI and tooltip state
  - global hotkey registration
  - DXGI screen capture
  - image hashing and dedupe window checks
  - spool file persistence
  - `capture_jobs` insertion
  - spawning and monitoring the Python worker
  - capture cadence coordination, including `after source ready`
- Architectural note:
  - The daemon is more than a thin wrapper. It embeds substantial scheduling and state-machine logic, but still uses SQLite as its only durable coordination mechanism.

### `JP/schema.sql`
- Size: 337 lines.
- Role: bootstrapping schema file for `init-db`.
- Verified domains:
  - capture jobs
  - sources
  - terms/forms/source links
  - reviewed source segments
  - source-analysis drafts/candidates
  - source translations
  - term/kanji stats
  - flashcards and kanji flashcards
  - summary views
- Architectural note:
  - The schema is also mirrored in `lang_jp.py` via runtime SQL fragments and ad-hoc `ALTER TABLE` logic, so there is no single schema authority in practice.

## Data Model Summary

### Core tables
- `capture_jobs`: queue and job lifecycle for passive ingestion.
- `sources`: raw truth records for image/text/manual inputs.
- `terms`, `term_forms`: canonical vocabulary and observed forms.
- `source_terms`: canonical term links per source.
- `source_segments`, `source_segment_terms`: reviewed segment decomposition per source.
- `source_analysis_segments`, `source_analysis_candidates`: OCR/NLP draft layer before review/promotion.
- `source_translations`: machine-translation cache for sources.
- `kanji`, `source_kanji`, `kanji_stats`: kanji extraction and derived prioritization.
- `flashcards`, `kanji_flashcards`: generated study payloads plus Anki note linkage.
- `term_stats`, `kanji_stats`: derived caches for ranking.

### Key architectural pattern
- `sources` is the durable raw layer.
- `source_analysis_*` is the draft/assist layer.
- `source_terms` and `source_segment_terms` are the curated/published vocabulary layer.
- `term_stats` and `kanji_stats` are derived caches.
- `flashcards` are downstream materializations for Anki/export.

## Control Flow and Data Flow

### Passive capture path
1. Tray daemon captures a frame and computes SHA-256 plus perceptual hash.
2. Daemon writes a spool PNG to `JP/data/capture_spool` and inserts a `capture_jobs` row.
3. Python worker claims one pending/failed job.
4. Worker runs OCR, filters/regions blocks, and builds `raw_text`.
5. Worker moves the accepted image into `JP/data/captures/YYYY/MM/DD/...`.
6. Worker inserts a pending `sources` row plus source-analysis segments and candidates.
7. Worker optionally auto-promotes candidates into `source_terms` and `source_segment_terms`.
8. Worker optionally stores an English machine translation for the source.
9. CLI or web browsing surfaces the resulting source and downstream term/kanji/card views.

### Manual catalog path
1. User adds a source or links terms via `lang_jp.py` commands.
2. `lang_jp.py` writes canonical term/link data and recalculates stats.
3. Flashcard generation materializes candidate cards from ranked terms/kanji.
4. Anki sync pushes or updates local Anki notes and records note ids back into SQLite.

### Web path
1. Web UI executes direct SQL queries against SQLite for every page request.
2. Source images are read directly from filesystem paths stored in SQLite.
3. Anki sync buttons shell back into `lang_jp.py` through subprocess calls.

## Technology Inventory Notes

### Python standard library core
- `lang_jp.py` and `lang_jp_web.py` rely heavily on stdlib modules such as `argparse`, `sqlite3`, `urllib`, `http.server`, and `webbrowser`.
- Likely rationale:
  - keep the core catalog/browser usable without heavyweight dependencies
  - minimize setup cost for the local database and UI paths

### SQLite
- Used by every runtime component.
- WAL mode and `busy_timeout` are enabled in Python and Rust connectors.
- Likely rationale:
  - local-first single-user system with multiple cooperating processes
  - queue, catalog, and derived-study state can all live in one durable store

### Rust daemon stack
- `windows-capture`, `trayicon`, `winit`, `rusqlite`, `tokio`.
- Likely rationale:
  - native tray app, hotkeys, and DXGI capture are better served by Rust/Win32 than Python

### OCR and NLP stack
- `easyocr`, `manga-ocr`, `Pillow`, `opencv-python-headless`, `numpy`, `SudachiPy`, `sudachidict_core`.
- Dependency declaration drift exists:
  - `requirements-capture.txt` still includes `paddleocr` and `paddlepaddle`, but the current worker path is EasyOCR-based.

### External HTTP integrations
- Google GTX translate endpoint in `lang_jp.py` and `capture_worker.py`
- `kanjiapi.dev` in `lang_jp.py`
- AnkiConnect in `lang_jp.py`

### Front-end technology
- No JS framework, no TypeScript, no bundler, no CSS pipeline.
- UI is server-rendered HTML with inline CSS/JS inside `lang_jp_web.py`.

## Tooling and Workflow Notes

### What exists
- Rust uses `Cargo.toml` and `Cargo.lock`.
- OCR worker dependencies are declared in `JP/requirements-capture.txt`.
- Launchers exist for browser UI, tray daemon, and log tailing.
- `python -m unittest JP.test_capture_draft` currently passes with 8 tests.

### What is absent
- No `pyproject.toml`, `setup.py`, `pytest.ini`, `mypy.ini`, `ruff.toml`, `pre-commit` config, Makefile/justfile, or CI workflow files were found.
- No formal package structure for the Python code.
- No root dependency manifest for the overall Python environment.

## Observed Runtime State From Live Database
- `sources`: 599
- `terms`: 2554
- `source_terms`: 9992
- `source_analysis_candidates`: 12304
- `flashcards`: 2305
- `kanji_flashcards`: 898
- `capture_jobs`: 430
- Review distribution:
  - `pending`: 397
  - `reviewed`: 202
- Ingest distribution:
  - `auto_capture`: 399
  - `manual`: 200
- Translation rows:
  - 213 translated EN rows
  - 1 failed EN row
- Operational signal:
  - 6 `capture_jobs` rows are stuck in `processing`
  - 395 of 397 pending sources currently have zero pending candidates
  - 394 pending sources already have published `source_terms`

## Architecture Strengths
- Strong local-first architecture with SQLite as a shared durable contract.
- Clear end-to-end learning workflow from source capture through Anki synchronization.
- Core catalog/browser paths avoid unnecessary dependency bloat by staying largely stdlib-based.
- OCR abstractions were extracted into `ocr_backends.py`, which is a step toward cleaner boundaries.
- Rust is used where it provides real value: tray UX, capture, hotkeys, and background scheduling.

## Architecture Weaknesses Confirmed
- `lang_jp.py` remains the dominant coupling point across nearly all layers.
- Schema definition lives in both `schema.sql` and runtime SQL fragments in `lang_jp.py`.
- The worker depends on internals from the monolith instead of a smaller shared service/data layer.
- The web UI is read-directly-from-DB but shells out to the CLI for side effects, creating awkward hybrid boundaries.
- Review status, analysis-candidate status, and published term-link state do not form a clean lifecycle model.
- Operational recovery around stuck processing jobs is incomplete.
- Documentation truth level is mixed:
  - high-level READMEs are useful
  - some spec/review files and launcher behavior have drifted from the current implementation
