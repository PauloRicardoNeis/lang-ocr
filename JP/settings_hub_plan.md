# Settings hub: plan and design

(After approval this file is copied to `JP/settings_hub_plan.md`.)

## Context

User-tunable behaviour is spread over four stores, and only one of them works well:

- **~28 env vars** (`LANG_JP_*`, `LANG_OVERLAY_*`), read at import into `DEFAULT_*` module constants in `capture_worker.py` l.76-120, `lang_jp.py` l.47-57 and `translate_overlay.py` l.49-51, or lazily in `ocr_backends.py` (`resolve_capture_ocr_backend`, `resolve_easyocr_use_gpu`, `resolve_easyocr_languages`). Changing one means editing the environment and restarting. Some are frozen even harder, as function parameter defaults (`downscale_for_easyocr(max_side=DEFAULT_OCR_MAX_SIDE)`, `crop_image_region`, `run_with_progress_logs`).
- **`capture_daemon.toml`**, read once at daemon start. The tray's interval, cadence, OCR size and debug choices live in atomics and are lost on restart. OCR size and debug reach the worker as env vars at spawn (`spawn_worker`, main.rs l.1224).
- **`data/translate_overlay.toml`**: overlay mode, languages, enabled, scale. Written by the tray and the overlay.
- **SQLite**: `languages.capture_enabled`, `capture_skip_rules`, `capture_profiles`. Editable from tray, web and CLI, applied on the next job. This is the model to copy.

Goal: one declared list of settings, stored in the database, read at the point of use, editable from a `/settings` page ("Ajustes"), the tray and the CLI. No new app: the web UI already is the hub.

## Design of the page

The mockup shown in the conversation is the reference. In words:

- New nav pill **Ajustes** (`/settings`) after Captura. The language bar's dashed "Captura" link stays.
- A **section index** under the hero: OCR, Captura, Traducao, Overlay de leitura, Atalhos. Anchor links, plus a count of settings that differ from their default.
- One **panel per section**, full width. Each setting is a row: label and one-line help on the left (with the default value), control on the right.
  - Control by type: select for choices, number input with min/max/step, switch (styled checkbox) for booleans, text for hotkeys.
  - Chips on the label: `alterado` when the value differs from the default; `recarrega os motores` or `reinicia o daemon` when the change is not just "next job".
  - A setting pinned by an env var shows a dashed read-only box, "Fixado por LANG_JP_X = value", instead of a control (same idea as today's `LANG_CAPTURE_LANGUAGES` notice on `/capture`).
  - Rarely-touched settings sit in a native `<details>` "Avancado" block at the end of the section.
- Each panel is one form with **Salvar <secao>** and a secondary **Restaurar padroes** link. Saving redirects back to the section anchor with the usual notice banner.
- `/capture` keeps what is operational (languages, profiles, skip rules, recent captures). Its "Motores de OCR" panel gains a link to `/settings#ocr`.
- New CSS is small and goes in `PAGE_STYLE`: `.setting-row`, `.setting-help`, `.setting-lock`, `.switch`, `.chip.is-cool`, `.section-index`. Everything else reuses `.panel`, `.chip`, `.action-row`, `.notice-banner`, `.secondary-link`.

## 1. Registry: new module `JP/settings.py`

No dependency on `lang_jp.py`, so worker, overlay, web and CLI can all import it.

```python
@dataclass(frozen=True)
class Setting:
    key: str            # "ocr.backend"
    kind: str           # "bool" | "int" | "float" | "choice" | "text"
    default: object
    section: str        # "ocr" | "capture" | "translation" | "overlay" | "hotkeys"
    label: str          # Portuguese, unaccented, like the rest of the UI
    help: str
    env: str | None     # legacy env var; when set it pins the value
    applies: str        # "next_job" | "engines" | "daemon_restart"
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    advanced: bool = False
```

- `REGISTRY: tuple[Setting, ...]` declares every setting once. Default, env name, range and label all come from here; the `DEFAULT_*` env reads in the other modules are deleted.
- Table (in `SETTINGS_SCHEMA_SQL`, applied from `ensure_runtime_schema` and mirrored in `schema.sql`):
  ```sql
  CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
  );
  ```
  Only non-default values are stored, so changing a default in code reaches everyone who never touched that setting.
- Resolution order: **env var (pin) > database row > registry default**. Unknown keys in the table are ignored.
- API: `load_settings(connection) -> Settings` (immutable snapshot, one `SELECT`), `Settings.get(key)`, `Settings.source(key)` (`"env" | "db" | "default"`), `set_setting`, `reset_setting`, `reset_section`. Validation (type, range, choice) lives here and raises `SystemExit(message)`, the convention the web handlers already catch.

### What goes in, what stays out

| Section | Keys (from today's env vars and files) |
|---|---|
| ocr | backend, easyocr GPU mode, max side, multi-reader max side, min confidence, confident read, min term confidence; advanced: horizontal min ratio, horizontal max angle, region padding, max regions, legacy EasyOCR languages, meikiocr min char confidence |
| capture | interval seconds, capture after source ready, auto-promote known terms, stale job hours, debug log |
| translation | auto-translate term meanings, meaning language, timeout; translate every capture, source translation target; overlay target |
| overlay | enabled, mode, languages (`ja`/`capture`), scale |
| hotkeys | manual capture, overlay show/hide, overlay mode |

**Stays in `capture_daemon.toml`** (bootstrap: needed to find the database or spawn processes): `db_path`, `spool_dir`, log paths, python executables and args, script paths, `web_port`, `web_server_enabled`, `auto_start_worker`, `auto_process_jobs`, dedupe internals. `LANG_JP_JMDICT_DB_PATH` stays an env var for the same reason. `LANG_CAPTURE_LANGUAGES` and the two `LANG_CAPTURE_SKIP_*` vars keep working as they do now; languages, skip rules and profiles keep their own tables.

## 2. Read at use: worker and OCR refactor (the real work)

Today the job path carries no config object; every tunable is a module global. This step introduces one.

- `process_job_once` (capture_worker.py l.1657) loads `Settings` right after it claims the job, next to where it already re-reads languages, profiles and skip rules, and passes it down.
- Every global read becomes a field read on that snapshot: `filter_easyocr_blocks` (l.724-736), `MultiEngineOcrAdapter._read_easyocr_groups` (l.1202, 1220), `LanguageSegmentAnalyzer.analyze_block` (l.1395), `prepare_ocr_input_image` (l.508), region selection (l.976-984), auto-promote (l.1588). The frozen parameter defaults (`downscale_for_easyocr`, `crop_image_region`, `run_with_progress_logs`) become required arguments.
- **Engine settings** (backend, GPU mode, legacy EasyOCR languages, meikiocr min char confidence) are baked into cached readers. `CapturePipeline` (l.1441) gets an `engine_key` derived from those four values; when the key of the incoming snapshot differs, it drops `_adapters`/`_analyzers` and calls `ocr_backends.clear_backend_cache()` (l.1421, today only used by tests). `resolve_capture_ocr_backend`, `resolve_easyocr_use_gpu` and `resolve_easyocr_languages` lose their env fallback and take the value from the caller. Result: changing the backend from the page reloads models on the next job, without restarting the worker.
- `GoogleGtxSourceTranslator` target and the "translate every capture" switch are read per job instead of once in `main()` (l.2345-2349).
- `lang_jp.py`: the three term-translation constants (l.47-54) become settings read through the connection at the call site. `rebuild_auto_sources.py` l.560-564 and 651, which assign to `lang_jp` module attributes to turn translation off, switch to an explicit argument.
- `translate_overlay.py` builds its own `CapturePipeline`, so it gets the same snapshot per read; its target and hotkey defaults come from the registry.
- The diagnose path and tests that have no database use `Settings.defaults()`.

## 3. Daemon (Rust)

- `spawn_worker` stops passing `LANG_JP_OCR_MAX_SIDE` and `LANG_JP_CAPTURE_DEBUG`. This matters: an env var pins the setting, so leaving them would lock those two rows on the page.
- Tray items "OCR size", "Capture interval", cadence and "Toggle debug mode" write `settings` rows (same pattern as `languages.capture_enabled`, main.rs l.1838-1868) and are re-read on the existing 5 s menu refresh, so tray and page stay in sync and choices survive a restart.
- Interval and cadence are consumed by the daemon itself; it falls back to `DaemonConfig` (TOML or built-in) when there is no row. The registry defaults for those two keys must equal the Rust defaults; a Rust unit test asserts the constants and the registry entry carries a comment pointing at it.
- Hotkeys are read from `settings` at startup, falling back to the TOML. They are the only `daemon_restart` settings.
- New tray item "Open settings (web)" next to "Open Lang", opening `/settings`.

## 4. Overlay state file

`data/translate_overlay.toml` (mode, languages, enabled, scale) moves into the `settings` table: `load_overlay_state`/`save_overlay_state` (translate_overlay.py l.112, 130) and the daemon's `update_overlay_state` (main.rs l.1002) read and write rows instead. The daemon's 1 s supervisor poll becomes a one-row query. On first run the existing file is imported once and left in place, unused. `overlay_state_path` is dropped from `DaemonConfig`. This is the last step and can be skipped without affecting the rest.

## 5. Web and CLI

- **Page:** `build_settings_view(db_path, params)` on the `PageView`/route-table model from `JP/live_ui_plan.md`. One region per section; forms are static regions, so a save from the tray refreshes values without disturbing a field being edited. `settings` is added to `CHANGE_SPECS` in `catalog_changes.py` as entity `setting`, which the page watches.
- **Rendering:** one `setting_row_html(setting, value, source)` helper that switches on `kind`. The web file has no generic field helper today; this is the first, and it is driven entirely by the registry, so adding a setting later needs no web change.
- **Action:** `POST /actions/settings` with `section=<name>` and the fields of that section, or `op=reset`. Unchecked switches are absent from the body (`read_form_data` drops blanks), so the handler iterates the section's registry entries rather than the submitted keys. Pinned settings are skipped. Same redirect-with-notice and JSON branch as `run_capture_skip_rule_action` (l.4918).
- **API:** `GET /api/settings` returns key, value, default, source and metadata, next to `/api/capture`.
- **CLI** in `lang_jp.py`, matching `set-capture-languages` and `skip-window`: `list-settings`, `set-setting KEY VALUE`, `reset-setting KEY`.

## Ordering and constraints

- **The live-UI work is in progress in another session right now** (`JP/catalog_changes.py` and edits to `lang_jp.py` appeared in the working tree during this investigation; `PageView` has not landed in `lang_jp_web.py` yet). Steps 1-2 below touch `settings.py`, `capture_worker.py`, `ocr_backends.py` and one hook line in `ensure_runtime_schema`; they start only once that session has committed, to avoid two writers in `lang_jp.py`. Step 4 needs the route table from that work.
- Each step is its own commit on main, no attribution trailer. The uncommitted user edits in `README.md`, `main.rs`, `lang_jp.py`, `schema.sql` are left alone (`git add -p` for own hunks only).

1. `settings.py` (registry, table, API) + `ensure_runtime_schema`/`schema.sql` hook + CLI + `test_settings.py`. No behaviour change yet.
2. Worker/OCR/overlay/`lang_jp` read from `Settings`; delete the env-derived `DEFAULT_*` constants; `engine_key` reload. Env vars keep working as pins.
3. Daemon: stop passing env, tray writes rows, "Open settings" item.
4. `/settings` page, action, API, CSS, `/capture` link, `setting` change-log entity.
5. Overlay state file into the table.
6. Docs: `JP/README.md` (settings section replaces the env-var lists, which become "pins"), `AGENTS.md` (a line like the capture-languages one: settings live in the database, do not add new env-only tunables), copy this plan to `JP/settings_hub_plan.md`.

## Verification

- `python -m unittest` in `JP/` (whole suite).
- New `test_settings.py`: default/db/env resolution, validation errors, only non-default rows stored, reset, unknown keys ignored, every registry key unique and every `env` name unique.
- `test_capture_pipeline.py` (fake OCR): a threshold changed between two jobs applies to the second; changing `ocr.backend` rebuilds the adapter (assert on `clear_backend_cache` / adapter identity); an env pin wins over a row.
- `test_lang_jp_web.py`: page renders every section; POST saves, rejects out-of-range values with `level=error`, turns a switch off, skips pinned keys, resets a section; `/api/settings` shape.
- `cargo test` in `desktop_capture_daemon` for the settings read/write helpers and the default-parity test.
- By hand, in the built-in browser pane with daemon and worker running: change min confidence on the page and see it in the next job's log line; switch the backend and watch the worker reload engines without a restart; change OCR size in the tray and see the page update, and the reverse; set `LANG_JP_OCR_MIN_CONFIDENCE` and confirm the row shows as pinned.
