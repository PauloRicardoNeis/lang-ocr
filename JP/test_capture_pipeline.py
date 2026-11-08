from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import capture_worker
import lang_jp
from lang_jp import SCHEMA_PATH, db_connect
from ocr_backends import OcrBlock, OcrEnginePlan, plausible_reading, recognize_with_shared_detection

_LOG_DIR = tempfile.TemporaryDirectory()
_LOG_PATCH = patch.object(capture_worker, "DEFAULT_LOG_PATH", Path(_LOG_DIR.name) / "worker.log")


def setUpModule() -> None:
    # The worker logs every analyzer and job step; keep that out of the real
    # data/capture_daemon.log.
    _LOG_PATCH.start()


def tearDownModule() -> None:
    _LOG_PATCH.stop()
    _LOG_DIR.cleanup()


class StubReader:
    """Stands in for ``EasyOcrBackend``: fixed readings per box index and language group."""

    def __init__(self, languages_list, readings: dict[int, tuple[str, float]], detected: list):
        self.languages = tuple(languages_list)
        self._readings = readings
        self._detected = detected
        self.recognized_boxes: list[list] = []

    def detect_boxes(self, color_image):
        return list(self._detected), []

    def recognize_boxes(self, grey_image, horizontal_list, free_list):
        self.recognized_boxes.append(list(horizontal_list))
        blocks = []
        for box in horizontal_list:
            index = self._detected.index(box)
            text, confidence = self._readings.get(index, ("???", 0.1))
            blocks.append(OcrBlock(text=text, confidence=confidence, bbox=None))
        return blocks


BOXES = [[10, 200, 10, 40], [10, 200, 60, 90], [10, 200, 110, 140]]  # x0, x1, y0, y1


class SharedDetectionTest(unittest.TestCase):
    def test_detection_runs_once_and_confident_boxes_skip_later_readers(self) -> None:
        latin = StubReader(("en", "es"), {0: ("Press START", 0.95), 1: ("ERJ RO", 0.3), 2: ("Options", 0.8)}, BOXES)
        korean = StubReader(("ko", "en"), {0: ("Press START", 0.6), 1: ("안녕하세요", 0.92), 2: ("Options", 0.5)}, BOXES)

        blocks = recognize_with_shared_detection([latin, korean], color_image=None, grey_image=None, confident=0.7)

        # The Korean reader only saw the box the Latin reader could not read well.
        self.assertEqual(korean.recognized_boxes, [[BOXES[1]]])
        self.assertEqual([block.text for block in blocks], ["Press START", "안녕하세요", "Options"])
        # Boxes come back with the detector rectangle when the reader gave none.
        self.assertEqual(blocks[1].bbox, [[10.0, 60.0], [200.0, 60.0], [200.0, 90.0], [10.0, 90.0]])

    def test_better_reading_replaces_a_weak_one(self) -> None:
        first = StubReader(("ru", "en"), {0: ("Пpивет", 0.45)}, BOXES[:1])
        second = StubReader(("en",), {0: ("Privet", 0.5)}, BOXES[:1])
        blocks = recognize_with_shared_detection([first, second], None, None, confident=0.7)
        self.assertEqual([(block.text, block.confidence) for block in blocks], [("Privet", 0.5)])

    def test_lookalike_readings_from_other_scripts_do_not_replace_latin(self) -> None:
        # Real misreadings of an English screen by the Cyrillic and Devanagari readers.
        latin = StubReader(
            ("en",), {0: ("x 1189", 0.67), 1: ("Resets at 2.00 PM", 0.62), 2: ("Привет мир", 0.3)}, BOXES
        )
        cyrillic = StubReader(
            ("ru", "en"), {0: ("х 1189", 0.82), 1: ("Resets at 2.00 РM", 0.7), 2: ("Привет мир", 0.9)}, BOXES
        )
        devanagari = StubReader(("hi", "en"), {1: ("Resets at २:०० PM", 0.72)}, BOXES)
        blocks = recognize_with_shared_detection([latin, cyrillic, devanagari], None, None, confident=0.7)
        self.assertEqual([block.text for block in blocks], ["x 1189", "Resets at 2.00 PM", "Привет мир"])

    def test_plausible_reading(self) -> None:
        for text in ("Press START", "Привет мир", "안녕하세요 Press START", "是", "PS5版", "Ελληνικά"):
            with self.subTest(text=text):
                self.assertTrue(plausible_reading(text))
        for text in ("х 1189", "2.00 РM", "२:०० PM", "٥٥٥", "Сс"[:1]):
            with self.subTest(text=text):
                self.assertFalse(plausible_reading(text))


class StubEngine:
    def __init__(self, name: str, blocks: list[OcrBlock]) -> None:
        self.engine_name = name
        self._blocks = blocks
        self.calls = 0

    def extract_blocks(self, image_path, *, prepared=False, progress_label=None, echo_stdout=False):
        self.calls += 1
        return list(self._blocks)


def block(text: str, left: float, top: float, right: float, bottom: float, conf: float = 0.9) -> OcrBlock:
    return OcrBlock(text=text, confidence=conf, bbox=[[left, top], [right, top], [right, bottom], [left, bottom]])


class MultiEngineAdapterTest(unittest.TestCase):
    def test_meikiocr_and_easyocr_blocks_are_merged_in_reading_order(self) -> None:
        plan = OcrEnginePlan(
            language_codes=("ja", "en"),
            use_meikiocr=True,
            easyocr_groups=(("en",),),
            unsupported=(),
            backend="auto",
        )
        adapter = capture_worker.MultiEngineOcrAdapter.__new__(capture_worker.MultiEngineOcrAdapter)
        adapter.plan = plan
        adapter.enabled_languages = ("ja", "en")
        meiki = StubEngine("meikiocr", [block("勇者の墓は", 100, 200, 400, 240)])
        adapter.engines = [meiki]
        easy_blocks = [block("勇者の墓は", 102, 198, 398, 242), block("Press START", 100, 50, 300, 80)]
        with patch.object(
            capture_worker.MultiEngineOcrAdapter, "_read_easyocr_groups", return_value=easy_blocks
        ):
            adapter.engines.append(capture_worker.EasyOcrGroupAdapter.__new__(capture_worker.EasyOcrGroupAdapter))
            merged = adapter.extract_blocks(Path("ignored.png"))
        self.assertEqual([item.text for item in merged], ["Press START", "勇者の墓は"])
        self.assertEqual(meiki.calls, 1)

    def test_build_adapter_picks_meikiocr_alone_for_japanese_only(self) -> None:
        with patch.object(capture_worker, "MeikiOcrAdapter") as meiki, patch.object(
            capture_worker, "MultiEngineOcrAdapter"
        ) as multi:
            capture_worker.build_capture_ocr_adapter("auto", enabled_languages=("ja",))
            self.assertEqual(meiki.call_count, 1)
            self.assertEqual(multi.call_count, 0)
            capture_worker.build_capture_ocr_adapter("auto", enabled_languages=("ja", "es"))
            self.assertEqual(multi.call_count, 1)
            plan = multi.call_args.args[0]
            self.assertTrue(plan.use_meikiocr)
            self.assertEqual(plan.easyocr_groups, (("es",),))


class DominantLanguageTest(unittest.TestCase):
    def test_cjk_characters_outweigh_latin_ui_labels(self) -> None:
        analyzed = [
            [({"language_code": "ja", "raw_text": "勇者の墓はここにある"}, [])],
            [({"language_code": "en", "raw_text": "Press START to continue"}, [])],
        ]
        code, share = capture_worker.dominant_block_language(analyzed, default="en")
        self.assertEqual(code, "ja")
        self.assertGreater(share, 0.5)
        korean = [[({"language_code": "ko", "raw_text": "안녕하세요"}, [])], [({"language_code": "en", "raw_text": "OK"}, [])]]
        self.assertEqual(capture_worker.dominant_block_language(korean, default="en")[0], "ko")


class PipelineCacheTest(unittest.TestCase):
    def test_pipeline_reuses_adapters_and_analyzers_per_language_set(self) -> None:
        with patch.object(capture_worker, "build_capture_ocr_adapter", side_effect=lambda name, enabled_languages: object()) as build:
            pipeline = capture_worker.CapturePipeline()
            first = pipeline.ocr_for(("ja", "en"))
            second = pipeline.ocr_for(("ja", "en"))
            third = pipeline.ocr_for(("ko",))
            analyzer_a = pipeline.analyzer_for(("ko", "en"))
            analyzer_b = pipeline.analyzer_for(("ko", "en"))
            analyzer_forced = pipeline.analyzer_for(("ko",), forced_language="ko")
        self.assertIs(first, second)
        self.assertIsNot(first, third)
        self.assertEqual(build.call_count, 2)
        self.assertIs(analyzer_a, analyzer_b)
        self.assertEqual(analyzer_forced.forced_language, "ko")


class ProcessJobTest(unittest.TestCase):
    """The real worker loop with OCR replaced by a stub: window metadata, profiles, languages."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "worker.sqlite3"
        self._auto_translate = lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = False
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()
        self._capture_dir = capture_worker.DEFAULT_CAPTURE_DIR
        capture_worker.DEFAULT_CAPTURE_DIR = self.root / "captures"

    def tearDown(self) -> None:
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = self._auto_translate
        capture_worker.DEFAULT_CAPTURE_DIR = self._capture_dir
        self.temp_dir.cleanup()

    def enqueue(self, connection, name: str, *, window_title: str | None, process_name: str | None) -> int:
        image = self.root / f"{name}.png"
        from PIL import Image

        Image.new("RGB", (64, 32), (255, 255, 255)).save(image)
        return lang_jp.create_capture_job(
            connection,
            captured_at="2026-10-24T10:00:00-03:00",
            temp_image_path=str(image),
            image_sha256=f"sha-{name}",
            image_phash=None,
            window_title=window_title,
            process_name=process_name,
        )

    def test_profile_pins_language_and_window_title_becomes_source_title(self) -> None:
        connection = db_connect(self.db_path)
        try:
            lang_jp.set_capture_languages(connection, ["ja", "en"])
            lang_jp.upsert_capture_profile(
                connection, name="Spanish RPG", language_codes="es", window_title_pattern="dragon quest"
            )
            job_id = self.enqueue(connection, "dq", window_title="DRAGON QUEST XI", process_name="dq11.exe")
            connection.commit()
        finally:
            connection.close()

        stub = StubEngine("stub", [block("No puede ser", 10, 10, 200, 40, conf=0.9)])
        pipeline = capture_worker.CapturePipeline()
        with patch.object(capture_worker, "build_capture_ocr_adapter", return_value=stub):
            processed = capture_worker.process_job_once(self.db_path, translator=None, pipeline=pipeline)
        self.assertTrue(processed)

        connection = db_connect(self.db_path)
        try:
            job = connection.execute("SELECT * FROM capture_jobs WHERE id = ?", (job_id,)).fetchone()
            source = connection.execute("SELECT * FROM sources WHERE capture_job_id = ?", (job_id,)).fetchone()
            segments = connection.execute(
                "SELECT language_code FROM source_analysis_segments WHERE source_id = ?", (source["id"],)
            ).fetchall()
            terms = connection.execute("SELECT language_code, lemma FROM terms ORDER BY lemma").fetchall()
        finally:
            connection.close()
        self.assertEqual(job["status"], "accepted")
        self.assertIsNotNone(job["capture_profile_id"])
        self.assertEqual(source["language_code"], "es")
        self.assertEqual(source["title"], "DRAGON QUEST XI")
        self.assertEqual(source["game"], "Spanish RPG")
        self.assertEqual([row["language_code"] for row in segments], ["es"])
        self.assertEqual([(row["language_code"], row["lemma"]) for row in terms], [("es", "no"), ("es", "poder"), ("es", "ser")])

    def test_term_meanings_are_translated_without_holding_the_write_lock(self) -> None:
        connection = db_connect(self.db_path)
        try:
            lang_jp.set_capture_languages(connection, ["es"])
            self.enqueue(connection, "menu", window_title="Menu", process_name="game.exe")
            connection.commit()
        finally:
            connection.close()

        def translate(lemma, **_kwargs):
            # The daemon must still be able to enqueue while a meaning is looked up.
            other = sqlite3.connect(self.db_path, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            finally:
                other.close()
            return f"pt:{lemma}"

        stub = StubEngine("stub", [block("No puede ser", 10, 10, 200, 40, conf=0.9)])
        with (
            patch.object(capture_worker, "build_capture_ocr_adapter", return_value=stub),
            patch.object(lang_jp, "auto_translate_term_meaning_pt", side_effect=AssertionError("called inside the transaction")),
            patch.object(capture_worker, "auto_translate_term_meaning_pt", side_effect=translate),
        ):
            capture_worker.process_job_once(self.db_path, translator=None, pipeline=capture_worker.CapturePipeline())
        connection = db_connect(self.db_path)
        try:
            meanings = [row["meaning_pt"] for row in connection.execute("SELECT meaning_pt FROM terms ORDER BY lemma")]
        finally:
            connection.close()
        self.assertEqual(meanings, ["pt:no", "pt:poder", "pt:ser"])

    def test_ocr_misreadings_stay_pending_instead_of_becoming_terms(self) -> None:
        connection = db_connect(self.db_path)
        try:
            lang_jp.set_capture_languages(connection, ["en"])
            self.enqueue(connection, "ui", window_title="Editor", process_name="editor.exe")
            connection.commit()
        finally:
            connection.close()
        stub = StubEngine("stub", [block("The fetch is stuck Frobably", 10, 10, 400, 40, conf=0.9)])
        with patch.object(capture_worker, "build_capture_ocr_adapter", return_value=stub):
            capture_worker.process_job_once(self.db_path, translator=None, pipeline=capture_worker.CapturePipeline())
        connection = db_connect(self.db_path)
        try:
            lemmas = sorted(row["lemma"] for row in connection.execute("SELECT lemma FROM terms"))
            pending = [
                row["surface_form"]
                for row in connection.execute(
                    "SELECT surface_form FROM source_analysis_candidates WHERE approval_status = 'pending'"
                )
            ]
        finally:
            connection.close()
        self.assertNotIn("frobably", lemmas)
        self.assertIn("fetch", lemmas)
        self.assertIn("stick", lemmas)
        self.assertEqual(pending, ["Frobably"])

    def test_nothing_is_skipped_without_skip_rules(self) -> None:
        connection = db_connect(self.db_path)
        try:
            lang_jp.set_capture_languages(connection, ["en"])
            claude = self.enqueue(connection, "claude", window_title="Claude", process_name="claude.exe")
            connection.commit()
        finally:
            connection.close()
        stub = StubEngine("stub", [block("Press START", 10, 10, 200, 40)])
        with patch.object(capture_worker, "build_capture_ocr_adapter", return_value=stub):
            while capture_worker.process_job_once(self.db_path, translator=None, pipeline=capture_worker.CapturePipeline()):
                pass
        connection = db_connect(self.db_path)
        try:
            status = connection.execute("SELECT status FROM capture_jobs WHERE id = ?", (claude,)).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(status, "accepted")
        self.assertEqual(stub.calls, 1)

    def test_windows_matching_skip_rules_are_skipped_before_ocr(self) -> None:
        connection = db_connect(self.db_path)
        try:
            lang_jp.set_capture_languages(connection, ["en"])
            # Rules are stored lower-case: process names match exactly, titles by substring.
            lang_jp.add_capture_skip_rule(connection, "process", "Claude.EXE")
            lang_jp.add_capture_skip_rule(connection, "title", "Lang Catalog")
            self.assertFalse(lang_jp.add_capture_skip_rule(connection, "process", "claude.exe"))
            claude = self.enqueue(connection, "claude", window_title="Claude", process_name="Claude.exe")
            catalog = self.enqueue(connection, "catalog", window_title="Lang Catalog - Brave", process_name="brave.exe")
            # Inner catalog pages, titled through lang_jp_web.page_title().
            sources_page = self.enqueue(
                connection, "sources-page", window_title="Fontes - Lang Catalog - Brave", process_name="brave.exe"
            )
            connection.commit()
        finally:
            connection.close()
        stub = StubEngine("stub", [block("Press START", 10, 10, 200, 40)])
        with patch.object(capture_worker, "build_capture_ocr_adapter", return_value=stub):
            while capture_worker.process_job_once(self.db_path, translator=None, pipeline=capture_worker.CapturePipeline()):
                pass
        connection = db_connect(self.db_path)
        try:
            statuses = [
                row["status"]
                for row in connection.execute(
                    "SELECT status FROM capture_jobs WHERE id IN (?, ?, ?) ORDER BY id",
                    (claude, catalog, sources_page),
                )
            ]
            sources = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(statuses, ["rejected", "rejected", "rejected"])
        self.assertEqual(sources, 0)
        self.assertEqual(stub.calls, 0)

    def test_capture_set_from_database_sorts_blocks_per_language(self) -> None:
        connection = db_connect(self.db_path)
        try:
            lang_jp.set_capture_languages(connection, ["ko", "en"])
            self.enqueue(connection, "mixed", window_title="Some VN", process_name="vn.exe")
            connection.commit()
        finally:
            connection.close()
        stub = StubEngine(
            "stub",
            [block("안녕하세요", 10, 10, 200, 40), block("Press START", 10, 60, 200, 90), block("回復しました", 10, 110, 200, 140)],
        )
        with patch.object(capture_worker, "build_capture_ocr_adapter", return_value=stub):
            capture_worker.process_job_once(self.db_path, translator=None, pipeline=capture_worker.CapturePipeline())
        connection = db_connect(self.db_path)
        try:
            source = connection.execute("SELECT language_code, title FROM sources").fetchone()
            segment_languages = sorted(
                row["language_code"] for row in connection.execute("SELECT language_code FROM source_analysis_segments")
            )
            term_languages = sorted({row["language_code"] for row in connection.execute("SELECT language_code FROM terms")})
            korean = connection.execute("SELECT lemma, reading FROM terms WHERE language_code = 'ko'").fetchone()
        finally:
            connection.close()
        # Japanese is not in the capture set: its block stays visible as a segment
        # tagged ja, but yields no candidates and no terms.
        self.assertEqual(segment_languages, ["en", "ja", "ko"])
        self.assertEqual(term_languages, ["en", "ko"])
        self.assertEqual(source["title"], "Some VN")
        self.assertEqual((korean["lemma"], korean["reading"]), ("안녕하세요", "annyeonghaseyo"))

    def test_screen_only_in_a_language_not_captured_is_rejected(self) -> None:
        connection = db_connect(self.db_path)
        try:
            lang_jp.set_capture_languages(connection, ["en", "es"])
            job_id = self.enqueue(connection, "feed", window_title="Home / X", process_name="chrome.exe")
            connection.commit()
        finally:
            connection.close()
        stub = StubEngine(
            "stub",
            [
                block("O que está acontecendo?", 10, 10, 300, 40),
                block("Mostrar mais", 10, 60, 200, 90),
                block("Seguir", 10, 110, 200, 140),
            ],
        )
        with patch.object(capture_worker, "build_capture_ocr_adapter", return_value=stub):
            capture_worker.process_job_once(self.db_path, translator=None, pipeline=capture_worker.CapturePipeline())
        connection = db_connect(self.db_path)
        try:
            job = connection.execute("SELECT status FROM capture_jobs WHERE id = ?", (job_id,)).fetchone()
            sources = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            terms = connection.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(job["status"], "rejected")
        self.assertEqual((sources, terms), (0, 0))


if __name__ == "__main__":
    unittest.main()
