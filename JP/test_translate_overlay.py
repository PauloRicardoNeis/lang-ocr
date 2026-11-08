from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from importlib.util import find_spec
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from ocr_backends import OcrBlock, OcrTextLine
from translate_overlay import (
    MAX_FONT_PX,
    MIN_FONT_PX,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    LineAnalysis,
    OverlayState,
    TranslationCache,
    analyze_lines,
    batch_by_newlines,
    block_rect,
    build_overlay_items,
    dictionary_then_translate,
    font_px_for,
    furigana_annotations,
    load_overlay_state,
    next_mode,
    parse_hotkey,
    save_overlay_state,
    shorten_gloss,
    span_placements,
    zoom_scale,
    small_font_px_for,
    wheel_event,
    place_labels,
    ruby_runs_vertically,
    Annotation,
    stack_rows,
    trim_okurigana,
    word_annotations,
)

HAS_SUDACHI = find_spec("sudachipy") is not None


def box(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


class FakeTranslator:
    def __init__(self, table: dict[str, str] | None = None, *, fail_on: set[str] | None = None) -> None:
        self.table = table or {}
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, texts: list[str], code: str) -> list[str]:
        with self.lock:
            self.calls.append((tuple(texts), code))
        if self.fail_on.intersection(texts):
            raise OSError("network down")
        return [self.table.get(text, f"<{text}>") for text in texts]


class BlockRectTests(unittest.TestCase):
    def test_rounds_the_quad_to_an_axis_aligned_rect(self) -> None:
        block = OcrBlock(text="x", confidence=1.0, bbox=[[10.4, 20.6], [99.6, 19.8], [100.2, 40.4], [9.8, 41.0]])
        self.assertEqual(block_rect(block), (10, 20, 100, 41))

    def test_missing_or_degenerate_boxes_are_skipped(self) -> None:
        self.assertIsNone(block_rect(OcrBlock(text="x", confidence=1.0, bbox=None)))
        self.assertIsNone(block_rect(OcrBlock(text="x", confidence=1.0, bbox=box(5, 5, 5, 9))))


class BuildOverlayItemsTests(unittest.TestCase):
    def test_translates_each_line_and_keeps_its_box(self) -> None:
        translate = FakeTranslator({"Press any button": "Pressione qualquer botão"})
        items = build_overlay_items(
            [OcrBlock(text="Press any button", confidence=0.9, bbox=box(100, 200, 400, 230))],
            candidates=("en",),
            translate=translate,
            target_language="pt",
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item.left, item.top, item.right, item.bottom), (100, 200, 400, 230))
        self.assertEqual(item.language_code, "en")
        self.assertEqual(item.translation, "Pressione qualquer botão")
        self.assertIsNone(item.reading)
        self.assertEqual(translate.calls, [(("Press any button",), "en")])

    def test_lines_already_in_the_target_language_are_not_overlaid(self) -> None:
        translate = FakeTranslator()
        items = build_overlay_items(
            [OcrBlock(text="Não é possível salvar o jogo agora", confidence=0.9, bbox=box(0, 0, 300, 30))],
            candidates=("en", "pt"),
            translate=translate,
            target_language="pt",
        )
        self.assertEqual(items, [])
        self.assertEqual(translate.calls, [])

    def test_failed_batch_without_reading_drops_its_lines_only(self) -> None:
        translate = FakeTranslator(fail_on={"Continue"})
        items = build_overlay_items(
            [
                OcrBlock(text="Continue the game", confidence=0.9, bbox=box(0, 0, 100, 20)),
                OcrBlock(text="Continue", confidence=0.9, bbox=box(0, 40, 100, 60)),
                OcrBlock(text="안녕하세요", confidence=0.9, bbox=box(0, 80, 100, 100)),
            ],
            candidates=("en", "ko"),
            translate=translate,
        )
        self.assertEqual([item.text for item in items], ["안녕하세요"])

    def test_one_translation_request_per_language(self) -> None:
        translate = FakeTranslator()
        build_overlay_items(
            [
                OcrBlock(text="Load the saved game", confidence=0.9, bbox=box(0, 0, 200, 20)),
                OcrBlock(text="Quit to the desktop", confidence=0.9, bbox=box(0, 40, 200, 60)),
                OcrBlock(text="감사합니다", confidence=0.9, bbox=box(0, 80, 200, 100)),
            ],
            candidates=("en", "ko"),
            translate=translate,
            target_language="pt",
        )
        self.assertEqual(
            sorted(translate.calls),
            sorted([(("Load the saved game", "Quit to the desktop"), "en"), (("감사합니다",), "ko")]),
        )

    def test_empty_and_boxless_blocks_are_ignored(self) -> None:
        translate = FakeTranslator()
        items = build_overlay_items(
            [
                OcrBlock(text="  ", confidence=0.9, bbox=box(0, 0, 10, 10)),
                OcrBlock(text="Options", confidence=0.9, bbox=None),
            ],
            candidates=("en",),
            translate=translate,
        )
        self.assertEqual(items, [])
        self.assertEqual(translate.calls, [])

    def test_items_follow_reading_order(self) -> None:
        items = build_overlay_items(
            [
                OcrBlock(text="Second line here", confidence=0.9, bbox=box(0, 100, 200, 120)),
                OcrBlock(text="First line here", confidence=0.9, bbox=box(0, 10, 200, 30)),
            ],
            candidates=("en",),
            translate=FakeTranslator(),
            target_language="pt",
        )
        self.assertEqual([item.text for item in items], ["First line here", "Second line here"])

    def test_korean_lines_carry_a_romanization(self) -> None:
        items = build_overlay_items(
            [OcrBlock(text="안녕하세요", confidence=0.9, bbox=box(0, 0, 200, 30))],
            candidates=("ko",),
            translate=FakeTranslator({"안녕하세요": "Olá"}),
        )
        self.assertEqual(items[0].language_code, "ko")
        self.assertTrue(items[0].reading)
        self.assertEqual(items[0].translation, "Olá")

    @unittest.skipUnless(HAS_SUDACHI, "Sudachi is needed for Japanese readings")
    def test_japanese_lines_carry_romaji(self) -> None:
        from lang_jp import romanize_kana

        items = build_overlay_items(
            [OcrBlock(text="セーブしますか", confidence=0.9, bbox=box(0, 0, 300, 40))],
            candidates=("ja",),
            translate=FakeTranslator({"セーブしますか": "Deseja salvar?"}),
            romanize_kana=romanize_kana,
        )
        self.assertEqual(items[0].language_code, "ja")
        self.assertIn("se", (items[0].reading or "").lower())
        self.assertEqual(items[0].translation, "Deseja salvar?")

    @unittest.skipUnless(HAS_SUDACHI, "Sudachi is needed for Japanese readings")
    def test_japanese_romaji_reads_particles_and_geminates(self) -> None:
        from lang_jp import romanize_kana

        items = build_overlay_items(
            [OcrBlock(text="パウレはレベル3にあがった!", confidence=0.9, bbox=box(0, 0, 600, 40))],
            candidates=("ja",),
            translate=FakeTranslator(),
            romanize_kana=romanize_kana,
        )
        reading = items[0].reading or ""
        self.assertIn(" wa ", reading)
        self.assertIn("agatta", reading)


class StateFileTests(unittest.TestCase):
    def test_round_trip_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.toml"
            self.assertEqual(load_overlay_state(path), OverlayState())
            save_overlay_state(path, OverlayState(mode="words", languages="capture", enabled=False))
            self.assertEqual(
                load_overlay_state(path), OverlayState(mode="words", languages="capture", enabled=False)
            )
            path.write_text('mode = "bogus"\nlanguages = 3\n', encoding="utf-8")
            self.assertEqual(load_overlay_state(path), OverlayState())
            path.write_text("not toml [", encoding="utf-8")
            self.assertEqual(load_overlay_state(path), OverlayState())

    def test_next_mode_cycles(self) -> None:
        self.assertEqual(next_mode("translation"), "furigana")
        self.assertEqual(next_mode("furigana"), "words")
        self.assertEqual(next_mode("words"), "translation")
        self.assertEqual(next_mode("bogus"), "translation")


class LayoutHelperTests(unittest.TestCase):
    def test_horizontal_spans_weight_full_width_characters(self) -> None:
        line = LineAnalysis(text="AB漢字", language_code="ja", rect=(0, 0, 310, 40))
        # Weights: A .55, B .55, 漢 1, 字 1 -> total 3.1
        self.assertEqual([p.rect for p in span_placements(line, 0, 2)], [(0, 0, 110, 40)])
        self.assertEqual([p.rect for p in span_placements(line, 2, 4)], [(110, 0, 310, 40)])

    def test_vertical_spans_follow_their_column(self) -> None:
        # Two columns read right to left: "今日は" (right) then "天気" (left).
        line = LineAnalysis(
            text="今日は天気",
            language_code="ja",
            rect=(100, 0, 220, 300),
            geometry=(
                OcrTextLine(start=0, end=3, left=170, top=0, right=220, bottom=300, vertical=True),
                OcrTextLine(start=3, end=5, left=100, top=0, right=150, bottom=200, vertical=True),
            ),
        )
        today = span_placements(line, 0, 2)
        self.assertEqual([(p.rect, p.vertical) for p in today], [((170, 0, 220, 200), True)])
        weather = span_placements(line, 3, 5)
        self.assertEqual([p.rect for p in weather], [(100, 0, 150, 200)])
        # A word wrapping across both columns gets a box in each.
        self.assertEqual(len(span_placements(line, 2, 4)), 2)

    def test_place_labels_horizontal_above_and_below(self) -> None:
        word = Annotation(left=100, top=200, right=160, bottom=240, surface="今日", text="きょう",
                          line=(100, 200, 400, 240), block=(100, 200, 400, 240))
        [above] = place_labels([word], [(40, 20)], ruby=True)
        self.assertEqual((above.x, above.y, above.leader), (110, 179, None))
        [below] = place_labels([word], [(40, 20)], ruby=False)
        self.assertEqual((below.x, below.y), (110, 241))

    def test_place_labels_vertical_ruby_and_gloss_gutter(self) -> None:
        bubble = (1087, 0, 1218, 600)
        sword = Annotation(left=1168, top=180, right=1218, bottom=240, surface="剣", text="けん",
                           vertical=True, line=(1168, 0, 1218, 300), block=bubble)
        take = Annotation(left=1087, top=0, right=1144, bottom=60, surface="取っ", text="と",
                          vertical=True, line=(1087, 0, 1144, 300), block=bubble)
        ruby = place_labels([sword, take], [(24, 50), (24, 25)], ruby=True)
        # Right of each word's own column, centred on the word.
        self.assertEqual([(p.x, p.y) for p in ruby], [(1219, 185), (1145, 17)])
        gloss = place_labels([sword, take], [(70, 30), (60, 30)], ruby=False)
        # One list in the gutter left of the bubble, in reading order: the right
        # column's 剣 first (level with it), then 取っ, pushed below 剣's gloss.
        self.assertEqual([(p.x, p.y) for p in gloss], [(1087 - 8 - 70, 195), (1087 - 8 - 60, 227)])
        self.assertEqual(gloss[0].leader, (1168, 210, 1087 - 8, 210))
        self.assertEqual(gloss[1].leader, (1087, 30, 1087 - 8, 242))
        self.assertTrue(ruby_runs_vertically(sword, ruby=True))
        self.assertFalse(ruby_runs_vertically(sword, ruby=False))

    def test_stack_rows_moves_overlapping_labels_down(self) -> None:
        self.assertEqual(stack_rows([(0, 50), (60, 100), (90, 140), (95, 120)]), [0, 0, 1, 2])

    def test_shorten_gloss_keeps_the_first_sense(self) -> None:
        self.assertEqual(shorten_gloss("poder; força"), "poder")
        self.assertEqual(shorten_gloss("  "), None)
        self.assertEqual(len(shorten_gloss("x" * 80) or ""), 28)
        self.assertEqual(shorten_gloss("sword (esp. a large, double-edged one); blade"), "sword")
        self.assertEqual(shorten_gloss("(n) (abbr)"), "(n) (abbr)")

    def test_dictionary_meaning_wins_over_translation(self) -> None:
        translate = FakeTranslator({"剣": "espada (mt)"})
        lookup = dictionary_then_translate(lambda lemma, code: "espada" if lemma == "剣" else None, translate)
        self.assertEqual(lookup(["剣", "盾"], "ja"), ["espada", "<盾>"])
        self.assertEqual(translate.calls, [(("盾",), "ja")])

    def test_trim_okurigana(self) -> None:
        from lang_jp import katakana_to_hiragana

        self.assertEqual(trim_okurigana("新しい", "あたらしい", katakana_to_hiragana), (0, 1, "あたら"))
        self.assertEqual(trim_okurigana("お茶", "おちゃ", katakana_to_hiragana), (1, 2, "ちゃ"))
        self.assertEqual(trim_okurigana("天気", "てんき", katakana_to_hiragana), (0, 2, "てんき"))


@unittest.skipUnless(HAS_SUDACHI, "Sudachi is needed for Japanese tokens")
class ModeTests(unittest.TestCase):
    def lines(self, text: str) -> list[LineAnalysis]:
        return analyze_lines(
            [OcrBlock(text=text, confidence=0.9, bbox=box(100, 500, 100 + 60 * len(text), 560))],
            candidates=("ja",),
        )

    def test_furigana_sits_over_the_kanji_only(self) -> None:
        from lang_jp import katakana_to_hiragana

        annotations = furigana_annotations(self.lines("新しいゲームを始める"), to_hiragana=katakana_to_hiragana)
        by_surface = {annotation.surface: annotation for annotation in annotations}
        self.assertEqual(by_surface["新しい"].text, "あたら")
        self.assertEqual((by_surface["新しい"].left, by_surface["新しい"].right), (100, 160))
        self.assertNotIn("ゲーム", by_surface)
        self.assertEqual((by_surface["新しい"].top, by_surface["新しい"].bottom), (500, 560))

    def test_words_gloss_content_words_by_dictionary_form(self) -> None:
        from lang_jp import katakana_to_hiragana

        gloss = FakeTranslator({"剣": "espada", "手": "mão", "入れる": "colocar"})
        annotations = word_annotations(
            self.lines("剣を手に入れた"), gloss=gloss, to_hiragana=katakana_to_hiragana
        )
        self.assertEqual(
            [(a.surface, a.text, a.sub) for a in annotations],
            [("剣", "espada", "けん"), ("手", "mão", "て"), ("入れ", "colocar", "いれ")],
        )
        # One lookup for the whole screen, particles and auxiliaries left out.
        self.assertEqual(len(gloss.calls), 1)
        self.assertNotIn("を", gloss.calls[0][0])

    def test_vertical_bubble_annotations_sit_on_their_column(self) -> None:
        from lang_jp import katakana_to_hiragana
        from ocr_backends import OcrLineBox, ocr_line_boxes_to_blocks

        def column(text: str, left: float, right: float) -> OcrLineBox:
            return OcrLineBox(
                text=text, confidence=0.9, left=left, top=0, right=right, bottom=60 * len(text), vertical=True
            )

        blocks = ocr_line_boxes_to_blocks([column("そこの剣を", 1168, 1218), column("取ってくれ", 1087, 1144)])
        lines = analyze_lines(blocks, candidates=("ja",))
        self.assertEqual(lines[0].text, "そこの剣を取ってくれ")
        self.assertTrue(lines[0].vertical)
        by_surface = {
            a.surface: a for a in furigana_annotations(lines, to_hiragana=katakana_to_hiragana)
        }
        sword, take = by_surface["剣"], by_surface["取っ"]
        self.assertTrue(sword.vertical)
        # 剣 is the 4th of 5 cells in the right column; 取 heads the left one.
        self.assertEqual((sword.left, sword.top, sword.right, sword.bottom), (1168, 180, 1218, 240))
        self.assertEqual((take.left, take.top, take.right), (1087, 0, 1144))
        self.assertEqual(take.text, "と")
        self.assertEqual(sword.thickness, 50)

    def test_words_skip_lines_in_the_target_language(self) -> None:
        lines = analyze_lines(
            [OcrBlock(text="Salvar o jogo agora", confidence=0.9, bbox=box(0, 0, 300, 30))],
            candidates=("pt", "en"),
        )
        gloss = FakeTranslator()
        self.assertEqual(word_annotations(lines, gloss=gloss, target_language="pt"), [])
        self.assertEqual(gloss.calls, [])


class ScaleTests(unittest.TestCase):
    def test_zoom_steps_clamp_and_reset(self) -> None:
        self.assertEqual(zoom_scale(1.0, 1), 1.15)
        self.assertEqual(zoom_scale(1.0, -1), 0.87)
        self.assertEqual(zoom_scale(2.9, 1), 3.0)
        self.assertEqual(zoom_scale(0.5, -1), 0.5)
        self.assertEqual(zoom_scale(2.2, 0), 1.0)

    def test_fonts_follow_the_scale(self) -> None:
        self.assertEqual(font_px_for(50, 1.0), 31)
        self.assertEqual(font_px_for(50, 2.0), 62)
        self.assertEqual(font_px_for(400, 2.0), 2 * MAX_FONT_PX)
        self.assertEqual(small_font_px_for(50, 1.0), 21)
        self.assertEqual(small_font_px_for(50, 1.5), 31)
        self.assertEqual(small_font_px_for(5, 0.5), MIN_FONT_PX)

    def test_scale_is_saved_and_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.toml"
            save_overlay_state(path, OverlayState(scale=1.32))
            self.assertEqual(load_overlay_state(path).scale, 1.32)
            path.write_text("scale = 9\n", encoding="utf-8")
            self.assertEqual(load_overlay_state(path).scale, 3.0)
            path.write_text("scale = true\n", encoding="utf-8")
            self.assertEqual(load_overlay_state(path).scale, 1.0)

    def test_wheel_direction(self) -> None:
        self.assertEqual(wheel_event(120 << 16), "zoom_in")
        self.assertEqual(wheel_event((0x10000 - 120) << 16), "zoom_out")
        self.assertIsNone(wheel_event(0))


class EnglishTargetTests(unittest.TestCase):
    def test_english_is_the_default_target_and_english_lines_are_left_alone(self) -> None:
        from translate_overlay import DEFAULT_TARGET_LANGUAGE

        self.assertEqual(DEFAULT_TARGET_LANGUAGE, "en")
        translate = FakeTranslator()
        items = build_overlay_items(
            [OcrBlock(text="Press any button to continue", confidence=0.9, bbox=box(0, 0, 300, 30))],
            candidates=("en", "ja"),
            translate=translate,
        )
        self.assertEqual(items, [])
        self.assertEqual(translate.calls, [])


class TranslationCacheTests(unittest.TestCase):
    def test_only_unseen_lines_reach_the_translator(self) -> None:
        translate = FakeTranslator()
        cache = TranslationCache(translate)
        self.assertEqual(cache(["Start"], "en"), ["<Start>"])
        self.assertEqual(cache(["Start", "Quit", "Quit"], "en"), ["<Start>", "<Quit>", "<Quit>"])
        self.assertEqual(cache(["Start"], "fr"), ["<Start>"])
        self.assertEqual(translate.calls, [(("Start",), "en"), (("Quit",), "en"), (("Start",), "fr")])

    def test_failures_are_reported_and_not_cached(self) -> None:
        translate = FakeTranslator(fail_on={"Start"})
        cache = TranslationCache(translate)
        self.assertEqual(cache(["Start"], "en"), [None])
        self.assertIsInstance(cache.last_error, OSError)
        translate.fail_on = set()
        self.assertEqual(cache(["Start"], "en"), ["<Start>"])


class BatchByNewlinesTests(unittest.TestCase):
    def test_lines_go_in_one_request_and_split_back(self) -> None:
        requests: list[str] = []

        def translate_text(text: str, code: str) -> str:
            requests.append(text)
            return "\n".join(f"pt:{line}" for line in text.split("\n"))

        batch = batch_by_newlines(translate_text)
        self.assertEqual(batch(["Load  game", "Quit"], "en"), ["pt:Load game", "pt:Quit"])
        self.assertEqual(requests, ["Load game\nQuit"])

    def test_falls_back_to_line_by_line_when_lines_merge(self) -> None:
        requests: list[str] = []

        def translate_text(text: str, code: str) -> str:
            requests.append(text)
            return text.replace("\n", " ").upper()

        batch = batch_by_newlines(translate_text)
        self.assertEqual(batch(["a b", "c"], "en"), ["A B", "C"])
        self.assertEqual(requests, ["a b\nc", "a b", "c"])


class HotkeyAndFontTests(unittest.TestCase):
    def test_parse_hotkey(self) -> None:
        self.assertEqual(parse_hotkey("Ctrl+Alt+T"), (MOD_CONTROL | MOD_ALT, ord("T")))
        self.assertEqual(parse_hotkey("shift + f9"), (MOD_SHIFT, 0x78))

    def test_parse_hotkey_rejects_bad_input(self) -> None:
        for raw in ("T", "Ctrl+", "Ctrl+A+B", "Ctrl+PageUp", ""):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_hotkey(raw)

    def test_font_follows_line_height_within_bounds(self) -> None:
        self.assertEqual(font_px_for(5), MIN_FONT_PX)
        self.assertEqual(font_px_for(50), 31)
        self.assertEqual(font_px_for(400), MAX_FONT_PX)


if __name__ == "__main__":
    unittest.main()
