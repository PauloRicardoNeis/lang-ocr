# JP catalog

Minimal local foundation for cataloging Japanese vocabulary from game screenshots.

## Files
- `schema.sql`: SQLite schema.
- `lang_jp.py`: CLI for sources, terms, and recurrence.
- `capture_worker.py`: Python OCR + linguistic-analysis worker for passive capture jobs.
- `requirements-capture.txt`: Python worker dependencies for the dedicated 3.12 environment.
- `requirements-main.txt`: small helper dependencies for the main catalog environment.
- `desktop_capture_daemon/`: Rust tray daemon for passive full-screen capture and job enqueueing.
- `data/lang_jp.sqlite3`: local database file after initialization.
- `jmdict.py`: imports JMdict into `data/jmdict.sqlite3` (git-ignored) and looks up term meanings.
- `ocr_capture_spec.md`: spec and implementation plan for automated screenshot capture + OCR ingestion.

## Fresh Windows setup
For a clean Windows rebuild after formatting the machine:

1. Install Python 3.12 x64.
2. Install Git.
3. Install Rust/Cargo if you want passive capture.
4. From the repository root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 `
  -PythonExe "C:\Path\To\Python312\python.exe" `
  -CapturePythonExe "C:\Path\To\Python312\python.exe" `
  -InstallCaptureDeps `
  -BuildDaemon `
  -RunSmokeTests
```

This creates a fresh `.venv` for the catalog, an optional `.venv-capture312` for OCR/capture, installs the project requirements, and runs a small smoke-test pass.

If you use Anki sync, also install Anki Desktop and the AnkiConnect add-on separately.

## Quick start
```powershell
python .\lang_jp.py init-db
python .\lang_jp.py add-source --source-type image --path "C:\prints\game-001.png" --game "Persona 5" --scene "battle menu"
python .\lang_jp.py link-term --source-id 1 --lemma 回復 --reading かいふく --meaning-pt "cura, recuperacao" --pos noun --count 2
python .\lang_jp.py list-terms
python .\lang_jp.py browse
python .\lang_jp.py generate-flashcards --limit 20
python .\lang_jp.py sync-anki
python .\lang_jp.py sync-anki-words
python .\lang_jp.py generate-kanji-flashcards
python .\lang_jp.py sync-anki-kanji
python .\lang_jp.py export-anki-tsv --include-header
python .\lang_jp.py rebuild-stats
```

For a shorter Windows entry point, run `..\open_jp_catalog.cmd` from the repository root.
For the passive-capture tray, run `..\open_jp_capture_tray.cmd` from the repository root. The launcher reuses an existing compiled daemon when available and otherwise builds `desktop_capture_daemon.exe` once before starting it.
To follow the capture log live in a console window, run `..\open_jp_capture_logs.cmd` from the repository root.

## Passive capture workflow
1. Prepare a dedicated Python 3.12 environment for the capture worker and install `requirements-capture.txt`.
2. Start the Rust tray daemon with `..\open_jp_capture_tray.cmd` from the repository root, or run `cargo run` inside `desktop_capture_daemon` if you want to launch it manually from a shell.
3. The daemon captures the primary monitor either on a fixed interval or in an `after source ready` cadence, dedupes recent frames, writes spool PNGs, and enqueues `capture_jobs`.
4. The Python worker consumes those jobs, runs `EasyOCR` with horizontal-box filtering + Japanese draft analysis, and creates pending `sources`.
5. Review drafts with:
   - `python .\lang_jp.py review-sources`
   - `python .\lang_jp.py review-source --source-id N`
   - `python .\lang_jp.py approve-analysis --source-id N`
   - `python .\lang_jp.py ignore-source --source-id N`

The daemon reads `desktop_capture_daemon\capture_daemon.toml` when present. A starter template lives at `desktop_capture_daemon\capture_daemon.toml.example`.

The tray menu now exposes both capture cadences:
- `Cadence: Fixed interval`: uses the selected capture interval as before.
- `Cadence: After source ready`: waits until the current capture job finishes turning into a source before taking the next screenshot. This mode works best with `auto_start_worker = false` and `auto_process_jobs = true`.
- `manual_capture_hotkey` in `desktop_capture_daemon\capture_daemon.toml`: binds a global shortcut to the same `Capture now` action in the tray. Example: `Ctrl+Shift+F8`.

At the moment, the automatic worker is optimized for normal horizontal text in screenshots. It is not tuned for manga-style vertical text.

## Term meanings (JMdict)
When a new term is created without `--meaning-pt`, its meaning comes from a local copy of JMdict: the first 1-3 English glosses of the best-matching sense are translated to Portuguese. Only when JMdict has no match is the bare lemma machine-translated, as before. The term's part of speech (Sudachi's first field, or an older English label) is a preference when picking the JMdict entry and sense, so kana grammar words resolve correctly: ます with `助動詞` is the polite auxiliary, not 鱒 "trout".

One-time setup (re-run to refresh; stop `browse` first so the file is not locked):

1. Download `JMdict_e.gz` (about 10 MB) from https://www.edrdg.org/pub/Nihongo/JMdict_e.gz into its own folder, for example `data\jmdict_src\`.
2. Import it:

```powershell
python -I .\jmdict.py import .\data\jmdict_src\JMdict_e.gz
python .\jmdict.py lookup 優しい --reading ヤサシク
python .\jmdict.py lookup ます --pos 助動詞
```

Without `data\jmdict.sqlite3` everything keeps working with the old lemma translation. `LANG_JP_JMDICT_DB_PATH` points to another file.

To compare stored meanings with the JMdict-derived ones (read-only, nothing is written):

```powershell
python .\lang_jp.py jmdict-meaning-report --limit 50
python .\lang_jp.py jmdict-meaning-report --missing-only --no-translate --limit 200
```

Attribution: this project uses the JMdict dictionary files. These files are the property of the [Electronic Dictionary Research and Development Group](https://www.edrdg.org/) and are used in conformance with the Group's [licence](https://www.edrdg.org/edrdg/licence.html) (CC BY-SA 4.0).

## Single-image OCR
For a working one-image-at-a-time OCR path, use `single_image_ocr.py`.

Example with visible green overlay boxes already present in the screenshot:

```powershell
py -3.12 .\single_image_ocr.py `
  --image "D:\work\Lang\JP\data\capture_spool\20260329-215354-e140e16cbace.png" `
  --region-mode overlay_boxes `
  --output-text "D:\work\Lang\JP\data\single_image_ocr.txt" `
  --output-json "D:\work\Lang\JP\data\single_image_ocr.json" `
  --debug-image "D:\work\Lang\JP\data\single_image_ocr_debug.png"
```

This path uses a region-first flow with swappable OCR backends:
- `manga-ocr` for manga-style / vertical Japanese text
- `easyocr` for cleaner UI or social-media screenshots

Example for a UI-like screenshot with a manual ROI around the center content column:

```powershell
py -3.12 .\single_image_ocr.py `
  --image "D:\work\Lang\JP\data\capture_spool\zzzlaston e.png" `
  --backend easyocr `
  --region-mode manual `
  --roi "1300,1260,2420,1760" `
  --japanese-only `
  --output-text "D:\work\Lang\JP\data\single_image_ocr_zzz_easyocr.txt" `
  --output-json "D:\work\Lang\JP\data\single_image_ocr_zzz_easyocr.json" `
  --debug-image "D:\work\Lang\JP\data\single_image_ocr_zzz_easyocr_debug.png"
```

## Current workflow
1. Save the screenshot as a source.
2. Review the image manually or with assisted OCR later.
3. Link canonical terms to the source.
4. When a raw string needs to be split for study, keep the original text as a reviewed source segment and link multiple canonical terms to that same segment.
5. Let `term_stats` rank what repeats most across sources.
6. Generate flashcards from ranked terms.
7. Generate kanji flashcards from ranked kanji when you want kanji mixed into the same study queue as vocabulary.
8. Sync pending cards directly to Anki with AnkiConnect, or export a TSV for manual import.

## Notes
- `sources` is the raw truth.
- `source_terms` stores counts per source.
- `source_segments` and `source_segment_terms` preserve reviewed decompositions such as one raw UI string mapping to multiple study terms.
- `term_stats` is derived cache and can be rebuilt.
- `link-term --mode add` increments an existing count for the same source.
- `link-term --segment-text "獲得経験値"` can be called multiple times to attach `獲得` and `経験値` to the same reviewed segment.
- `flashcards` stores generated cards before export.
- `list-flashcards`, `export-anki-tsv`, and `sync-anki` use the current `term_stats.priority_score`, so upload/review order stays aligned with the latest score instead of the original card creation id.
- `sync-anki` also repositions existing Anki new cards by the current score when AnkiConnect exposes the custom `repositionNewCards` action.
- `sync-anki` creates decks on demand and keeps track of the linked Anki note id.
- `sync-anki-words` regenerates vocabulary flashcards before syncing, so missing term cards are created and existing ones are refreshed in one step.
- `generate-kanji-flashcards` and `sync-anki-kanji` now use the main `Lang::Japanese` deck so kanji and vocabulary share the same new-card queue.
- `refresh-kanji-metadata` fetches canonical kanji meanings plus `on/kun` readings from `kanjiapi.dev`.
- `sync-anki-kanji` regenerates kanji payloads before syncing, so the current score/order is what gets pushed to Anki.
- The `/kanji` page includes a button that runs the same kanji sync flow without using the terminal.
- `export-anki-tsv` writes UTF-8 with BOM for better Windows compatibility.
- `browse` now defaults to port `8766` so it does not conflict with AnkiConnect on `8765`.
