from __future__ import annotations

import struct
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import zlib

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import lang_jp
import lang_jp_web
from capture_worker import (
    GoogleGtxSourceTranslator,
    build_job_log_banner,
    enrich_job_progress_message,
    process_pending_translation_once,
)
from lang_jp import (
    SCHEMA_PATH,
    approve_source_analysis_candidates,
    claim_next_source_translation,
    claim_next_capture_job,
    create_source_analysis_candidate,
    create_source_analysis_segment,
    create_capture_job,
    create_source_record,
    db_connect,
    upsert_source_translation,
)
from ocr_backends import OcrBlock, sort_ocr_blocks_reading_order


class StubSourceTranslator(GoogleGtxSourceTranslator):
    def translate_text(self, text: str, *, source_language: str = "ja") -> str:
        return f"en:{text}"


def write_rgba_png(path: Path, width: int, height: int) -> None:
    def chunk(chunk_type: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(payload))
            + chunk_type
            + payload
            + struct.pack(">I", checksum)
        )

    row = b"\x00" + (b"\x20\x40\x60\xFF" * width)
    raw = row * height
    png = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
            ),
            chunk(b"IDAT", zlib.compress(raw)),
            chunk(b"IEND", b""),
        ]
    )
    path.write_bytes(png)


class CaptureDraftWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "lang_jp_test.sqlite3"
        self._original_auto_translate_terms = (
            lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS
        )
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = False
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        connection = db_connect(self.db_path)
        try:
            connection.executescript(schema_sql)
        finally:
            connection.close()

    def tearDown(self) -> None:
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = (
            self._original_auto_translate_terms
        )
        self.temp_dir.cleanup()

    def test_approve_source_analysis_promotes_terms_without_deleting_source(self) -> None:
        connection = db_connect(self.db_path)
        try:
            source_id = create_source_record(
                connection,
                source_type="image",
                ingest_mode="auto_capture",
                review_status="pending",
                raw_text="回復アイテム",
                source_path="C:/tmp/test.png",
                image_sha256="sha-test-1",
            )
            segment_id = create_source_analysis_segment(
                connection,
                source_id=source_id,
                segment_index=0,
                raw_text="回復アイテム",
                normalized_text="回復アイテム",
                reading="カイフクアイテム",
                confidence=0.95,
                bbox_json="[[0,0],[1,0],[1,1],[0,1]]",
            )
            create_source_analysis_candidate(
                connection,
                source_analysis_segment_id=segment_id,
                candidate_type="term",
                surface_form="回復アイテム",
                dictionary_form="回復アイテム",
                reading="カイフクアイテム",
                pos="名詞",
                confidence=0.95,
                rank=0,
                parent_candidate_id=None,
                matched_term_id=None,
                matched_kanji_id=None,
            )
            create_source_analysis_candidate(
                connection,
                source_analysis_segment_id=segment_id,
                candidate_type="kanji",
                surface_form="回",
                dictionary_form="回",
                reading=None,
                pos="kanji",
                confidence=0.95,
                rank=1,
                parent_candidate_id=None,
                matched_term_id=None,
                matched_kanji_id=None,
            )

            promoted_terms, promoted_candidates = approve_source_analysis_candidates(
                connection,
                source_id=source_id,
            )

            self.assertEqual(promoted_terms, 1)
            self.assertEqual(promoted_candidates, 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_terms").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_segment_terms").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT review_status FROM sources WHERE id = ?",
                    (source_id,),
                ).fetchone()[0],
                "reviewed",
            )
        finally:
            connection.close()

    def test_approve_source_analysis_can_promote_terms_without_marking_source_reviewed(
        self,
    ) -> None:
        connection = db_connect(self.db_path)
        try:
            source_id = create_source_record(
                connection,
                source_type="image",
                ingest_mode="auto_capture",
                review_status="pending",
                raw_text="回復アイテム",
                source_path="C:/tmp/test.png",
                image_sha256="sha-test-2",
            )
            segment_id = create_source_analysis_segment(
                connection,
                source_id=source_id,
                segment_index=0,
                raw_text="回復アイテム",
                normalized_text="回復アイテム",
                reading="カイフクアイテム",
                confidence=0.95,
                bbox_json="[[0,0],[1,0],[1,1],[0,1]]",
            )
            create_source_analysis_candidate(
                connection,
                source_analysis_segment_id=segment_id,
                candidate_type="term",
                surface_form="回復アイテム",
                dictionary_form="回復アイテム",
                reading="カイフクアイテム",
                pos="名詞",
                confidence=0.95,
                rank=0,
                parent_candidate_id=None,
                matched_term_id=None,
                matched_kanji_id=None,
            )

            promoted_terms, promoted_candidates = approve_source_analysis_candidates(
                connection,
                source_id=source_id,
                mark_reviewed=False,
            )

            self.assertEqual(promoted_terms, 1)
            self.assertEqual(promoted_candidates, 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_terms").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT review_status FROM sources WHERE id = ?",
                    (source_id,),
                ).fetchone()[0],
                "pending",
            )
        finally:
            connection.close()

    def test_source_translation_upsert_persists_english_draft(self) -> None:
        connection = db_connect(self.db_path)
        try:
            source_id = create_source_record(
                connection,
                source_type="image",
                ingest_mode="auto_capture",
                review_status="pending",
                raw_text="伝説の勇者が大魔王を倒し",
                source_path="C:/tmp/test-translation.png",
                image_sha256="sha-test-translation",
            )
            translation_id = upsert_source_translation(
                connection,
                source_id=source_id,
                source_text_hash="hash-translation-1",
                target_language="en",
                provider="google_gtx",
                status="translated",
                translated_text="The legendary hero defeats the great demon king.",
                error_message=None,
            )
            self.assertGreater(translation_id, 0)
            row = connection.execute(
                """
                SELECT target_language, provider, status, translated_text
                FROM source_translations
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["target_language"], "en")
            self.assertEqual(row["provider"], "google_gtx")
            self.assertEqual(row["status"], "translated")
            self.assertEqual(
                row["translated_text"],
                "The legendary hero defeats the great demon king.",
            )
        finally:
            connection.close()

    def test_claim_next_source_translation_prefers_newest_pending_row(self) -> None:
        connection = db_connect(self.db_path)
        try:
            failed_source_id = create_source_record(
                connection,
                source_type="image",
                ingest_mode="auto_capture",
                review_status="pending",
                raw_text="古い翻訳",
                source_path="C:/tmp/test-translation-failed.png",
                image_sha256="sha-test-translation-failed",
            )
            pending_source_id = create_source_record(
                connection,
                source_type="image",
                ingest_mode="auto_capture",
                review_status="pending",
                raw_text="新しい翻訳",
                source_path="C:/tmp/test-translation-pending.png",
                image_sha256="sha-test-translation-pending",
            )
            upsert_source_translation(
                connection,
                source_id=failed_source_id,
                source_text_hash="hash-translation-failed",
                target_language="en",
                provider="google_gtx",
                status="failed",
                translated_text=None,
                error_message="boom",
            )
            older_pending_translation_id = upsert_source_translation(
                connection,
                source_id=pending_source_id,
                source_text_hash="hash-translation-pending",
                target_language="en",
                provider="google_gtx",
                status="pending",
                translated_text=None,
                error_message=None,
            )
            newer_pending_source_id = create_source_record(
                connection,
                source_type="image",
                ingest_mode="auto_capture",
                review_status="pending",
                raw_text="最新の翻訳",
                source_path="C:/tmp/test-translation-newest.png",
                image_sha256="sha-test-translation-newest",
            )
            newer_pending_translation_id = upsert_source_translation(
                connection,
                source_id=newer_pending_source_id,
                source_text_hash="hash-translation-newest",
                target_language="en",
                provider="google_gtx",
                status="pending",
                translated_text=None,
                error_message=None,
            )
            connection.commit()

            row = claim_next_source_translation(
                connection,
                target_language="en",
                provider="google_gtx",
            )

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["id"], newer_pending_translation_id)
            self.assertNotEqual(row["id"], older_pending_translation_id)
            self.assertEqual(row["source_id"], newer_pending_source_id)
            self.assertEqual(row["raw_text"], "最新の翻訳")
        finally:
            connection.close()

    def test_claim_next_capture_job_prefers_newest_pending_capture(self) -> None:
        connection = db_connect(self.db_path)
        try:
            older_job_id = create_capture_job(
                connection,
                captured_at="2026-04-05T10:00:00-03:00",
                temp_image_path="C:/tmp/capture-older.png",
                image_sha256="sha-capture-older",
                image_phash=None,
            )
            newer_job_id = create_capture_job(
                connection,
                captured_at="2026-04-05T10:00:05-03:00",
                temp_image_path="C:/tmp/capture-newer.png",
                image_sha256="sha-capture-newer",
                image_phash=None,
            )
            connection.commit()

            row = claim_next_capture_job(connection)

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["id"], newer_job_id)
            self.assertNotEqual(row["id"], older_job_id)
            self.assertEqual(row["status"], "processing")
        finally:
            connection.close()

    def test_process_pending_translation_once_translates_backlog_row(self) -> None:
        translator = StubSourceTranslator(target_language="en")
        connection = db_connect(self.db_path)
        try:
            source_id = create_source_record(
                connection,
                source_type="image",
                ingest_mode="auto_capture",
                review_status="pending",
                raw_text="一行目\n二行目",
                source_path="C:/tmp/test-translation-backlog.png",
                image_sha256="sha-test-translation-backlog",
                ocr_text_hash="hash-translation-backlog",
            )
            upsert_source_translation(
                connection,
                source_id=source_id,
                source_text_hash="hash-translation-backlog",
                target_language="en",
                provider="google_gtx",
                status="pending",
                translated_text=None,
                error_message=None,
            )
            connection.commit()
        finally:
            connection.close()

        processed = process_pending_translation_once(self.db_path, translator=translator)
        self.assertTrue(processed)

        connection = db_connect(self.db_path)
        row = None
        try:
            row = connection.execute(
                """
                SELECT status, translated_text, error_message
                FROM source_translations
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "translated")
            self.assertEqual(row["translated_text"], "en:一行目\nen:二行目")
            self.assertIsNone(row["error_message"])
        finally:
            connection.close()
            row = None
            connection = None

    def test_create_source_record_stores_original_image_resolution(self) -> None:
        image_path = Path(self.temp_dir.name) / "source-resolution.png"
        write_rgba_png(image_path, 13, 7)

        connection = db_connect(self.db_path)
        try:
            source_id = create_source_record(
                connection,
                source_type="image",
                ingest_mode="image_import",
                review_status="pending",
                source_path=str(image_path),
                raw_text="テスト",
            )
            row = connection.execute(
                "SELECT image_width, image_height FROM sources WHERE id = ?",
                (source_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["image_width"], 13)
            self.assertEqual(row["image_height"], 7)
        finally:
            connection.close()

    def test_source_translation_preserves_one_output_line_per_source_line(self) -> None:
        translator = StubSourceTranslator(target_language="en")

        translated = translator.translate_lines(
            [
                "一行目\n二行目",
                "   ",
                "三行目\r\n四行目",
            ]
        )

        self.assertEqual(
            translated,
            "en:一行目\nen:二行目\nen:三行目\nen:四行目",
        )


    def test_upsert_term_record_autofills_portuguese_meaning_when_missing(self) -> None:
        connection = db_connect(self.db_path)
        try:
            with (
                patch.object(
                    lang_jp,
                    "DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS",
                    True,
                ),
                patch.object(
                    lang_jp,
                    "auto_translate_term_meaning_pt",
                    return_value="item de cura",
                ) as translate_mock,
            ):
                term_id = lang_jp.upsert_term_record(
                    connection,
                    lemma="回復アイテム",
                    reading="かいふくアイテム",
                    meaning_pt=None,
                    pos="名詞",
                    notes=None,
                )

            row = connection.execute(
                "SELECT id, meaning_pt FROM terms WHERE id = ?",
                (term_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["meaning_pt"], "item de cura")
            translate_mock.assert_called_once_with(
                "回復アイテム",
                reading="かいふくアイテム",
            )
        finally:
            connection.close()

    def test_job_log_banner_marks_the_start_of_a_new_image(self) -> None:
        banner = build_job_log_banner(
            job_id=321,
            temp_image_path=Path("C:/tmp/capture_spool/20260404-123456-abcdef.png"),
            image_sha256="abcdef1234567890",
            captured_at="2026-04-04T12:34:56-03:00",
        )

        self.assertEqual(len(banner), 1)
        self.assertEqual(
            banner[0],
            (
                "job=321 step=start image=20260404-123456-abcdef.png "
                "sha=abcdef123456 captured=2026-04-04T12:34:56-03:00"
            ),
        )

    def test_enrich_job_progress_message_adds_bar_before_other_fields(self) -> None:
        message = enrich_job_progress_message(
            "job=321 step=source_saved t=3538ms source_id=651 text_len=48 blocks=1"
        )

        self.assertEqual(
            message,
            (
                "progress=[########----](70%) job=321 step=source_saved "
                "t=3538ms source_id=651 text_len=48 blocks=1"
            ),
        )

    def test_enrich_job_progress_message_moves_existing_progress_to_the_front(self) -> None:
        message = enrich_job_progress_message(
            "job=321 step=done progress=[############](100%) status=accepted"
        )

        self.assertEqual(
            message,
            "progress=[############](100%) job=321 step=done status=accepted",
        )


class OcrOrderingTest(unittest.TestCase):
    @staticmethod
    def make_block(text: str, left: float, top: float, right: float, bottom: float) -> OcrBlock:
        return OcrBlock(
            text=text,
            confidence=0.95,
            bbox=[
                [left, top],
                [right, top],
                [right, bottom],
                [left, bottom],
            ],
        )

    def test_sort_ocr_blocks_reading_order_groups_by_row_before_column(self) -> None:
        blocks = [
            self.make_block("2", 20, 42, 60, 62),
            self.make_block("6", 85, 86, 125, 106),
            self.make_block("4", 150, 43, 190, 63),
            self.make_block("1", 90, 8, 130, 28),
            self.make_block("8", 95, 130, 135, 150),
            self.make_block("3", 85, 45, 125, 65),
            self.make_block("7", 150, 88, 190, 108),
            self.make_block("5", 20, 84, 60, 104),
        ]

        ordered = sort_ocr_blocks_reading_order(blocks)

        self.assertEqual([block.text for block in ordered], ["1", "2", "3", "4", "5", "6", "7", "8"])

    def test_sort_ocr_blocks_reading_order_keeps_same_row_with_small_vertical_jitter(self) -> None:
        blocks = [
            self.make_block("left", 20, 50, 70, 68),
            self.make_block("mid", 90, 56, 140, 74),
            self.make_block("right", 160, 52, 210, 70),
            self.make_block("next", 20, 86, 70, 104),
        ]

        ordered = sort_ocr_blocks_reading_order(blocks)

        self.assertEqual([block.text for block in ordered], ["left", "mid", "right", "next"])


class WebFormattingTest(unittest.TestCase):
    def test_format_datetime_display_renders_iso_timestamp_for_humans(self) -> None:
        formatted = lang_jp_web.format_datetime_display(
            "2026-04-05T01:10:41.318062500-03:00"
        )

        self.assertEqual(formatted, "05/04/2026 01:10:41")

    def test_format_datetime_display_keeps_unknown_values(self) -> None:
        formatted = lang_jp_web.format_datetime_display("not-a-date")

        self.assertEqual(formatted, "not-a-date")


if __name__ == "__main__":
    unittest.main()
