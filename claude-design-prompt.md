# Design brief: Lang, learn any language from the media you love

> "Lang" is a working name. Feel free to propose a better name and wordmark direction.

## 0. How to read this brief

You are designing the **ideal version** of a language-learning product: a desktop app, a mobile app, a browser extension and a marketing website. It works for **any language**. There is no existing interface to respect or migrate from, and you should ignore any assumptions about how it is built today. Design the product as it should be at its best. When something here is ambiguous, pick whatever makes the experience better for the learner and note the decision.

What I want back (details in §13): visual directions, a design system, high-fidelity key screens for every surface, clickable prototypes of the hero flows, and the marketing website.

---

## 1. The product in one paragraph

Most language apps teach you *their* curriculum. Lang turns *your* media into the curriculum. You play games, watch series and films, read books and comics, listen to podcasts, browse the web, or simply live somewhere the language is spoken. Lang captures the language you run into (screenshot, camera, subtitles, browser, audio) and helps you understand it on the spot with meaning, pronunciation, grammar and translation. In the background it builds a personal dictionary of every word and phrase you've met, ranked by how often each one shows up across **the things you actually consume**. Then it teaches you exactly those words with spaced repetition, and every flashcard carries the real moment where you met the word: the screenshot, the line of dialogue, the audio clip. The loop closes when you meet the word again in the wild and understand it.

The core experience is the same for every language. Language-specific behaviour (reading aids, word-structure breakdowns, character study, tones, right-to-left text) plugs into shared components, so learners of Spanish, Japanese, German, Korean, Arabic or Russian all feel the product was built for their language.

---

## 2. Who it's for

- **Primary: the immersion learner.** Somewhere between A2 and B2 in their target language. They already consume media in it, and they're tired of pausing to look things up and of flashcard decks full of words they'll never see. They want to understand *this* show, *this* game, *this* book.
- **The beginner with a goal.** "I want to watch [a series] without subtitles someday." They need a path: what to learn first to make that possible.
- **The expat or traveler.** Lives in or visits the country. Captures signs, menus, letters, forms and overheard phrases, and wants the everyday language of *their* life.
- **The power learner and polyglot.** Learns more than one language, has done thousands of reviews in other tools, and cares about control: editing, bulk operations, data export, scheduler tuning.

Contexts of use: on PC mid-game or mid-episode (immersion must not break), on the couch with a TV and a phone, out in the street with only a phone, commuting (study), and at a desk doing deliberate review.

Three language settings are independent:
- **Target language(s):** one or several, each with its own library and progress.
- **Gloss language:** the language meanings are explained in. The learner can switch to target-language-only definitions once advanced.
- **UI language.**

For example: English UI, learning Japanese and Spanish, with glosses in Brazilian Portuguese. Show at least one screen with the UI localized in PT-BR and one with a right-to-left UI (Arabic).

---

## 3. The core loop

**Capture → Understand → Collect → Prioritize → Study → Meet again**

A story to anchor the hero flows (the same loop works for every language):

1. You're playing an RPG in Spanish on PC. A line of dialogue appears: "¡No puede ser! ¿Nos ha traicionado?". You press a hotkey, or passive capture is already running.
2. Within about a second, a small overlay shows the line with a natural translation, and every word can be clicked. "ha traicionado" is broken down as *traicionar* (to betray) in the present perfect. You keep playing.
3. After the session you get a recap: "Tonight: 214 lines, 61 new words. 18 are worth learning, because they also show up in your other games and shows." You triage them in under a minute from the keyboard (arrows to move, `1` learn, `2` already know, `3` ignore).
4. Next morning on your phone, a review card shows *traicionar* with a crop of the exact scene where you met it, plus the audio.
5. Two days later the word comes up in a Spanish series you're watching. The overlay gives it a light "learning" highlight and logs the sighting, and the review scheduler takes that encounter into account.

---

## 4. Features

The experience is organized around eight pillars. For each feature: what it does, how it should ideally behave, and what that means for the design. Features marked ★ are hero features, so spend the most polish on them.

### 4.1 Capture: get language in from anywhere with no friction

- ★ **Snip capture (desktop).** Global hotkey → screen dims → drag a region, or click a detected text box → result panel. A "repeat last region" hotkey covers dialogue boxes and subtitles that always appear in the same place.
- ★ **Passive capture.** An opt-in mode that watches a chosen window (a game, a video player) and captures only when the on-screen text changes. It drops repeats and frames with no text. A status indicator is always visible, and one key pauses it.
- **Capture profiles per app/media.** Each profile remembers the text region, text direction, font hints, the target language, which recognition model works best, and which media its captures belong to.
- **Video and subtitles.** The browser extension captures subtitle lines from streaming and video sites, with a timestamp, a frame grab and a short audio clip. Subtitle files can be imported for local video, and dual subtitles (target language plus your own) are supported.
- **Reading material.** Import ebooks, PDFs and comics, paste text, or capture web pages through the extension (selected text, or "capture page").
- ★ **Mobile camera.** Point the phone at a book, a sign, a menu, a letter, or a TV. Text is detected live and a tap captures it. Screenshots come in through the share sheet, and photos can be imported from the library in batches.
- **Audio.** Turn speech from podcasts, streams, videos without subtitles, and real conversations (with consent) into transcript lines, each aligned to its audio clip.
- **Language detection.** Each capture's language is detected automatically, and mixed-language lines are handled.
- **Hard text.** Stylized and pixel fonts, handwriting, speech bubbles, vertical text, right-to-left scripts, heavy diacritics, unspaced scripts, and text over busy art. When recognition is unsure, show a confidence level.

*Design implications:* capture must feel instant and must never steal focus from the media. Each capture result is a **Moment** (see 4.3).

### 4.2 Understand: comprehension at the moment you meet the language

- ★ **Live reader overlay.** Text detected in a game or video window becomes interactive. Hover or click a word to get a popover with its meaning, pronunciation, part of speech, your status, and something like "seen 14× in 3 shows." Optionally, reading aids are drawn directly on screen and unknown words get a light underline. The overlay must look at home over any game art or video: minimal, dark, easy to dismiss.
- ★ **Sentence breakdown.** The sentence is split into words (including scripts written without spaces), and any word can be tapped for details. Inflected forms are unpacked through a shared breakdown component:
  - Spanish "se lo habría dicho" → *se* + *lo* + *habría dicho*, the conditional perfect of *decir*
  - German "Mach die Tür auf!" → the separable verb *aufmachen*, imperative
  - Korean 먹었어요 → 먹다 + 었 (past) + 어요 (polite)
  - Japanese 食べさせられた → 食べる + causative + passive + past

  Grammar patterns (the subjunctive, cases, aspect, particles) are detected and linked to short explanations.
- **Translation, two ways.** A natural translation plus a literal word-by-word gloss. A "hide translation" toggle lets you try first.
- **Ask about this.** An AI tutor scoped to the Moment: "Why the subjunctive here?", "Is this rude?", "How would I say this to my boss?" Answers are short, refer to the actual sentence, and can be saved as a note on the Moment.
- **Nuance tags.** Register (formal, informal, slang, vulgar, archaic), regional variety (e.g. Spain vs. Latin American Spanish), idioms, wordplay, and a character's way of speaking.
- **Word anatomy.** Whatever explains a word's structure in that language: roots and affixes, compound parts, Arabic root families (k-t-b → *kitāb*, *kātib*, *maktab*), and the components of Chinese characters and kanji. Also cognates with languages you already know, with clear **false-friend warnings** (Spanish *embarazada* means "pregnant", not "embarrassed").
- **Pronunciation.** Natural text-to-speech for any word or sentence, the original audio clip when there is one, and a prosody view that adapts to the language: stress, tones, pitch accent, vowel length. Optional IPA.
- **Smart reading aids.** The aid a language needs, shown only on words you don't know yet: furigana, pinyin, romanization, vowel marks, stress marks. A global setting switches between always, unknown only, and never.
- ★ **Second-screen mode.** A phone or tablet shows a live, scrolling transcript of what's on the main screen (game dialogue, video subtitles, a podcast) with tap-to-look-up, so the main screen stays clean. It works with PC capture and with the phone camera pointed at a TV.

### 4.3 Collect: your personal corpus

- **Moments.** Every capture becomes a Moment: the image or audio, the text, the translation, linked words, the media it came from, context tags (dialogue, menu, sign, article, lyric, conversation), the speaker and a timestamp. The Moment is the source of truth, and everything links back to it.
- **Library by media.** Media (a game, show, book, podcast, site, or "Daily life" for camera captures) contain Sessions (one play, watch or reading session), which contain Moments. Each media item has cover art, progress and comprehension stats.
- ★ **Session recap and triage inbox.** After a session, or at any time, new words wait in an inbox. Triage should be fast and satisfying, with a one-word-at-a-time mode and a bulk table. It is keyboard-first on desktop and uses swipes on mobile. Actions: **Learn · Already know · Ignore** (names, noise, UI junk) **· Later**. The system pre-sorts by priority and pre-flags likely names, cognates you'll know and noise, so most of the user's work is confirming.
- **Personal dictionary.** Every word you've ever met, with meanings, pronunciation, every form it appeared in (e.g. *traicionado*, *traicionó*, *traicionarán*), every Moment where it appeared (shown as a visual gallery), your personal frequency next to general frequency, its level, and its study status and history.
- **Lemmas and forms.** Inflected forms roll up to one dictionary entry automatically. The learner can see and correct how a form was grouped.
- **Phrase and compound splitting.** A captured string keeps its exact original form but links to its study units. German *Erfahrungspunkte* ("experience points") links to *Erfahrung* and *Punkte*, and a multi-word expression like *echar de menos* is kept as a single item. Splitting and merging happen directly on the text: drag a divider to split, tap to merge.
- **Beyond single words.** Grammar patterns, idioms, collocations and phrasal verbs are collected and studied the same way as words, each with the Moments that use them.
- **Corrections in context.** Fix recognition errors, change a meaning, re-split a phrase or merge duplicate entries right where you notice the problem, with undo. Never in a separate admin tool.
- **Universal search.** Search by word, any form of it, meaning, transliteration, root, media, speaker, date ("what did I see last Tuesday?"), or a loose description like "that word about betrayal from the castle scene". It works across all your languages at once or within one. A command palette (Ctrl/Cmd-K) covers search and actions.

### 4.4 Prioritize: learn what matters to *you* first

- ★ **Personal priority score.** Words that recur across *different* sources rank higher than a word repeated in one menu. UI text that shows up constantly is penalized, general frequency is blended in, and easy cognates are moved down. The ranking can always explain itself: "Why this word? Seen in 9 moments across 4 media · among the 2,000 most common words · appears in the episode you're on."
- ★ **Comprehension meter per media.** "You know 82% of the words in Calle Luna. Learn these 40 to reach 90%." One click turns that list into a study plan.
- **Difficulty preview.** Before you start a new game, show, book or podcast, it estimates how much *you* would understand and lists the top words to learn first, using community data (see 4.8).
- **Pre-study.** Before tonight's episode or chapter, a 5-minute deck of the words you're about to meet.
- **Goals and plans.** "Watch X without subtitles", "Reach B2 by June", "Pass the exam in December", "15 minutes a day". Levels are shown on the CEFR scale, mapped to each language's own exams where they exist (DELE, Goethe, JLPT, HSK, TOPIK…). The app turns a goal into a daily plan and is honest about whether you're on track.

### 4.5 Study: spaced repetition with real context

- ★ **Context cards.** Every card shows where you met the word: the cropped screenshot, the sentence with the word highlighted, the audio clip. Revealing the answer shows the meaning, pronunciation and breakdown, plus other Moments with the same word ("you've also seen it here, here and here").
- **Card types in one mixed queue.**
  - Recognition (target → meaning)
  - Production (meaning → target)
  - Listening (audio → meaning)
  - Sentence cloze
  - Inflection drills (conjugate or decline this word in context)
  - Gender/article (for languages that have them)
  - Character, script or tone cards (for languages that need them)

  Users choose which card types each word gets, and the defaults suit the language.
- **Modern scheduler.** FSRS-class spaced repetition. Simple by default (Again / Good, with optional Hard / Easy) and tunable for power users. Leeches (cards you keep failing) are detected and get useful help: a mnemonic, more context, or splitting the card.
- **Wild-encounter credit.** Meeting a word you're learning in your media and understanding it counts as exposure, and the schedule adjusts.
- **Speaking.** Shadowing with your own captured audio clips, with speech-recognition feedback on pronunciation and prosody.
- **Session shapes.** Daily review, 2-minute micro-sessions, "only this show's words", the pre-study deck, and a mixed or per-language queue for polyglots. Each session ends with a clear summary. Everything works offline.
- **Reading and listening practice.** Re-read your own captured scenes as a graded reader, where reading aids and translations fade as you learn words. Re-listen to captured audio with the transcript revealed progressively.
- **Edit in place.** Fix a card mid-review without leaving the session, suspend it, or mark "I know this" to graduate it.
- **Interop.** Two-way Anki sync and export for people with existing setups. It's an option, not the core.

### 4.6 Script onboarding: for languages with a new writing system

- For absolute beginners in a language with an unfamiliar script (Japanese kana, Korean Hangul, Cyrillic, Arabic, Greek, Devanagari, Thai…), a short, beautiful script trainer: letter shapes, sounds, writing practice with stroke guidance, and reading real words from the learner's own captures as early as possible.
- The trainer hands over smoothly to the normal loop, and its progress feeds the reading-aid setting (aids fade as the script is learned).

### 4.7 Progress: honest, motivating insight

- Known words over time, retention rate, review forecast, time spent immersing vs. studying, and listening vs. reading comprehension.
- ★ **Coverage map.** An explorable grid of the language's most frequent words (e.g. the top 5,000, grouped in frequency bands) colored by your status. It shows at a glance how much of everyday language you've covered. Languages with characters add a character map (kanji, hanzi). This should be a beautiful view that users are proud of.
- **Per-media comprehension over time** ("Episode 1: 61% → Episode 12: 84%").
- **Word journey.** For any word: first met → started learning → mastered → seen again in the wild, as a small timeline that makes progress feel real.
- **Polyglot view.** All your languages side by side: level, momentum, time split.
- **Milestones and recaps.** "1,000 words known", "first episode with no lookups", and a yearly recap worth sharing. Streaks exist but stay gentle: freezes are allowed and there's no guilt messaging.

### 4.8 Platform, sync, privacy, community

- **Local-first.** Works fully offline, with an on-device text and speech recognition option. Sync across devices is optional and end-to-end encrypted.
- ★ **Privacy controls for capture.** Visible and easy to use: app allow/block lists, a pause hotkey, a capture indicator, auto-deleting or compressing raw screenshots and audio after N days, blurring regions, and never capturing outside allowed windows. A clear "what's stored where" view.
- **Import and export everything.** Bring in known words from other apps and lists (Anki, word lists, frequency lists, exam vocabularies), and export all your data.
- **Gloss language per layer.** For example, word meanings in Portuguese, grammar terms in English, and target-language-only definitions once you're advanced.
- **Community (optional, anonymous).** Combined frequency lists per media ("the 500 words that matter in Calle Luna"), shareable decks and difficulty ratings. Screenshots, audio and private captures are never shared.
- **Language packs.** Each language plugs its own dictionary data, word-splitting rules, reading aids, inflection tables, prosody model and card types into the shared components. Adding a language never requires a redesign. Design the components with this flexibility in mind (see §10).

---

## 5. Surfaces

1. **Desktop app (Windows/macOS), the home base.** Library, inbox, dictionary, study, insights and settings. Keyboard-first, and dense where density helps.
2. **Desktop capture layer.** A tray/menu-bar icon, the snip overlay, the live reader overlay on games and video, a mini HUD (capture status, session word count) and the session recap.
3. **Mobile app (iOS/Android), the study and capture companion.** Today, study, camera capture, audio capture, triage, lookup and the second-screen transcript, plus widgets and reviews from the lock screen or notifications.
4. **Browser extension.** A popup dictionary on any page, subtitle capture on video sites, and "send to Lang".
5. **Web app.** The desktop experience in a browser so it's reachable from anywhere (no capture layer).
6. **Marketing website and help center.**

All surfaces share one visual language. The capture layer is deliberately the most minimal.

---

## 6. App screens to design (in priority order)

Spread the target languages across the screens so the system is proven to work for all of them (see §12 for which languages).

**Desktop**
1. ★ **Snip → result.** A game screenshot dimmed, a region being selected, and the result panel with the breakdown.
2. ★ **Live reader overlay** on a game or video. The reading-aid layer, a word popover, the HUD and the passive-capture indicator.
3. ★ **Session recap and triage.** Both the one-word mode and the bulk table mode.
4. **Moment detail.** The image or audio waveform with text regions, sentence breakdown, translation, linked words, split/merge, notes and "ask about this".
5. ★ **Word page.** Meanings, pronunciation and prosody, forms or inflection table, the Moments gallery, word anatomy, cognates and false friends, stats, "why this priority", study history and related words.
6. **Today / home.** Reviews due, inbox count, current media with its comprehension meter, goal progress, and pick up where you left off. For polyglots, a language switcher or combined view.
7. ★ **Media page.** Cover, comprehension meter, "learn 40 words to reach 90%", sessions, top unknown words, and characters/speakers.
8. **Library.** All media and their sessions, filterable by language.
9. **Dictionary (power table).** Filters (language, status, level, part of speech, media, priority), sorting, bulk actions and saved views.
10. ★ **Study session.** Front and back for each card type, edit-in-place, and the end-of-session summary.
11. **Coverage map** (plus the character map variant).
12. **Insights dashboard** (including the polyglot view).
13. **Onboarding.** Choose target, gloss and UI languages; check your level with a quick placement test or by importing known words; say what you watch, play and read; set up capture with permissions explained; do a guided first capture. Show the script-onboarding branch for a new writing system.
14. **Settings.** Capture profiles, privacy and storage, scheduler, languages (with per-language options such as reading aids and regional variety), integrations, sync and account.
15. **Command palette and universal search.**

**Mobile**
1. Today
2. Study card (answer buttons within thumb reach)
3. ★ Camera capture with live text detection (e.g. a menu or street sign)
4. Audio capture with a live transcript
5. Swipe triage
6. Word page
7. ★ Second-screen live transcript
8. Script trainer lesson
9. Home-screen widget and review notification

**Browser extension.** The popup dictionary on a foreign-language article, and the subtitle capture panel on a video page.

---

## 7. Marketing website

Goal: within one scroll, any immersion learner, whatever their language, should think "this is exactly what I've wanted."

- ★ **Hero:** the magic moment, animated and cycling through languages. A game scene in Spanish, a series in Korean, a street sign in German: the text lights up, its words become tappable, and one word flies into a flashcard that still carries its screenshot. Headline direction: *"Your games, shows and books are the curriculum."* (Propose alternatives.)
- **How it works:** Capture → Understand → Remember. Three steps, each shown with a real UI snippet rather than an abstract icon.
- **Feature deep-dives:** live overlay, smart priority and the comprehension meter, context cards, second-screen mode, the coverage map, pronunciation and speaking, privacy and local-first.
- **Audience sections or pages:** gamers, series and film watchers, readers (books, comics, manga), podcast listeners, expats and travelers, exam students.
- **Language page template** (public, for search traffic): "Learn Spanish from what you watch". It shows what's supported for that language (reading aids, breakdowns, pronunciation), popular media, and a start button.
- **Media page template** (public, for search traffic): "Spanish for Calle Luna", with difficulty, top words, a comprehension curve and a "start learning these" button.
- A privacy section in plain language, pricing (Free / Pro, illustrative), FAQ, downloads per platform with OS detection, and a footer with a site-language switcher.
- **Help center / docs** template and a **changelog** page.

The website should feel like the product: real UI and real text in several languages, showing the app at work instead of stock illustrations.

---

## 8. Design principles

1. **Never a word without its moment.** The original context (image, sentence, audio) is always one interaction away. This is the heart of the product and what sets it apart.
2. **Immersion first, study second.** Capture and lookup surfaces are instant and nearly invisible. Study surfaces are calm and focused.
3. **The machine drafts, the human confirms, quickly.** AI and recognition propose, and the UI makes confirming 20 items take seconds. Show confidence and never fake certainty.
4. **Every language is first-class.** Nothing should feel like an English-and-Latin-alphabet app with other scripts bolted on. Typography, layout and components work for all scripts and directions.
5. **One system, language-shaped details.** Shared components carry language-specific behavior through clear slots (the reading-aid layer, the breakdown, prosody, word anatomy). This avoids building a different UI for each language.
6. **Intelligence that explains itself.** Every ranking, estimate and recommendation can answer "why?".
7. **Keyboard-first on desktop, thumb-first on mobile.**
8. **Respect the learner.** No dark patterns, no guilt streaks, no cartoon nagging. Motivation comes from understanding more of what you love.
9. **Dense when you need power, airy when you need focus.**

---

## 9. Visual direction

This is open to your exploration. Propose 2–3 directions before committing. Constraints and leanings:

- Dark mode is primary (gamers, overlays on game art and video). Light mode gets equal polish.
- Mood: a precise, quiet tool with some warmth, like a beautifully made notebook crossed with a modern pro app. Crafted, not cute.
- **Languages are not countries.** Identify languages by name, native name and code (e.g. "Spanish · Español · ES", "Japanese · 日本語 · JA"), not by flags.
- Avoid cultural clichés for any language: no landmarks, stereotypical motifs or mascots in costume.
- One consistent **status color system** used everywhere (overlay underlines, text highlights, table badges, the coverage map, cards): **Unseen · Learning · Known · Mastered · Ignored**. It must be colorblind-safe and always paired with a shape or label, never color alone. Underlines and highlights must work on any script, including ones with marks above and below the line, ruby annotations, and right-to-left text.
- An optional, subtle, toggleable secondary system for parts of speech and grammatical gender.
- Motion with a purpose: words being "collected" into the dictionary, the comprehension meter filling, cards flipping. Every animation has a reduced-motion version.

---

## 10. Multilingual typography and text requirements

- **Font system:** a cohesive stack that covers Latin (including extended diacritics such as ç, ã, ø, ő, ș), Cyrillic, Greek, Arabic, Hebrew, Devanagari, Thai, Hangul and CJK, with consistent weights and optical sizes. The type scale must work for lines that mix scripts.
- **Reading-aid layer** as a single component with variants:
  - annotations above the text (furigana, pinyin, romanization)
  - marks on the letters themselves (stress marks, Arabic/Hebrew vowel marks)
  - optional tone coloring

  All variants stay readable at small sizes and are toggleable through the smart-reading-aid setting.
- **Breakdown component:** stem or lemma plus labeled pieces. It must handle conjugation chains, separable verbs, case endings, agglutinative morphemes and compounds.
- **Right-to-left and bidirectional text:** fully mirrored layouts for RTL UI languages, and correct mixed-direction rendering when the target language is RTL inside an LTR interface (and vice versa).
- **Vertical text** for languages and media that use it (manga, some novels).
- **Unspaced scripts and long words:** word boundaries drawn clearly for Japanese, Chinese and Thai; graceful wrapping and hyphenation for long German or Finnish compounds.
- **UI text expansion:** layouts that survive 30–40% longer strings (German, Portuguese) and very short ones (Chinese).
- **Input:** search accepts native script, transliteration and input without diacritics ("traicion" finds *traición*).
- **Study cards:** the target word large and crisp in any script.
- **Character tiles** with stroke-order animation for scripts that benefit from it.

---

## 11. States, edge cases, accessibility

Design these explicitly:
- First run with an empty library
- Recognition in progress (results appear progressively)
- Low-confidence recognition
- Language detected incorrectly (easy to fix)
- A line mixing two languages
- A language with limited support (be honest about which features are available)
- A huge inbox (500+ items) that feels manageable, not shameful
- No reviews due
- Offline
- Sync conflict
- Passive capture paused or blocked
- Permission denied (screen recording, camera, microphone)
- Deleting data, with the consequences made clear

**Accessibility:** WCAG AA contrast in both themes, full keyboard navigation, screen-reader labels with the correct language tags so text is read with the right voice, scalable text, reduced motion, a colorblind-safe status system, and large touch targets.

---

## 12. Sample content (real language, no lorem ipsum)

Use fictional media titles and original placeholder art, with no real game, show or book IP: e.g. **Aster Chronicle** (RPG, played in Spanish), **Calle Luna** (Spanish-language series), **Nachtzug** (German podcast), **Blue Hour** (Korean webtoon), **Rainy Station** (Japanese visual novel).

Use these languages across the mockups as a stress test. Spanish can be the main demo language, and every other language should appear at least once.

| Language | Sample | Translation | What it demonstrates |
|---|---|---|---|
| Spanish | ¡No puede ser! ¿Nos ha traicionado? | "No way! He's betrayed us?" | the hero story; *traicionar*, present perfect |
| Spanish | Se lo habría dicho si lo hubiera sabido. | "I would have told him if I'd known." | breakdown of pronouns, conditional perfect, subjunctive |
| Spanish | *embarazada* | "pregnant" | false-friend warning for English speakers |
| German | Erfahrungspunkte erhalten | "Experience points gained" | compound splitting (*Erfahrung* + *Punkte*) |
| German | Mach die Tür auf! | "Open the door!" | separable verb *aufmachen* |
| French | Il faut que tu viennes demain. | "You have to come tomorrow." | subjunctive of *venir* |
| Japanese | この先は危険だ。気をつけて進め。 | "It's dangerous ahead. Proceed with care." | furigana, unspaced text |
| Korean | 먹었어요 | "(I) ate", polite | agglutinative breakdown |
| Mandarin | 你说什么？ (nǐ shuō shénme?) | "What did you say?" | pinyin and tones |
| Russian | Осторо́жно, две́ри закрыва́ются. | "Careful, the doors are closing." | Cyrillic, stress marks |
| Arabic | أين المحطة؟ (ayna al-maḥaṭṭa?) | "Where is the station?" | right-to-left text, a mobile camera capture of a sign |

Plausible numbers for a committed learner in their main language: about 600 Moments, 2,500 words met, 900 known, and 120 reviews due today. A polyglot might add a second language at an earlier stage.

---

## 13. Deliverables and order of work

1. **Design directions.** 2–3 visual directions, each shown on the same two screens (Word page and Study card) plus the website hero. Across the directions, show at least three languages, including one non-Latin script and one right-to-left language. **Stop here so I can pick one.**
2. **Design system.** Tokens (color including the status system, a multi-script type scale, spacing, radius, elevation, motion) and components: the reading-aid layer, word chip, status badge, Moment card, word popover, breakdown component, inflection table, prosody view, flashcard, comprehension meter, coverage-map cell, the "why this priority" tooltip, triage controls, language identity chip, command palette and capture HUD. Show each language-flexible component in at least three scripts.
3. **Hero flows as clickable prototypes:**
   a. snip → understand → keep playing
   b. session recap → triage → added to study
   c. study session with context cards
   d. media page → "reach 90%" plan
   e. mobile camera capture and the second-screen transcript
4. **The remaining app screens** from §6, on desktop and mobile, in both themes, including one PT-BR UI screen and one RTL UI screen.
5. **Marketing website.** Landing page (responsive, desktop and mobile), one feature page, the language and media page templates, pricing and download.

For each screen, add short notes on interaction details and the reasoning behind any non-obvious choice.

---

## 14. Avoid

- Designing around any current implementation, CLI or developer tooling. Users never see databases, job queues or recognition engines, only outcomes and confidence.
- Designing for one language and "localizing" later. Prove the system on several scripts from the start.
- Making it look like a generic flashcard app with a camera bolted on. The Moment and its context are central.
- Flags as language icons, and cultural stereotypes.
- Gamification for its own sake: XP bars, leagues, pushy mascots.
- Clutter in the game or video overlay. When in doubt, show less.
