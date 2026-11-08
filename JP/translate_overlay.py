"""Hotkey-triggered reading overlay for the primary monitor.

A resident process: the OCR engines load once at start, then every press of
the hotkey grabs the primary monitor, reads it with the same engines the
capture worker uses and draws a reading aid over the text, in one of three
modes:

- ``translation``: each line covered by its translation (English by
  default) with the romaji or romanization under it;
- ``furigana``: the hiragana reading over every word with kanji (romanization
  over each word for Korean, Chinese, Russian, ...), leaving the text visible;
- ``words``: a short gloss under every content word, word by word.

The mode hotkey cycles the mode (re-rendering the same read, no new OCR). The
mode and the languages to read live in ``data/translate_overlay.toml``, shared
with the tray daemon, so the tray menu and the hotkey stay in sync.

The window is click-through, always on top and excluded from screen capture,
so the passive capture never OCRs it. Press the hotkey again (or Esc) to clear
it; it also clears when the foreground window changes. Nothing is written to
the catalog.

    .venv-capture312\\Scripts\\python.exe JP\\translate_overlay.py
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tomllib
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import languages
from ocr_backends import OcrBlock, OcrTextLine, bbox_bounds, sort_ocr_blocks_reading_order

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / "data" / "translate_overlay.toml"
DEFAULT_HOTKEY = os.environ.get("LANG_OVERLAY_HOTKEY", "Ctrl+Alt+Q")
DEFAULT_MODE_HOTKEY = os.environ.get("LANG_OVERLAY_MODE_HOTKEY", "Ctrl+Alt+W")
DEFAULT_TARGET_LANGUAGE = os.environ.get("LANG_OVERLAY_TARGET", "en")

MODES = ("translation", "furigana", "words")
MODE_LABELS = {
    "translation": "Tradução",
    "furigana": "Furigana",
    "words": "Palavra por palavra",
}
LANGUAGE_CHOICES = ("ja", "capture")

MIN_FONT_PX = 12
MAX_FONT_PX = 72
MIN_LABEL_WIDTH_PX = 220
MAX_GLOSS_CHARS = 28

TRANSPARENT_KEY = "#ff00fe"
LABEL_BACKGROUND = "#16181d"
LABEL_BORDER = "#f2b84b"
TRANSLATION_COLOR = "#ffffff"
READING_COLOR = "#a9b4c2"
FURIGANA_COLOR = "#ffe7a3"
GLOSS_COLOR = "#ffffff"
STATUS_BACKGROUND = "#f2b84b"
STATUS_COLOR = "#16181d"
# Outline around the word a furigana/gloss label belongs to, and the leader
# line joining a vertical-text gloss to it.
MARKER_COLOR = "#f2b84b"


# ---------------------------------------------------------------------------
# Shared state with the tray daemon
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OverlayState:
    mode: str = "translation"
    # "ja": Japanese only (meikiocr, fast); "capture": the capture language set.
    languages: str = "ja"
    # Whether the tray daemon keeps the overlay process running.
    enabled: bool = True
    # Label size multiplier, changed with Ctrl+=/Ctrl+-/Ctrl+wheel while the overlay is up.
    scale: float = 1.0


MIN_SCALE = 0.5
MAX_SCALE = 3.0
SCALE_STEP = 1.15


def clamp_scale(value: float) -> float:
    return round(max(MIN_SCALE, min(MAX_SCALE, value)), 2)


def zoom_scale(scale: float, steps: int) -> float:
    """``steps`` notches bigger (positive) or smaller (negative), clamped; 0 resets."""
    if steps == 0:
        return 1.0
    return clamp_scale(scale * SCALE_STEP**steps)


def load_overlay_state(path: Path) -> OverlayState:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return OverlayState()
    state = OverlayState()
    if raw.get("mode") in MODES:
        state.mode = raw["mode"]
    if raw.get("languages") in LANGUAGE_CHOICES:
        state.languages = raw["languages"]
    if isinstance(raw.get("enabled"), bool):
        state.enabled = raw["enabled"]
    scale = raw.get("scale")
    if isinstance(scale, (int, float)) and not isinstance(scale, bool):
        state.scale = clamp_scale(float(scale))
    return state


def save_overlay_state(path: Path, state: OverlayState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        f'mode = "{state.mode}"\nlanguages = "{state.languages}"\n'
        f"enabled = {'true' if state.enabled else 'false'}\n"
        f"scale = {clamp_scale(state.scale):.2f}\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def next_mode(mode: str) -> str:
    index = MODES.index(mode) if mode in MODES else -1
    return MODES[(index + 1) % len(MODES)]


# ---------------------------------------------------------------------------
# Line analysis (tested without a screen)
# ---------------------------------------------------------------------------

Rect = tuple[int, int, int, int]


@dataclass(slots=True)
class LineAnalysis:
    """One OCR block (a row of text or a vertical speech bubble), analyzed.

    ``geometry`` holds the block's physical lines (columns for vertical text)
    with their character ranges; without it the whole block is treated as one
    horizontal line filling ``rect``.
    """

    text: str
    language_code: str
    rect: Rect
    runs: list[languages.AnalyzedRun] = field(default_factory=list)
    geometry: tuple[OcrTextLine, ...] = ()

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def vertical(self) -> bool:
        return bool(self.geometry) and all(line.vertical for line in self.geometry)

    def text_lines(self) -> tuple[OcrTextLine, ...]:
        if self.geometry:
            return self.geometry
        left, top, right, bottom = self.rect
        return (OcrTextLine(start=0, end=len(self.text), left=left, top=top, right=right, bottom=bottom),)


@dataclass(slots=True)
class TokenSpan:
    surface: str
    lemma: str
    reading: str | None
    pos: str | None
    language_code: str
    start: int
    end: int


@dataclass(slots=True)
class OverlayItem:
    """Translation mode: one label covering a whole line."""

    left: int
    top: int
    right: int
    bottom: int
    text: str
    language_code: str
    reading: str | None
    translation: str | None

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def width(self) -> int:
        return self.right - self.left


@dataclass(slots=True)
class Annotation:
    """Furigana and word modes: a small label tied to one word on screen.

    ``left``..``bottom`` is the word's own box inside its text line, and
    ``line`` the box of that line. Horizontal text gets furigana above the line
    and glosses below it; vertical text gets furigana right of the column and
    glosses left of it, as manga sets its ruby.
    """

    left: int
    top: int
    right: int
    bottom: int
    surface: str
    text: str
    sub: str | None = None
    vertical: bool = False
    line: Rect = (0, 0, 0, 0)
    # The whole block the word belongs to (a speech bubble for vertical text).
    block: Rect = (0, 0, 0, 0)

    @property
    def thickness(self) -> int:
        """Cross size of the line: its height, or its width for a vertical column."""
        left, top, right, bottom = self.line
        return (right - left) if self.vertical else (bottom - top)


def block_rect(block: OcrBlock) -> Rect | None:
    bounds = bbox_bounds(block.bbox)
    if bounds is None:
        return None
    left, top, right, bottom = (int(round(value)) for value in bounds)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def line_language(runs: list[languages.AnalyzedRun], text: str, candidates: tuple[str, ...], prior: str | None) -> str:
    """The language covering most of the line; mixed lines are translated as a whole."""
    if runs:
        weights: dict[str, int] = {}
        for run in runs:
            weights[run.language_code] = weights.get(run.language_code, 0) + len(run.text)
        return max(weights.items(), key=lambda item: item[1])[0]
    default = candidates[0] if candidates else languages.DEFAULT_LANGUAGE_CODE
    return languages.detect_language(text, default=default, prior=prior).code


def analyze_lines(blocks: Iterable[OcrBlock], *, candidates: tuple[str, ...]) -> list[LineAnalysis]:
    """Every OCR line with a box, in reading order, detected and tokenized."""
    placed: list[tuple[OcrBlock, Rect]] = []
    for block in sort_ocr_blocks_reading_order(list(blocks)):
        block = block.with_text((block.text or "").strip())
        rect = block_rect(block)
        if block.text and rect is not None:
            placed.append((block, rect))
    prior = languages.latin_language_prior(block.text for block, _ in placed)
    lines: list[LineAnalysis] = []
    for block, rect in placed:
        runs = languages.analyze_text(block.text, candidates=candidates or None, prior=prior)
        lines.append(
            LineAnalysis(
                text=block.text,
                language_code=line_language(runs, block.text, candidates, prior),
                rect=rect,
                runs=runs,
                geometry=block.lines,
            )
        )
    return lines


def token_spans(line: LineAnalysis) -> list[TokenSpan]:
    """Tokens with their character offsets in the line text (unlocatable ones are skipped)."""
    spans: list[TokenSpan] = []
    run_cursor = 0
    for run in line.runs:
        run_start = line.text.find(run.text, run_cursor)
        if run_start < 0:
            continue
        run_cursor = run_start + len(run.text)
        token_cursor = run_start
        for token in run.tokens:
            if not token.surface:
                continue
            start = line.text.find(token.surface, token_cursor, run_cursor)
            if start < 0:
                continue
            end = start + len(token.surface)
            token_cursor = end
            spans.append(
                TokenSpan(
                    surface=token.surface,
                    lemma=token.lemma or token.surface,
                    reading=token.reading,
                    pos=token.pos,
                    language_code=token.language_code or run.language_code,
                    start=start,
                    end=end,
                )
            )
    return spans


def char_weight(char: str) -> float:
    """Relative advance of a glyph: full-width CJK is ~twice a Latin letter."""
    if char.isspace():
        return 0.35
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 1.0
    return 0.55


@dataclass(slots=True)
class SpanPlacement:
    """Where a character range sits on screen: its box and the line it is in."""

    rect: Rect
    line: Rect
    vertical: bool


def span_placements(line: LineAnalysis, start: int, end: int) -> list[SpanPlacement]:
    """Approximate boxes of ``line.text[start:end]``, one per physical line it touches.

    Positions are interpolated along each line's reading axis: by glyph width
    for horizontal text (full-width CJK is about twice a Latin letter), one
    cell per character for a vertical column.
    """
    placements: list[SpanPlacement] = []
    for text_line in line.text_lines():
        first, last = max(start, text_line.start), min(end, text_line.end)
        if first >= last:
            continue
        chars = line.text[text_line.start:text_line.end]
        weights = [1.0 if text_line.vertical else char_weight(char) for char in chars]
        total = sum(weights) or 1.0
        before = sum(weights[: first - text_line.start]) / total
        inside = sum(weights[first - text_line.start : last - text_line.start]) / total
        box = (
            int(round(text_line.left)),
            int(round(text_line.top)),
            int(round(text_line.right)),
            int(round(text_line.bottom)),
        )
        left, top, right, bottom = box
        if text_line.vertical:
            length = bottom - top
            rect = (left, int(round(top + length * before)), right, int(round(top + length * (before + inside))))
        else:
            length = right - left
            rect = (int(round(left + length * before)), top, int(round(left + length * (before + inside))), bottom)
        placements.append(SpanPlacement(rect=rect, line=box, vertical=text_line.vertical))
    return placements


def has_kanji(text: str) -> bool:
    return any(
        "一" <= char <= "鿿" or "㐀" <= char <= "䶿" or char == "々"
        for char in text
    )


NON_CONTENT_POS = ("助詞", "助動詞", "補助記号", "記号", "空白", "接尾辞", "接頭辞")


def is_content_token(span: TokenSpan) -> bool:
    if not any(char.isalpha() for char in span.surface):
        return False
    return not (span.pos or "").startswith(NON_CONTENT_POS)


def is_japanese(code: str) -> bool:
    return languages.base_language_code(code) == "ja"


PARTICLE_ROMAJI = {"は": "wa", "へ": "e", "を": "o"}
SOKUON = ("っ", "ッ")


def japanese_romaji(runs: list[languages.AnalyzedRun], romanize_kana: Callable[[str], str]) -> str | None:
    """Word-spaced romaji from Sudachi readings.

    Particles read as spoken (は -> wa), and a token ending in っ is glued to
    the next one, since the geminate only romanizes with the consonant after it
    (あがっ + た -> agatta).
    """
    words: list[str] = []
    carry = ""
    for run in runs:
        if not is_japanese(run.language_code):
            if carry:
                words.append(romanize_kana(carry) or carry)
                carry = ""
            words.append(run.text)
            continue
        for token in run.tokens:
            if (token.pos or "").startswith("助詞") and token.surface in PARTICLE_ROMAJI and not carry:
                words.append(PARTICLE_ROMAJI[token.surface])
                continue
            reading = carry + (token.reading or token.surface)
            carry = ""
            if reading.endswith(SOKUON):
                carry = reading
                continue
            words.append(romanize_kana(reading) or token.surface)
    if carry:
        words.append(romanize_kana(carry) or carry)
    joined = " ".join(word for word in words if word).strip()
    return joined or None


def line_reading(
    text: str,
    code: str,
    runs: list[languages.AnalyzedRun],
    *,
    romanize_kana: Callable[[str], str] | None = None,
) -> str | None:
    """Romaji for Japanese, the profile's romanizer otherwise, nothing for Latin script."""
    if is_japanese(code):
        if romanize_kana is None:
            return None
        reading = japanese_romaji(runs, romanize_kana)
        return reading if reading and reading != text else None
    return languages.romanize(text, code)


def word_reading(span: TokenSpan, to_hiragana: Callable[[str], str] | None) -> str | None:
    """Hiragana for a Japanese word with kanji, the romanizer's output otherwise."""
    if is_japanese(span.language_code):
        if to_hiragana is None or not span.reading or not has_kanji(span.surface):
            return None
        reading = to_hiragana(span.reading)
        return reading if reading and reading != span.surface else None
    return languages.romanize(span.surface, span.language_code)


TranslateBatch = Callable[[list[str], str], list[str | None]]
GlossLookup = Callable[[list[str], str], list[str | None]]


def translate_in_batches(
    entries: list[tuple[str, str]],
    translate: TranslateBatch,
) -> list[str | None]:
    """Translate ``(text, language)`` pairs with one ``translate`` call per language."""
    by_language: dict[str, list[int]] = {}
    for index, (_, code) in enumerate(entries):
        by_language.setdefault(code, []).append(index)
    results: list[str | None] = [None] * len(entries)
    for code, indexes in by_language.items():
        texts = [entries[index][0] for index in indexes]
        try:
            answers = list(translate(texts, code))
        except Exception:
            answers = []
        if len(answers) != len(texts):
            answers = [None] * len(texts)
        for index, text, answer in zip(indexes, texts, answers):
            cleaned = (answer or "").strip()
            results[index] = cleaned if cleaned and cleaned != text else None
    return results


def translation_items(
    lines: list[LineAnalysis],
    *,
    translate: TranslateBatch,
    target_language: str = DEFAULT_TARGET_LANGUAGE,
    romanize_kana: Callable[[str], str] | None = None,
) -> list[OverlayItem]:
    """Translation mode. Lines already in the target language are left alone."""
    target_base = languages.base_language_code(target_language)
    pending = [line for line in lines if languages.base_language_code(line.language_code) != target_base]
    translations = translate_in_batches([(line.text, line.language_code) for line in pending], translate)
    items: list[OverlayItem] = []
    for line, translation in zip(pending, translations):
        reading = line_reading(line.text, line.language_code, line.runs, romanize_kana=romanize_kana)
        if not translation and not reading:
            continue
        left, top, right, bottom = line.rect
        items.append(
            OverlayItem(
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                text=line.text,
                language_code=line.language_code,
                reading=reading,
                translation=translation,
            )
        )
    return items


def build_overlay_items(
    blocks: Iterable[OcrBlock],
    *,
    candidates: tuple[str, ...],
    translate: TranslateBatch,
    target_language: str = DEFAULT_TARGET_LANGUAGE,
    romanize_kana: Callable[[str], str] | None = None,
) -> list[OverlayItem]:
    """Translation mode straight from OCR blocks."""
    return translation_items(
        analyze_lines(blocks, candidates=candidates),
        translate=translate,
        target_language=target_language,
        romanize_kana=romanize_kana,
    )


def trim_okurigana(surface: str, reading: str, to_hiragana: Callable[[str], str]) -> tuple[int, int, str]:
    """Narrow furigana to the kanji: 新しい/あたらしい -> (0, 1, "あたら").

    Kana the surface shares with its reading at either end are written out
    already, so the reading goes over the rest only. Returns character offsets
    into ``surface`` and the reading left for them.
    """
    surface_kana = to_hiragana(surface)
    start, end = 0, len(surface)
    head, tail = 0, len(reading)
    while end > start + 1 and tail > head + 1 and surface_kana[end - 1] == reading[tail - 1] and not has_kanji(surface[end - 1]):
        end -= 1
        tail -= 1
    while start < end - 1 and head < tail - 1 and surface_kana[start] == reading[head] and not has_kanji(surface[start]):
        start += 1
        head += 1
    return start, end, reading[head:tail]


def furigana_annotations(
    lines: list[LineAnalysis],
    *,
    to_hiragana: Callable[[str], str] | None = None,
) -> list[Annotation]:
    """Furigana mode: a reading over each word that needs one (over its kanji only)."""
    annotations: list[Annotation] = []
    for line in lines:
        for span in token_spans(line):
            reading = word_reading(span, to_hiragana)
            if not reading:
                continue
            start, end = span.start, span.end
            if is_japanese(span.language_code) and to_hiragana is not None:
                offset, stop, reading = trim_okurigana(span.surface, reading, to_hiragana)
                start, end = span.start + offset, span.start + stop
            annotation = annotation_at(line, start, end, surface=span.surface, text=reading)
            if annotation is not None:
                annotations.append(annotation)
    return annotations


def annotation_at(
    line: LineAnalysis,
    start: int,
    end: int,
    *,
    surface: str,
    text: str,
    sub: str | None = None,
) -> Annotation | None:
    """A label for ``line.text[start:end]``, placed on the first line the word touches."""
    placements = span_placements(line, start, end)
    if not placements:
        return None
    placement = placements[0]
    left, top, right, bottom = placement.rect
    return Annotation(
        left=left, top=top, right=right, bottom=bottom,
        surface=surface, text=text, sub=sub,
        vertical=placement.vertical, line=placement.line, block=line.rect,
    )


def shorten_gloss(value: str | None, limit: int = MAX_GLOSS_CHARS) -> str | None:
    """First sense of a catalog meaning: ``"poder; força; energia"`` -> ``"poder"``."""
    text = " ".join((value or "").split())
    if not text:
        return None
    for separator in (";", "\n", "/", "|"):
        text = text.split(separator)[0].strip()
    # Dictionary usage notes ("sword (esp. a large, double-edged one)") do not
    # fit a word label; keep them only when they are the whole gloss.
    without_notes = re.sub(r"\s*\([^)]*\)?", "", text).strip()
    text = without_notes or text
    if len(text) > limit:
        text = text[: limit - 1].rstrip(" ,") + "…"
    return text or None


def word_annotations(
    lines: list[LineAnalysis],
    *,
    gloss: GlossLookup,
    target_language: str = DEFAULT_TARGET_LANGUAGE,
    to_hiragana: Callable[[str], str] | None = None,
) -> list[Annotation]:
    """Word mode: a gloss under each content word, its reading under the gloss.

    Words are glossed by their dictionary form (あがっ -> 上がる), one lookup
    per language for the whole screen.
    """
    target_base = languages.base_language_code(target_language)
    placed: list[tuple[LineAnalysis, TokenSpan]] = []
    for line in lines:
        if languages.base_language_code(line.language_code) == target_base:
            continue
        for span in token_spans(line):
            if languages.base_language_code(span.language_code) == target_base:
                continue
            if is_content_token(span):
                placed.append((line, span))

    unique: dict[tuple[str, str], None] = {}
    for _, span in placed:
        unique[(span.lemma, span.language_code)] = None
    keys = list(unique)
    by_language: dict[str, list[str]] = {}
    for lemma, code in keys:
        by_language.setdefault(code, []).append(lemma)
    glosses: dict[tuple[str, str], str | None] = {}
    for code, lemmas in by_language.items():
        try:
            answers = list(gloss(lemmas, code))
        except Exception:
            answers = []
        if len(answers) != len(lemmas):
            answers = [None] * len(lemmas)
        for lemma, answer in zip(lemmas, answers):
            glosses[(lemma, code)] = shorten_gloss(answer)

    annotations: list[Annotation] = []
    for line, span in placed:
        text = glosses.get((span.lemma, span.language_code))
        reading = word_reading(span, to_hiragana)
        if not text and not reading:
            continue
        annotation = annotation_at(
            line, span.start, span.end,
            surface=span.surface, text=text or reading or "", sub=reading if text else None,
        )
        if annotation is not None:
            annotations.append(annotation)
    return annotations


def font_px_for(line_height: int, scale: float = 1.0) -> int:
    """Translation font size that roughly matches the line it covers, times the user's scale."""
    return max(MIN_FONT_PX, min(int(MAX_FONT_PX * scale), int(line_height * 0.62 * scale)))


def small_font_px_for(line_height: int, scale: float = 1.0) -> int:
    """Furigana and gloss labels: under half the line height, still legible, times the scale."""
    return max(MIN_FONT_PX, min(int(40 * scale), int(line_height * 0.42 * scale)))


def stack_rows(spans: list[tuple[int, int]], gap: int = 6) -> list[int]:
    """Row index for each horizontal span so labels in the same row never overlap."""
    row_ends: list[int] = []
    rows: list[int] = []
    for left, right in spans:
        for row, end in enumerate(row_ends):
            if left >= end + gap:
                row_ends[row] = right
                rows.append(row)
                break
        else:
            row_ends.append(right)
            rows.append(len(row_ends) - 1)
    return rows


@dataclass(slots=True)
class LabelPlacement:
    """Top-left corner of a label's padded box, and an optional leader line to its word."""

    x: int
    y: int
    leader: tuple[int, int, int, int] | None = None


def ruby_runs_vertically(annotation: Annotation, *, ruby: bool) -> bool:
    """Furigana on a vertical column is written vertically, one kana per line."""
    return ruby and annotation.vertical


def place_labels(
    annotations: list[Annotation],
    sizes: list[tuple[int, int]],
    *,
    ruby: bool,
    gutter: int = 8,
) -> list[LabelPlacement]:
    """Where each label goes, given its measured (width, height); same order as given.

    - Horizontal text: furigana centred above the word, glosses centred below
      the line; labels that would collide drop into extra rows.
    - Vertical text, furigana: a vertical strip in the gap right of the column,
      centred on the word, as manga sets its ruby.
    - Vertical text, glosses: Latin-script glosses cannot run vertically and the gap
      between columns is too narrow, so glosses line up in a gutter left of
      the whole bubble, level with their word, with a leader line to it.
    """
    placements: list[LabelPlacement | None] = [None] * len(annotations)
    groups: dict[tuple, list[int]] = {}
    for index, annotation in enumerate(annotations):
        if annotation.vertical and not ruby:
            key: tuple = ("bubble", annotation.block)
        else:
            key = ("line", annotation.line, annotation.vertical)
        groups.setdefault(key, []).append(index)

    for indexes in groups.values():
        vertical = annotations[indexes[0]].vertical
        indexes.sort(key=lambda i: annotations[i].top if vertical else annotations[i].left)
        if vertical and not ruby:
            # One list in reading order (columns right to left, each top to
            # bottom), each gloss as level with its word as the ones above allow.
            indexes.sort(key=lambda i: (-annotations[i].line[2], annotations[i].top))
            right_edge = annotations[indexes[0]].block[0] - gutter
            floor = None
            for i in indexes:
                word, (label_width, height) = annotations[i], sizes[i]
                y = int((word.top + word.bottom) / 2 - height / 2)
                if floor is not None:
                    y = max(y, floor)
                floor = y + height + 2
                x = right_edge - label_width
                leader = (word.left, (word.top + word.bottom) // 2, right_edge, y + height // 2)
                placements[i] = LabelPlacement(x=x, y=y, leader=leader)
        elif vertical:
            starts = {}
            for i in indexes:
                word = annotations[i]
                starts[i] = int((word.top + word.bottom) / 2 - sizes[i][1] / 2)
            lanes = stack_rows([(starts[i], starts[i] + sizes[i][1]) for i in indexes], gap=1)
            lane_width = max(sizes[i][0] for i in indexes) + 2
            for i, lane in zip(indexes, lanes):
                word = annotations[i]
                y = starts[i]
                placements[i] = LabelPlacement(x=word.line[2] + 1 + lane * lane_width, y=y)
        else:
            spans = []
            for i in indexes:
                centre = (annotations[i].left + annotations[i].right) / 2
                spans.append((int(centre - sizes[i][0] / 2), int(centre + sizes[i][0] / 2)))
            lanes = stack_rows(spans, gap=2)
            for i, (x, _), lane in zip(indexes, spans, lanes):
                word, (_, height) = annotations[i], sizes[i]
                if ruby:
                    y = word.line[1] - (lane + 1) * (height + 1)
                else:
                    y = word.line[3] + 1 + lane * (height + 1)
                placements[i] = LabelPlacement(x=x, y=y)
    return [placement for placement in placements if placement is not None]


def batch_by_newlines(
    translate_text: Callable[[str, str], str],
) -> TranslateBatch:
    """One request per batch: lines joined by newlines, split back afterwards.

    Free translation endpoints rate-limit hard (HTTP 429), so a screen of
    thirty lines must not become thirty requests. When the answer does not
    split back into as many lines (a line merged with the next), the batch is
    retried line by line.
    """

    def translate(texts: list[str], code: str) -> list[str | None]:
        if not texts:
            return []
        cleaned = [" ".join(text.split()) for text in texts]
        if len(cleaned) == 1:
            return [translate_text(cleaned[0], code)]
        joined = translate_text("\n".join(cleaned), code)
        lines = [line.strip() for line in (joined or "").split("\n")]
        lines = [line for line in lines if line]
        if len(lines) == len(cleaned):
            return list(lines)
        return [translate_text(text, code) for text in cleaned]

    return translate


class TranslationCache:
    """Remembers translations across presses: game and app screens repeat a lot.

    Only lines not seen before go to the translator, and only successful
    answers are kept, so a rate-limited press is retried next time.
    """

    def __init__(self, translate: TranslateBatch) -> None:
        self._translate = translate
        self._values: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self.last_error: Exception | None = None

    def __call__(self, texts: list[str], code: str) -> list[str | None]:
        with self._lock:
            missing = list(dict.fromkeys(text for text in texts if (text, code) not in self._values))
        if missing:
            try:
                results = self._translate(missing, code)
            except Exception as exc:
                self.last_error = exc
                results = [None] * len(missing)
            with self._lock:
                for text, result in zip(missing, results):
                    if result:
                        self._values[(text, code)] = result
        with self._lock:
            return [self._values.get((text, code)) for text in texts]


def dictionary_then_translate(
    dictionary: Callable[[str, str], str | None],
    translate: TranslateBatch,
) -> GlossLookup:
    """Glosses from an offline dictionary first, machine translation for the rest."""

    def lookup(lemmas: list[str], code: str) -> list[str | None]:
        found = [dictionary(lemma, code) for lemma in lemmas]
        missing = [lemma for lemma, value in zip(lemmas, found) if not value]
        translated = dict(zip(missing, translate(missing, code))) if missing else {}
        return [value or translated.get(lemma) for lemma, value in zip(lemmas, found)]

    return lookup


# ---------------------------------------------------------------------------
# Hotkey parsing
# ---------------------------------------------------------------------------

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_ESCAPE = 0x1B

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}


def parse_hotkey(raw: str) -> tuple[int, int]:
    """``"Ctrl+Alt+T"`` -> (modifiers, virtual key). One key, at least one modifier."""
    modifiers = 0
    key: int | None = None
    for part in (piece.strip() for piece in raw.split("+")):
        if not part:
            raise ValueError(f"invalid hotkey `{raw}`")
        lowered = part.lower()
        if lowered in _MODIFIERS:
            modifiers |= _MODIFIERS[lowered]
            continue
        if key is not None:
            raise ValueError(f"hotkey `{raw}` must contain exactly one non-modifier key")
        if len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif lowered.startswith("f") and lowered[1:].isdigit() and 1 <= int(lowered[1:]) <= 24:
            key = 0x70 + int(lowered[1:]) - 1
        else:
            raise ValueError(f"unsupported hotkey key `{part}`")
    if key is None:
        raise ValueError(f"hotkey `{raw}` is missing the final key")
    if not modifiers:
        raise ValueError(f"hotkey `{raw}` needs at least one modifier")
    return modifiers, key


# ---------------------------------------------------------------------------
# Win32 glue
# ---------------------------------------------------------------------------

WM_HOTKEY = 0x0312
WM_APP = 0x8000
# Keys grabbed only while the overlay is on screen (Esc, zoom, Ctrl+wheel), so
# apps keep them the rest of the time.
WM_APP_VISIBLE_KEYS_ON = WM_APP + 1
WM_APP_VISIBLE_KEYS_OFF = WM_APP + 2
WM_APP_QUIT = WM_APP + 3
HOTKEY_TOGGLE_ID = 1
HOTKEY_ESC_ID = 2
HOTKEY_MODE_ID = 3

VK_OEM_PLUS = 0xBB  # the =/+ key
VK_OEM_MINUS = 0xBD
VK_ADD = 0x6B  # numpad
VK_SUBTRACT = 0x6D
VK_NUMPAD0 = 0x60
VK_0 = 0x30
VK_CONTROL = 0x11

# (id, modifiers, key, event) for the keys active while the overlay is up.
VISIBLE_HOTKEYS = (
    (HOTKEY_ESC_ID, 0, VK_ESCAPE, "hide"),
    (10, MOD_CONTROL, VK_OEM_PLUS, "zoom_in"),
    (11, MOD_CONTROL | MOD_SHIFT, VK_OEM_PLUS, "zoom_in"),
    (12, MOD_CONTROL, VK_ADD, "zoom_in"),
    (13, MOD_CONTROL, VK_OEM_MINUS, "zoom_out"),
    (14, MOD_CONTROL, VK_SUBTRACT, "zoom_out"),
    (15, MOD_CONTROL, VK_0, "zoom_reset"),
    (16, MOD_CONTROL, VK_NUMPAD0, "zoom_reset"),
)

WH_MOUSE_LL = 14
WM_MOUSEWHEEL = 0x020A
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def wheel_event(mouse_data: int) -> str | None:
    """``zoom_in``/``zoom_out`` from a WM_MOUSEWHEEL's mouseData (delta in the high word)."""
    delta = ctypes.c_short((mouse_data >> 16) & 0xFFFF).value
    if delta > 0:
        return "zoom_in"
    if delta < 0:
        return "zoom_out"
    return None

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def set_dpi_awareness() -> None:
    """Physical pixels everywhere, so OCR boxes and window coordinates agree."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def foreground_window() -> int:
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


class HotkeyThread(threading.Thread):
    """RegisterHotKey and its message loop must live on the same thread."""

    def __init__(
        self,
        toggle: tuple[int, int],
        mode: tuple[int, int] | None,
        events: "queue.Queue[str]",
    ) -> None:
        super().__init__(daemon=True, name="overlay-hotkey")
        self.toggle = toggle
        self.mode = mode
        self.events = events
        self.thread_id: int | None = None
        self.ready = threading.Event()
        self.error: str | None = None
        self.warning: str | None = None
        self._hook: int | None = None
        self._hook_proc = None

    def run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Make sure this thread has a message queue before anyone posts to it.
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        modifiers, key = self.toggle
        if not user32.RegisterHotKey(None, HOTKEY_TOGGLE_ID, modifiers | MOD_NOREPEAT, key):
            self.error = f"RegisterHotKey failed (error {kernel32.GetLastError()}); is the hotkey taken?"
            self.ready.set()
            return
        mode_registered = False
        if self.mode is not None:
            modifiers, key = self.mode
            mode_registered = bool(user32.RegisterHotKey(None, HOTKEY_MODE_ID, modifiers | MOD_NOREPEAT, key))
            if not mode_registered:
                self.warning = f"mode hotkey not registered (error {kernel32.GetLastError()})"
        self.ready.set()
        visible_events = {hotkey_id: event for hotkey_id, _, _, event in VISIBLE_HOTKEYS}
        visible_registered: list[int] = []
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    if msg.wParam == HOTKEY_TOGGLE_ID:
                        self.events.put("toggle")
                    elif msg.wParam == HOTKEY_MODE_ID:
                        self.events.put("mode")
                    elif msg.wParam in visible_events:
                        self.events.put(visible_events[msg.wParam])
                elif msg.message == WM_APP_VISIBLE_KEYS_ON and not visible_registered:
                    for hotkey_id, modifiers, key, event in VISIBLE_HOTKEYS:
                        # Esc fires once per press; zoom keys repeat while held.
                        flags = modifiers | (MOD_NOREPEAT if event == "hide" else 0)
                        if user32.RegisterHotKey(None, hotkey_id, flags, key):
                            visible_registered.append(hotkey_id)
                    self._install_wheel_hook()
                elif msg.message == WM_APP_VISIBLE_KEYS_OFF:
                    for hotkey_id in visible_registered:
                        user32.UnregisterHotKey(None, hotkey_id)
                    visible_registered.clear()
                    self._remove_wheel_hook()
                elif msg.message == WM_APP_QUIT:
                    break
        finally:
            for hotkey_id in visible_registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self._remove_wheel_hook()
            if mode_registered:
                user32.UnregisterHotKey(None, HOTKEY_MODE_ID)
            user32.UnregisterHotKey(None, HOTKEY_TOGGLE_ID)

    def _install_wheel_hook(self) -> None:
        """Ctrl+wheel zooms the overlay; the hook swallows it so the app below does not zoom too.

        A low-level mouse hook runs on this thread's message loop, so it is
        installed here and only while the overlay is visible.
        """
        if self._hook:
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def on_mouse(code: int, message: int, data: int) -> int:
            if code == 0 and message == WM_MOUSEWHEEL and user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
                info = ctypes.cast(data, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                event = wheel_event(info.mouseData)
                if event:
                    self.events.put(event)
                return 1
            return user32.CallNextHookEx(None, code, message, data)

        self._hook_proc = HOOKPROC(on_mouse)  # kept alive for as long as the hook is installed
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_proc, kernel32.GetModuleHandleW(None), 0)

    def _remove_wheel_hook(self) -> None:
        if self._hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._hook_proc = None

    def post(self, message: int) -> None:
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, message, 0, 0)


# ---------------------------------------------------------------------------
# Screen reading
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Snapshot:
    lines: list[LineAnalysis]
    codes: tuple[str, ...]
    ocr_seconds: float
    grab_seconds: float = 0.0


@dataclass(slots=True)
class Rendered:
    mode: str
    items: list[OverlayItem] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    seconds: float = 0.0
    error: Exception | None = None

    @property
    def count(self) -> int:
        return len(self.items) + len(self.annotations)


class ScreenReader:
    """Warm OCR engines, translation and catalog glosses."""

    def __init__(
        self,
        *,
        db_path: Path,
        target_language: str,
        languages_override: tuple[str, ...] | None,
        state_path: Path = DEFAULT_STATE_PATH,
    ) -> None:
        # Imported here: the worker pulls in the OCR stack, which tests do not need.
        from capture_worker import CapturePipeline, GoogleGtxSourceTranslator, resolve_capture_languages
        from jmdict import JMdict
        from lang_jp import db_connect, katakana_to_hiragana, romanize_kana

        self.db_path = db_path
        self.state_path = state_path
        self.target_language = target_language
        self.languages_override = languages_override
        self._resolve_capture_languages = resolve_capture_languages
        self._db_connect = db_connect
        self.romanize_kana = romanize_kana
        self.to_hiragana = katakana_to_hiragana
        self.pipeline = CapturePipeline()
        translator = GoogleGtxSourceTranslator(target_language=target_language)
        self.translate = TranslationCache(
            batch_by_newlines(
                lambda text, code: translator.translate_text(
                    text, source_language=languages.base_language_code(code)
                )
            )
        )
        self._open_jmdict = JMdict.open
        self.gloss = dictionary_then_translate(self.dictionary_meaning, self.translate)
        self.codes = self.current_languages()
        self.pipeline.ocr_for(self.codes)

    def state(self) -> OverlayState:
        return load_overlay_state(self.state_path)

    def current_languages(self) -> tuple[str, ...]:
        if self.languages_override:
            return self.languages_override
        if self.state().languages == "ja":
            return ("ja",)
        connection = self._db_connect(self.db_path)
        try:
            return tuple(self._resolve_capture_languages(connection))
        finally:
            connection.close()

    def dictionary_meaning(self, lemma: str, code: str) -> str | None:
        """An offline gloss in the target language, before falling back to machine translation.

        English: JMdict's first gloss for Japanese words. Portuguese: the
        catalog's own ``meaning_pt``. Other targets have no offline source.
        """
        target = languages.base_language_code(self.target_language)
        if target == "en" and is_japanese(code):
            return self.jmdict_gloss(lemma)
        if target == "pt":
            return self.catalog_meaning(lemma, code)
        return None

    def jmdict_gloss(self, lemma: str) -> str | None:
        # Opened per call: renders run on worker threads and sqlite connections
        # stay on the thread that opened them; a lookup is a few indexed reads.
        dictionary = self._open_jmdict()
        if dictionary is None:
            return None
        with dictionary:
            match = dictionary.lookup(lemma)
        return match.gloss_text(1) if match is not None else None

    def catalog_meaning(self, lemma: str, code: str) -> str | None:
        """The catalog's own Portuguese meaning for a lemma."""
        connection = self._db_connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT meaning_pt FROM terms
                WHERE language_code = ? AND lemma = ? AND COALESCE(TRIM(meaning_pt), '') <> ''
                ORDER BY id LIMIT 1
                """,
                (code, lemma),
            ).fetchone()
        finally:
            connection.close()
        return str(row[0]) if row else None

    def read(self, image_path: Path) -> Snapshot:
        started = time.perf_counter()
        self.codes = self.current_languages()
        adapter = self.pipeline.ocr_for(self.codes)
        blocks = adapter.extract_blocks(image_path, progress_label="overlay step=ocr")
        lines = analyze_lines(blocks, candidates=self.codes)
        return Snapshot(lines=lines, codes=self.codes, ocr_seconds=time.perf_counter() - started)

    def render(self, snapshot: Snapshot, mode: str) -> Rendered:
        started = time.perf_counter()
        self.translate.last_error = None
        rendered = Rendered(mode=mode)
        if mode == "furigana":
            rendered.annotations = furigana_annotations(snapshot.lines, to_hiragana=self.to_hiragana)
        elif mode == "words":
            rendered.annotations = word_annotations(
                snapshot.lines,
                gloss=self.gloss,
                target_language=self.target_language,
                to_hiragana=self.to_hiragana,
            )
        else:
            rendered.items = translation_items(
                snapshot.lines,
                translate=self.translate,
                target_language=self.target_language,
                romanize_kana=self.romanize_kana,
            )
        rendered.error = self.translate.last_error
        rendered.seconds = time.perf_counter() - started
        return rendered


def grab_primary_monitor(path: Path) -> tuple[int, int]:
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=False)
    image.save(path)
    return image.size


# ---------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------


class OverlayApp:
    def __init__(
        self,
        reader: ScreenReader,
        hotkeys: HotkeyThread,
        events: "queue.Queue[str]",
        hotkey_label: str,
        mode_hotkey_label: str | None,
        *,
        capturable: bool = False,
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.tkfont = tkfont
        self.reader = reader
        self.hotkeys = hotkeys
        self.events = events
        self.hotkey_label = hotkey_label
        self.mode_hotkey_label = mode_hotkey_label
        self.results: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.busy = False
        self.visible = False
        self.snapshot: Snapshot | None = None
        self.snapshot_foreground = 0
        self.excluded_from_capture = False
        self.capturable = capturable
        self._toast_after: str | None = None
        # What is on screen, kept so a zoom can redraw it without a new read.
        self.shown: tuple[Snapshot, Rendered] | None = None
        self.scale = reader.state().scale

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"{self.screen_w}x{self.screen_h}+0+0")
        self.root.configure(bg=TRANSPARENT_KEY)
        self.canvas = tk.Canvas(self.root, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self._apply_window_styles()
        self.root.after(30, self._poll)

    def _hwnd(self) -> int:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = wintypes.HWND
        handle = user32.GetParent(self.root.winfo_id())
        return int(handle or self.root.winfo_id())

    def _apply_window_styles(self) -> None:
        user32 = ctypes.windll.user32
        hwnd = self._hwnd()
        user32.GetWindowLongW.restype = ctypes.c_long
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        # Tk sets the layered colour key; apply it after the style so it sticks.
        self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.root.attributes("-topmost", True)
        if self.capturable:
            return
        self.excluded_from_capture = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
        if not self.excluded_from_capture:
            print("warning: overlay could not be excluded from capture; it is hidden while grabbing")

    # -- event plumbing ----------------------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                self._on_event(self.events.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                kind, payload = self.results.get_nowait()
                self._on_result(kind, payload)
        except queue.Empty:
            pass
        if self.visible and not self.busy and self.snapshot_foreground:
            if foreground_window() != self.snapshot_foreground:
                self.hide("foreground window changed")
        self.root.after(60, self._poll)

    def _on_event(self, event: str) -> None:
        if event == "hide":
            self.hide("Esc")
        elif event in ("zoom_in", "zoom_out", "zoom_reset"):
            self._zoom(event)
        elif event == "toggle":
            if self.busy:
                return
            if self.visible and self.snapshot is not None:
                self.hide("hotkey")
            else:
                self.start_read()
        elif event == "mode":
            if self.busy:
                return
            state = self.reader.state()
            state.mode = next_mode(state.mode)
            save_overlay_state(self.reader.state_path, state)
            print(f"overlay mode {state.mode}", flush=True)
            if self.visible and self.snapshot is not None:
                self.start_render(self.snapshot, state.mode)
            else:
                self._toast(f"Modo: {MODE_LABELS[state.mode]}")

    def start_read(self) -> None:
        self.busy = True
        self.snapshot = None
        self.shown = None
        self._cancel_toast()
        self.snapshot_foreground = foreground_window()
        state = self.reader.state()
        mode, self.scale = state.mode, state.scale
        if self.excluded_from_capture:
            self.canvas.delete("all")
            self._draw_status(f"Lendo a tela… ({MODE_LABELS[mode]})")
            self._show()
        else:
            self.root.withdraw()
            self.root.update()

        def work() -> None:
            try:
                with tempfile.TemporaryDirectory(prefix="lang-overlay-") as tmp:
                    path = Path(tmp) / "screen.png"
                    started = time.perf_counter()
                    grab_primary_monitor(path)
                    grab_seconds = time.perf_counter() - started
                    snapshot = self.reader.read(path)
                snapshot.grab_seconds = grab_seconds
                self.results.put(("snapshot", snapshot))
                self.results.put(("rendered", (snapshot, self.reader.render(snapshot, mode))))
            except Exception as exc:  # noqa: BLE001 - surfaced on the overlay
                self.results.put(("error", exc))

        threading.Thread(target=work, daemon=True, name="overlay-read").start()

    def start_render(self, snapshot: Snapshot, mode: str) -> None:
        self.busy = True
        self.canvas.delete("all")
        self._draw_status(f"{MODE_LABELS[mode]}…")

        def work() -> None:
            try:
                self.results.put(("rendered", (snapshot, self.reader.render(snapshot, mode))))
            except Exception as exc:  # noqa: BLE001
                self.results.put(("error", exc))

        threading.Thread(target=work, daemon=True, name="overlay-render").start()

    def _on_result(self, kind: str, payload: object) -> None:
        if kind == "snapshot":
            self.snapshot = payload  # type: ignore[assignment]
            return
        self.busy = False
        self.canvas.delete("all")
        if kind == "error":
            print(f"overlay read failed: {payload!r}", flush=True)
            self._draw_status(f"Falha ao ler a tela: {payload}")
            self._show()
            return
        snapshot, rendered = payload  # type: ignore[misc]
        print(
            f"overlay {rendered.mode} grab={snapshot.grab_seconds:.2f}s ocr={snapshot.ocr_seconds:.2f}s "
            f"render={rendered.seconds:.2f}s lines={len(snapshot.lines)} shown={rendered.count} "
            f"languages={','.join(snapshot.codes)}",
            flush=True,
        )
        if rendered.error is not None:
            print(f"translation failed: {rendered.error!r}", flush=True)
        self.shown = (snapshot, rendered)
        self._draw_rendered(snapshot, rendered)
        self._show()

    def _zoom(self, event: str) -> None:
        """Grow or shrink the labels and redraw what is on screen; the size is saved."""
        steps = {"zoom_in": 1, "zoom_out": -1, "zoom_reset": 0}[event]
        state = self.reader.state()
        scale = zoom_scale(self.scale, steps)
        if scale == self.scale and steps:
            return
        self.scale = state.scale = scale
        save_overlay_state(self.reader.state_path, state)
        if self.visible and self.shown is not None and not self.busy:
            self.canvas.delete("all")
            self._draw_rendered(*self.shown)
            self._draw_status(f"Tamanho {round(scale * 100)}%")

    def _draw_rendered(self, snapshot: Snapshot, rendered: Rendered) -> None:
        if rendered.error is not None:
            self._draw_status(f"Tradução indisponível agora ({rendered.error}); mostrando só as leituras")
        elif not rendered.count:
            self._draw_status("Nada para mostrar nesta tela neste modo")
        for item in rendered.items:
            self._draw_item(item)
        if rendered.mode == "furigana":
            self._draw_annotations(rendered.annotations, ruby=True)
        elif rendered.mode == "words":
            self._draw_annotations(rendered.annotations, ruby=False)
        keys = f"{self.hotkey_label} ou Esc fecha"
        if self.mode_hotkey_label:
            keys += f" · {self.mode_hotkey_label} muda o modo"
        keys += " · Ctrl+roda ou Ctrl +/- tamanho"
        self._draw_status(
            f"{MODE_LABELS[rendered.mode]} · {rendered.count} · "
            f"{snapshot.ocr_seconds + rendered.seconds:.1f}s · {keys}",
            corner=True,
        )

    def _show(self) -> None:
        if not self.visible:
            print("overlay shown", flush=True)
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        if not self.visible:
            self.visible = True
            self.hotkeys.post(WM_APP_VISIBLE_KEYS_ON)

    def hide(self, reason: str = "") -> None:
        if self.visible:
            print(f"overlay hidden ({reason})", flush=True)
        self._cancel_toast()
        self.root.withdraw()
        self.canvas.delete("all")
        self.snapshot = None
        self.shown = None
        if self.visible:
            self.visible = False
            self.hotkeys.post(WM_APP_VISIBLE_KEYS_OFF)

    def _toast(self, text: str) -> None:
        """A short status while the overlay is otherwise hidden (mode changed)."""
        self._cancel_toast()
        self.canvas.delete("all")
        self._draw_status(text)
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._toast_after = self.root.after(1400, self._end_toast)

    def _end_toast(self) -> None:
        self._toast_after = None
        if not self.visible:
            self.root.withdraw()
            self.canvas.delete("all")

    def _cancel_toast(self) -> None:
        if self._toast_after is not None:
            self.root.after_cancel(self._toast_after)
            self._toast_after = None

    # -- drawing -------------------------------------------------------------

    def _draw_item(self, item: OverlayItem) -> None:
        size = font_px_for(item.height, self.scale)
        main_font = self.tkfont.Font(family="Segoe UI", size=-size)
        reading_font = self.tkfont.Font(family="Segoe UI", size=-max(10, int(size * 0.62)))
        wrap = max(item.width, int(MIN_LABEL_WIDTH_PX * self.scale))
        wrap = min(wrap, self.screen_w - item.left - 8) if item.left < self.screen_w - 8 else wrap
        pad = max(4, size // 6)
        x = item.left
        y = item.top
        ids: list[int] = []
        cursor = y + pad
        if item.translation:
            ids.append(
                self.canvas.create_text(
                    x + pad, cursor, text=item.translation, anchor="nw",
                    fill=TRANSLATION_COLOR, font=main_font, width=wrap,
                )
            )
            cursor = self.canvas.bbox(ids[-1])[3]
        if item.reading:
            # Without a translation the reading is the content: full size.
            ids.append(
                self.canvas.create_text(
                    x + pad, cursor + 1, text=item.reading, anchor="nw",
                    fill=READING_COLOR if item.translation else TRANSLATION_COLOR,
                    font=reading_font if item.translation else main_font, width=wrap,
                )
            )
        boxes = [self.canvas.bbox(i) for i in ids]
        right = max(box[2] for box in boxes) + pad
        bottom = max(box[3] for box in boxes) + pad
        right = max(right, item.right)
        bottom = max(bottom, item.bottom)
        background = self.canvas.create_rectangle(
            x, y, right, bottom, fill=LABEL_BACKGROUND, outline=LABEL_BORDER, width=1,
        )
        self.canvas.tag_lower(background, ids[0])

    def _draw_annotations(self, annotations: list[Annotation], *, ruby: bool) -> None:
        """Small labels beside their word, each word outlined (layout in :func:`place_labels`)."""
        fonts: dict[int, tuple[object, object]] = {}
        labels: list[tuple[list[int], Rect]] = []
        for annotation in annotations:
            size = small_font_px_for(annotation.thickness, self.scale)
            if size not in fonts:
                fonts[size] = (
                    self.tkfont.Font(family="Segoe UI", size=-size, weight="bold" if ruby else "normal"),
                    self.tkfont.Font(family="Segoe UI", size=-max(10, int(size * 0.8))),
                )
            main_font, sub_font = fonts[size]
            labels.append(self._make_label(annotation, main_font, sub_font, max(2, size // 6), ruby))
        sizes = [(bounds[2] - bounds[0], bounds[3] - bounds[1]) for _, bounds in labels]
        for annotation, (ids, bounds), placement in zip(
            annotations, labels, place_labels(annotations, sizes, ruby=ruby)
        ):
            self._place_label(ids, bounds, placement.x, placement.y)
            # Mark the word itself, so it is clear which word a label explains.
            self.canvas.create_rectangle(
                annotation.left - 1, annotation.top - 1, annotation.right + 1, annotation.bottom + 1,
                outline=MARKER_COLOR, width=2,
            )
            if placement.leader is not None:
                self.canvas.create_line(*placement.leader, fill=MARKER_COLOR, width=1)

    def _make_label(self, annotation: Annotation, main_font, sub_font, pad: int, ruby: bool) -> tuple[list[int], Rect]:
        """Create a label's text items at the origin; returns them and their padded bounds."""
        text = annotation.text
        if ruby_runs_vertically(annotation, ruby=ruby):
            text = "\n".join(text)
        ids = [
            self.canvas.create_text(
                0, 0, text=text, anchor="n", justify="center",
                fill=FURIGANA_COLOR if ruby else GLOSS_COLOR, font=main_font,
            )
        ]
        if annotation.sub:
            first = self.canvas.bbox(ids[0])
            ids.append(
                self.canvas.create_text(
                    0, first[3], text=annotation.sub, anchor="n", fill=READING_COLOR, font=sub_font,
                )
            )
        boxes = [self.canvas.bbox(item_id) for item_id in ids]
        bounds = (
            min(box[0] for box in boxes) - pad,
            min(box[1] for box in boxes) - pad,
            max(box[2] for box in boxes) + pad,
            max(box[3] for box in boxes) + pad,
        )
        return ids, bounds

    def _place_label(self, ids: list[int], bounds: Rect, x: int, y: int) -> None:
        """Move a label so its padded box starts at (x, y) and put a background behind it."""
        dx, dy = x - bounds[0], y - bounds[1]
        for item_id in ids:
            self.canvas.move(item_id, dx, dy)
        background = self.canvas.create_rectangle(
            bounds[0] + dx, bounds[1] + dy, bounds[2] + dx, bounds[3] + dy,
            fill=LABEL_BACKGROUND, outline="",
        )
        self.canvas.tag_lower(background, ids[0])

    def _draw_status(self, text: str, *, corner: bool = False) -> None:
        size = max(15, self.screen_h // 80)
        font = self.tkfont.Font(family="Segoe UI", size=-size, weight="bold")
        x = self.screen_w - size
        y = self.screen_h - size * 5 if corner else size
        label = self.canvas.create_text(x - 10, y + 6, text=text, anchor="ne", fill=STATUS_COLOR, font=font)
        left, top, right, bottom = self.canvas.bbox(label)
        background = self.canvas.create_rectangle(
            left - 10, top - 6, right + 10, bottom + 6, fill=STATUS_BACKGROUND, outline="",
        )
        self.canvas.tag_lower(background, label)

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self.hotkeys.post(WM_APP_QUIT)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    from lang_jp import DEFAULT_DB_PATH

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY, help="Show/hide hotkey (default Ctrl+Alt+Q).")
    parser.add_argument(
        "--mode-hotkey",
        default=DEFAULT_MODE_HOTKEY,
        help="Hotkey that cycles translation -> furigana -> words (default Ctrl+Alt+W; '' to disable).",
    )
    parser.add_argument("--mode", choices=MODES, help="Set the mode in the state file before starting.")
    parser.add_argument("--target", default=DEFAULT_TARGET_LANGUAGE, help="Translation target language (default en).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Catalog database.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Mode/languages file shared with the tray.")
    parser.add_argument(
        "--languages",
        help="Comma-separated languages to read, overriding the state file, e.g. ja,en.",
    )
    parser.add_argument("--trigger-now", action="store_true", help="Read the screen once right after start.")
    parser.add_argument("--exit-after", type=float, help="Quit after this many seconds (testing).")
    parser.add_argument(
        "--capturable",
        action="store_true",
        help="Debug: leave the overlay visible to screenshots (it hides while grabbing instead).",
    )
    parser.add_argument(
        "--image",
        type=Path,
        help="Read this image once and print what each mode shows instead of running the overlay.",
    )
    return parser


def print_rendered(rendered: Rendered) -> None:
    print(f"--- {rendered.mode} ({rendered.count}, {rendered.seconds:.2f}s)")
    for item in rendered.items:
        print(f"[{item.left},{item.top},{item.right},{item.bottom}] {item.language_code} {item.text}")
        if item.reading:
            print(f"    {item.reading}")
        print(f"    -> {item.translation}")
    for annotation in rendered.annotations:
        sub = f" ({annotation.sub})" if annotation.sub else ""
        print(f"[{annotation.left}-{annotation.right} @ {annotation.top}] {annotation.surface}: {annotation.text}{sub}")
    if rendered.error is not None:
        print(f"translation failed: {rendered.error!r}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    override = None
    if args.languages:
        override = tuple(code.strip() for code in args.languages.split(",") if code.strip())
    if args.mode:
        state = load_overlay_state(args.state)
        state.mode = args.mode
        save_overlay_state(args.state, state)

    if args.image is None:
        # Before anything opens a window or reads a screen size.
        set_dpi_awareness()
        toggle = parse_hotkey(args.hotkey)
        mode_key = parse_hotkey(args.mode_hotkey) if args.mode_hotkey.strip() else None

    print("loading OCR engines…", flush=True)
    reader = ScreenReader(
        db_path=args.db,
        target_language=args.target,
        languages_override=override,
        state_path=args.state,
    )
    state = reader.state()
    print(
        f"ready: languages={','.join(reader.codes)} target={args.target} mode={state.mode}",
        flush=True,
    )

    if args.image is not None:
        snapshot = reader.read(args.image)
        print(f"ocr={snapshot.ocr_seconds:.2f}s lines={len(snapshot.lines)}")
        modes = [args.mode] if args.mode else list(MODES)
        for mode in modes:
            print_rendered(reader.render(snapshot, mode))
        return

    events: "queue.Queue[str]" = queue.Queue()
    hotkeys = HotkeyThread(toggle, mode_key, events)
    hotkeys.start()
    hotkeys.ready.wait(5)
    if hotkeys.error:
        raise SystemExit(hotkeys.error)
    if hotkeys.warning:
        print(f"warning: {hotkeys.warning}", flush=True)
    mode_label = args.mode_hotkey if mode_key is not None and not hotkeys.warning else None
    print(
        f"press {args.hotkey} to read the screen"
        + (f", {mode_label} to change mode" if mode_label else ""),
        flush=True,
    )
    app = OverlayApp(reader, hotkeys, events, args.hotkey, mode_label, capturable=args.capturable)
    if args.trigger_now:
        events.put("toggle")
    if args.exit_after:
        app.root.after(int(args.exit_after * 1000), app.root.destroy)
    app.run()


if __name__ == "__main__":
    main()
