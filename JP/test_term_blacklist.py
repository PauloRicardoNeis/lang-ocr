from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import lang_jp
from lang_jp import SCHEMA_PATH, db_connect


class TermBlacklistTest(unittest.TestCase):
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

    def test_term_blacklist_reason_blocks_single_kana_and_placeholders(self) -> None:
        self.assertEqual(
            lang_jp.term_blacklist_reason("の"),
            "single_kana_syllable",
        )
        self.assertEqual(
            lang_jp.term_blacklist_reason("ロ"),
            "single_kana_syllable",
        )
        self.assertEqual(
            lang_jp.term_blacklist_reason("?"),
            "placeholder_or_punctuation",
        )
        self.assertEqual(
            lang_jp.term_blacklist_reason("・"),
            "placeholder_or_punctuation",
        )
        self.assertEqual(
            lang_jp.term_blacklist_reason("。"),
            "placeholder_or_punctuation",
        )
        self.assertIsNone(lang_jp.term_blacklist_reason("ここ"))
        self.assertIsNone(lang_jp.term_blacklist_reason("カー"))
        self.assertIsNone(lang_jp.term_blacklist_reason("ロム"))

    def test_upsert_term_record_rejects_blacklisted_single_kana(self) -> None:
        connection = db_connect(self.db_path)
        try:
            with self.assertRaises(lang_jp.BlacklistedTermError):
                lang_jp.upsert_term_record(
                    connection,
                    lemma="の",
                    reading="ノ",
                    meaning_pt=None,
                    pos="助詞",
                    notes=None,
                )
        finally:
            connection.close()

    def test_build_flashcard_payload_falls_back_to_lemma_for_single_kana_surface_form(
        self,
    ) -> None:
        payload = lang_jp.build_flashcard_payload(
            {
                "id": 77,
                "lemma": "する",
                "reading": "する",
                "meaning_pt": "fazer",
                "pos": "動詞",
            },
            {
                "source_id": 5,
                "surface_form": "し",
            },
            deck_name="Lang::Japanese",
            note_type="Basic",
            template_name="recognition_basic",
        )
        self.assertEqual(
            payload["front_text"],
            lang_jp.build_front_text("する", "する"),
        )

    def test_prune_blacklisted_terms_removes_stale_terms_and_flashcards(self) -> None:
        connection = db_connect(self.db_path)
        try:
            source_id = connection.execute(
                """
                INSERT INTO sources (source_type, raw_text)
                VALUES ('manual', 'の ここ')
                """
            ).lastrowid
            bad_term_id = connection.execute(
                """
                INSERT INTO terms (canonical_key, lemma, reading, pos)
                VALUES ('の::ノ', 'の', 'ノ', '助詞')
                """
            ).lastrowid
            good_term_id = connection.execute(
                """
                INSERT INTO terms (canonical_key, lemma, reading, pos)
                VALUES ('ここ::ココ', 'ここ', 'ココ', '名詞')
                """
            ).lastrowid
            connection.execute(
                """
                INSERT INTO term_forms (term_id, form_key, surface_form, reading)
                VALUES (?, 'の::ノ', 'の', 'ノ')
                """,
                (bad_term_id,),
            )
            connection.execute(
                """
                INSERT INTO term_forms (term_id, form_key, surface_form, reading)
                VALUES (?, 'ここ::ココ', 'ここ', 'ココ')
                """,
                (good_term_id,),
            )
            connection.execute(
                """
                INSERT INTO source_terms (source_id, term_id, surface_form, count_in_source)
                VALUES (?, ?, 'の', 1)
                """,
                (source_id, bad_term_id),
            )
            connection.execute(
                """
                INSERT INTO source_terms (source_id, term_id, surface_form, count_in_source)
                VALUES (?, ?, 'ここ', 1)
                """,
                (source_id, good_term_id),
            )
            connection.execute(
                """
                INSERT INTO flashcards (term_id, source_id, front_text, back_text)
                VALUES (?, ?, 'の', 'bad')
                """,
                (bad_term_id, source_id),
            )
            connection.execute(
                """
                INSERT INTO flashcards (term_id, source_id, front_text, back_text)
                VALUES (?, ?, 'ここ', 'good')
                """,
                (good_term_id, source_id),
            )

            summary = lang_jp.prune_blacklisted_terms(connection)

            self.assertEqual(summary["term_count"], 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM terms WHERE lemma = 'の'").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM flashcards WHERE front_text = 'の'").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM terms WHERE lemma = 'ここ'").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM flashcards WHERE front_text = 'ここ'").fetchone()[0],
                1,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
