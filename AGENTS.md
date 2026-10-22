# Agent Instructions

## Scope
- These instructions apply to the whole repository.

## Default Image-Only Behavior
- If the user sends only an image, with no extra request text, interpret that as a request to populate the vocabulary database from that image.
- This is a standing convention already agreed with the user. Do not ask for confirmation in that case.
- The catalog is polyglot: detect the language of the visible text (Japanese is still the most common case) and tag the source and its terms with that language. Mixed-language screens get one segment per language.

## What "Populate the Database" Means
- Treat the image as a new source for the catalog.
- Manually transcribe the visible text when needed.
- Insert the source into `JP/data/lang_jp.sqlite3` with the right `language_code` (`add-source --language-code auto` detects it from the raw text).
- Link the relevant words and expressions from the image to the source, reusing existing terms when appropriate and creating new ones when necessary. Terms are scoped by language (`link-term` defaults to the source language; pass `--language` to override).
- When a visible string is a compound that should be studied as smaller units, preserve the original string as a reviewed source segment and link multiple canonical terms to that same segment.
- Preserve the current workflow and schema implemented in `JP/lang_jp.py`; language profiles, detection and analyzers live in `JP/languages.py`.

## Expected Outcome
- After an image-only message, the database should be updated and the user should receive:
- A romanization or reading aid for the visible text when the language has one (romaji for Japanese; nothing extra for Latin-script languages).
- A short Portuguese translation of the visible text.
- A short confirmation of what was added, including the language it was filed under.

## Translation Preference
- For kanji metadata and kanji flashcards, prefer canonical meanings and glosses in English.
- Do not translate canonical kanji meanings to Portuguese unless the user explicitly asks for that.
