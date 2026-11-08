# Architecture

How Lang is built: what the pieces are, how data moves between them, and why it is shaped this way. For commands and day-to-day use see `README.md` and `JP/README.md`; for the polyglot requirements and the full decision log see `polyglot-plan.md`.

> `architecture-overview.md` is an earlier snapshot (Japanese-only era, April 2026). This document supersedes it.

## 1. What the system is

Lang is a **local, single-user vocabulary catalog** built from real screens. You play a game or browse; screenshots (or pasted text) become *sources*; OCR and per-language analysis turn those sources into *candidate words*; the words are promoted into canonical *terms*; recurrence across sources ranks what to study; flashcards go to Anki, one deck per language.

Three principles drive the design:

1. **The source is the raw truth.** Images and raw text are preserved. Everything after them (OCR, segmentation, terms, stats, cards) is a derivation that can be reviewed, corrected or rebuilt.
2. **Machine output is a draft.** OCR and tokenization produce analysis segments and candidates with an approval status; humans (or an explicit auto-promotion rule) decide what becomes canonical.
3. **SQLite is the integration boundary.** There is no message broker and no service API between components. The tray daemon, the OCR worker, the CLI and the web UI all meet in one database file, `JP/data/lang_jp.sqlite3`.

The `JP/` folder keeps its historical name; since October 2026 the catalog is polyglot end to end.

## 2. Components

```
                         ┌─────────────────────────────────────────┐
  open_lang.cmd ───────► │ desktop_capture_daemon (Rust, tray)     │
                         │  · screen capture (windows-capture)      │
                         │  · sha256 + perceptual-hash dedupe       │
                         │  · spool PNG + INSERT capture_jobs       │
                         │  · foreground window title / process     │
                         │  · supervises worker + web server        │
                         └───────┬─────────────────────┬───────────┘
                     spawns      │                     │ spawns, restarts with backoff
                                 ▼                     ▼
         ┌──────────────────────────────┐   ┌──────────────────────────────┐
         │ capture_worker.py (Python)   │   │ lang_jp_web.py (Python)      │
         │  · claims jobs               │   │  · ThreadingHTTPServer :8766 │
         │  · ocr_backends.py engines   │   │  · server-rendered HTML      │
         │  · languages.py analysis     │   │  · JSON API /api/*           │
         │  · creates pending sources   │   │  · writes via lang_jp / CLI  │
         └──────────────┬───────────────┘   └──────────────┬───────────────┘
                        │      imports lang_jp.py (domain)  │
                        ▼                                   ▼
                ┌───────────────────────────────────────────────────┐
                │  JP/data/lang_jp.sqlite3   (schema.sql + runtime   │
                │  upgrades in lang_jp.ensure_runtime_schema)        │
                └───────────────────────────────────────────────────┘
                        ▲                                   │
                lang_jp.py CLI (manual sources,             ▼
                review, stats, cards)            AnkiConnect :8765 / TSV export
```

| Component | Path | Role |
|---|---|---|
| Domain core + CLI | `JP/lang_jp.py` | Schema upgrades, sources, terms, segments, kanji, stats, flashcards, Anki sync, translation helpers. Every other Python process imports it. |
| Language layer | `JP/languages.py` | Language profiles, offline detection, script-run splitting, analyzers, romanizers, junk-word rules. |
| OCR layer | `JP/ocr_backends.py` | `OcrBlock` model, meikiocr / EasyOCR / manga-ocr backends, engine planning per language set, line filters, cross-engine merge, reading order. |
| Capture worker | `JP/capture_worker.py` | Consumes `capture_jobs`, runs OCR + analysis, writes pending sources and candidates, auto-promotes, fills meanings and translations. |
| Tray daemon | `JP/desktop_capture_daemon/src/main.rs` | Windows tray app: capture scheduling, hotkey, dedupe, enqueue, process supervision, capture-language menu. |
| Web UI | `JP/lang_jp_web.py` | Local browser for terms, kanji, sources, cards; `/capture` settings page; JSON API. |
| Dictionary | `JP/jmdict.py` | Imports JMdict into a separate `data/jmdict.sqlite3` and looks up Japanese meanings. |
| Maintenance scripts | `rebuild_auto_sources.py`, `compress_source_images.py`, `single_image_ocr.py`, `ocr_bench/` | Re-run capture analysis on old sources, shrink stored images, one-off OCR, OCR benchmarking against `gt.json`. |
| Bootstrap | `setup_windows.ps1`, `open_lang.cmd` | Build the two venvs and the daemon; single entry point that starts (or reuses) the tray and opens the catalog. |

### Why two languages and two Python environments

- **Rust for the daemon** because it is a long-lived Windows desktop process: tray icon (`trayicon`, `winit`), global hotkey and foreground-window lookup (`windows-sys`), GPU-surface screen capture (`windows-capture`, including HDR formats converted from RGBA16F / RGB10A2), hashing (`sha2`, `img_hash`) and direct SQLite writes (`rusqlite`, bundled). It must stay small and responsive while OCR is heavy.
- **Python for everything linguistic** because that is where the ecosystem is: Sudachi, simplemma, jieba, pypinyin, EasyOCR/PyTorch, meikiocr.
- **Two venvs**: `.venv` (catalog, light `requirements-main.txt`) and `.venv-capture312` (OCR stack, `requirements-capture.txt`, pinned to Python 3.12 for wheel availability). The daemon launches the worker with the capture interpreter.

## 3. Data model

Defined in `JP/schema.sql`; applied and upgraded in place by `lang_jp.ensure_runtime_schema` on every command (no migration tool). The daemon also adds the columns it needs itself (`ensure_capture_job_window_columns`) so capture never fails against an older database.

**Layers, from raw to derived:**

| Layer | Tables | Notes |
|---|---|---|
| Configuration | `languages`, `capture_profiles` | `languages.enabled`, `capture_enabled`, `deck_name`, `script`. Profiles pin a window title / process pattern to a language set. |
| Ingestion queue | `capture_jobs` | `pending → processing → accepted / rejected / failed`, attempt count, image sha256 + phash, OCR text hash, window title, process name. |
| Raw truth | `sources`, `source_translations` | `source_type` image/text/manual, `ingest_mode`, `review_status` pending/reviewed/ignored, `language_code`. Translations have their own queue states. |
| Machine draft | `source_analysis_segments`, `source_analysis_candidates` | One segment per OCR block / language run; candidates carry confidence and `approval_status` pending/approved/rejected. |
| Reviewed catalog | `terms`, `term_forms`, `source_terms`, `source_segments`, `source_segment_terms`, `kanji`, `source_kanji` | Canonical terms keyed per language (`es::no::` ≠ `en::no::`). A reviewed segment can map one raw string to several terms (`獲得経験値` → `獲得` + `経験値`). |
| Derived cache | `term_stats`, `kanji_stats` | Rebuildable (`rebuild-stats`). |
| Study output | `flashcards`, `kanji_flashcards` | `export_status`, `exported_at`, `anki_note_id` per card. |
| Read models | `v_terms_overview`, `v_kanji_overview`, `v_flashcards_overview` | Used by listings and the web UI. |

**Priority score** (`lang_jp.py`, stats recalculation):

```
priority = total_occurrences + 3 × distinct_sources + 0.5 × distinct_forms
```

Words that recur across *different* sources dominate; proper names (detected from their meaning) score 0. Card export and Anki new-card repositioning both use the current score, so study order follows the catalog, not card creation order.

## 4. The passive capture pipeline

### 4.1 Daemon: from screen to job

1. **Trigger.** A tick of the chosen cadence, the tray's *Capture now*, or the global hotkey.
   - *Fixed interval*: captures on a timer, but skips a tick while the worker still has the previous frame waiting (`previous_frame_waiting`), so a 30–50 s OCR never builds a spool backlog.
   - *After source ready*: waits until the daemon's last job has become a source.
2. **Skip rules.** Recent identical frames (sha256, perceptual hash) are dropped; so are hashes already in the database.
3. **Foreground window.** Title and process name are recorded with the job (opt out with `record_foreground_window = false`).
4. **Enqueue.** PNG written to `data/capture_spool/`, row inserted into `capture_jobs` as `pending`.

The daemon also owns process lifecycle: it spawns the worker (`loop`, `once` or `drain` mode), supervises `lang_jp.py browse` on port 8766 with restart backoff (or reuses a server already on the port), holds a named single-instance guard (a second launch just opens the catalog), and exposes the *Capture languages* menu that writes `languages.capture_enabled` directly.

Configuration comes from `capture_daemon.toml` (template: `capture_daemon.toml.example`), patched from the tray menu.

### 4.2 Worker: from job to source

`process_job_once` in `capture_worker.py`:

1. **Claim.** `claim_next_capture_job` runs `BEGIN IMMEDIATE`, picks the newest `pending` (then retryable `failed`, < 3 attempts) and marks it `processing`. Stale `processing` jobs are recovered at startup.
2. **Early rejects.** Missing image; foreground window matching a capture skip rule (`capture_skip_rules`, empty by default, edited from the tray, `/capture` and `skip-window`; the `LANG_CAPTURE_SKIP_*` env vars add to it); image already a source.
3. **Resolve languages.** The capture set is read from the database per job (env `LANG_CAPTURE_LANGUAGES` only overrides); a matching capture profile narrows it or, with one language, fixes it.
4. **OCR** via `build_capture_ocr_adapter` and `ocr_backends.plan_ocr_engines`:
   - `ja` only → meikiocr alone (the original Japanese flow, with furigana and plausibility filters).
   - Otherwise → meikiocr for the Japanese pass, then one EasyOCR reader per compatible language group (all Latin languages share one; `ko`, `zh`, `th`, Cyrillic, Arabic, Devanagari each get their own, paired with English).
   - EasyOCR's CRAFT detection runs **once** per capture; later readers only re-read boxes nobody read with confidence ≥ 0.7.
   - Blocks from all engines are merged by bounding-box overlap and text similarity; the **earlier engine wins**, and lookalike misreadings (stray Cyrillic/Greek letters in Latin text) never replace a reading.
   - Multi-reader captures are read at up to 3840 px so 4K desktop text stays legible.
5. **Language split and analysis.** Each block goes through `languages.analyze_text`: script runs first (CJK, Hangul, Cyrillic, Arabic, … decided by Unicode ranges), then Latin runs are scored by stop words and orthographic hints against *every* language, with a screen-level Latin prior for short strings. Text in a language outside the capture set stays a visible segment but yields no candidates.
6. **Tokenize.** Per-language analyzer: `SudachiAnalyzer` (ja), `HanAnalyzer` (zh, jieba when installed), `ThaiAnalyzer`, `GenericAnalyzer` (space-delimited, simplemma lemmas). Junk rules drop URLs, handles, digit-glued tokens, odd casing, vowel-less lemmas.
7. **Store.** Image moved to `data/captures/…`, a `pending` source created under the dominant captured language, analysis segments and candidates written, job `accepted`. A job with no candidates is rejected.
8. **Auto-promote.** Candidates the language's simplemma dictionary knows (or any candidate for ja/ko/zh/th/vi, which have no such dictionary) become terms; unknown words stay pending for review.
9. **After commit.** Meanings of new terms are filled (JMdict for Japanese, machine translation otherwise) **one short write per term after the source transaction**, so the daemon is never blocked on the database lock. Source translations are not queued by default: the web UI translates one source on demand (`translate_source_on_demand`, batched lines). The old background queue (`process_pending_translation_once`) only runs with `LANG_JP_ENABLE_SOURCE_TRANSLATION=1`; per-capture translation kept the free endpoint answering HTTP 429.

### 4.3 Concurrency model

All processes share one SQLite file. Writers keep transactions short: the job claim is a single `BEGIN IMMEDIATE`, network calls (translation) happen outside transactions, and slow work (OCR) happens before the write. This was a deliberate fix (commit `4b6f133`): translating inside the commit held the lock long enough to make the daemon's enqueue fail.

## 5. Language layer

`languages.LanguageProfile` is the extension point. A profile declares code, names, script, whether it has readings or characters, its analyzer and romanizer. Adding a language is mostly adding a profile; `enable-language --code de` registers it in the database.

- **Detection is offline** — no models, no network; script histograms plus weighted stop-word/hint tables. This keeps capture deterministic and fast and was tuned against real screens (Portuguese chat being misfiled as Spanish/English/Polish led to the screen prior, tie-break by language order, and weaker two-letter stop words).
- **Readings**: kana from Sudachi + romaji (ja), pinyin via optional `pypinyin` (zh), Revised Romanization (ko) and BGN-style transliteration (ru, uk) built in.
- **Meanings** come from machine translation (Google's public `gtx` endpoint, `google_gtx_translate_text`) and are written in the *gloss language* (`LANG_JP_TERM_TRANSLATION_TARGET`, default `pt`). That language is off for capture and for `--language all` card generation by default, because its cards would show the same word on both sides. Kanji meanings stay canonical English (`kanjiapi.dev`).
- **Japanese meanings** come from a local JMdict import: the first 1–3 English glosses of the best sense (part of speech from Sudachi used as a preference) translated to Portuguese; bare-lemma translation only as a fallback.

## 6. Surfaces

- **CLI (`lang_jp.py`)** — the full command set: `add-source`, `ingest-text`, `link-term`, review (`review-sources`, `approve-analysis`, `ignore-source`, `delete-source`), stats, cards, Anki, language and capture-profile management. The CLI is the reference implementation of every write.
- **Web UI (`lang_jp_web.py`)** — stdlib `http.server` (`ThreadingHTTPServer`), server-rendered HTML with inline CSS/JS, no framework or build step. A language bar filters every page (`?lang=es`, `lang=all`). Reads use SQL directly; writes either call `lang_jp` functions or shell out to the CLI (Anki sync) so long side effects run in their own process. Every page title includes "Lang Catalog" so a `title` skip rule for "Lang Catalog" can keep it out of capture (not set by default).
- **JSON API** — `/api/languages`, `/api/capture`, `/api/terms[/{id}]`, `/api/sources`, `/api/flashcards`, with the same `lang`, `q`, paging parameters. Search is case-insensitive in every script but never folds accents.
- **Tray** — capture control, cadence, OCR size, capture languages, open catalog, tail logs.

## 7. Anki integration

- **AnkiConnect** (local HTTP, port 8765) for create/update/reposition; **TSV** (Anki 2.1.54+ headers, `Front, Back, Tags, Deck, Notetype, GUID`) as the offline path.
- One deck per language (`Lang::Japanese`, `Lang::Spanish`, …); characters share their language's word deck so kanji and vocabulary interleave in one new-card queue.
- **Identity is language-scoped, except Japanese.** Japanese notes keep the pre-polyglot identity (first-field match, collection-wide duplicate check) so existing notes were never duplicated. Other languages use per-deck duplicate checks and a deterministic GUID from the canonical key, so `ser` (es) and `ser` (it) are distinct notes.
- Note lookup: stored note id → `term_N` tag → front text in the deck (never across languages).

## 8. How it was built (timeline)

| When | Step |
|---|---|
| Apr 2026 | Initial Japanese catalog: SQLite schema, CLI, manual sources, term linking, recurrence stats, Anki sync, local web UI; first Rust daemon + EasyOCR worker. |
| 21 Oct 2026 | meikiocr replaces EasyOCR for Japanese capture; local JMdict meanings; repo-relative paths so the DB survives a reinstall; `rebuild_auto_sources.py` re-runs old captures through the new pipeline. |
| 21 Oct 2026 | **Polyglot catalog**: `language_code` on every table, per-language terms, `languages.py`, UI filters, JSON API. Existing rows upgraded in place to `ja` with `ja::` keys. |
| 24–25 Oct 2026 | Single `open_lang.cmd` launcher; daemon supervises the web server and enforces one instance. Capture set and capture profiles moved into the database; OCR engines routed by language set with shared detection and block merge; romanizers; `/capture` page; tray language menu; foreground window metadata. |
| 1–2 Nov 2026 | Live checks on real desktop screens: screen-level language prior, junk-token filters, gloss language off for capture, language-aware frontend, language-scoped Anki export/sync. Merged as PR #3. |
| 2–4 Nov 2026 | Hardening: hyphenated-word detection, meanings translated after commit, clean multi-language desktop capture, catalog page titles skipped by capture. |
| In progress | Worker-aware fixed cadence (skip ticks while a frame waits) and permanent `delete-source` from the CLI and source page. |

Process notes: most work after April was done in short branches/worktrees with Claude Code, each phase driven by a written plan (`polyglot-plan.md`, `ocr_capture_spec.md`, `polyglot-finish-prompt.md`) and validated against the live database and real screenshots, with decisions recorded back into the plan.

## 9. Testing

Unit and integration tests live next to the code (`JP/test_*.py`, standard `unittest`): language detection and analyzers (`test_languages.py`), OCR planning and merge (`test_ocr_backends.py`), the capture pipeline end to end on temporary databases (`test_capture_pipeline.py`, `test_capture_integration.py`, `test_capture_draft.py`), Anki export identity (`test_anki_export.py`), JMdict lookup, the web UI, the blacklist, image compression and source rebuilds. The Rust daemon has its own `#[cfg(test)]` tests for argument parsing, hotkeys and cadence decisions. `ocr_bench/` compares OCR engines against a small ground-truth set. `setup_windows.ps1 -RunSmokeTests` runs a quick pass after a rebuild.

## 10. Known trade-offs

- **`lang_jp.py` is the coupling hub** (~8k lines): schema, repository, domain logic, CLI, card formatting and HTTP integrations in one module. The worker and web UI import its internals, so process boundaries are not module boundaries. See `refactor-backlog.md` and `maintainability-review.md`.
- **Schema lives in two places**: `schema.sql` for fresh databases and runtime upgrade SQL in `ensure_runtime_schema`.
- **One translation request per new term** makes a Latin-heavy first capture slow.
- **CPU OCR is slow** (tens of seconds per 4K screen with many languages), which is why the cadences are worker-aware.
- **Coverage gaps by design**: Greek and Hebrew have no EasyOCR model (text-only via `ingest-text`), Arabic has no romanizer, vertical manga text is only handled by the separate `single_image_ocr.py` path.
- **Windows-only capture**; the Python catalog itself is portable.
