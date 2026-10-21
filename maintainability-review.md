# Maintainability Review

## Overall Assessment

The repository is functionally coherent and the end-to-end product story is strong, but maintainability is currently constrained by concentration of responsibility, mixed boundary styles, and weak operational hygiene around long-running capture workflows.

The most important maintainability fact is this:

- the system boundary between processes is cleaner than the boundary between Python modules

In other words, Rust, worker, CLI, and web are separated at runtime, but the Python code still behaves as if one large file is the application.

## High-Leverage Findings

### 1. `JP/lang_jp.py` is the system bottleneck module
- Evidence:
  - 4641 lines
  - owns DB setup, schema migration, queue helpers, source CRUD, stats, flashcards, Anki, translations, parser, and browser handoff
- Why it matters:
  - every new feature reinforces the monolith
  - unrelated changes increase merge/conflict risk
  - testing at the right abstraction level becomes harder
- Architectural consequence:
  - worker and web evolve around it rather than against a stable service layer

### 2. Worker-to-core coupling is direct and wide
- Evidence:
  - `JP/capture_worker.py:19` imports many symbols directly from `JP/lang_jp.py`
  - imported concerns include queue management, source creation, promotion, matching keys, normalization, translation persistence, and stats-adjacent helpers
- Why it matters:
  - the worker is not insulated from internal refactors of the CLI/core module
  - process separation does not buy true modular independence

### 3. Schema authority is split across files
- Evidence:
  - `JP/schema.sql` defines the bootstrapping schema
  - `JP/lang_jp.py` also contains runtime SQL fragments for tables/views plus `ALTER TABLE` logic in `ensure_runtime_schema`
- Why it matters:
  - schema drift becomes easier
  - reviewers must inspect two places to understand current truth
  - migrations are implicit rather than deliberate

### 4. Web boundary is hybrid and awkward
- Evidence:
  - `JP/lang_jp_web.py` issues direct SQL for reads
  - sync actions shell out through subprocess to `lang_jp.py`
  - the module duplicates at least five domain helper names from `lang_jp.py`
- Why it matters:
  - the web layer has no clear service/API contract
  - view logic, query logic, and action orchestration are interleaved
  - fixes have to consider both duplicated logic and subprocess behavior

### 5. Review lifecycle semantics are blurred
- Evidence from live DB:
  - 397 sources are `pending`
  - 395 of them have zero pending analysis candidates
  - 394 pending sources already have published `source_terms`
- Code path involved:
  - worker auto-promotes source-analysis candidates
  - `review_status` remains `pending`
- Why it matters:
  - `'pending'` no longer means one stable thing
  - review queues can stop being trustworthy operationally
  - future automation/features will build on ambiguous state

### 6. Editing a source can leave derived data inconsistent
- Evidence:
  - `JP/lang_jp.py:617` `update_source_record()` edits `raw_text`, `scene`, `review_status`, and `notes`
  - it does not clear or recompute source-analysis rows, source-term links, source-segment links, translations, or kanji derivations
  - `clear_source_analysis()` exists but appears unused
- Why it matters:
  - source truth can diverge from draft analysis and published links
  - this is especially risky because many pending sources already have promoted terms

### 7. Queue recovery is incomplete
- Evidence:
  - live DB contains 6 `capture_jobs` rows stuck in `processing`
  - `claim_next_capture_job()` only claims `pending` and `failed`
  - there is no visible reset/requeue path for stale `processing` jobs
  - Rust `capture_pipeline_ready_for_next_capture()` also checks for `processing` jobs
- Why it matters:
  - stale jobs accumulate silently
  - `after source ready` cadence can be blocked by stale state
  - long-running automation loses reliability over time

## Code Quality Findings

### 8. Priority and flashcard generation overfit frequency, not study quality
- Evidence from live DB:
  - highest-priority terms currently include `の`, `。`, `た`, `に`, `て`
  - top exported flashcards also include those entries with null meanings
- Code path involved:
  - `recalculate_term_stats()` scores by occurrences/distinct sources/forms with only a proper-name exclusion
  - `build_flashcard_payload()` happily builds cards without a meaning
- Why it matters:
  - downstream flashcards can be noisy or low-value
  - quality issues are being materialized, not just ranked internally

### 9. Anki sync logic is duplicated and large
- Evidence:
  - separate long flows in `sync_anki()` and `sync_anki_kanji()`
  - reprioritization helpers are also duplicated for term and kanji paths
- Why it matters:
  - behavior drift is likely
  - fixes to note creation, deck moving, or tagging must be made twice

### 10. Build and launcher behavior drift from documentation
- Evidence:
  - `open_jp_capture_tray.cmd` runs `cargo build --release` whenever Cargo is available
  - `JP/README.md` describes reuse/build behavior more optimistically than the script actually does
- Why it matters:
  - launch time and developer expectations drift apart
  - debugging operational issues becomes harder when docs are not exact

### 11. Dependency declaration drift exists in the OCR stack
- Evidence:
  - `JP/requirements-capture.txt` still lists `paddleocr` and `paddlepaddle`
  - current worker path is EasyOCR-based and repo docs acknowledge the migration
- Why it matters:
  - onboarding and environment rebuilds become less predictable
  - unused heavy packages increase setup cost

### 12. Quality controls are minimal
- Evidence:
  - only one unit-test file was found
  - 8 tests pass, but coverage is concentrated around review/promotion helpers and OCR ordering
  - no CI, linting, typing, or formatting configs were found
- Why it matters:
  - architectural regressions will surface through runtime use rather than guardrails
  - refactors remain expensive because there is little safety net

## Medium / Lower-Severity Issues

### 13. Default port inconsistency in the web entry layer
- Evidence:
  - CLI `browse` defaults to `8766`
  - `serve_catalog_ui()` itself still defaults to `8765`
- Why it matters:
  - low severity, but it encodes conflicting assumptions close to the same boundary

### 14. Small dead/stranded pieces are accumulating
- Evidence:
  - `clear_source_analysis()` appears unused
  - `/actions/sync-kanji-anki` exists in the web handler but no matching form/action usage was found
- Why it matters:
  - these are not critical by themselves
  - they indicate organic growth without regular consolidation passes

## Strengths Worth Preserving

### Local-first design is coherent
- SQLite as the durable contract is the right choice for this problem.
- The project does not overcomplicate itself with services it does not need.

### Core/browser dependency footprint is disciplined
- The main CLI and web UI stay mostly within the Python standard library.
- Heavy OCR/NLP dependencies are isolated to the worker/tooling side.

### The product workflow is legible
- Capture, draft, review, rank, flashcard, sync is a strong narrative.
- The database model reflects that narrative better than the module structure does.
