# Polyglot plan: one catalog for every language

Status legend: `[x]` done in this pass, `[ ]` open, `[~]` partially done.

State as of 2026-10-21: Phases A, B, C1-C2, D1-D3, E1 and E2 are implemented and covered by `JP/test_languages.py` (27 tests) plus the existing suite. Open items are the ones still unchecked below.

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
- R14. The capture worker keeps OCR blocks whose detected language is enabled for capture (`LANG_CAPTURE_LANGUAGES`, default `ja`), tags each segment, and derives the source language from the majority of its blocks. Translation of sources uses the source language as the source side.
- R15. Text ingestion without OCR (`ingest-text`) runs the same pipeline: detect, split, analyze, create candidates, auto-promote terms.

### 2.3 Surfaces

- R16. CLI: `--language` on term, link, list and flashcard commands; `list-languages`, `enable-language`, `detect-language`, `analyze-text`, `ingest-text`, `set-source-language`.
- R17. Web UI: a language switcher on every page (`?lang=xx`), language shown in term, source and card tables, language-aware kanji navigation (hidden for languages without characters), per-language counts on the dashboard.
- R18. JSON API for other front ends: `/api/languages`, `/api/terms`, `/api/terms/{id}`, `/api/sources`, `/api/flashcards`, all accepting `lang`, `q`, `order`, `limit`, `page`.
- R19. Anki: decks per language, tags `lang_xx` on every note, reprioritization per deck.

### 2.4 Non-functional

- R20. No new mandatory dependencies for the catalog environment: detection and the generic analyzer are standard library. Sudachi, simplemma and jieba are optional and loaded lazily.
- R21. Tests cover detection, mixed-text splitting, generic analysis, key migration and multi-language ingestion end to end.
- R22. The passive-capture OCR layer stays as is. Multi-language OCR needs EasyOCR with `LANG_JP_EASYOCR_LANGS` (ja can only be paired with en in EasyOCR); meikiocr is Japanese-only.

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
- [ ] C3. Capture profiles per window/game with a fixed language (skips detection for known apps).
- [ ] C4. OCR backend selection per language set (EasyOCR language packs, PaddleOCR for zh, Tesseract for Latin scripts).

### Phase D: surfaces

- [x] D1. Web: language switcher, `?lang=` on dashboard, terms, sources, flashcards; language columns.
- [x] D2. Web: JSON API endpoints for languages, terms, sources, flashcards.
- [x] D3. Kanji pages hidden for languages without characters.
- [ ] D4. Web: edit a source's language and re-run analysis from the source page.
- [ ] D5. Web: per-language dashboard cards (counts and top terms side by side).

### Phase E: quality and docs

- [x] E1. Tests: detection, script runs, generic analyzer, migration, multi-language ingestion.
- [x] E2. Docs: README and JP/README updated; AGENTS.md image-only rule becomes language-agnostic.
- [ ] E3. Rename `lang_jp.py` / `lang_jp.sqlite3` to language-neutral names behind compatibility shims.
- [ ] E4. Frequency lists per language for a coverage map (top 5000 words).
- [ ] E5. Readings for non-Japanese languages (pinyin for zh, romanization for ko/ru/ar) through the profile's romanizer hook.
- [ ] E6. Inflection-aware matching for Latin languages (spaCy models as an optional plugin behind the analyzer interface).

## 5. Operating notes

- Default behaviour is unchanged for Japanese: `LANG_CAPTURE_LANGUAGES` defaults to `ja`, decks stay `Lang::Japanese`.
- To capture a Spanish game: set `LANG_CAPTURE_LANGUAGES=es,en`, `LANG_JP_OCR_BACKEND=easyocr`, `LANG_JP_EASYOCR_LANGS=es,en`, then run the worker as usual.
- To add text manually in any language: `python JP/lang_jp.py ingest-text --language auto --text "..."`.
- To browse one language: open `/terms?lang=es` or use the switcher; `lang=all` shows everything.
- Other front ends read `/api/terms?lang=es&order=priority`.
