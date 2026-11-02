# Prompt: finish the polyglot capture pipeline end to end

Copy everything below the line into a fresh Claude Code session opened at `C:\Users\paulo\Documents\work\Lang`.

---

You are working in the `Lang` repository, a local vocabulary catalog that captures screenshots, runs OCR, splits text into words and builds Anki flashcards. The code lives in `JP/` for historical reasons but the data model is already polyglot. Read `README.md`, `AGENTS.md`, `JP/README.md` and `polyglot-plan.md` first. Treat `polyglot-plan.md` as the requirements document: phases A, B, C1-C2, D1-D3, E1-E2 are done. Your job is to make the whole application capture, catalog and study **every word in every language that appears on screen**, with no manual environment variables, and to prove it with real data.

## Where things stand today

Verify each of these before changing anything, and correct this list in your own notes if the code disagrees.

- `JP/languages.py` has 24 built-in language profiles, offline detection, script-run splitting and analyzers (Sudachi for `ja`, a generic tokenizer with optional `simplemma`, `jieba` for `zh`, `pythainlp` for `th`). `simplemma` is not installed in either venv.
- The live database `JP/data/lang_jp.sqlite3` has only `ja` in the `languages` table (8352 terms, 10416 sources, all `ja`). No other language has ever gone through the pipeline on real data.
- `JP/capture_worker.py` reads `LANG_CAPTURE_LANGUAGES` from the environment, default `ja`. Blocks whose language is not in that set are dropped (`contains_target_language_text`, stat still named `non_japanese`), and a job with no block in an enabled language is rejected with `no_target_language_text`.
- The default OCR engine is `meikiocr`, which is Japanese-only. `JP/ocr_backends.py` filters its lines with `is_plausible_japanese_line` and `filter_ocr_line_boxes`, so Latin, Hangul, Cyrillic and other text never reaches the analyzer regardless of `LANG_CAPTURE_LANGUAGES`. EasyOCR is the alternative, but one EasyOCR reader can pair `ja` only with `en`, and other CJK languages each need their own reader.
- The Rust daemon in `JP/desktop_capture_daemon/src/main.rs` launches the worker and only passes `LANG_JP_OCR_MAX_SIDE`, `LANG_JP_CAPTURE_DEBUG`, `PYTHONIOENCODING`. There is no way to choose capture languages from the tray or from `capture_daemon.toml`.
- `.venv` is the catalog environment (`JP/requirements-main.txt`), `.venv-capture312` is the OCR environment (`JP/requirements-capture.txt`, has `easyocr` and `meikiocr`). Tests are plain `unittest` files `JP/test_*.py`; run them with `.venv\Scripts\python.exe -m unittest discover -s JP -p "test_*.py"` and the capture-related ones also under `.venv-capture312`.
- The working tree has uncommitted work: `open_lang.cmd` plus daemon changes that supervise the web server and enforce a single instance, and the three old `open_jp_*.cmd` launchers deleted. Review that diff, run the daemon build (`cargo build --release` in `JP/desktop_capture_daemon`) and commit it first as its own commit so the polyglot work starts from a clean tree.

## Definition of done

When you are finished, this must be true on this machine, not just in tests:

1. Starting `open_lang.cmd` and playing or browsing anything in Japanese, English, Spanish, Portuguese, Korean, Chinese or any other profile in `languages.py` produces pending sources whose segments are tagged with the right language and whose candidates become terms in that language. Nothing is dropped only because of its language.
2. Which languages are captured is stored in the database (`languages.enabled`, plus a per-language `capture_enabled` flag or equivalent) and editable from the tray menu, from the web UI and from the CLI. The environment variable `LANG_CAPTURE_LANGUAGES` may remain as an override but is no longer required. Default for a fresh install: all built-in languages enabled for capture.
3. OCR engine selection follows the enabled language set automatically (plan item C4): `meikiocr` when the set is `ja` only, `meikiocr` plus an EasyOCR reader for the remaining languages otherwise, with blocks from both engines merged and deduplicated by bounding box overlap and text similarity. EasyOCR readers are built per compatible language group (EasyOCR's rule: `ja`, `ko`, `zh_sim`, `zh_tra`, `th` can each only be combined with Latin-script languages). Cache readers so a capture does not rebuild models. All Japanese-only filters in `ocr_backends.py` and `capture_worker.py` become language-aware or apply only to the Japanese pass.
4. Every term in a non-Japanese language gets a lemma (install `simplemma` in both venvs and add it to both requirement files), a meaning in the gloss language (`LANG_JP_TERM_TRANSLATION_TARGET`, default `pt`), and, when the profile has readings, a reading through the romanizer hook (plan item E5: `pypinyin` for `zh`, a Hangul romanizer for `ko`, a Cyrillic transliteration table for `ru`/`uk`). Optional dependencies stay optional with a clear fallback.
5. Flashcards go to one Anki deck per language with `lang_xx` tags, and `sync-anki --language all` syncs every deck. Verify against a running Anki with AnkiConnect if it is available; otherwise verify with `export-anki-tsv --language all` and say so.
6. The web UI shows all languages at once on the dashboard (plan item D5: one card per language with counts and top terms) and lets the user change a source's language and re-run analysis from the source page (plan item D4). `lang=all` works on every page and on every `/api/*` endpoint.
7. Capture profiles per window (plan item C3): the daemon records the foreground window title and process name in `capture_jobs`, and a `capture_profiles` table maps a window title pattern to a fixed language set that skips detection. Manage profiles from the CLI and the web UI. This can be the last item if time is short, but ship it or state clearly that it is missing.
8. Tests cover: engine routing by language set, block merging across engines, language-aware filters, readings for `zh`/`ko`/`ru`, the capture flag and profile tables, `lang=all` on the API, and one end-to-end test that renders synthetic screenshots with Pillow in at least `es`, `ko` and `ja`+`en`, runs the real worker pipeline on them under `.venv-capture312`, and asserts the terms land in the right languages. Mark that test as an integration test that is skipped when the OCR environment is not present.
9. `polyglot-plan.md` checkboxes updated, `README.md`, `JP/README.md` and `AGENTS.md` describe the new behaviour, and `JP/desktop_capture_daemon/capture_daemon.toml.example` documents the new keys.

## How to work

- Preserve the existing Japanese behaviour exactly: JMdict meanings, Sudachi, kanji, romaji, `Lang::Japanese` deck. Run the full suite after every phase.
- Keep the schema upgrade path in `ensure_runtime_schema` so the existing database upgrades in place on the next command. Back up `JP/data/lang_jp.sqlite3` to `JP/data/backups/` before the first run against the real database.
- Commit after each phase directly on `main`, with a conventional message (`feat:`, `fix:`, `test:`, `docs:`), no `Co-Authored-By` trailer and no "Generated with Claude Code" footer.
- Do the phases in this order: commit pending work, database and registry flags, OCR routing and filters, worker language handling, lemmas and readings and meanings, flashcards and Anki, web UI, daemon and tray, capture profiles, tests, docs, real-data verification.
- For real-data verification, enable `es`, `en`, `ko` and `ja` for capture, run the worker once on the synthetic screenshots from the integration test plus three real screenshots you take with the daemon (any windows with text are fine), then show `list-languages`, `list-terms --language es`, `list-terms --language ko`, `list-sources --language all` output and a screenshot of the dashboard with several language cards. Report what failed or could not be checked.
- When a decision is not covered here, choose what keeps the Japanese flow intact and the multi-language flow automatic, and write the decision down in `polyglot-plan.md` under a new "Decisions" section.
