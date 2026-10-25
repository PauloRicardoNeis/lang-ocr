from __future__ import annotations

import os
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from PIL import Image

from ocr_backends import (
    DEFAULT_CAPTURE_OCR_BACKEND,
    DEFAULT_EASYOCR_LANGUAGES,
    MeikiOcrBackend,
    OcrBlock,
    OcrLineBox,
    clear_backend_cache,
    drop_contained_boxes,
    drop_furigana_boxes,
    filter_ocr_line_boxes,
    get_easyocr_backend,
    is_plausible_japanese_line,
    is_plausible_text_line,
    meikiocr_line_to_box,
    merge_engine_blocks,
    merge_ocr_blocks,
    ocr_line_boxes_to_blocks,
    plan_easyocr_reader_groups,
    plan_ocr_engines,
    resolve_capture_ocr_backend,
    resolve_easyocr_languages,
    resolve_easyocr_use_gpu,
    scale_ocr_blocks,
)


def box(text: str, left: float, top: float, right: float, bottom: float, conf: float = 0.9) -> OcrLineBox:
    return OcrLineBox(text=text, confidence=conf, left=left, top=top, right=right, bottom=bottom)


def meiki_char(char: str, left: int, conf: float, *, top: int = 100, size: int = 40) -> dict:
    return {"char": char, "bbox": [left, top, left + size, top + size], "conf": conf}


# A 40 px dialogue line with 16 px ruby sitting just above its first two kanji.
BODY_LINE = box("勇者の墓は", 100, 200, 400, 240)
RUBY_ABOVE_BODY = box("ゆうしゃ", 100, 180, 160, 196)


class EasyOcrConfigTest(unittest.TestCase):
    def test_auto_gpu_uses_torch_visibility(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("ocr_backends.torch_cuda_is_available", return_value=True):
                self.assertTrue(resolve_easyocr_use_gpu())
            with patch("ocr_backends.torch_cuda_is_available", return_value=False):
                self.assertFalse(resolve_easyocr_use_gpu())

    def test_explicit_gpu_override_enables_gpu(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_EASYOCR_GPU": "1"}, clear=True):
            with patch("ocr_backends.torch_cuda_is_available", return_value=False):
                self.assertTrue(resolve_easyocr_use_gpu())

    def test_explicit_cpu_override_disables_gpu(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_EASYOCR_GPU": "false"}, clear=True):
            with patch("ocr_backends.torch_cuda_is_available", return_value=True):
                self.assertFalse(resolve_easyocr_use_gpu())

    def test_languages_default_when_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_easyocr_languages(), DEFAULT_EASYOCR_LANGUAGES)

    def test_languages_can_be_overridden(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_EASYOCR_LANGS": "ja"}, clear=True):
            self.assertEqual(resolve_easyocr_languages(), ("ja",))

    def test_empty_languages_fall_back_to_default(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_EASYOCR_LANGS": " , "}, clear=True):
            self.assertEqual(resolve_easyocr_languages(), DEFAULT_EASYOCR_LANGUAGES)


class CaptureOcrBackendConfigTest(unittest.TestCase):
    def test_defaults_to_auto_routing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_capture_ocr_backend(), "auto")
        self.assertEqual(DEFAULT_CAPTURE_OCR_BACKEND, "auto")

    def test_env_selects_easyocr(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_OCR_BACKEND": " EasyOCR "}, clear=True):
            self.assertEqual(resolve_capture_ocr_backend(), "easyocr")

    def test_blank_env_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_OCR_BACKEND": "  "}, clear=True):
            self.assertEqual(resolve_capture_ocr_backend(), "auto")

    def test_unknown_backend_is_rejected(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_OCR_BACKEND": "paddle"}, clear=True):
            with self.assertRaises(ValueError):
                resolve_capture_ocr_backend()


class EngineRoutingTest(unittest.TestCase):
    def test_japanese_only_uses_meikiocr_alone(self) -> None:
        plan = plan_ocr_engines(("ja",), backend="auto")
        self.assertTrue(plan.use_meikiocr)
        self.assertEqual(plan.easyocr_groups, ())
        self.assertEqual(plan.engine_names, ("meikiocr",))
        self.assertFalse(plan.multi_engine)

    def test_japanese_plus_latin_adds_one_latin_reader(self) -> None:
        plan = plan_ocr_engines(("ja", "en", "es", "pt-BR"), backend="auto")
        self.assertTrue(plan.use_meikiocr)
        self.assertEqual(plan.easyocr_groups, (("en", "es", "pt"),))
        self.assertTrue(plan.multi_engine)

    def test_cjk_and_script_groups_each_get_their_own_reader_paired_with_english(self) -> None:
        groups = plan_easyocr_reader_groups(("ja", "en", "ko", "zh", "th", "ru", "uk", "ar", "hi", "es"))
        self.assertEqual(
            groups,
            (
                ("en", "es"),
                ("ar", "en"),
                ("ru", "uk", "en"),
                ("hi", "en"),
                ("ko", "en"),
                ("ch_sim", "en"),
                ("th", "en"),
            ),
        )

    def test_without_english_exclusive_packs_stay_alone(self) -> None:
        self.assertEqual(plan_easyocr_reader_groups(("ko", "zh")), (("ko",), ("ch_sim",)))

    def test_unsupported_languages_are_reported_not_dropped_silently(self) -> None:
        plan = plan_ocr_engines(("ja", "el", "he", "ko"), backend="auto")
        self.assertEqual(plan.unsupported, ("el", "he"))
        self.assertEqual(plan.easyocr_groups, (("ko",),))

    def test_no_japanese_means_no_meikiocr(self) -> None:
        plan = plan_ocr_engines(("es", "en"), backend="auto")
        self.assertFalse(plan.use_meikiocr)
        self.assertEqual(plan.engine_names, ("easyocr:es+en",))

    def test_forced_backends_keep_the_old_single_engine_behaviour(self) -> None:
        meiki = plan_ocr_engines(("ja", "en"), backend="meikiocr")
        self.assertEqual(meiki.engine_names, ("meikiocr",))
        self.assertEqual(meiki.unsupported, ("en",))
        easy = plan_ocr_engines(("ja", "en"), backend="easyocr")
        self.assertFalse(easy.use_meikiocr)
        self.assertEqual(easy.easyocr_groups, (("en",), ("ja", "en")))

    def test_reader_cache_builds_each_group_once(self) -> None:
        built: list[tuple[str, ...]] = []

        class StubBackend:
            def __init__(self, languages_list):
                built.append(tuple(languages_list))

        clear_backend_cache()
        with patch("ocr_backends.EasyOcrBackend", StubBackend):
            first = get_easyocr_backend(("en", "es"))
            second = get_easyocr_backend(("en", "es"))
            get_easyocr_backend(("ko", "en"))
        clear_backend_cache()
        self.assertIs(first, second)
        self.assertEqual(built, [("en", "es"), ("ko", "en")])


class LanguageAwareLineFilterTest(unittest.TestCase):
    def test_japanese_set_keeps_the_benchmark_rule(self) -> None:
        self.assertTrue(is_plausible_text_line("勇者の墓は", ("ja",)))
        self.assertFalse(is_plausible_text_line("Steam Library あい", ("ja",)))

    def test_latin_languages_accept_latin_lines_and_reject_icons(self) -> None:
        self.assertTrue(is_plausible_text_line("Press START to continue", ("es", "en")))
        self.assertTrue(is_plausible_text_line("¿Nos ha traicionado?", ("es",)))
        self.assertFalse(is_plausible_text_line("12:45", ("es", "en")))
        self.assertFalse(is_plausible_text_line("안녕하세요", ("es", "en")))

    def test_mixed_sets_accept_every_enabled_script(self) -> None:
        codes = ("ja", "en", "ko")
        self.assertTrue(is_plausible_text_line("안녕하세요", codes))
        self.assertTrue(is_plausible_text_line("勇者の墓は", codes))
        self.assertTrue(is_plausible_text_line("Options", codes))
        self.assertFalse(is_plausible_text_line("Привет", codes))

    def test_filter_pipeline_only_drops_furigana_for_japanese(self) -> None:
        latin = box("Press START", 600, 10, 900, 40)
        kept, stats = filter_ocr_line_boxes(
            [latin, RUBY_ABOVE_BODY, BODY_LINE], language_codes=("ja", "en")
        )
        self.assertEqual(kept, [latin, BODY_LINE])
        self.assertEqual(stats["furigana"], 1)
        self.assertEqual(stats["off_language"], 0)
        kept_ko, stats_ko = filter_ocr_line_boxes(
            [latin, RUBY_ABOVE_BODY, BODY_LINE], language_codes=("ko", "en")
        )
        self.assertEqual(kept_ko, [latin])
        self.assertEqual(stats_ko["off_language"], 2)
        self.assertEqual(stats_ko["furigana"], 0)


class BlockMergeTest(unittest.TestCase):
    @staticmethod
    def block(text: str, left: float, top: float, right: float, bottom: float, conf: float = 0.9) -> OcrBlock:
        return OcrBlock(
            text=text,
            confidence=conf,
            bbox=[[left, top], [right, top], [right, bottom], [left, bottom]],
        )

    def test_overlapping_boxes_from_two_engines_collapse_into_the_primary_block(self) -> None:
        meiki = [self.block("勇者の墓は", 100, 200, 400, 240)]
        easy = [
            self.block("勇者の墓は", 102, 198, 398, 242),  # same line, read again
            self.block("Press START", 100, 400, 300, 430),  # Latin UI label only EasyOCR saw
        ]
        merged = merge_ocr_blocks(meiki, easy)
        self.assertEqual([block.text for block in merged], ["勇者の墓は", "Press START"])

    def test_latin_garbage_over_japanese_text_is_dropped_by_overlap_alone(self) -> None:
        meiki = [self.block("勇者の墓は", 100, 200, 400, 240)]
        easy = [self.block("ERJ RO E", 110, 205, 390, 238)]
        self.assertEqual([block.text for block in merge_ocr_blocks(meiki, easy)], ["勇者の墓は"])

    def test_weak_overlap_needs_similar_text(self) -> None:
        primary = [self.block("Continue", 100, 100, 300, 140)]
        similar = [self.block("Continu e", 250, 100, 460, 140)]  # 25% overlap, same words
        different = [self.block("Options", 250, 100, 460, 140)]
        self.assertEqual(len(merge_ocr_blocks(primary, similar)), 1)
        self.assertEqual(len(merge_ocr_blocks(primary, different)), 2)

    def test_merge_engine_blocks_keeps_reading_order(self) -> None:
        merged = merge_engine_blocks(
            [
                [self.block("二行目", 100, 300, 300, 340)],
                [self.block("First", 100, 100, 300, 140), self.block("二行目", 101, 301, 299, 339)],
            ]
        )
        self.assertEqual([block.text for block in merged], ["First", "二行目"])

    def test_scale_ocr_blocks_maps_downscaled_boxes_back(self) -> None:
        scaled = scale_ocr_blocks([self.block("x", 10, 20, 30, 40)], 2.0)
        self.assertEqual(scaled[0].bbox, [[20, 40], [60, 40], [60, 80], [20, 80]])


class CharConfidenceFilterTest(unittest.TestCase):
    def test_low_confidence_chars_are_dropped_and_box_shrinks(self) -> None:
        line = {
            "text": "、竜王」",
            "chars": [
                meiki_char("、", 0, 0.15),
                meiki_char("竜", 40, 0.95),
                meiki_char("王", 80, 0.8),
                meiki_char("」", 120, 0.2),
            ],
        }
        result = meikiocr_line_to_box(line, min_char_confidence=0.3)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "竜王")
        self.assertAlmostEqual(result.confidence, 0.8)
        self.assertEqual((result.left, result.top, result.right, result.bottom), (40, 100, 120, 140))

    def test_threshold_is_inclusive(self) -> None:
        line = {"chars": [meiki_char("城", 0, 0.3)]}
        result = meikiocr_line_to_box(line, min_char_confidence=0.3)
        assert result is not None
        self.assertEqual(result.text, "城")

    def test_line_with_only_low_confidence_chars_is_dropped(self) -> None:
        line = {"chars": [meiki_char("。", 0, 0.1), meiki_char("、", 40, 0.29)]}
        self.assertIsNone(meikiocr_line_to_box(line, min_char_confidence=0.3))
        self.assertIsNone(meikiocr_line_to_box({"text": "", "chars": []}))


class PlausibleJapaneseLineTest(unittest.TestCase):
    def test_accepts_japanese_lines(self) -> None:
        self.assertTrue(is_plausible_japanese_line("竜王が棲む城だ。"))
        self.assertTrue(is_plausible_japanese_line("ラルス16世"))

    def test_rejects_single_japanese_char(self) -> None:
        # Glyph-shaped desktop noise such as 川 / 口 / 日.
        self.assertFalse(is_plausible_japanese_line("川"))
        self.assertFalse(is_plausible_japanese_line("日 12:30"))

    def test_rejects_mostly_latin_lines(self) -> None:
        # 2 of 12 visible chars are Japanese: 17% < 30%.
        self.assertFalse(is_plausible_japanese_line("Codex あい build"))
        # 2 of 5 visible chars: 40% >= 30%.
        self.assertTrue(is_plausible_japanese_line("HP あい 9"))

    def test_rejects_punctuation_only(self) -> None:
        self.assertFalse(is_plausible_japanese_line("「。」、"))


class ContainedBoxFilterTest(unittest.TestCase):
    def test_box_inside_larger_box_is_dropped(self) -> None:
        outer = box("竜王が棲む城だ。", 100, 100, 500, 140)
        duplicate = box("竜王が", 100, 102, 220, 138)
        self.assertEqual(drop_contained_boxes([duplicate, outer]), [outer])

    def test_partial_overlap_below_threshold_is_kept(self) -> None:
        outer = box("竜王が棲む城だ。", 100, 100, 500, 140)
        # 200x40 box with a 50x40 overlap: 25% covered.
        neighbor = box("ぜひ会って", 450, 100, 650, 140)
        self.assertEqual(drop_contained_boxes([outer, neighbor]), [outer, neighbor])

    def test_mostly_covered_box_is_dropped(self) -> None:
        outer = box("竜王が棲む城だ。", 100, 100, 500, 140)
        # 100x40 box, 75x40 inside the outer box: 75% covered.
        edge = box("城だ。", 425, 100, 525, 140)
        self.assertEqual(drop_contained_boxes([outer, edge]), [outer])

    def test_equal_size_boxes_are_not_compared(self) -> None:
        first = box("竜王", 100, 100, 200, 140)
        second = box("竜王", 100, 100, 200, 140)
        self.assertEqual(len(drop_contained_boxes([first, second])), 2)


class FuriganaFilterTest(unittest.TestCase):
    def test_kana_ruby_above_kanji_line_is_dropped(self) -> None:
        self.assertEqual(drop_furigana_boxes([RUBY_ABOVE_BODY, BODY_LINE]), [BODY_LINE])

    def test_full_height_kana_line_above_is_kept(self) -> None:
        # Same height as the kanji line: dialogue, not ruby.
        kana_line = box("そうだね", 100, 158, 260, 198)
        self.assertEqual(drop_furigana_boxes([kana_line, BODY_LINE]), [kana_line, BODY_LINE])

    def test_small_kana_far_above_is_kept(self) -> None:
        distant = box("ゆうしゃ", 100, 100, 160, 116)
        self.assertEqual(drop_furigana_boxes([distant, BODY_LINE]), [distant, BODY_LINE])

    def test_small_kana_below_is_kept(self) -> None:
        below = box("ゆうしゃ", 100, 245, 160, 261)
        self.assertEqual(drop_furigana_boxes([BODY_LINE, below]), [BODY_LINE, below])

    def test_small_kana_without_horizontal_overlap_is_kept(self) -> None:
        aside = box("ゆうしゃ", 500, 180, 560, 196)
        self.assertEqual(drop_furigana_boxes([aside, BODY_LINE]), [aside, BODY_LINE])

    def test_small_box_with_kanji_is_kept(self) -> None:
        small_kanji = box("勇者", 100, 180, 160, 196)
        self.assertEqual(drop_furigana_boxes([small_kanji, BODY_LINE]), [small_kanji, BODY_LINE])

    def test_kana_above_kana_only_line_is_kept(self) -> None:
        kana_body = box("ここはどこ", 100, 200, 400, 240)
        self.assertEqual(drop_furigana_boxes([RUBY_ABOVE_BODY, kana_body]), [RUBY_ABOVE_BODY, kana_body])


class LineBoxPipelineTest(unittest.TestCase):
    def test_filters_run_in_order_and_report_stats(self) -> None:
        noise = box("口", 10, 10, 30, 30)
        latin = box("Steam Library あい", 600, 10, 900, 40)
        duplicate = box("勇者の", 100, 202, 220, 238)
        kept, stats = filter_ocr_line_boxes([noise, latin, RUBY_ABOVE_BODY, duplicate, BODY_LINE])
        self.assertEqual(kept, [BODY_LINE])
        self.assertEqual(
            stats,
            {"detected": 5, "kept": 1, "off_language": 2, "contained": 1, "furigana": 1},
        )

    def test_rows_are_joined_in_reading_order(self) -> None:
        second_line = box("本番はこれからだぞ。", 100, 260, 460, 300)
        right = box("はい", 400, 100, 480, 140, conf=0.7)
        left = box("いいえ", 100, 102, 220, 142, conf=0.9)
        blocks = ocr_line_boxes_to_blocks([second_line, right, left])
        self.assertEqual([block.text for block in blocks], ["いいえ はい", "本番はこれからだぞ。"])
        self.assertAlmostEqual(blocks[0].confidence, 0.7)
        self.assertEqual(blocks[0].bbox, [[100, 100], [480, 100], [480, 142], [100, 142]])


class MeikiOcrBackendTest(unittest.TestCase):
    @unittest.skipUnless(find_spec("numpy") is not None, "numpy is only installed in the OCR environment")
    def test_read_blocks_filters_engine_output_without_loading_models(self) -> None:
        received: list[tuple[int, ...]] = []

        class StubEngine:
            def run_ocr(self, image):
                received.append((image.shape, tuple(int(value) for value in image[0, 0])))
                return [
                    {"chars": [meiki_char("ゆ", 100, 0.9, top=180, size=16), meiki_char("う", 116, 0.9, top=180, size=16)]},
                    {"chars": [meiki_char("勇", 100, 0.95, top=200), meiki_char("者", 140, 0.9, top=200), meiki_char("。", 180, 0.1, top=200)]},
                    {"chars": [meiki_char("川", 900, 0.99, top=10)]},
                ]

        backend = MeikiOcrBackend.__new__(MeikiOcrBackend)
        backend.min_char_confidence = 0.3
        backend._engine = StubEngine()
        image = Image.new("RGB", (1000, 400), (255, 0, 0))

        blocks = backend.read_blocks(image)

        self.assertEqual([block.text for block in blocks], ["勇者"])
        self.assertEqual(blocks[0].bbox, [[100, 200], [180, 200], [180, 240], [100, 240]])
        # meikiocr expects OpenCV-style BGR input.
        self.assertEqual(received, [((400, 1000, 3), (0, 0, 255))])


if __name__ == "__main__":
    unittest.main()
