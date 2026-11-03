"""Language registry, detection and analyzer plugins for the polyglot catalog.

Everything in this module is standard library only. Language-specific
dependencies (SudachiPy for Japanese, ``simplemma`` for lemmatizing
word-delimited languages, ``jieba`` for Chinese) are imported lazily inside the
analyzer that needs them, so the catalog keeps working for every other language
when they are missing.

Main entry points:

- :func:`get_language` returns the :class:`LanguageProfile` for a code.
- :func:`detect_language` guesses the language of a text without network.
- :func:`split_script_runs` splits mixed text into runs of one script.
- :func:`get_analyzer` returns the tokenizer/lemmatizer for a language.
- :func:`analyze_text` detects, splits and tokenizes in one call.
- :func:`romanize` gives non-Japanese languages with readings a Latin reading
  (pinyin via ``pypinyin`` when installed, Hangul and Cyrillic built in).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Protocol


DEFAULT_LANGUAGE_CODE = "ja"
AUTO_LANGUAGE_CODE = "auto"
ALL_LANGUAGES_CODE = "all"

SCRIPT_LATIN = "latin"
SCRIPT_JAPANESE = "japanese"
SCRIPT_HAN = "han"
SCRIPT_HANGUL = "hangul"
SCRIPT_CYRILLIC = "cyrillic"
SCRIPT_ARABIC = "arabic"
SCRIPT_HEBREW = "hebrew"
SCRIPT_GREEK = "greek"
SCRIPT_THAI = "thai"
SCRIPT_DEVANAGARI = "devanagari"
SCRIPT_OTHER = "other"


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    name: str
    native_name: str
    script: str
    deck_name: str
    word_delimited: bool = True
    has_readings: bool = False
    has_characters: bool = False
    rtl: bool = False
    analyzer: str = "generic"
    dictionary: str | None = None
    romanizer: str | None = None

    @property
    def display_label(self) -> str:
        if self.native_name and self.native_name != self.name:
            return f"{self.name} ({self.native_name})"
        return self.name


def _profile(
    code: str,
    name: str,
    native_name: str,
    script: str,
    **overrides: object,
) -> LanguageProfile:
    deck_name = str(overrides.pop("deck_name", f"Lang::{name}"))
    return LanguageProfile(
        code=code,
        name=name,
        native_name=native_name,
        script=script,
        deck_name=deck_name,
        **overrides,  # type: ignore[arg-type]
    )


BUILTIN_LANGUAGES: dict[str, LanguageProfile] = {
    profile.code: profile
    for profile in (
        _profile(
            "ja",
            "Japanese",
            "日本語",
            SCRIPT_JAPANESE,
            word_delimited=False,
            has_readings=True,
            has_characters=True,
            analyzer="sudachi",
            dictionary="jmdict",
            romanizer="kana",
        ),
        _profile("en", "English", "English", SCRIPT_LATIN),
        _profile("es", "Spanish", "Español", SCRIPT_LATIN),
        _profile("pt", "Portuguese", "Português", SCRIPT_LATIN),
        _profile("de", "German", "Deutsch", SCRIPT_LATIN),
        _profile("fr", "French", "Français", SCRIPT_LATIN),
        _profile("it", "Italian", "Italiano", SCRIPT_LATIN),
        _profile("nl", "Dutch", "Nederlands", SCRIPT_LATIN),
        _profile("sv", "Swedish", "Svenska", SCRIPT_LATIN),
        _profile("pl", "Polish", "Polski", SCRIPT_LATIN),
        _profile("tr", "Turkish", "Türkçe", SCRIPT_LATIN),
        _profile("id", "Indonesian", "Bahasa Indonesia", SCRIPT_LATIN),
        _profile("vi", "Vietnamese", "Tiếng Việt", SCRIPT_LATIN),
        _profile(
            "ko",
            "Korean",
            "한국어",
            SCRIPT_HANGUL,
            has_readings=True,
            romanizer="hangul",
        ),
        _profile(
            "zh",
            "Chinese",
            "中文",
            SCRIPT_HAN,
            word_delimited=False,
            has_readings=True,
            has_characters=True,
            analyzer="han",
            romanizer="pinyin",
        ),
        _profile("ru", "Russian", "Русский", SCRIPT_CYRILLIC, has_readings=True, romanizer="cyrillic"),
        _profile(
            "uk",
            "Ukrainian",
            "Українська",
            SCRIPT_CYRILLIC,
            has_readings=True,
            romanizer="cyrillic",
        ),
        _profile("el", "Greek", "Ελληνικά", SCRIPT_GREEK),
        _profile("ar", "Arabic", "العربية", SCRIPT_ARABIC, rtl=True),
        _profile("he", "Hebrew", "עברית", SCRIPT_HEBREW, rtl=True),
        _profile(
            "th",
            "Thai",
            "ไทย",
            SCRIPT_THAI,
            word_delimited=False,
            analyzer="thai",
        ),
        _profile("hi", "Hindi", "हिन्दी", SCRIPT_DEVANAGARI),
    )
}

LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def normalize_language_code(value: str | None) -> str:
    """Normalize ``JA``, ``ja_JP`` or ``pt-br`` into ``ja``, ``ja-JP`` or ``pt-BR``."""
    if value is None:
        return ""
    raw = value.strip().replace("_", "-")
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in (AUTO_LANGUAGE_CODE, ALL_LANGUAGES_CODE):
        return lowered
    parts = lowered.split("-")
    normalized = [parts[0]]
    for part in parts[1:]:
        if len(part) == 2:
            normalized.append(part.upper())
        elif len(part) == 4:
            normalized.append(part.title())
        else:
            normalized.append(part)
    return "-".join(normalized)


def is_valid_language_code(value: str | None) -> bool:
    normalized = normalize_language_code(value)
    return bool(normalized) and LANGUAGE_CODE_PATTERN.match(normalized) is not None


def base_language_code(value: str | None) -> str:
    normalized = normalize_language_code(value)
    return normalized.split("-", 1)[0] if normalized else ""


def get_language(code: str | None) -> LanguageProfile:
    """Return the profile for ``code``.

    Regional variants fall back to the base language (``pt-BR`` -> ``pt``) but
    keep their own code, and unknown codes get a generic Latin-script profile so
    that nothing breaks on a language nobody registered yet.
    """
    normalized = normalize_language_code(code) or DEFAULT_LANGUAGE_CODE
    profile = BUILTIN_LANGUAGES.get(normalized)
    if profile is not None:
        return profile
    base = base_language_code(normalized)
    base_profile = BUILTIN_LANGUAGES.get(base)
    if base_profile is not None:
        return LanguageProfile(
            **{**base_profile.__dict__, "code": normalized}
        )
    return LanguageProfile(
        code=normalized,
        name=normalized.upper(),
        native_name=normalized.upper(),
        script=SCRIPT_LATIN,
        deck_name=f"Lang::{normalized.upper()}",
    )


def language_display_name(code: str | None) -> str:
    return get_language(code).name


# ---------------------------------------------------------------------------
# Script classification
# ---------------------------------------------------------------------------


def char_script(char: str) -> str | None:
    """Classify one character into a script bucket, or ``None`` for non-letters."""
    codepoint = ord(char)
    if 0x3041 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF:
        return SCRIPT_JAPANESE  # hiragana, katakana, phonetic extensions
    if 0xFF66 <= codepoint <= 0xFF9D:
        return SCRIPT_JAPANESE  # halfwidth katakana
    if (
        0x4E00 <= codepoint <= 0x9FFF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0xF900 <= codepoint <= 0xFAFF
        or codepoint in (0x3005, 0x3007)
    ):
        return SCRIPT_HAN
    if 0xAC00 <= codepoint <= 0xD7AF or 0x1100 <= codepoint <= 0x11FF or 0x3130 <= codepoint <= 0x318F:
        return SCRIPT_HANGUL
    if 0x0400 <= codepoint <= 0x052F:
        return SCRIPT_CYRILLIC
    if 0x0600 <= codepoint <= 0x06FF or 0x0750 <= codepoint <= 0x077F or 0xFB50 <= codepoint <= 0xFDFF or 0xFE70 <= codepoint <= 0xFEFF:
        return SCRIPT_ARABIC
    if 0x0590 <= codepoint <= 0x05FF:
        return SCRIPT_HEBREW
    if 0x0370 <= codepoint <= 0x03FF or 0x1F00 <= codepoint <= 0x1FFF:
        return SCRIPT_GREEK
    if 0x0E00 <= codepoint <= 0x0E7F:
        return SCRIPT_THAI
    if 0x0900 <= codepoint <= 0x097F:
        return SCRIPT_DEVANAGARI
    category = unicodedata.category(char)
    if category.startswith("L"):
        if codepoint < 0x0250 or 0x1E00 <= codepoint <= 0x1EFF or 0xFF21 <= codepoint <= 0xFF5A:
            return SCRIPT_LATIN
        return SCRIPT_OTHER
    return None


def script_histogram(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for char in text:
        script = char_script(char)
        if script is None:
            continue
        counts[script] = counts.get(script, 0) + 1
    return counts


def dominant_script(text: str) -> str | None:
    counts = script_histogram(text)
    if not counts:
        return None
    # Kana anywhere means Japanese even when kanji outnumber them.
    if counts.get(SCRIPT_JAPANESE):
        counts[SCRIPT_JAPANESE] += counts.pop(SCRIPT_HAN, 0)
    # On a tie the non-Latin script wins: Latin letters show up in every language
    # as acronyms and UI labels (HP, OK, START).
    return max(counts.items(), key=lambda item: (item[1], item[0] != SCRIPT_LATIN, item[0]))[0]


@dataclass(frozen=True)
class ScriptRun:
    text: str
    script: str | None
    start: int
    end: int


def _run_script_for_char(char: str) -> str | None:
    script = char_script(char)
    if script is None:
        return None
    return script


def split_script_runs(text: str) -> list[ScriptRun]:
    """Split text into maximal runs that share a script.

    Punctuation, digits and whitespace attach to the run they are next to, so
    ``"HP 100 回復しました"`` yields a Latin run (``"HP 100 "``) followed by a
    Japanese run. Han characters merge into an adjacent Japanese run because kanji
    and kana belong to the same text.
    """
    if not text:
        return []
    # Each entry: [chars, script, start]. Neutral characters (spaces, digits,
    # punctuation) join the run they follow; a leading neutral prefix joins the
    # first run.
    runs: list[list[object]] = []
    for index, char in enumerate(text):
        script = _run_script_for_char(char)
        if not runs:
            runs.append([[char], script, 0])
            continue
        current = runs[-1]
        current_script = current[1]
        if script is None:
            current[0].append(char)
            continue
        if current_script is None:
            current[0].append(char)
            current[1] = script
            continue
        compatible = script == current_script or {script, current_script} <= {
            SCRIPT_JAPANESE,
            SCRIPT_HAN,
        }
        if compatible:
            current[0].append(char)
            if SCRIPT_JAPANESE in (script, current_script):
                current[1] = SCRIPT_JAPANESE
            continue
        runs.append([[char], script, index])
    result: list[ScriptRun] = []
    for position, (chars, script, start) in enumerate(runs):
        end = runs[position + 1][2] if position + 1 < len(runs) else len(text)
        result.append(ScriptRun(text="".join(chars), script=script, start=int(start), end=int(end)))
    return result


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageGuess:
    code: str
    confidence: float
    script: str | None
    scores: dict[str, float] = field(default_factory=dict)


SCRIPT_DEFAULT_LANGUAGE: dict[str, str] = {
    SCRIPT_JAPANESE: "ja",
    SCRIPT_HAN: "zh",
    SCRIPT_HANGUL: "ko",
    SCRIPT_CYRILLIC: "ru",
    SCRIPT_ARABIC: "ar",
    SCRIPT_HEBREW: "he",
    SCRIPT_GREEK: "el",
    SCRIPT_THAI: "th",
    SCRIPT_DEVANAGARI: "hi",
    SCRIPT_LATIN: "en",
}

# Small, high-frequency function-word lists. They only need to tell Latin-script
# languages apart, not to be complete. Their order is also the tie-break order
# when two languages score the same.
LATIN_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "a an i the and of to in on at by is it be as or if no so do up my we me us our "
        "you that was for with this have from they are not but what all were when there can "
        "your will one would about which their said them into than then its also been has had his her "
        "she him who get got just like know think how why now new more some any only out over "
        "after here off very much these those may should could must does did let yes".split()
    ),
    "es": frozenset(
        "a e u el la los las de del que y en un una por con para es no se su al lo como más pero sus le ya "
        "o este esta fue son muy hay está están todo también puede ser sin sobre entre cuando donde "
        "nos ha has he mi tu te me qué cómo esto eso aquí ahora".split()
    ),
    "pt": frozenset(
        "o a os as de do da dos das no na nos nas que e em um uma por pelo pela com para é não se seu "
        "sua ao aos à às lo como mais mas seus suas lhe já ou nem este esta foi são muito há está estão "
        "tudo também pode ser sem sobre entre quando onde você vocês eu tu me te meu minha isso isto "
        "aqui agora ele ela eles elas dele dela num numa tem ter vai vão só bem porque então".split()
    ),
    "de": frozenset(
        "der die das und du ich er sie es wir ihr nicht ein eine einer eines einem einen ist sind war "
        "mit von zu auf für den dem des im in an auch noch aber wenn dann nur wie was wer kann muss "
        "sich hat haben wird werden ja nein mich dich mir dir sein ihre".split()
    ),
    "fr": frozenset(
        "le la les de des du et un une est sont pas ne que qui dans pour avec sur ce cette ces il elle "
        "ils elles nous vous je tu on au aux mais ou où plus très être avoir fait tout tous été son sa "
        "ses leur leurs moi toi lui y en comme quand même".split()
    ),
    "it": frozenset(
        "il lo la i gli le di del della dei delle e ed un una uno è sono non che chi con per su in da "
        "nel nella questo questa quello come ma anche più molto essere avere ha hanno io tu lui lei "
        "noi voi loro mi ti si ci vi ne già ancora dove quando perché".split()
    ),
    "nl": frozenset(
        "de het een en van is zijn niet op met voor dat die dit ik je jij hij zij wij we ze u er ook "
        "maar als dan nog naar om te bij uit aan heeft hebben wordt worden geen wel".split()
    ),
    "sv": frozenset(
        "och att det är en ett jag du han hon vi ni de inte som på med för till av om var har kan ska "
        "men så här där när vad vem hur den dessa denna".split()
    ),
    "pl": frozenset(
        "i w z na do nie się jest to że jak co ale po od za dla przez przy tak jak już tylko może być "
        "ja ty on ona my wy oni są był była było będzie czy".split()
    ),
    "tr": frozenset(
        "ve bir bu da de için ile mi ne o ben sen biz siz onlar var yok çok daha gibi ama ki her şey "
        "değil evet hayır".split()
    ),
    "id": frozenset(
        "dan yang di ke dari untuk dengan ini itu tidak adalah ada akan saya kamu dia kami mereka pada "
        "juga bisa sudah belum atau karena".split()
    ),
    "vi": frozenset(
        "và của là có không được trong một những các này cho với tôi bạn anh chị em họ chúng rằng thì "
        "đã sẽ đang".split()
    ),
}

# Acute accents (á, é, í, ó, ú) are as common in Portuguese as in Spanish, so
# they are no hint for either.
LATIN_DIACRITIC_HINTS: dict[str, dict[str, float]] = {
    "pt": {"ã": 3.0, "õ": 3.0, "ç": 1.0, "ê": 0.5, "â": 0.5, "nh": 1.0, "lh": 1.0},
    "es": {"ñ": 3.0, "¿": 3.0, "¡": 3.0},
    "de": {"ß": 3.0, "ä": 1.5, "ö": 1.5, "ü": 1.5},
    "fr": {"ç": 0.5, "è": 1.0, "ê": 0.5, "œ": 3.0, "à": 1.0, "ù": 2.0},
    "it": {"ì": 2.0, "ò": 1.5, "à": 0.5, "è": 0.5},
    "sv": {"å": 2.5, "ä": 0.5, "ö": 0.5},
    "pl": {"ł": 3.0, "ż": 3.0, "ś": 3.0, "ą": 3.0, "ę": 3.0},
    "tr": {"ı": 3.0, "ş": 2.0, "ğ": 3.0},
    "vi": {"ă": 3.0, "ơ": 3.0, "ư": 3.0, "đ": 2.0},
}

CYRILLIC_HINTS: dict[str, dict[str, float]] = {
    "uk": {"ї": 3.0, "є": 3.0, "і": 2.0, "ґ": 3.0},
    "ru": {"ы": 2.0, "э": 1.5, "ё": 1.5, "ъ": 1.0},
}

WORD_PATTERN = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)

LATIN_TIE_ORDER: dict[str, int] = {code: index for index, code in enumerate(LATIN_STOPWORDS)}
# Two-letter function words ("no", "do", "to") are shared by many languages, so
# they count less than longer ones ("ser", "the").
SHORT_STOPWORD_WEIGHT = 1.5
STOPWORD_WEIGHT = 2.0
# What the rest of the screen says: enough to settle a tie or a lone accent, not
# enough to beat a real stop word of another language.
PRIOR_WEIGHT = 1.0
# Stop-word score at which a guess counts as fully backed by evidence.
FULL_EVIDENCE_SCORE = 6.0


def _words(text: str) -> list[str]:
    return [match.group(0) for match in WORD_PATTERN.finditer(text)]


def _hint_scores(text: str, hints: dict[str, dict[str, float]]) -> dict[str, float]:
    lowered = text.lower()
    scores: dict[str, float] = {}
    for code, weights in hints.items():
        total = 0.0
        for char, weight in weights.items():
            total += lowered.count(char) * weight
        if total:
            scores[code] = total
    return scores


def _restrict(candidates: Iterable[str] | None) -> set[str] | None:
    if candidates is None:
        return None
    restricted = {base_language_code(code) for code in candidates if normalize_language_code(code)}
    restricted.discard("")
    return restricted or None


def detect_language(
    text: str | None,
    *,
    candidates: Iterable[str] | None = None,
    default: str | None = None,
    prior: str | None = None,
) -> LanguageGuess:
    """Guess the language of ``text`` without any network call.

    ``candidates`` restricts the answer to those languages (the enabled set for
    capture, for example). ``default`` wins when the text carries no evidence.
    ``prior`` is the Latin-script language the surrounding text is in (the rest
    of the screen): it breaks ties and decides short strings with no stop word,
    but a clear stop word of another language still wins.
    """
    text = text or ""
    restricted = _restrict(candidates)
    fallback = normalize_language_code(default) or (
        sorted(restricted)[0] if restricted else DEFAULT_LANGUAGE_CODE
    )
    histogram = script_histogram(text)
    if not histogram:
        return LanguageGuess(code=fallback, confidence=0.0, script=None)

    script = dominant_script(text)
    letters = sum(histogram.values())

    def allowed(code: str) -> bool:
        return restricted is None or base_language_code(code) in restricted

    if script == SCRIPT_JAPANESE:
        return LanguageGuess(code="ja", confidence=min(1.0, 0.6 + letters / 20), script=script)
    if script == SCRIPT_HAN:
        # Han-only text is Chinese unless Chinese is not a candidate and Japanese is.
        if allowed("zh") or not allowed("ja"):
            return LanguageGuess(code="zh", confidence=0.55, script=script)
        return LanguageGuess(code="ja", confidence=0.45, script=script)
    if script == SCRIPT_CYRILLIC:
        scores = _hint_scores(text, CYRILLIC_HINTS)
        code = max(scores.items(), key=lambda item: item[1])[0] if scores else "ru"
        if not allowed(code):
            code = next((c for c in ("ru", "uk") if allowed(c)), code)
        return LanguageGuess(code=code, confidence=0.7 if scores else 0.5, script=script, scores=scores)
    if script in SCRIPT_DEFAULT_LANGUAGE and script != SCRIPT_LATIN:
        return LanguageGuess(code=SCRIPT_DEFAULT_LANGUAGE[script], confidence=0.8, script=script)
    if script == SCRIPT_OTHER:
        return LanguageGuess(code=fallback, confidence=0.1, script=script)

    # Latin script: score stop words and diacritics. Hyphenated words are scored
    # by their parts, so "Voulez-vous" still counts the French "vous".
    words = [part.lower() for word in _words(text) for part in re.split(r"[\-‐‑]", word) if part]
    scores: dict[str, float] = {}
    for code, stopwords in LATIN_STOPWORDS.items():
        if not allowed(code):
            continue
        score = sum(
            STOPWORD_WEIGHT if len(word) > 2 else SHORT_STOPWORD_WEIGHT
            for word in words
            if word in stopwords
        )
        if score:
            scores[code] = scores.get(code, 0.0) + score
    for code, value in _hint_scores(text, LATIN_DIACRITIC_HINTS).items():
        if allowed(code):
            scores[code] = scores.get(code, 0.0) + value
    evidence_score = max(scores.values(), default=0.0)
    prior_code = base_language_code(prior) if prior else ""
    if prior_code and allowed(prior_code) and get_language(prior_code).script == SCRIPT_LATIN:
        scores[prior_code] = scores.get(prior_code, 0.0) + PRIOR_WEIGHT
    if not scores:
        latin_fallback = fallback if get_language(fallback).script == SCRIPT_LATIN else "en"
        if not allowed(latin_fallback):
            latin_fallback = next(
                (code for code in LATIN_STOPWORDS if allowed(code)),
                latin_fallback,
            )
        return LanguageGuess(code=latin_fallback, confidence=0.2, script=script, scores={})
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], LATIN_TIE_ORDER.get(item[0], len(LATIN_TIE_ORDER)), item[0]),
    )
    best_code, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    total = sum(scores.values()) or 1.0
    margin = (best_score - second_score) / total
    # One accent or one shared stop word is a hint, not a verdict: the margin
    # only counts in proportion to how much evidence there is.
    evidence = min(1.0, evidence_score / FULL_EVIDENCE_SCORE)
    confidence = min(0.95, 0.3 + margin * 0.5 * evidence + evidence * 0.15)
    return LanguageGuess(code=best_code, confidence=round(confidence, 3), script=script, scores=scores)


PRIOR_MIN_SCORE = 3.0
PRIOR_MIN_LEAD = 1.5


def latin_language_prior(texts: Iterable[str]) -> str | None:
    """The Latin-script language of a whole screen, when its text says so clearly.

    Detection runs over every language, not only the captured ones, so a screen
    in a language that is not captured (the gloss language, usually) is
    recognized as such and its short strings are dropped with it instead of
    being filed under the closest captured language. "Clearly" means a few stop
    words and a lead of at least one distinctive word over the runner-up
    (Portuguese and Spanish share most of their short function words).
    """
    latin_text = " ".join(
        run.text
        for text in texts
        for run in split_script_runs(text or "")
        if run.script == SCRIPT_LATIN
    )
    if not _words(latin_text):
        return None
    guess = detect_language(latin_text)
    if guess.script != SCRIPT_LATIN or not guess.scores:
        return None
    ranked = sorted(guess.scores.values(), reverse=True)
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else 0.0
    if best < PRIOR_MIN_SCORE or best - second < PRIOR_MIN_LEAD:
        return None
    return guess.code


# ---------------------------------------------------------------------------
# Readings (romanizer hook, plan item E5)
# ---------------------------------------------------------------------------
#
# Japanese readings come from Sudachi (kana) and are romanized by lang_jp; the
# romanizers below give the other languages with readings a Latin reading:
# pinyin for Chinese (pypinyin, optional), Revised Romanization for Hangul and a
# transliteration table for Cyrillic (both standard library).

HANGUL_BASE = 0xAC00
HANGUL_END = 0xD7A3
HANGUL_INITIALS = (
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h",
)
HANGUL_MEDIALS = (
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we",
    "wi", "yu", "eu", "ui", "i",
)
HANGUL_FINALS = (
    "", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p",
    "t", "t", "ng", "t", "t", "k", "t", "p", "t",
)
# A final consonant carried into a following vowel-initial syllable (ㅇ) keeps its
# own sound: 한국어 -> han-gug-eo, not han-guk-eo.
HANGUL_FINALS_BEFORE_VOWEL: dict[int, str] = {
    1: "g", 2: "kk", 3: "ks", 4: "n", 5: "nj", 6: "nh", 7: "d", 8: "r", 9: "lg", 10: "lm",
    11: "lb", 12: "ls", 13: "lt", 14: "lp", 15: "lh", 16: "m", 17: "b", 18: "ps", 19: "s",
    20: "ss", 21: "ng", 22: "j", 23: "ch", 24: "k", 25: "t", 26: "p", 27: "h",
}

CYRILLIC_TABLE_RU: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Letters that only exist in neighbouring alphabets still get a sensible value.
    "є": "ye", "і": "i", "ї": "yi", "ґ": "g",
}
CYRILLIC_TABLE_UK: dict[str, str] = {
    **CYRILLIC_TABLE_RU,
    "г": "h", "ґ": "g", "е": "e", "є": "ye", "и": "y", "і": "i", "ї": "yi", "й": "y", "щ": "shch",
}
CYRILLIC_TABLES: dict[str, dict[str, str]] = {"ru": CYRILLIC_TABLE_RU, "uk": CYRILLIC_TABLE_UK}


def romanize_hangul(text: str) -> str:
    """Revised Romanization, syllable by syllable (no sound-change rules)."""
    pieces: list[str] = []
    for position, char in enumerate(text):
        codepoint = ord(char)
        if HANGUL_BASE <= codepoint <= HANGUL_END:
            index = codepoint - HANGUL_BASE
            initial = HANGUL_INITIALS[index // 588]
            medial = HANGUL_MEDIALS[(index % 588) // 28]
            final_index = index % 28
            final = HANGUL_FINALS[final_index]
            next_char = text[position + 1] if position + 1 < len(text) else ""
            if final_index and next_char and HANGUL_BASE <= ord(next_char) <= HANGUL_END:
                next_initial = (ord(next_char) - HANGUL_BASE) // 588
                if next_initial == 11:  # ㅇ: the final is pronounced as the next initial
                    final = HANGUL_FINALS_BEFORE_VOWEL.get(final_index, final)
            pieces.append(f"{initial}{medial}{final}")
        else:
            pieces.append(char)
    return "".join(pieces)


def romanize_cyrillic(text: str, language_code: str = "ru") -> str:
    table = CYRILLIC_TABLES.get(base_language_code(language_code), CYRILLIC_TABLE_RU)
    pieces: list[str] = []
    for char in text:
        lowered = char.lower()
        mapped = table.get(lowered)
        if mapped is None:
            pieces.append(char)
            continue
        pieces.append(mapped.capitalize() if char != lowered and mapped else mapped)
    return "".join(pieces)


@lru_cache(maxsize=1)
def _pypinyin_module():
    try:
        import pypinyin  # type: ignore
    except ImportError:
        return None
    return pypinyin


def romanize_pinyin(text: str) -> str | None:
    """Tone-marked pinyin via ``pypinyin``; ``None`` when it is not installed."""
    pypinyin = _pypinyin_module()
    if pypinyin is None:
        return None
    try:
        syllables = pypinyin.lazy_pinyin(text, style=pypinyin.Style.TONE)
    except Exception:  # pragma: no cover - defensive around third-party input handling
        return None
    return " ".join(piece for piece in syllables if piece) or None


def romanizer_available(code: str | None) -> bool:
    profile = get_language(code)
    if profile.romanizer == "pinyin":
        return _pypinyin_module() is not None
    return profile.romanizer in ("hangul", "cyrillic")


def romanize(text: str | None, code: str | None) -> str | None:
    """Reading for ``text`` in language ``code`` through the profile's romanizer.

    Returns ``None`` for languages without a romanizer, for Japanese (its
    readings are kana from Sudachi) and when an optional dependency is missing,
    so callers simply store no reading in those cases.
    """
    normalized = _normalize(text)
    if not normalized:
        return None
    profile = get_language(code)
    if profile.romanizer == "hangul":
        reading = romanize_hangul(normalized)
    elif profile.romanizer == "cyrillic":
        reading = romanize_cyrillic(normalized, profile.code)
    elif profile.romanizer == "pinyin":
        reading = romanize_pinyin(normalized)
    else:
        return None
    if not reading or reading == normalized:
        return None
    return reading


# ---------------------------------------------------------------------------
# Analyzers
# ---------------------------------------------------------------------------


@dataclass
class Token:
    surface: str
    lemma: str
    reading: str | None = None
    pos: str | None = None
    sub_tokens: list["Token"] = field(default_factory=list)
    language_code: str = DEFAULT_LANGUAGE_CODE


class Analyzer(Protocol):
    language_code: str
    name: str

    def analyze(self, text: str) -> list[Token]: ...


class AnalyzerUnavailable(RuntimeError):
    """Raised when a language needs a dependency that is not installed."""


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().split())


class SudachiAnalyzer:
    """Japanese analyzer: Sudachi split mode C with mode A sub-tokens."""

    name = "sudachi"
    language_code = "ja"

    def __init__(self) -> None:
        try:
            from sudachipy import Dictionary, SplitMode
        except ImportError as exc:
            raise AnalyzerUnavailable(
                "Missing Japanese NLP dependency. Install SudachiPy + sudachidict_core "
                "to analyze Japanese text."
            ) from exc
        self._split_mode = SplitMode
        self._tokenizer = Dictionary().create()

    def analyze(self, text: str) -> list[Token]:
        normalized = _normalize(text)
        if not normalized:
            return []
        tokens: list[Token] = []
        for token in self._tokenizer.tokenize(normalized, self._split_mode.C):
            surface = _normalize(token.surface())
            if not surface:
                continue
            lemma = _normalize(token.dictionary_form()) or surface
            reading = _normalize(token.reading_form()) or None
            pos = _normalize(" ".join(token.part_of_speech()[:2])) or None
            sub_tokens: list[Token] = []
            for sub_token in token.split(self._split_mode.A):
                sub_surface = _normalize(sub_token.surface())
                if not sub_surface or sub_surface == surface:
                    continue
                sub_tokens.append(
                    Token(
                        surface=sub_surface,
                        lemma=_normalize(sub_token.dictionary_form()) or sub_surface,
                        reading=_normalize(sub_token.reading_form()) or None,
                        pos=_normalize(" ".join(sub_token.part_of_speech()[:2])) or None,
                        language_code="ja",
                    )
                )
            tokens.append(
                Token(
                    surface=surface,
                    lemma=lemma,
                    reading=reading,
                    pos=pos,
                    sub_tokens=sub_tokens,
                    language_code="ja",
                )
            )
        return tokens


GENERIC_TOKEN_PATTERN = re.compile(
    r"[^\W\d_]+(?:[’'\-][^\W\d_]+)*|\d+(?:[.,]\d+)*",
    re.UNICODE,
)
APOSTROPHE_PREFIX_LANGUAGES = frozenset({"fr", "it"})
APOSTROPHE_CLITIC_PATTERN = re.compile(r"^([^\W\d_]{1,2})['’]([^\W\d_].*)$", re.UNICODE)
HYPHEN_SPLIT_LANGUAGES = frozenset({"fr", "pt", "es"})
# Screen chrome that is not vocabulary: links (OCR often turns "https://" into
# "https //", so a link runs to the end of the line), e-mail addresses, domains,
# @handles and #hashtags.
NON_WORD_PATTERN = re.compile(
    r"(?:\bhttps?\b|\bwww\.)[^\n]*"
    r"|\S+@\S+\.\S+"
    r"|[@#][\w.\-]+"
    r"|\b[\w\-]+(?:\.[\w\-]+)*\.(?:com|net|org|io|gg|tv|dev|app|info|br|jp|kr|cn|uk|ru)\b\S*",
    re.UNICODE | re.IGNORECASE,
)
# All-caps words this short stay as they are (HP, START, RPG); longer ones and
# every word of an all-caps line are folded so "VOTAÇÃO" and "votação" merge.
ACRONYM_MAX_LENGTH = 5
WORD_PART_PATTERN = re.compile(r"['’\-]")
SINGLE_LETTER_WORDS: dict[str, frozenset[str]] = {
    "ru": frozenset("а в и к о с у я".split()),
    "uk": frozenset("а в з і й о у я".split()),
    "el": frozenset("ο η ή α σ".split()),
}
VOWELS = frozenset("aeiouyæøœı")
JUNK_CHECK_SCRIPTS = frozenset({SCRIPT_LATIN, SCRIPT_CYRILLIC, SCRIPT_GREEK})


def _irregular_case(word: str) -> bool:
    """``YouTube``, ``kStrear``, ``GHANGEMYMINn``: OCR noise or brand names, not words."""
    for part in WORD_PART_PATTERN.split(word):
        cased = [char for char in part if char.isupper() or char.islower()]
        rest = cased[1:]
        if any(char.isupper() for char in rest) and any(char.islower() for char in rest):
            return True
    return False


def _glued_to_digit(text: str, start: int, end: int) -> bool:
    """``21k``, ``1ok``, ``3.3kStrear``: letters stuck to a number are units or noise."""
    return (start > 0 and text[start - 1].isdigit()) or (end < len(text) and text[end].isdigit())


def _is_shouting(words: list[str]) -> bool:
    """A line written in capitals (``DEBATE PÚBLICO NO MACKENZIE``), not one acronym."""
    cased = [word for word in words if any(char.isalpha() for char in word)]
    upper = [word for word in cased if word.isupper()]
    return (
        len(upper) >= 2
        and len(upper) >= 0.8 * len(cased)
        and any(len(word) > 3 for word in upper)
    )


def _has_vowel(word: str) -> bool:
    return any(
        unicodedata.normalize("NFD", char)[0].lower() in VOWELS for char in word
    )


def junk_word_reason(word: str | None, code: str | None) -> str | None:
    """Why a lemma of an alphabetic language is not a word worth cataloging.

    Single letters are noise unless the language has them as words (``a``,
    ``y``, ``и``); Latin-script lemmas without a vowel (``dc``, ``Wr``, ``tpg``)
    are OCR fragments unless written in capitals (acronyms).
    """
    word = (word or "").strip()
    profile = get_language(code)
    if not word or profile.script not in JUNK_CHECK_SCRIPTS:
        return None
    letters = [char for char in word if char.isalpha()]
    base = base_language_code(profile.code)
    if len(letters) == 1:
        allowed = LATIN_STOPWORDS.get(base, frozenset()) | SINGLE_LETTER_WORDS.get(base, frozenset())
        return None if word.lower() in allowed else "single_letter"
    if profile.script == SCRIPT_LATIN and not word.isupper() and not _has_vowel(word):
        return "no_vowel"
    return None


@lru_cache(maxsize=1)
def _simplemma_module():
    try:
        import simplemma  # type: ignore
    except ImportError:
        return None
    return simplemma


class GenericAnalyzer:
    """Tokenizer for word-delimited languages.

    Lemmas come from ``simplemma`` when it is installed, otherwise the lower-cased
    surface is used (acronyms keep their case). Clitics attached with apostrophes
    in French and Italian (``l'homme``) are split so the content word becomes a
    term of its own. Links, handles, letters stuck to numbers and oddly cased
    tokens (OCR noise, brand names) are skipped.
    """

    name = "generic"

    def __init__(self, language_code: str) -> None:
        self.language_code = normalize_language_code(language_code) or "en"
        self._base = base_language_code(self.language_code)
        self._simplemma = _simplemma_module()
        self._has_readings = get_language(self.language_code).has_readings

    def reading(self, surface: str) -> str | None:
        return romanize(surface, self.language_code) if self._has_readings else None

    def lemmatize(self, surface: str, *, fold_caps: bool = False) -> str:
        # Acronyms and UI labels (HP, START) keep their case so they stay recognizable.
        if len(surface) > 1 and surface.isupper():
            if not fold_caps and len(surface) <= ACRONYM_MAX_LENGTH:
                return surface
            surface = surface.lower()
        if self._simplemma is not None:
            try:
                # A real inflection ("Häuser" -> "Haus") wins as is; a token simplemma
                # leaves untouched is retried lower-cased so sentence-initial words
                # ("Press") merge with their lower-case form ("press").
                lemma = self._simplemma.lemmatize(surface, lang=self._base)
                if lemma and lemma != surface:
                    return lemma
                lowered = self._simplemma.lemmatize(surface.lower(), lang=self._base)
                if lowered:
                    return lowered
            except (ValueError, KeyError, TypeError):
                pass
        return surface.lower()

    def analyze(self, text: str) -> list[Token]:
        normalized = _normalize(NON_WORD_PATTERN.sub(" ", text or ""))
        if not normalized:
            return []
        matches = [
            match
            for match in GENERIC_TOKEN_PATTERN.finditer(normalized)
            if match.group(0) and not match.group(0)[0].isdigit()
        ]
        fold_caps = _is_shouting([match.group(0) for match in matches])
        tokens: list[Token] = []
        for match in matches:
            surface = match.group(0)
            if _glued_to_digit(normalized, match.start(), match.end()) or _irregular_case(surface):
                continue
            parts = [surface]
            if self._base in APOSTROPHE_PREFIX_LANGUAGES:
                clitic = APOSTROPHE_CLITIC_PATTERN.match(surface)
                if clitic is not None:
                    parts = [clitic.group(2)]
            if self._base in HYPHEN_SPLIT_LANGUAGES and "-" in surface:
                parts = [part for part in surface.split("-") if part]
            for part in parts:
                tokens.append(
                    Token(
                        surface=part,
                        lemma=self.lemmatize(part, fold_caps=fold_caps),
                        reading=self.reading(part),
                        pos=None,
                        language_code=self.language_code,
                    )
                )
        return tokens


class HanAnalyzer:
    """Chinese analyzer: ``jieba`` when installed, otherwise han character runs."""

    name = "han"

    def __init__(self, language_code: str = "zh") -> None:
        self.language_code = normalize_language_code(language_code) or "zh"
        try:
            import jieba  # type: ignore

            jieba.setLogLevel(60)
            self._jieba = jieba
        except ImportError:
            self._jieba = None
        self._fallback = GenericAnalyzer(self.language_code)

    def analyze(self, text: str) -> list[Token]:
        normalized = _normalize(text)
        if not normalized:
            return []
        tokens: list[Token] = []
        if self._jieba is not None:
            pieces = [piece for piece in self._jieba.cut(normalized) if _normalize(piece)]
        else:
            pieces = re.findall(r"[一-鿿㐀-䶿]+|[^\W\d_]+", normalized)
        for piece in pieces:
            piece = _normalize(piece)
            if not piece or not any(char_script(char) for char in piece):
                continue
            sub_tokens: list[Token] = []
            if len(piece) > 1 and all(char_script(char) == SCRIPT_HAN for char in piece):
                sub_tokens = [
                    Token(
                        surface=char,
                        lemma=char,
                        reading=romanize(char, self.language_code),
                        language_code=self.language_code,
                    )
                    for char in piece
                ]
            tokens.append(
                Token(
                    surface=piece,
                    lemma=piece,
                    reading=romanize(piece, self.language_code),
                    sub_tokens=sub_tokens,
                    language_code=self.language_code,
                )
            )
        return tokens


class ThaiAnalyzer:
    """Thai has no spaces between words; use ``pythainlp`` when available."""

    name = "thai"
    language_code = "th"

    def __init__(self) -> None:
        try:
            from pythainlp.tokenize import word_tokenize  # type: ignore

            self._tokenize = word_tokenize
        except ImportError:
            self._tokenize = None

    def analyze(self, text: str) -> list[Token]:
        normalized = _normalize(text)
        if not normalized:
            return []
        if self._tokenize is not None:
            pieces = [piece for piece in self._tokenize(normalized) if _normalize(piece)]
        else:
            pieces = normalized.split()
        return [
            Token(surface=piece, lemma=piece, language_code="th")
            for piece in pieces
            if any(char_script(char) for char in piece)
        ]


@lru_cache(maxsize=32)
def get_analyzer(code: str | None) -> Analyzer:
    profile = get_language(code)
    if profile.analyzer == "sudachi":
        return SudachiAnalyzer()
    if profile.analyzer == "han":
        return HanAnalyzer(profile.code)
    if profile.analyzer == "thai":
        return ThaiAnalyzer()
    return GenericAnalyzer(profile.code)


def analyzer_available(code: str | None) -> bool:
    try:
        get_analyzer(code)
    except AnalyzerUnavailable:
        return False
    return True


@dataclass
class AnalyzedRun:
    text: str
    language_code: str
    confidence: float
    tokens: list[Token]


def enabled_capture_languages(raw_value: str | None, *, default: str = DEFAULT_LANGUAGE_CODE) -> tuple[str, ...]:
    """Parse ``LANG_CAPTURE_LANGUAGES``-style values (``ja,en``) into codes."""
    if raw_value is None:
        raw_value = default
    codes: list[str] = []
    for part in re.split(r"[,\s;]+", raw_value):
        normalized = normalize_language_code(part)
        if normalized and normalized not in codes and normalized != AUTO_LANGUAGE_CODE:
            codes.append(normalized)
    return tuple(codes) or (default,)


def _script_matches_language(script: str | None, code: str) -> bool:
    profile = get_language(code)
    if script is None:
        return True
    if script == SCRIPT_HAN:
        return profile.script in (SCRIPT_HAN, SCRIPT_JAPANESE)
    return profile.script == script


def _detect_run_language(
    run: ScriptRun,
    candidates: list[str] | None,
    prior: str | None,
) -> LanguageGuess:
    if candidates is None or run.script != SCRIPT_LATIN:
        return detect_language(run.text, candidates=candidates, prior=prior)
    # Latin runs are detected against every language first: Portuguese text on a
    # screen where Portuguese is not captured must be dropped, not filed as the
    # closest captured language. Only text with no evidence at all (no stop
    # word, no accent, no screen prior) falls back to the captured languages.
    guess = detect_language(run.text, prior=prior)
    allowed = {base_language_code(code) for code in candidates}
    if base_language_code(guess.code) in allowed or guess.scores:
        return guess
    return detect_language(run.text, candidates=candidates, prior=prior)


def analyze_text(
    text: str | None,
    *,
    language: str | None = AUTO_LANGUAGE_CODE,
    candidates: Iterable[str] | None = None,
    split_mixed: bool = True,
    prior: str | None = None,
) -> list[AnalyzedRun]:
    """Detect, split and tokenize ``text``.

    With ``language`` set to a code the whole text is analyzed as that language.
    With ``auto`` the text is split into script runs; runs are detected
    separately and only runs whose language is in ``candidates`` (when given)
    are analyzed. Neighbouring runs that resolve to the same language are merged
    back so that word order inside a sentence is preserved. ``prior`` is the
    screen's Latin-script language (:func:`latin_language_prior`).
    """
    text = text or ""
    if not _normalize(text):
        return []
    forced = normalize_language_code(language)
    if forced and forced not in (AUTO_LANGUAGE_CODE, ALL_LANGUAGES_CODE):
        analyzer = get_analyzer(forced)
        return [AnalyzedRun(text=text, language_code=forced, confidence=1.0, tokens=analyzer.analyze(text))]

    candidate_list = list(candidates) if candidates is not None else None
    runs = split_script_runs(text) if split_mixed else [ScriptRun(text=text, script=dominant_script(text), start=0, end=len(text))]
    labelled: list[tuple[str, str, float]] = []
    for run in runs:
        guess = _detect_run_language(run, candidate_list, prior)
        if candidate_list is not None and base_language_code(guess.code) not in {
            base_language_code(code) for code in candidate_list
        }:
            continue
        if not _script_matches_language(run.script, guess.code):
            continue
        if labelled and labelled[-1][1] == guess.code:
            previous_text, code, confidence = labelled[-1]
            labelled[-1] = (previous_text + run.text, code, max(confidence, guess.confidence))
        else:
            labelled.append((run.text, guess.code, guess.confidence))
    results: list[AnalyzedRun] = []
    for run_text, code, confidence in labelled:
        try:
            analyzer = get_analyzer(code)
        except AnalyzerUnavailable:
            results.append(AnalyzedRun(text=run_text, language_code=code, confidence=confidence, tokens=[]))
            continue
        results.append(
            AnalyzedRun(
                text=run_text,
                language_code=code,
                confidence=confidence,
                tokens=analyzer.analyze(run_text),
            )
        )
    return results


def dominant_language(runs: Iterable[AnalyzedRun], *, default: str = DEFAULT_LANGUAGE_CODE) -> str:
    """Pick the language that covers most letters across analyzed runs."""
    weights: dict[str, int] = {}
    for run in runs:
        letters = sum(script_histogram(run.text).values())
        weights[run.language_code] = weights.get(run.language_code, 0) + letters
    if not weights:
        return default
    return max(weights.items(), key=lambda item: (item[1], item[0] == default, item[0]))[0]


def tokens_with_characters(tokens: Iterable[Token]) -> bool:
    return any(char_script(char) == SCRIPT_HAN for token in tokens for char in token.surface)
