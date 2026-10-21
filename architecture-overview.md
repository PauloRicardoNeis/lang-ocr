# Architecture Overview

## System Summary

This repository is a local Japanese-study catalog centered on the `JP/` application. Its primary workflow is:

1. capture or import a source
2. preserve it in SQLite as raw truth
3. generate OCR/NLP drafts
4. promote canonical terms and kanji links
5. derive study priority
6. generate and sync flashcards to Anki

The system is local-first and single-user in design. Its main integration boundary is SQLite, not HTTP or message queues.

## Main Runtime Pieces

### 1. Python core CLI
- Path: `JP/lang_jp.py`
- Purpose:
  - database access
  - runtime schema upgrades
  - source and term management
  - stats recalculation
  - flashcard generation
  - Anki and kanji-metadata integration
- Architectural character:
  - this is the central application layer and currently also the main coupling hub

### 2. Local web UI
- Path: `JP/lang_jp_web.py`
- Purpose:
  - browse terms, kanji, sources, and flashcards
  - show source images and translations
  - trigger Anki sync actions
- Architectural character:
  - server-rendered HTML with inline CSS/JS
  - read-side uses direct SQL
  - write-side shells back into the CLI for sync actions

### 3. OCR worker
- Path: `JP/capture_worker.py`
- Purpose:
  - claim queued capture jobs
  - run OCR and Japanese token analysis
  - create sources and analysis candidates
  - optionally auto-promote terms
- Architectural character:
  - separate process, but strongly coupled to `lang_jp.py`

### 4. Desktop capture daemon
- Path: `JP/desktop_capture_daemon/src/main.rs`
- Purpose:
  - tray UX
  - passive capture scheduling
  - global hotkey handling
  - spool file creation
  - capture-job enqueueing
  - worker spawning/monitoring
- Architectural character:
  - native Windows utility with substantial local orchestration logic

### 5. Shared OCR helper layer
- Path: `JP/ocr_backends.py`
- Purpose:
  - OCR block structures
  - shared region grouping and reading-order logic
  - `EasyOCR` and `manga-ocr` backends
- Architectural character:
  - one of the cleaner modular seams in the repo

### 6. Data and outputs
- Main DB: `JP/data/lang_jp.sqlite3`
- Raw captures: `JP/data/captures/`
- Spool area: `JP/data/capture_spool/`
- Logs: `JP/data/capture_daemon.log`
- Exports: `JP/exports/`

## Dependency Direction

### Runtime dependency graph
- `desktop_capture_daemon` -> SQLite `capture_jobs` -> `capture_worker.py`
- `capture_worker.py` -> SQLite `sources` and analysis tables
- `lang_jp.py` -> SQLite domain tables/views -> AnkiConnect and `kanjiapi.dev`
- `lang_jp_web.py` -> SQLite read model
- `lang_jp_web.py` -> subprocess -> `lang_jp.py` for sync side effects

### External integrations
- AnkiConnect:
  - local HTTP API for note/card sync and reprioritization
- `kanjiapi.dev`:
  - public metadata source for canonical kanji meanings/readings
- Google GTX translate endpoint:
  - used for term/source translation assistance

## Boundary Assessment

### Clean boundaries
- Rust capture and Windows tray concerns are isolated from OCR/NLP details.
- SQLite is a clear shared persistence boundary.
- OCR backend extraction into `ocr_backends.py` is a good modular move.

### Leaky boundaries
- `lang_jp.py` mixes schema, repositories, domain logic, CLI concerns, flashcard formatting, HTTP integrations, and orchestration.
- `capture_worker.py` imports many internals directly from `lang_jp.py`, so the worker boundary is process-level but not module-level.
- `lang_jp_web.py` duplicates some domain helpers and reads the DB directly, while also shelling out to the CLI for side effects.
- Schema definition lives both in `schema.sql` and in runtime SQL fragments inside `lang_jp.py`.

## Operational Reality

The live database confirms the system is under active use:

- 599 sources
- 2554 terms
- 9992 source-term links
- 2305 flashcards
- 898 kanji flashcards
- 430 capture jobs

That matters architecturally because several current weaknesses are no longer theoretical:

- there are stale `processing` jobs
- the pending-review queue is large
- many pending sources already have promoted terms, which means workflow states overlap in practice
