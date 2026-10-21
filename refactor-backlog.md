# Refactor Backlog

## Do Now

### 1. Add stale capture-job recovery
- Problem:
  - `processing` jobs can remain stuck indefinitely and block queue semantics.
- Current status:
  - manual recovery now exists via `lang_jp.py recover-stale-capture-jobs`
  - worker startup now reclassifies stale `processing` jobs to `failed` using a conservative age threshold
- Proposed fix:
  - keep the CLI/admin command to reset stale `capture_jobs` from `processing` back to `failed` or `pending` based on age
  - keep reclaiming stale jobs during worker startup
  - if false positives ever appear, add an explicit processing heartbeat/lease timestamp instead of relying only on `updated_at`
- Expected benefit:
  - immediate operational reliability improvement
  - restores trust in `after source ready` cadence
- Tradeoff:
  - must choose a careful age threshold to avoid reclaiming truly active work
- Impact:
  - high

### 2. Separate source editing from derived-state inconsistency
- Problem:
  - editing `raw_text` can leave analysis rows, links, and translations stale.
- Proposed fix:
  - define one explicit policy:
    - either raw-text edits clear draft analysis and derived links
    - or raw-text edits trigger a recompute pipeline
- Expected benefit:
  - removes silent truth-vs-derived divergence
- Tradeoff:
  - more work on source edits
- Impact:
  - high

### 3. Stop generating low-value flashcards
- Problem:
  - punctuation and function words currently rise to the top and get exported.
- Proposed fix:
  - add a ranking/filter layer before flashcard generation:
    - exclude punctuation-only lemmas
    - optionally exclude short grammar particles unless explicitly allowlisted
    - require a meaning or explicit override for auto-generated cards
- Expected benefit:
  - immediate study-quality improvement without changing the full architecture
- Tradeoff:
  - needs a pragmatic heuristic set, not a perfect linguistic model
- Impact:
  - high

### 4. Clean `requirements-capture.txt`
- Problem:
  - dependency declarations lag the actual OCR implementation.
- Proposed fix:
  - remove or clearly comment deprecated Paddle packages if they are no longer required
- Expected benefit:
  - faster onboarding and less environment confusion
- Tradeoff:
  - confirm no hidden/manual path still depends on them
- Impact:
  - medium

### 5. Make tray launch behavior honest and cheaper
- Problem:
  - the current launcher rebuilds the Rust daemon whenever Cargo is available.
- Proposed fix:
  - either:
    - only build when the executable is missing or older than sources
    - or keep current behavior and document it accurately
- Expected benefit:
  - less startup friction and cleaner expectations
- Tradeoff:
  - slightly more launcher logic
- Impact:
  - medium

## Do Next

### 6. Extract a shared Python service layer from `lang_jp.py`
- Problem:
  - worker, CLI, and parts of the web are all coupled to the monolith.
- Proposed fix:
  - split into modules such as:
    - `jp/db.py`
    - `jp/schema_runtime.py`
    - `jp/sources.py`
    - `jp/analysis.py`
    - `jp/stats.py`
    - `jp/flashcards.py`
    - `jp/anki.py`
    - `jp/cli.py`
- Expected benefit:
  - narrower imports
  - easier testing
  - clearer dependency direction
- Tradeoff:
  - medium refactor with real integration risk
- Impact:
  - very high

### 7. Replace schema split-brain with a clearer migration model
- Problem:
  - schema truth is divided between `schema.sql` and runtime SQL fragments.
- Proposed fix:
  - choose one primary approach:
    - authoritative migrations folder plus bootstrap
    - or authoritative Python migration registry
  - keep `schema.sql` as generated output if needed, not peer authority
- Expected benefit:
  - lower schema drift risk
  - clearer onboarding and review
- Tradeoff:
  - migration design work
- Impact:
  - high

### 8. Refactor the web layer around shared read/write services
- Problem:
  - direct SQL for reads plus subprocess for writes is a brittle hybrid.
- Proposed fix:
  - keep direct rendering, but route state changes through imported service functions instead of shelling out
  - optionally add a tiny internal action layer for web handlers
- Expected benefit:
  - fewer duplicated boundaries
  - easier error handling and timeouts
- Tradeoff:
  - web becomes more tightly bound to Python services, but in a cleaner way than subprocess glue
- Impact:
  - high

### 9. Consolidate Anki sync flows
- Problem:
  - term and kanji sync logic are duplicated.
- Proposed fix:
  - extract a generic note-sync pipeline parameterized by table/query/identity functions
- Expected benefit:
  - fewer drift bugs
  - easier to evolve reprioritization and error handling consistently
- Tradeoff:
  - some abstraction work; avoid over-generalizing too early
- Impact:
  - medium-high

## Do Later

### 10. Clarify workflow state model
- Problem:
  - `review_status`, candidate approval state, and promoted term state overlap ambiguously.
- Proposed fix:
  - define explicit source lifecycle states and separate them from candidate lifecycle states
  - example:
    - source ingestion state
    - source review state
    - analysis draft state
    - publication state
- Expected benefit:
  - clearer UI/CLI semantics
  - easier automation and backlog handling
- Tradeoff:
  - schema and workflow migration
- Impact:
  - very high

### 11. Add focused quality automation
- Problem:
  - architecture changes currently rely on manual confidence.
- Proposed fix:
  - add a small CI path with:
    - unit tests
    - import/smoke tests for CLI commands
    - maybe one SQLite fixture integration test path
- Expected benefit:
  - safer refactors
  - faster iteration
- Tradeoff:
  - setup effort and maintenance overhead
- Impact:
  - high

### 12. Move inline HTML generation toward reusable page/query helpers
- Problem:
  - page rendering and SQL assembly are intertwined in `lang_jp_web.py`.
- Proposed fix:
  - extract shared page/view helpers and query helpers without introducing a full frontend stack
- Expected benefit:
  - keeps the zero-build philosophy while reducing entropy
- Tradeoff:
  - moderate refactor
- Impact:
  - medium

## Maybe / Investigate Further

### 13. Consider whether source and analysis drafting should stay in SQLite
- Question:
  - should queue and draft-analysis state remain in the same database as curated catalog data?
- Why investigate:
  - current local-first design is good, but long-running queue recovery may eventually justify a cleaner subsystem boundary
- Caution:
  - do not split this prematurely; the current scale does not yet justify service sprawl

### 14. Revisit auto-promotion defaults
- Question:
  - should passive-capture candidates auto-promote into `source_terms` by default?
- Why investigate:
  - current live data suggests many pending sources already have published links
- Caution:
  - this is a product-workflow decision, not only a code refactor

### 15. Investigate image retention/compression policy
- Question:
  - should raw captures stay as PNG forever, or should `compress_source_images.py` become part of the normal maintenance path?
- Why investigate:
  - active capture sessions already produce substantial local image volume
- Caution:
  - preserve source fidelity and path stability before automating compression
