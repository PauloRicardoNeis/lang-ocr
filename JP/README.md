# JP catalog

Local foundation for cataloging vocabulary from game screenshots and text, in any language. The folder keeps its historical `JP` name; the catalog itself is polyglot (see "Multiple languages" below and `../polyglot-plan.md`).

## Files
- `schema.sql`: SQLite schema.
- `lang_jp.py`: CLI for sources, terms, and recurrence.
- `languages.py`: language registry, offline language detection, mixed-text splitting and per-language analyzers (Sudachi for Japanese, a generic tokenizer for word-delimited languages, jieba/pythainlp when installed).
- `capture_worker.py`: Python OCR + linguistic-analysis worker for passive capture jobs. Picks the OCR engines from the capture language set, merges their output and sorts every block per language.
- `ocr_backends.py`: OCR engines (meikiocr, EasyOCR, manga-ocr), engine routing by language set, language-aware line filters and cross-engine block merging.
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
python .\lang_jp.py export-anki-tsv
python .\lang_jp.py rebuild-stats
```

## Multiple languages
Every source, segment, term, character and flashcard carries a `language_code`. Terms are keyed per language (`es::no::` and `en::no::` are different terms), each language gets its own Anki deck (`Lang::Japanese`, `Lang::Spanish`, ...), and the web UI and JSON API filter by language.

```powershell
# What languages are in the catalog
python .\lang_jp.py list-languages

# Detect and split text without saving anything
python .\lang_jp.py detect-language --text "¡No puede ser! ¿Nos ha traicionado?" --runs
python .\lang_jp.py analyze-text --text "안녕하세요 Press START" --candidates ko,en

# Create a text source, split it into words per language and catalog the terms
python .\lang_jp.py ingest-text --language auto --candidates es,en --title "Spanish RPG" --text "¡No puede ser! ¿Nos ha traicionado?"

# Everything else accepts --language (auto, a code, or all for listings)
python .\lang_jp.py add-source --source-type text --language-code auto --raw-text "Eu não sei o que fazer"
python .\lang_jp.py link-term --source-id 12 --lemma traicionar --meaning-pt "trair" --language es
python .\lang_jp.py list-terms --language es
python .\lang_jp.py generate-flashcards --language es
python .\lang_jp.py sync-anki --language es
python .\lang_jp.py set-source-language --source-id 12 --language pt --reanalyze
python .\lang_jp.py enable-language --code de --deck-name "Lang::Deutsch"
```

Detection is offline: script ranges decide CJK, Hangul, Cyrillic, Arabic, Hebrew, Greek, Thai and Devanagari; stop words and diacritics tell Latin-script languages apart. Mixed lines are split into script runs, so a Korean line with an English button label produces one Korean and one English segment. Pass `--candidates` to restrict detection to the languages you actually study.

Japanese keeps JMdict meanings, Sudachi analysis, kanji and romaji. Other languages get lemmas from `simplemma` (installed by both requirement files; lower-cased surface as fallback), meanings from machine translation of the lemma into `LANG_JP_TERM_TRANSLATION_TARGET` (default `pt`), and, when the profile has readings, a Latin reading through the romanizer hook: pinyin for Chinese (`pypinyin`, optional), Revised Romanization for Korean and a transliteration table for Russian and Ukrainian (both built in). Chinese uses `jieba` when installed and falls back to character runs.

### Passive capture in every language

The passive capture catalogs every language that appears on screen; nothing is dropped only because of its language. Which languages are kept lives in the database (`languages.enabled` plus `languages.capture_enabled`), defaults to every built-in profile except the gloss language (`LANG_JP_TERM_TRANSLATION_TARGET`, `pt` by default: meanings are written in it, so its screens are not study material), and is edited from three places that all write the same flags:

- the tray menu `Capture languages` (checkbox per language, `Capture every language`, `Capture Japanese only`);
- the web page `/capture` (also lists the OCR plan, the capture profiles and the recent captures with their window titles);
- the CLI: `set-capture-languages --codes ja,en,es,ko`, `set-capture-languages --all`, `enable-language --code de --capture` / `--no-capture`.

The worker reads the set per job, so changes apply to the next capture without a restart. `LANG_CAPTURE_LANGUAGES` is no longer required; when set in the worker environment it overrides the database for that run.

Latin-script lines are detected against every language, not only the captured ones, and the screen's Latin text as a whole sets a prior that decides short strings with no stop word ("Seguir", "Mostrar mais"). A Portuguese screen with Portuguese off is therefore rejected instead of being filed as Spanish or English. Non-Japanese lines read with OCR confidence below `LANG_JP_MIN_TERM_CONFIDENCE` (default 0.5) are kept as segments without candidates. The word tokenizer skips links, e-mail addresses, @handles, #hashtags, letters stuck to numbers (`21k`) and oddly cased tokens (`kStrear`, `YouTube`), and folds all-caps lines and all-caps words longer than five letters to lower case. Single letters that are not words in their language and Latin lemmas without a vowel (unless written in capitals) are blacklisted.

OCR engines follow the set automatically (`LANG_JP_OCR_BACKEND=auto`, the default): `meikiocr` alone when only `ja` is enabled, otherwise `meikiocr` for the Japanese pass plus one EasyOCR reader per compatible language group (all Latin-script languages share one reader; `ko`, `zh`, `th`, Cyrillic, Arabic and Devanagari each get their own, paired with English when English is enabled). Readers are cached for the life of the worker, EasyOCR detection runs once per capture and later readers only re-read boxes nobody read confidently, and the blocks of every engine are merged by bounding-box overlap and text similarity. Greek and Hebrew have no EasyOCR model, so their text only enters through `ingest-text`. `LANG_JP_OCR_BACKEND=meikiocr` or `easyocr` still force the old single-engine flows.

Capture profiles pin a window to a fixed language set: the daemon records the foreground window title and process name with every job (`capture_jobs.window_title`, `capture_jobs.process_name`, off with `record_foreground_window = false`), and a matching profile fixes the languages for that capture, skipping detection when it has a single language. Sources from the capture get the window title as title and the profile name as game.

```powershell
python .\lang_jp.py list-languages                       # includes capture_enabled and the effective capture set
python .\lang_jp.py set-capture-languages --codes ja,en,es,ko
python .\lang_jp.py add-capture-profile --name "Dragon Quest ES" --languages es,en --window-pattern "*dragon quest*"
python .\lang_jp.py add-capture-profile --name "Korean VN" --languages ko --process-pattern "vn.exe"
python .\lang_jp.py list-capture-profiles
python .\lang_jp.py match-capture-profile --window-title "DRAGON QUEST XI - Steam"
python .\lang_jp.py remove-capture-profile --name "Korean VN"
python .\lang_jp.py list-sources --language all
python .\lang_jp.py set-source-language --source-id 12 --language es --reanalyze --promote
python .\lang_jp.py sync-anki --language all                # one Anki deck per language, lang_xx tags
python .\lang_jp.py export-anki-tsv --language all
```

The web UI has a language bar on every page (`?lang=es`, `lang=all`), and other front ends can read:

- `GET /api/languages` (with `capture_enabled` per language)
- `GET /api/capture` (effective capture set, OCR plan, capture profiles)
- `GET /api/terms?lang=es&q=&order=priority|recent|lemma&limit=&page=`
- `GET /api/terms/{id}`
- `GET /api/sources?lang=es&q=&limit=&page=`
- `GET /api/flashcards?lang=es&status=pending&q=&limit=&page=`

Searches (`q`) ignore case in every script (`água` finds `ÁGUA`, `привет` finds `ПРИВЕТ`) without folding accents, so kana, hangul and Devanagari match exactly.

`lang=all` (or no `lang`) works on every page and every endpoint. The dashboard shows one card per language with counts and top terms; a source page lets you change the source language and re-run its analysis.

### Anki across languages

- **Decks.** Each card goes to its language's deck (`languages.deck_name`, default `Lang::<Name>`); characters share the word deck of their language (kanji in `Lang::Japanese`, hanzi in `Lang::Chinese`). `--deck-name` still forces one deck for everything.
- **What `--language all` generates.** Every enabled language except the gloss language (`LANG_JP_TERM_TRANSLATION_TARGET`, `pt` by default), whose cards would show the same word on both sides. `generate-flashcards --language pt` still creates them on request. Existing cards are exported and synced whatever their language.
- **Fields.** Readings appear only for languages that have them: kana plus romaji for Japanese, then `Pinyin` (zh), `Romanization` (ko) or `Transliteration` (ru, uk). Character cards exist only for languages with characters, and on/kun readings are Japanese-only. Term meanings are in the gloss language, kanji meanings stay in English. Non-Japanese fields carry `lang="xx" dir="auto"`, so Arabic and Hebrew render right to left and Han text picks the right font. Japanese fields are byte-identical to before.
- **Tags.** Every note gets `lang_xx` (Japanese notes also keep `jp`). Older cards that only have `jp` get `lang_ja` on export and sync.
- **Identity.** AnkiConnect notes are found by their stored note id, then by the `term_N` tag, then by front text in the deck. A front match never links a note tagged for another language. Japanese keeps Anki's collection-wide duplicate check; other languages check duplicates per deck, so `ser` (es) and `ser` (it) both get a note.
- **TSV.** `export-anki-tsv` writes Anki 2.1.54+ file headers and the columns `Front, Back, Tags, Deck, Notetype, GUID`, so one import fills every language deck. Non-Japanese rows carry a deterministic GUID built from the language-scoped canonical key, so re-imports update them and same-spelling words never overwrite each other. Japanese rows leave the GUID empty and are matched on the first field, as before. `--format basic` writes the old `Front, Back, Tags` file. Use one path per language: a TSV import cannot match a non-Japanese note that `sync-anki` created (Anki gave it its own GUID), so the import would add a second copy.
- **Sync state.** `export_status`, `exported_at` and `anki_note_id` live on each card, so `--mark-exported` and `sync-anki --language es` only advance that language. A single-language sync only reorders the new cards of that language's decks.

Databases created before this change are upgraded in place on the next command: existing rows become `ja` and canonical keys gain the `ja::` prefix once.

On Windows, the single entry point is `..\open_lang.cmd` from the repository root. It builds `desktop_capture_daemon.exe` when Cargo is available (otherwise it reuses an existing build), starts the tray daemon, and opens the catalog in the browser. If the daemon is already running, it only opens the catalog.

The daemon supervises the catalog web server: it runs `lang_jp.py browse --no-open-browser` on `web_port` (default 8766), restarts it with backoff if it exits, and stops it on Quit. If something else is already serving that port (for example a manual `browse`), the daemon reuses it instead of starting a second server. Web server output goes to `data/catalog_web.log`. Use `Open Lang` in the tray menu, or double-click the tray icon, to open the catalog. Only one daemon runs at a time; launching it again just opens the catalog. To follow the capture log live, use `Tail logs` in the tray menu.

## Passive capture workflow
1. Prepare a dedicated Python 3.12 environment for the capture worker and install `requirements-capture.txt`.
2. Start the Rust tray daemon with `..\open_lang.cmd` from the repository root, or run `cargo run` inside `desktop_capture_daemon` if you want to launch it manually from a shell.
3. The daemon captures the primary monitor either on a fixed interval or in an `after source ready` cadence, dedupes recent frames, writes spool PNGs, and enqueues `capture_jobs`.
4. The Python worker consumes those jobs, runs the OCR engines for the capture language set (meikiocr for Japanese, EasyOCR readers for the other languages), splits the text per language and creates pending `sources` whose segments and candidates carry their language.
5. Review drafts with:
   - `python .\lang_jp.py review-sources`
   - `python .\lang_jp.py review-source --source-id N`
   - `python .\lang_jp.py approve-analysis --source-id N`
   - `python .\lang_jp.py ignore-source --source-id N`

The daemon reads `desktop_capture_daemon\capture_daemon.toml` when present. A starter template lives at `desktop_capture_daemon\capture_daemon.toml.example` (including `record_foreground_window`).

The tray menu now exposes both capture cadences:
- `Cadence: Fixed interval`: uses the selected capture interval as before.
- `Cadence: After source ready`: waits until the current capture job finishes turning into a source before taking the next screenshot. This mode works best with `auto_start_worker = false` and `auto_process_jobs = true`.
- `manual_capture_hotkey` in `desktop_capture_daemon\capture_daemon.toml`: binds a global shortcut to the same `Capture now` action in the tray. Example: `Ctrl+Shift+F8`.
- `Capture languages`: a checkbox per registered language plus `Capture every language` and `Capture Japanese only`. The choice is stored in the database and shared with the web UI and the CLI.

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
- `generate-kanji-flashcards` and `sync-anki-kanji` use the word deck of the character's language (`Lang::Japanese` for kanji), so kanji and vocabulary share the same new-card queue. Both accept `--language`.
- `refresh-kanji-metadata` fetches canonical kanji meanings plus `on/kun` readings from `kanjiapi.dev`.
- `sync-anki-kanji` regenerates kanji payloads before syncing, so the current score/order is what gets pushed to Anki.
- The `/kanji` page includes a button that runs the same kanji sync flow without using the terminal.
- `export-anki-tsv` writes UTF-8 with BOM for better Windows compatibility (Anki skips the BOM before its file headers).
- `browse` now defaults to port `8766` so it does not conflict with AnkiConnect on `8765`.
