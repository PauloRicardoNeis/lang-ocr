# Live catalog UI: plan

## Status (2026-11-14): implemented

All five steps are done. The implementation differs from the plan below in these places:

- **Module layout.** The change log lives in its own module, `catalog_changes.py` (specs, trigger generation, `Watch`, `changes_since`). `lang_jp.py` only calls `ensure_catalog_change_log` from `ensure_runtime_schema`. The view model lives in `web_views.py`.
- **Triggers are generated, not mirrored.** They are built from `CHANGE_SPECS` and brought in sync on every connection (one read when up to date). `schema.sql` has only the table and index, with a comment pointing at the generator, so there is one source of truth instead of 49 hand-copied triggers.
- **Regions can be parts of a panel.** On list pages the live table shares a `<section>` with static search and Anki forms. So instead of a list of whole panels, a page wraps its live parts in place with `LiveRegions.region(key, html)`, a `display: contents` wrapper, while composing its body. The full page and the fragment payload come from the same render.
- **The language bar is live too.** Its per-language counts sit in the header, so `PageView.live_regions()` adds it to every page's regions.
- **Watch size cap.** An entity with more than 150 watched ids falls back to a kind watch. A large capture can link hundreds of terms; without the cap the poll URL grew to several KB.
- **Progress is reported, not inferred.** `WorkerJobProbe.mark` logs a step after it ends, so the new `probe.begin(stage)` reports the stage now running (`ocr`, `analyze`, `save`, `meanings`) through `set_capture_job_progress`. The log lines are unchanged.
- **Commits.** Step 1 is its own commit. Steps 2 to 5 landed together, because the refactor and the live endpoints touch the same page functions.

Measured on a copy of the real catalog (508 MB): a `/live/changes` poll takes ~2.5 ms. A fragment refresh takes ~35 ms on a 314-term source page and ~250 ms on the dashboard, and runs only when something the page watches changed.

## Context

The web UI (`JP/lang_jp_web.py`) partly updates itself today, but badly:

- `database_revision_token` (l.1052) hashes the size and mtime of the sqlite file and its WAL. The worker writes `capture_jobs` on every frame, rejected ones included (~every 5s), so the token changes almost all the time.
- `auto_reload_script` (l.3398) does a full `location.reload()` on every token change. It runs on the **list** pages: dashboard (l.2490), `/terms` (2621), `/kanji` (2763) and `/flashcards` (4203). So those pages reload every few seconds while their content changes only about once per accepted job (~20–40s).
- The **detail** pages have no live updates at all: term (l.3250), kanji (2945) and source (4045). This corrects the brief. The source page is where it matters most: term meanings (`fill_source_term_meanings`) and the EN translation (`process_pending_translation_once`) arrive seconds after the source is saved.
- `sources_live_script` (l.3503) is the one granular updater. It polls `/sources/live` and swaps the table, count and pagination. Its revision embeds the same mtime token, so it re-renders on every frame too.
- `/capture` has no live updates, and the worker's per-job progress exists only in `capture_daemon.log`.

Goal: pages change only when what they show has changed. Changes arrive within ~3s, as in-place swaps of the affected regions that keep scroll, sort state and focus. There is one generic mechanism instead of three ad-hoc pollers.

## 1. Transport: cheap conditional polling, not SSE

The recommendation is a 2.5s poll of a tiny semantic endpoint (`/live/changes`), then a fragment fetch only when something relevant changed.

Why not SSE on `ThreadingHTTPServer`:
- Each open tab would hold one server thread and one HTTP/1.1 connection forever. Chrome allows 6 connections per origin, so 6 open catalog tabs (easy with term and source tabs) block all further page loads and fetches on `localhost:port`. Working around that (a SharedWorker multiplexer, or HTTP/2) is far more machinery than the problem needs.
- The server would also need a watcher thread per stream (or a shared notifier) polling the DB anyway, since the worker is another process and SQLite has no cross-process notification. SSE would only move the polling into the server.
- Server restarts and laptop sleep need reconnect handling. Stateless polling survives them for free.
- Update rate is ~1 relevant change per 20–40s. A 2.5s poll that answers from one indexed query on a read-only connection costs nothing, and latency (≤2.5s plus render) is well under the job time (~36s queue + processing).

Long-polling was also rejected: it has the same thread and connection-limit problem as SSE.

Client rules: poll only while `!document.hidden`, fire immediately on `visibilitychange`, back off exponentially to 30s on network errors, and keep one request in flight at a time.

## 2. Change detection: a trigger-written change log

**New table `catalog_changes`**, written only by SQLite triggers. Every writer is covered (worker, CLI `lang_jp.py`, web actions, Anki sync, rebuild scripts) and none of them needs code changes:

```sql
CREATE TABLE IF NOT EXISTS catalog_changes (
    id INTEGER PRIMARY KEY,           -- monotonically increasing revision
    entity TEXT NOT NULL,             -- 'source' | 'term' | 'kanji' | 'flashcard' | 'language' | 'job'
    entity_id INTEGER,                -- NULL = kind-level (languages)
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_changes_entity ON catalog_changes(entity, entity_id, id);
```

Triggers (AFTER INSERT/UPDATE/DELETE) map tables to entity keys, and a row-level change records every key it affects:

| table | records |
|---|---|
| `sources` | source:id |
| `source_terms`, `source_segment_terms` | source:source_id (via segment for the latter), term:term_id |
| `source_segments`, `source_analysis_segments`, `source_analysis_candidates`, `source_translations` | source:source_id |
| `terms`, `term_forms`, `term_stats` | term:id / term_id |
| `kanji`, `kanji_stats` | kanji:id; `source_kanji` → kanji + source |
| `flashcards` | flashcard:id + term:term_id; `kanji_flashcards` → flashcard + kanji |
| `languages` | language:NULL |
| `capture_jobs` (phase 4 only) | job:id |

- `capture_jobs` produces only `job` keys, and only the capture-progress region watches those. **Job-status churn and rejected frames never touch any catalog page.** A rejected frame writes only `capture_jobs`.
- `term_stats`/`kanji_stats` UPDATE triggers carry `WHEN` clauses on the displayed columns, so a recalculation that changes nothing logs nothing.
- Pruning: an `AFTER INSERT ON catalog_changes` trigger deletes `id <= NEW.id - 20000`, which is a cheap PK range delete. Bulk jobs (an Anki sync touching every flashcard) just roll the window. A client whose `since` falls below `MIN(id)` gets `reset: true` and refetches all regions.
- Rejected alternatives: `PRAGMA data_version` is per-connection and useless with the per-request connections the server opens. `max(id)`/`updated_at` per table misses deletes and tables without `updated_at`, and still can't tell a page whether *its* term changed. A worker-written log would be a bolt-on every other writer must remember.
- To verify in a test: FK `ON DELETE CASCADE` deletes (deleting a source) fire child-table triggers in SQLite. If they don't, the `sources` DELETE trigger also logs the term ids it is about to orphan (a BEFORE DELETE trigger).

**Endpoint `GET /live/changes?since=N&watch=term:42,term:57,source:17,source`**, where a bare kind means any id of that kind. It returns `{"revision": M, "changed": bool, "reset": bool}`. One `MAX(id)` plus one `EXISTS` over `id > since` restricted to the watch set. It opens a new **read-only** connection helper (`file:...?mode=ro` URI, no `ensure_runtime_schema`). Today `db_connect` runs the whole runtime-schema check on every connection, which a 2.5s poll must not do.

Removed: `database_revision_token`, `live_revision_payload`, `/live/revision`, `auto_reload_script`, `sources_live_script`, `/sources/live`, `sources_live_payload` and the `revision` key of `build_sources_payload`.

## 3. Granular updates: a page-view refactor (needed, stated up front)

Per the clean-architecture rule, this **needs a refactor of how pages are rendered**. Today each page function returns one big f-string handed to `layout()`, so nothing can render one panel alone. Bolting per-page fragment endpoints next to each (as `/sources/live` did) would make 9 more copies of that pattern.

**New rendering model** (in `lang_jp_web.py`, or a small `JP/web_views.py` if the file split reads better):

```python
@dataclass(frozen=True)
class Region:
    key: str                 # stable DOM key, e.g. "term-occurrences"
    html: str                # the panel markup (rendered with existing helpers)
    live: bool = True        # False for static chrome: search forms, notice banner, Anki sync, settings forms

@dataclass(frozen=True)
class PageView:
    title: str
    active_path: str
    lang: str
    language_bar: str
    regions: list[Region]
    watch: tuple[str, ...]   # entity keys this page depends on, computed from the data it loaded
```

- Each live page becomes `build_<page>_view(db_path, params) -> PageView`: dashboard, terms, kanji, sources, flashcards, term detail, kanji detail, source detail, capture. The existing queries and helpers (`table_html`, `sources_table_markup`, `pagination_nav`, `language_cards`, …) are reused unchanged. Only the final f-string is cut into regions. Today's `*_page()` functions become `render_page(view)`.
- `render_page(view)` = `layout()` + each region wrapped in `<div data-live-region="key" data-live-hash="h">`, plus `<body data-live-watch="…" data-live-revision="N">`. Wrappers use `display: contents` so the `.grid` CSS layout is unchanged.
- `render_fragments(view)` returns `{"revision": N, "watch": [...], "regions": {key: {"hash": h, "html": ...}}}` for live regions only. Revision N is read **before** the page queries run, so a change landing mid-render is caught by the next poll.
- **Routing:** replace the `do_GET` if-chain for pages with a route table `path pattern → view builder`. The same URL serves HTML, or fragments when called with `Accept: application/vnd.lang.fragments+json`. That gives one router and two renderers, with no `/x/live` twin per page. 404 handling moves into the route table.
- Watch sets come from the data each view loaded:
  - Dashboard: `source, term, kanji, flashcard, language`.
  - List pages: their kind (`/flashcards` also watches `term`, for priority and lemma).
  - Term detail: `term:id` + `kanji:<derived ids>`.
  - Source detail: `source:id` + `term:<linked ids>` + `kanji:<ids>`. This catches meanings filled after the commit.
  - Kanji detail: `kanji:id` + `term:<ids>`.
  - `/capture`: `job` + `language`.
- Static (non-live) regions: notice banner (from query params), search/filter forms, Anki sync panel, the source language/delete/translate forms and the capture settings forms. Their panels split into a static form part and a live data part where needed (e.g. the source "Tradução EN" panel: status and text live, button static).

**Client: one static `LIVE_SCRIPT`** in `PAGE_SCRIPT` (no per-page f-string, so no Python-to-JS literal injection):
1. Read `data-live-watch` and `data-live-revision` from `<body>`. Do nothing if there is no watch.
2. Poll `/live/changes`. On `changed` or `reset`, fetch the current URL with the fragments `Accept` header, then set the revision to the one in the response.
3. For each region whose hash differs:
   - Defer it if it contains `document.activeElement` or Anki sync is running (`jpCatalog.isSyncingAnki`). Deferred regions retry on `focusout` and on the next poll.
   - Before the swap, save `readSortState` for each sortable table in the region and the `scrollLeft` of its `overflow:auto` wrappers.
   - Swap `innerHTML`, then call `initSortableTables(region)`, restore sort and scroll, and update `data-live-hash`.
   - Window scroll stays put because only regions change, and CSS scroll anchoring keeps the viewport steady when a region above it grows.
4. Briefly flash changed regions with a CSS class, so the user sees what arrived.

The sessionStorage sort-state save/restore in `auto_reload_script` goes away, since there are no more reloads.

## 4. In-flight job progress (phase 4, optional but cheap once 1–3 exist)

- Add columns `capture_jobs.progress_step TEXT` and `progress_at TEXT`, through `ensure_runtime_schema` + `schema.sql`.
- `WorkerJobProbe.mark` (capture_worker.py l.340) is already the single place each step passes through. Give it an optional `on_step` callback. The worker passes one that does a short `UPDATE capture_jobs SET progress_step=?, progress_at=CURRENT_TIMESTAMP WHERE id=?` + commit, which is about 8 tiny writes per job, with no transaction held during OCR. The log line stays as-is.
- The `capture_jobs` trigger logs `job:id`. Only regions watching `job` react.
- `/capture` gets a live region "Captura em andamento": current job id, window, step, elapsed (ticked client-side from `progress_at`), and queue length (`pending` count). The existing "Capturas recentes" table becomes a live region too. Optionally the dashboard gets a one-line status region watching `job`. Opt-in, because it brings ~5s churn to that one small region.

## 5. Schema/migration impact and tests

**Schema** (`JP/schema.sql` + `JP/lang_jp.py`):
- New constant `CATALOG_CHANGE_SCHEMA_SQL` (table, index, triggers) applied in `ensure_runtime_schema`, mirrored in `schema.sql`, since the tests build DBs from `schema.sql`.
- Triggers are created through a generalized `ensure_view_definition` → `ensure_schema_object(connection, name, type, sql)`. That helper already compares the stored SQL via `schema_object_sql`, so a later trigger change is dropped and recreated automatically.
- No backfill: clients start from the current `MAX(id)`.
- `schema.sql` and `lang_jp.py` already have uncommitted user edits. Implementation edits on top of them and commits only its own hunks (`git add -p`), never the user's.

**Tests:**
- `JP/test_lang_jp_web.py`:
  - `/live/changes`: unchanged, changed for a watched id, unchanged for an unwatched id, kind-level watch, `reset` after pruning, malformed `since`/`watch` → 400.
  - Every live page: the fragment payload's regions equal the `data-live-region` contents of the full HTML (the two renderers stay consistent), region keys are stable, the notice banner and forms are not live, and the source detail watch includes its linked term ids.
  - Route-table 404s.
- A new `JP/test_catalog_changes.py`:
  - Each mapped table logs the expected keys.
  - `capture_jobs` updates log only `job`.
  - A no-op `term_stats` recalc logs nothing.
  - The pruning bound holds.
  - FK cascade on source delete is covered.
  - `ensure_runtime_schema` on an old DB without the table adds it idempotently.
  - `schema.sql` and the runtime SQL produce identical trigger definitions.
- Worker (existing `test_capture_pipeline.py` style with fake OCR):
  - A rejected frame adds no catalog change.
  - An accepted job adds source and term keys.
  - The later meaning fill adds `term:id`.
  - Phase 4: probe steps update `progress_step`.
- JS has no harness in the repo. Keep `LIVE_SCRIPT` small and static, and verify it manually (below).

## Implementation order (each step its own commit on main, no attribution trailer)

1. Change log schema + triggers + `ensure_schema_object` + `test_catalog_changes.py`.
2. `PageView`/`Region`/route-table refactor with **no behavior change** (old pollers still attached). The existing web tests must pass untouched.
3. `/live/changes`, read-only connection helper, fragments renderer, `LIVE_SCRIPT`. Remove the old token, pollers and `/sources/live`. Add the web tests.
4. Job progress columns, probe callback, `/capture` live regions.
5. Update `JP/README.md` (live UI section) and the `JP/live_ui_plan.md` status.

## Verification

- `python -m unittest` in `JP/` (the whole suite, since the refactor touches every page).
- Start the web server and the capture worker. In the built-in browser pane:
  - A source detail page fills meanings, then the translation, without reloading.
  - The dashboard and `/terms` stay still during rejected frames (network panel: only `/live/changes` 200s with `changed:false`).
  - A sorted table keeps its sort after a swap.
  - Typing in the search box is never interrupted.
  - Scroll position holds.
  - 8 open tabs still load (no connection starvation).
  - `/capture` shows the step and elapsed time of the running job.
- Measure `/live/changes` latency (should be ~1ms) and confirm `catalog_changes` stays ≤20k rows after an Anki sync.
