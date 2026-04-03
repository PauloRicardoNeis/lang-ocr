# Agent Instructions

## Scope
- These instructions apply to the whole repository.

## Default Image-Only Behavior
- If the user sends only an image, with no extra request text, interpret that as a request to populate the Japanese vocabulary database from that image.
- This is a standing convention already agreed with the user. Do not ask for confirmation in that case.

## What "Populate the Database" Means
- Treat the image as a new source for the JP catalog.
- Manually transcribe the visible Japanese text when needed.
- Insert the source into `JP/data/lang_jp.sqlite3`.
- Link the relevant words and expressions from the image to the source, reusing existing terms when appropriate and creating new ones when necessary.
- When a visible string is a compound that should be studied as smaller units, preserve the original string as a reviewed source segment and link multiple canonical terms to that same segment.
- Preserve the current workflow and schema implemented in `JP/lang_jp.py`.

## Expected Outcome
- After an image-only message, the database should be updated and the user should receive:
- Romaji for the visible Japanese text.
- A short Portuguese translation of the visible Japanese text.
- A short confirmation of what was added.

## Translation Preference
- For kanji metadata and kanji flashcards, prefer canonical meanings and glosses in English.
- Do not translate canonical kanji meanings to Portuguese unless the user explicitly asks for that.
