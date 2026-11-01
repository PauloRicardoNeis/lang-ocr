# Polyglot plan: one catalog for every language

Status legend: `[x]` done in this pass, `[ ]` open, `[~]` partially done.

State as of 2026-10-25: Phases A, B, C1-C4, D1-D5, E1, E2 and E5 are implemented and covered by `JP/test_languages.py`, `JP/test_ocr_backends.py`, `JP/test_capture_pipeline.py` and the integration test `JP/test_capture_integration.py` (real OCR on synthetic screenshots, skipped outside the OCR environment). Open items are the ones still unchecked below; the choices made along the way are in section 6.

## 1. Goal

Make the catalog work for all languages at the same time, in one database and one UI:

- Every source, segment, term, character and flashcard knows which language it belongs to.
- Text is split into study units automatically, with the right tokenizer for each language.
- Mixed input (a Japanese screen with English UI words, a Spanish game with English menus) is sorted per language, not per source.
- The front end and any other consumer (Anki, scripts, exports, future apps) can filter, group and sort by language through the same data model and a JSON API.
- Japanese keeps working exactly as before (JMdict meanings, kanji, readings, romaji, Sudachi) and becomes the first language plugin instead of the only code path.

## 2. Requirements

### 2.1 Data model

- R1. A `languages` table is the registry of languages present in the catalog: code (BCP-47 style, `ja`, `es`, `pt-BR`), display name, script, deck name, enabled flag, sort order.
- R2. `terms.language_code` is mandatory. The canonical key becomes `language::lemma::reading`, so the same lemma can exist in several languages without colliding (`no` in Spanish vs Norwegian, `sake` in Japanese vs English).
- R3. `sources.language_code` stays the dominant language of the source. `source_segments.language_code` and `source_analysis_segments.language_code` hold the per-segment language, which can differ from the source.
- R4. `kanji.language_code` marks which language a character entry belongs to (`ja` today, `zh` later). Characters only exist for languages whose profile has `has_characters`.
- R5. Flashcards inherit the language through their term. Each language has its own default deck (`Lang::Japanese`, `Lang::Spanish`, ...), overridable per language in the registry.
- R6. Existing data migrates automatically on the next `db_connect`: all current rows become `ja`, canonical keys are rewritten once, nothing is deleted.
- R7. Overview views (`v_terms_overview`, `v_flashcards_overview`) expose `language_code` so every list can be filtered without joins.

### 2.2 Automatic language handling

- R8. Language detection without network: script-based for CJK, Hangul, Cyrillic, Arabic, Hebrew, Greek, Thai, Devanagari; stop-word and diacritic scoring for Latin-script languages (en, es, pt, de, fr, it, nl, ...). Detection can be restricted to the enabled languages so that han-only text is `ja` when Chinese is not enabled.
- R9. Mixed text is split into script runs before analysis, so one line can produce terms in two languages.
- R10. A language profile tells the system how to treat a language: word-delimited or not, has readings, has characters, right-to-left, which analyzer, which dictionary, which romanizer.
- R11. Analyzer plugins behind one interface (`analyze(text) -> tokens` with surface, lemma, reading, part of speech and sub-tokens):
  - `ja`: Sudachi (existing behaviour, split modes C and A, kanji candidates).
  - word-delimited languages: generic tokenizer with optional `simplemma` lemmatization when installed, lower-cased lemma fallback.
  - `zh`: `jieba` when installed, character-run fallback.
  - `ko`: whitespace tokens (particle stripping is a later step).
- R12. Meaning lookup per language: JMdict for `ja`; machine translation of the lemma from its own language otherwise. The gloss language stays configurable (`LANG_JP_TERM_TRANSLATION_TARGET`).
- R13. The term blacklist is language-aware: single kana only applies to `ja`; punctuation and digit-only tokens apply everywhere.
- R14. The capture worker keeps OCR blocks whose detected language is enabled for capture. The capture set is stored in the database (`languages.enabled` and `languages.capture_enabled`, default: every built-in language except the gloss language) and edited from the tray, the web UI and the CLI; `LANG_CAPTURE_LANGUAGES` is only an override. Every segment is tagged, the source language is the majority of its blocks, and source translation uses the source language as the source side.
- R15. Text ingestion without OCR (`ingest-text`) runs the same pipeline: detect, split, analyze, create candidates, auto-promote terms.

### 2.3 Surfaces

- R16. CLI: `--language` on term, link, list and flashcard commands; `list-languages`, `enable-language`, `detect-language`, `analyze-text`, `ingest-text`, `set-source-language`.
- R17. Web UI: a language switcher on every page (`?lang=xx`), language shown in term, source and card tables, language-aware kanji navigation (hidden for languages without characters), per-language counts on the dashboard.
- R18. JSON API for other front ends: `/api/languages`, `/api/terms`, `/api/terms/{id}`, `/api/sources`, `/api/flashcards`, all accepting `lang`, `q`, `order`, `limit`, `page`.
- R19. Anki: decks per language, tags `lang_xx` on every note, reprioritization per deck.

### 2.4 Non-functional

- R20. No new mandatory dependencies for the catalog environment: detection and the generic analyzer are standard library. Sudachi, simplemma and jieba are optional and loaded lazily.
- R21. Tests cover detection, mixed-text splitting, generic analysis, key migration and multi-language ingestion end to end.
- R22. OCR engines follow the capture set: meikiocr for the Japanese pass (it is Japanese-only), one EasyOCR reader per compatible language group for the rest (EasyOCR pairs `ja`, `ko`, `zh`, `th` and the non-Latin script groups only with English), detection shared across readers, blocks merged by overlap and text similarity. `LANG_JP_OCR_BACKEND=meikiocr|easyocr` forces one engine.

## 3. Architecture after this change

```
                 +---------------------+
   image/text -> |  languages.py       |  profiles, detection, script runs, analyzers
                 +----------+----------+
                            |
                 +----------v----------+
                 |  lang_jp.py         |  schema + migration, sources, terms (lang-scoped keys),
                 |                     |  analysis candidates, flashcards (deck per language)
                 +---+------------+----+
                     |            |
        +------------v--+     +---v---------------+
        | capture_worker|     | lang_jp_web.py    |  ?lang= filter, switcher, /api/*
        | per-block lang|     +-------------------+
        +---------------+
```

Language-specific behaviour lives in `languages.py` (profiles, analyzers). Everything else reads the profile instead of assuming Japanese.

## 4. To-do list

### Phase A: foundation

- [x] A1. Create `JP/languages.py` with the language registry, profiles and `get_language()`.
- [x] A2. Script-based and stop-word-based detection (`detect_language`, `split_script_runs`).
- [x] A3. Analyzer interface and implementations: Sudachi (ja), generic (Latin, Cyrillic, Greek, Hangul...), jieba/han fallback (zh).
- [x] A4. Schema: `languages` table, `language_code` on `terms`, `source_segments`, `source_analysis_segments`, `kanji`; `language_confidence` on `sources`; language-aware views.
- [x] A5. One-shot migration of existing rows to `ja` with canonical key rewrite.

### Phase B: core behaviour

- [x] B1. Language-scoped canonical keys and term upsert/lookup.
- [x] B2. Language-aware blacklist.
- [x] B3. Meaning lookup per language (JMdict for ja, lemma translation elsewhere).
- [x] B4. Shared `analyze_text_to_candidates` used by worker and CLI; candidates carry the segment language; approval creates terms in that language.
- [x] B5. Kanji derivation only for languages with characters.
- [x] B6. Flashcards: deck per language, `lang_xx` tags, reading/romaji lines only when the profile has readings; `--language` filters on generate/list/export/sync.
- [x] B7. CLI commands: `list-languages`, `enable-language`, `detect-language`, `analyze-text`, `ingest-text`, `set-source-language`, `--language` on existing commands.

### Phase C: capture

- [x] C1. Worker keeps blocks in enabled capture languages instead of Japanese-only, tags segments, derives the source language.
- [x] C2. Source translation uses the source language as the source side.
- [x] C3. Capture profiles per window/game with a fixed language (skips detection for known apps). The daemon records the foreground window title and process name in `capture_jobs`; `capture_profiles` maps title/process patterns to a language set, managed from the CLI and `/capture`.
- [x] C4. OCR backend selection per language set: meikiocr + EasyOCR reader groups chosen from the capture set, cached readers, shared detection, cross-engine merge (`ocr_backends.plan_ocr_engines`, `capture_worker.MultiEngineOcrAdapter`). PaddleOCR/Tesseract were not needed.

### Phase D: surfaces

- [x] D1. Web: language switcher, `?lang=` on dashboard, terms, sources, flashcards; language columns.
- [x] D2. Web: JSON API endpoints for languages, terms, sources, flashcards.
- [x] D3. Kanji pages hidden for languages without characters.
- [x] D4. Web: edit a source's language and re-run analysis from the source page (`POST /actions/sources/{id}/language`, shared `change_source_language`).
- [x] D5. Web: per-language dashboard cards (counts and top terms side by side), `lang=all` on every page and API endpoint, `/capture` settings page, `/api/capture`.

### Phase E: quality and docs

- [x] E1. Tests: detection, script runs, generic analyzer, migration, multi-language ingestion.
- [x] E2. Docs: README and JP/README updated; AGENTS.md image-only rule becomes language-agnostic.
- [ ] E3. Rename `lang_jp.py` / `lang_jp.sqlite3` to language-neutral names behind compatibility shims.
- [ ] E4. Frequency lists per language for a coverage map (top 5000 words).
- [x] E5. Readings for non-Japanese languages through the profile's romanizer hook: pinyin for `zh` (`pypinyin`, optional), Revised Romanization for `ko`, transliteration tables for `ru`/`uk`. Arabic has no romanizer yet.
- [ ] E6. Inflection-aware matching for Latin languages (spaCy models as an optional plugin behind the analyzer interface).

## 5. Operating notes

- Japanese behaviour is unchanged: JMdict, Sudachi, kanji, romaji, `Lang::Japanese` deck, meikiocr for the Japanese pass.
- Every built-in language is captured by default. To narrow it: tray `Capture languages`, web `/capture`, or `python JP/lang_jp.py set-capture-languages --codes ja,en,es,ko`. The worker picks the change up on the next job.
- To pin a game to a language: `python JP/lang_jp.py add-capture-profile --name "Dragon Quest ES" --languages es,en --window-pattern "*dragon quest*"` (or from `/capture`).
- To add text manually in any language: `python JP/lang_jp.py ingest-text --language auto --text "..."`.
- To browse one language: open `/terms?lang=es` or use the switcher; `lang=all` shows everything, the dashboard shows one card per language.
- Other front ends read `/api/terms?lang=es&order=priority` and `/api/capture`.
- `LANG_CAPTURE_LANGUAGES`, `LANG_JP_OCR_BACKEND` and `LANG_JP_EASYOCR_LANGS` remain as overrides for one-off runs only.

## 6. Decisions

Choices made while finishing the capture pipeline (2026-10-24/25), where the plan left room:

- **Database is the source of truth for the capture set.** `languages.capture_enabled` (new column, default 1) next to `languages.enabled`; the tray, `/capture` and the CLI all write it, the worker reads it per job. The environment variable stays as an override because it is handy for one-off runs, but nothing requires it. An empty set falls back to `ja` so the worker never runs with nothing.
- **Upgrading registers every built-in language with capture on.** The first command after the upgrade registers the 24 built-in profiles. The language bar only lists languages that have data (plus the active one) so 24 chips do not clutter every page; the dashboard lists the empty ones in a single line.
- **meikiocr stays the Japanese engine and is never used for other languages.** It only runs when `ja` is in the capture set; with `ja` alone the flow is the pre-polyglot one (same filters, same furigana rule). EasyOCR never gets a `ja` reader in `auto` mode.
- **One EasyOCR reader per compatible group, shared detection, confidence-gated recognition.** EasyOCR's rule only lets `ja`, `ko`, `zh`, `th` and the non-Latin script groups pair with English, so each becomes a reader (paired with `en` when enabled) while all Latin languages share one. The CRAFT detector is the same model for every reader, so detection runs once per capture and later readers only recognize boxes nobody read with confidence >= 0.7 (`LANG_JP_OCR_CONFIDENT_READ`). Without this, 22 languages meant 8 full passes per capture.
- **Cross-engine merge: earlier engine wins.** Blocks whose boxes overlap >= 50% of the smaller box are the same text; 20-50% overlap also needs text similarity >= 0.6. meikiocr goes first so Japanese lines are never replaced by a Latin reader's garbage.
- **Japanese-only filters apply only to the Japanese pass.** `is_plausible_text_line` is language-aware (two letters of an enabled script, >= 30% of the line); the furigana filter runs only when `ja` is enabled; region scoring counts letters of any script. The stat formerly called `non_japanese` is `off_language`.
- **Capture profiles match substrings or wildcards, case-insensitively, title and process both when given, lowest priority first.** One language in a profile forces every block to that language (no detection); several restrict detection. The window title becomes the source title and the profile name the source `game`, because that is what the user searches for later.
- **The daemon adds the window columns itself** when the Python side has not upgraded the schema yet, so a capture never fails on a fresh database.
- **Blocks in a language outside the capture set** stay visible as a segment tagged with the language they look like (unrestricted detection) but produce no candidates, so the reviewer sees why nothing was cataloged.
- **Readings.** Korean and Cyrillic romanization are standard library (Revised Romanization with the vowel-carry rule, BGN-style tables for `ru`/`uk`); pinyin comes from `pypinyin` when installed, otherwise the term simply has no reading. `ru`/`uk` profiles gained `has_readings`. Japanese readings stay kana from Sudachi.
- **Lemmas.** `simplemma` is installed in both venvs. Acronyms keep their case; a token simplemma leaves unchanged is retried lower-cased so sentence-initial words merge with their lower-case form; real inflections (`Häuser` -> `Haus`) win as is.
- **Source translation skips same-language sources** (an English source with target `en` keeps its text) instead of calling the translator.
- **Real screens (live check, 2026-11-01).** Short Latin UI strings were filed by alphabetical tie-break and an `en` fallback, so Portuguese chat and feed text landed in en, es, fr and pl. Now: two-letter stop words weigh less than longer ones; acute accents are no hint (pt and es share them) while `nh`/`lh` are; ties go by language order, not alphabetically; confidence scales with evidence; the whole screen's Latin text sets a prior used for ties and stop-word-less strings; Latin runs are detected against every language, so text in a language outside the capture set is dropped instead of relabeled; a job whose analysis yields no candidates is rejected.
- **The gloss language is off for capture by default.** A fresh `languages` row for `LANG_JP_TERM_TRANSLATION_TARGET` (`pt`) gets `capture_enabled = 0`; turning it on stays one click away.
- **Junk filters for word-delimited languages.** Links, e-mails, @handles, #hashtags, letters glued to digits and oddly cased tokens are skipped by the tokenizer; non-Japanese lines under OCR confidence 0.5 give no candidates; single letters that are not words in their language and vowel-less Latin lemmas (not all-caps) are blacklisted. All-caps lines and all-caps words over five letters are folded to lower case; shorter all-caps words stay acronyms.
- **Not done, on purpose:** Greek and Hebrew have no EasyOCR model, so they are registered but only reachable through `ingest-text` (the capture page says so); Arabic has no romanizer; term meanings are still one translation request per new term, which makes a Latin-heavy first capture slow.
