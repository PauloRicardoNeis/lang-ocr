from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lang_jp import (
    SCHEMA_PATH,
    approve_source_analysis_candidates,
    create_source_analysis_candidate,
    create_source_analysis_segment,
    create_source_record,
    db_connect,
)


class CaptureDraftWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "lang_jp_test.sqlite3"
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        connection = db_connect(self.db_path)
        try:
            connection.executescript(schema_sql)
        finally:
            connection.close()

    def tearDown(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
