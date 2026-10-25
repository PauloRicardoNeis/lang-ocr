"""End-to-end capture test: synthetic screenshots -> real OCR -> terms per language.

Integration test: it needs the OCR environment (``.venv-capture312`` with
``meikiocr`` and ``easyocr``, models already downloaded) and the Windows fonts
used to render the screenshots. It is skipped everywhere else, so the regular
suite stays fast and offline.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import capture_worker
import lang_jp
from lang_jp import SCHEMA_PATH, db_connect

FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
LATIN_FONT = FONTS / "arial.ttf"
KOREAN_FONT = FONTS / "malgun.ttf"
JAPANESE_FONTS = (FONTS / "YuGothM.ttc", FONTS / "msgothic.ttc", FONTS / "meiryo.ttc")

OCR_ENVIRONMENT_PRESENT = (
    find_spec("easyocr") is not None
    and find_spec("meikiocr") is not None
    and find_spec("numpy") is not None
    and os.environ.get("LANG_JP_SKIP_OCR_INTEGRATION") != "1"
)


def japanese_font() -> Path | None:
    for candidate in JAPANESE_FONTS:
        if candidate.exists():
            return candidate
    return None


FONTS_PRESENT = LATIN_FONT.exists() and KOREAN_FONT.exists() and japanese_font() is not None


def render_screenshot(path: Path, lines: list[tuple[str, Path]], *, size=(1280, 720)) -> None:
    """A dark game-like screen with large white text lines, one font per line."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", size, (18, 22, 30))
    draw = ImageDraw.Draw(image)
    y = 120
    for text, font_path in lines:
        font = ImageFont.truetype(str(font_path), 54)
        draw.text((90, y), text, font=font, fill=(245, 245, 240))
        y += 120
    image.save(path)


@unittest.skipUnless(OCR_ENVIRONMENT_PRESENT, "OCR environment (easyocr + meikiocr) is not present")
@unittest.skipUnless(FONTS_PRESENT, "Windows fonts for Latin, Korean and Japanese text are not present")
class CaptureIntegrationTest(unittest.TestCase):
    """Runs the real worker pipeline (OCR routing, merging, analysis, promotion)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.db_path = cls.root / "integration.sqlite3"
        cls._auto_translate = lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = False
        cls._capture_dir = capture_worker.DEFAULT_CAPTURE_DIR
        cls._log_path = capture_worker.DEFAULT_LOG_PATH
        capture_worker.DEFAULT_CAPTURE_DIR = cls.root / "captures"
        capture_worker.DEFAULT_LOG_PATH = cls.root / "worker.log"
        connection = db_connect(cls.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            lang_jp.set_capture_languages(connection, ["ja", "en", "es", "ko"])
            connection.commit()
        finally:
            connection.close()

        cls.screens = {
            "es": [
                ("¡No puede ser! ¿Nos ha traicionado?", LATIN_FONT),
                ("Vamos a la batalla final", LATIN_FONT),
            ],
            "ko": [
                ("안녕하세요 반갑습니다", KOREAN_FONT),
                ("게임을 시작합니다", KOREAN_FONT),
            ],
            "ja_en": [
                ("勇者の墓はここにある", japanese_font()),
                ("Press START to continue", LATIN_FONT),
            ],
        }
        cls.job_ids: dict[str, int] = {}
        connection = db_connect(cls.db_path)
        try:
            for name, lines in cls.screens.items():
                image_path = cls.root / f"{name}.png"
                render_screenshot(image_path, lines)
                cls.job_ids[name] = lang_jp.create_capture_job(
                    connection,
                    captured_at="2026-10-24T12:00:00-03:00",
                    temp_image_path=str(image_path),
                    image_sha256=f"synthetic-{name}",
                    image_phash=None,
                    window_title=f"Synthetic {name}",
                    process_name="test.exe",
                )
            connection.commit()
        finally:
            connection.close()

        pipeline = capture_worker.CapturePipeline()
        processed = 0
        while capture_worker.process_job_once(cls.db_path, translator=None, pipeline=pipeline):
            processed += 1
        cls.processed = processed

    @classmethod
    def tearDownClass(cls) -> None:
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = cls._auto_translate
        capture_worker.DEFAULT_CAPTURE_DIR = cls._capture_dir
        capture_worker.DEFAULT_LOG_PATH = cls._log_path
        cls.temp_dir.cleanup()

    def source_for(self, name: str):
        connection = db_connect(self.db_path)
        try:
            source = connection.execute(
                "SELECT * FROM sources WHERE capture_job_id = ?", (self.job_ids[name],)
            ).fetchone()
            self.assertIsNotNone(source, f"{name}: the job did not become a source")
            segments = connection.execute(
                "SELECT language_code, raw_text FROM source_analysis_segments WHERE source_id = ? ORDER BY segment_index",
                (source["id"],),
            ).fetchall()
            terms = connection.execute(
                """
                SELECT t.language_code, t.lemma, t.reading
                FROM source_terms AS st
                JOIN terms AS t ON t.id = st.term_id
                WHERE st.source_id = ?
                ORDER BY t.language_code, t.lemma
                """,
                (source["id"],),
            ).fetchall()
        finally:
            connection.close()
        return source, segments, terms

    def test_every_synthetic_job_was_accepted(self) -> None:
        self.assertEqual(self.processed, 3)
        connection = db_connect(self.db_path)
        try:
            statuses = connection.execute(
                "SELECT id, status, last_error FROM capture_jobs ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            [row["status"] for row in statuses],
            ["accepted"] * 3,
            [dict(row) for row in statuses],
        )

    def test_spanish_screen_yields_spanish_terms(self) -> None:
        source, segments, terms = self.source_for("es")
        self.assertEqual(source["language_code"], "es")
        self.assertEqual(source["title"], "Synthetic es")
        spanish = {row["lemma"].lower() for row in terms if row["language_code"] == "es"}
        expected = {"no", "poder", "ser", "nos", "haber", "traicionar", "ir", "batalla", "final"}
        self.assertGreaterEqual(len(spanish & expected), 4, spanish)
        self.assertFalse({row["language_code"] for row in terms} - {"es", "en"}, terms)

    def test_korean_screen_yields_korean_terms_with_readings(self) -> None:
        source, segments, terms = self.source_for("ko")
        self.assertEqual(source["language_code"], "ko")
        korean = [row for row in terms if row["language_code"] == "ko"]
        self.assertGreaterEqual(len(korean), 2, [dict(row) for row in terms])
        lemmas = {row["lemma"] for row in korean}
        self.assertTrue(lemmas & {"안녕하세요", "반갑습니다", "게임을", "시작합니다"}, lemmas)
        self.assertTrue(all(row["reading"] for row in korean), [dict(row) for row in korean])

    def test_mixed_japanese_english_screen_is_split_per_language(self) -> None:
        source, segments, terms = self.source_for("ja_en")
        languages_seen = {row["language_code"] for row in segments}
        self.assertEqual(languages_seen, {"ja", "en"}, [dict(row) for row in segments])
        japanese = {row["lemma"] for row in terms if row["language_code"] == "ja"}
        english = {row["lemma"].lower() for row in terms if row["language_code"] == "en"}
        self.assertTrue(japanese & {"勇者", "墓", "ここ", "ある"}, japanese)
        self.assertGreaterEqual(len(english & {"press", "start", "to", "continue"}), 2, english)
        self.assertEqual(source["language_code"], "ja")


if __name__ == "__main__":
    unittest.main()
