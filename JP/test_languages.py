from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import capture_worker
import lang_jp
import lang_jp_web
import languages
from lang_jp import SCHEMA_PATH, db_connect
from ocr_backends import OcrBlock


class LanguageDetectionTest(unittest.TestCase):
    def test_script_based_detection(self) -> None:
        cases = {
            "回復しました": "ja",
            "HP 100 回復した": "ja",
            "안녕하세요": "ko",
            "我是中国人": "zh",
            "Привет, как дела?": "ru",
            "Привіт, як справи?": "uk",
            "مرحبا بك": "ar",
            "Καλημέρα": "el",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(languages.detect_language(text).code, expected)

    def test_latin_detection_uses_stopwords_and_diacritics(self) -> None:
        cases = {
            "¡No puede ser! ¿Nos ha traicionado?": "es",
            "The quick brown fox jumps over the lazy dog": "en",
            "Eu não sei o que fazer com isso": "pt",
            "Ich habe keine Ahnung, was das ist": "de",
            "L'homme qui n'est pas là": "fr",
            "Il gatto è sul tavolo e non vuole scendere": "it",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(languages.detect_language(text).code, expected)

    def test_candidates_restrict_the_answer(self) -> None:
        self.assertEqual(languages.detect_language("我是中国人", candidates=["ja", "en"]).code, "ja")
        self.assertEqual(languages.detect_language("我是中国人", candidates=["zh", "ja"]).code, "zh")
        self.assertEqual(
            languages.detect_language("no puede ser", candidates=["en", "pt"]).code,
            "pt",
        )

    def test_empty_text_falls_back_to_default(self) -> None:
        guess = languages.detect_language("", default="es")
        self.assertEqual(guess.code, "es")
        self.assertEqual(guess.confidence, 0.0)

    def test_split_script_runs_keeps_neutral_chars_with_previous_run(self) -> None:
        runs = languages.split_script_runs("HP 100 回復しました。Press START")
        self.assertEqual(
            [(run.text, run.script) for run in runs],
            [
                ("HP 100 ", "latin"),
                ("回復しました。", "japanese"),
                ("Press START", "latin"),
            ],
        )
        self.assertEqual(runs[0].start, 0)
        self.assertEqual(runs[1].start, 7)
        self.assertEqual(runs[-1].end, len("HP 100 回復しました。Press START"))

    def test_han_merges_into_japanese_run(self) -> None:
        runs = languages.split_script_runs("漢字とかな")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].script, "japanese")

    def test_language_code_normalization(self) -> None:
        self.assertEqual(languages.normalize_language_code(" PT_br "), "pt-BR")
        self.assertEqual(languages.normalize_language_code("JA"), "ja")
        self.assertEqual(languages.normalize_language_code("AUTO"), "auto")
        self.assertTrue(languages.is_valid_language_code("es"))
        self.assertFalse(languages.is_valid_language_code("spanish"))
        self.assertEqual(languages.get_language("pt-BR").name, "Portuguese")
        self.assertEqual(languages.get_language("pt-BR").code, "pt-BR")
        self.assertEqual(languages.get_language("xx").deck_name, "Lang::XX")

    def test_enabled_capture_languages_parsing(self) -> None:
        self.assertEqual(languages.enabled_capture_languages("ja, en;es"), ("ja", "en", "es"))
        self.assertEqual(languages.enabled_capture_languages(None), ("ja",))
        self.assertEqual(languages.enabled_capture_languages(""), ("ja",))


class AnalyzerTest(unittest.TestCase):
    def test_generic_analyzer_lemmatizes_and_keeps_acronyms(self) -> None:
        tokens = languages.GenericAnalyzer("es").analyze("¡No puede ser! HP 100")
        self.assertEqual([token.surface for token in tokens], ["No", "puede", "ser", "HP"])
        self.assertEqual(tokens[0].lemma, "no")
        self.assertEqual(tokens[-1].lemma, "HP")
        self.assertTrue(all(token.language_code == "es" for token in tokens))

    def test_french_clitics_are_split(self) -> None:
        tokens = languages.GenericAnalyzer("fr").analyze("L'homme qui n'est pas là")
        self.assertEqual([token.surface for token in tokens], ["homme", "qui", "est", "pas", "là"])

    def test_analyze_text_splits_mixed_languages(self) -> None:
        runs = languages.analyze_text(
            "Press START to continue. Vamos a la batalla",
            candidates=["en", "es"],
        )
        self.assertEqual(len(runs), 1)
        self.assertIn(runs[0].language_code, {"en", "es"})
        runs = languages.analyze_text("안녕하세요 Press START", candidates=["ko", "en"])
        self.assertEqual([run.language_code for run in runs], ["ko", "en"])
        self.assertEqual([token.surface for token in runs[1].tokens], ["Press", "START"])

    def test_analyze_text_skips_languages_outside_candidates(self) -> None:
        runs = languages.analyze_text("안녕하세요 Press START", candidates=["en"])
        self.assertEqual([run.language_code for run in runs], ["en"])

    def test_forced_language_skips_detection(self) -> None:
        runs = languages.analyze_text("Press START", language="de")
        self.assertEqual(runs[0].language_code, "de")
        self.assertEqual(runs[0].confidence, 1.0)

    def test_han_analyzer_falls_back_to_characters(self) -> None:
        tokens = languages.HanAnalyzer().analyze("我是中国人")
        self.assertTrue(tokens)
        self.assertTrue(all(token.language_code == "zh" for token in tokens))
        combined = "".join(token.surface for token in tokens)
        self.assertEqual(combined, "我是中国人")


class CatalogLanguageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "lang_test.sqlite3"
        self._auto_translate = lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = False
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = self._auto_translate
        self.temp_dir.cleanup()

    def test_canonical_key_is_language_scoped(self) -> None:
        self.assertEqual(lang_jp.canonical_key("no", None, "es"), "es::no::")
        self.assertEqual(lang_jp.canonical_key("no", None, "en"), "en::no::")
        self.assertEqual(lang_jp.canonical_key("回復", "かいふく"), "ja::回復::かいふく")

    def test_same_lemma_in_two_languages_are_different_terms(self) -> None:
        connection = db_connect(self.db_path)
        try:
            spanish = lang_jp.upsert_term_record(
                connection, lemma="no", reading=None, meaning_pt=None, pos=None, notes=None,
                language_code="es",
            )
            english = lang_jp.upsert_term_record(
                connection, lemma="no", reading=None, meaning_pt=None, pos=None, notes=None,
                language_code="en",
            )
            again = lang_jp.upsert_term_record(
                connection, lemma="no", reading=None, meaning_pt=None, pos=None, notes=None,
                language_code="es",
            )
            rows = connection.execute(
                "SELECT code FROM languages WHERE code IN ('en', 'es', 'ja') ORDER BY code"
            ).fetchall()
        finally:
            connection.close()
        self.assertNotEqual(spanish, english)
        self.assertEqual(spanish, again)
        self.assertEqual([row["code"] for row in rows], ["en", "es", "ja"])

    def test_blacklist_is_language_aware(self) -> None:
        self.assertEqual(lang_jp.term_blacklist_reason("の"), "single_kana_syllable")
        self.assertIsNone(lang_jp.term_blacklist_reason("a", language_code="es"))
        self.assertIsNone(lang_jp.term_blacklist_reason("I", language_code="en"))
        self.assertEqual(
            lang_jp.term_blacklist_reason("100", language_code="es"),
            "placeholder_or_punctuation",
        )
        self.assertEqual(
            lang_jp.term_blacklist_reason("?", language_code="es"),
            "placeholder_or_punctuation",
        )

    def test_source_language_auto_detection(self) -> None:
        connection = db_connect(self.db_path)
        try:
            source_id = lang_jp.create_source_record(
                connection,
                source_type="text",
                language_code="auto",
                raw_text="Eu não sei o que fazer com isso",
            )
            row = connection.execute(
                "SELECT language_code, language_confidence FROM sources WHERE id = ?",
                (source_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["language_code"], "pt")
        self.assertGreater(row["language_confidence"], 0.5)

    def test_mixed_text_is_sorted_per_language(self) -> None:
        connection = db_connect(self.db_path)
        try:
            source_id = lang_jp.create_source_record(
                connection,
                source_type="text",
                language_code="auto",
                raw_text="안녕하세요 반갑습니다\nPress START to continue",
            )
            summary = lang_jp.analyze_source_text(
                connection,
                source_id=source_id,
                text="안녕하세요 반갑습니다\nPress START to continue",
                candidates=["ko", "en"],
            )
            promoted_terms, _ = lang_jp.approve_source_analysis_candidates(
                connection,
                source_id=source_id,
            )
            connection.commit()
            terms = connection.execute(
                "SELECT language_code, lemma FROM terms ORDER BY language_code, lemma"
            ).fetchall()
            segments = connection.execute(
                "SELECT language_code FROM source_segments ORDER BY id"
            ).fetchall()
            kanji_count = connection.execute("SELECT COUNT(*) FROM kanji").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(summary["languages"], {"ko", "en"})
        self.assertEqual(summary["segments"], 2)
        self.assertEqual(promoted_terms, 6)
        self.assertEqual(
            [(row["language_code"], row["lemma"]) for row in terms],
            [
                ("en", "START"),
                ("en", "continue"),
                ("en", "press"),
                ("en", "to"),
                ("ko", "반갑습니다"),
                ("ko", "안녕하세요"),
            ],
        )
        self.assertEqual({row["language_code"] for row in segments}, {"ko", "en"})
        self.assertEqual(kanji_count, 0)

    def test_flashcards_use_one_deck_per_language(self) -> None:
        connection = db_connect(self.db_path)
        try:
            for language_code, text in (("es", "Vamos a la batalla"), ("en", "Press START")):
                source_id = lang_jp.create_source_record(
                    connection,
                    source_type="text",
                    language_code=language_code,
                    raw_text=text,
                )
                lang_jp.analyze_source_text(
                    connection,
                    source_id=source_id,
                    text=text,
                    language=language_code,
                )
                lang_jp.approve_source_analysis_candidates(connection, source_id=source_id)
            args = argparse.Namespace(
                template_name="recognition_basic",
                deck_name=None,
                note_type="Basic",
                min_priority=0.0,
                limit=None,
                term_id=None,
                language="all",
                existing_only=False,
                refresh_existing=False,
            )
            created, _ = lang_jp.upsert_flashcards(connection, args)
            connection.commit()
            decks = connection.execute(
                """
                SELECT t.language_code, f.deck_name, f.tags
                FROM flashcards AS f
                JOIN terms AS t ON t.id = f.term_id
                ORDER BY t.language_code, t.lemma
                """
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(created, len(decks))
        self.assertTrue(decks)
        for row in decks:
            expected = "Lang::Spanish" if row["language_code"] == "es" else "Lang::English"
            self.assertEqual(row["deck_name"], expected)
            self.assertIn(f"lang_{row['language_code']}", row["tags"])
            self.assertNotIn("jp", row["tags"].split())

    def test_language_filter_on_flashcard_generation(self) -> None:
        connection = db_connect(self.db_path)
        try:
            for language_code, text in (("es", "Vamos a la batalla"), ("en", "Press START")):
                source_id = lang_jp.create_source_record(
                    connection,
                    source_type="text",
                    language_code=language_code,
                    raw_text=text,
                )
                lang_jp.analyze_source_text(
                    connection, source_id=source_id, text=text, language=language_code
                )
                lang_jp.approve_source_analysis_candidates(connection, source_id=source_id)
            args = argparse.Namespace(
                template_name="recognition_basic",
                deck_name=None,
                note_type="Basic",
                min_priority=0.0,
                limit=None,
                term_id=None,
                language="es",
                existing_only=False,
                refresh_existing=False,
            )
            lang_jp.upsert_flashcards(connection, args)
            connection.commit()
            languages_with_cards = connection.execute(
                """
                SELECT DISTINCT t.language_code
                FROM flashcards AS f
                JOIN terms AS t ON t.id = f.term_id
                """
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row["language_code"] for row in languages_with_cards], ["es"])


class LegacyMigrationTest(unittest.TestCase):
    def test_pre_polyglot_database_is_upgraded_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.sqlite3"
            raw = sqlite3.connect(db_path)
            raw.executescript(
                """
                CREATE TABLE sources (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    language_code TEXT NOT NULL DEFAULT 'ja',
                    raw_text TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE terms (
                    id INTEGER PRIMARY KEY,
                    canonical_key TEXT NOT NULL UNIQUE,
                    lemma TEXT NOT NULL,
                    reading TEXT,
                    meaning_pt TEXT,
                    pos TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE term_stats (
                    term_id INTEGER PRIMARY KEY,
                    total_occurrences INTEGER NOT NULL DEFAULT 0,
                    distinct_sources INTEGER NOT NULL DEFAULT 0,
                    distinct_forms INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    priority_score REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO terms (canonical_key, lemma, reading)
                VALUES ('回復::かいふく', '回復', 'かいふく');
                INSERT INTO sources (source_type, raw_text) VALUES ('text', '回復');
                """
            )
            raw.commit()
            raw.close()

            connection = db_connect(db_path)
            try:
                term = connection.execute(
                    "SELECT canonical_key, language_code FROM terms"
                ).fetchone()
                found = lang_jp.get_term_by_key(
                    connection, lang_jp.canonical_key("回復", "かいふく", "ja")
                )
                registered = connection.execute("SELECT code FROM languages").fetchall()
                kanji_columns = lang_jp.table_columns(connection, "kanji")
                view_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(v_terms_overview)")
                }
            finally:
                connection.close()
            self.assertEqual(term["canonical_key"], "ja::回復::かいふく")
            self.assertEqual(term["language_code"], "ja")
            self.assertIsNotNone(found)
            registered_codes = [row["code"] for row in registered]
            # Every built-in profile is registered and captures by default; the
            # legacy Japanese data keeps its row.
            self.assertIn("ja", registered_codes)
            self.assertEqual(set(registered_codes), set(languages.BUILTIN_LANGUAGES))
            self.assertIn("language_code", kanji_columns)
            self.assertIn("language_code", view_columns)

            # A second connection must not rewrite the keys again.
            connection = db_connect(db_path)
            try:
                term = connection.execute("SELECT canonical_key FROM terms").fetchone()
            finally:
                connection.close()
            self.assertEqual(term["canonical_key"], "ja::回復::かいふく")


class WorkerLanguageTest(unittest.TestCase):
    def test_target_language_gate(self) -> None:
        self.assertTrue(capture_worker.contains_target_language_text("回復しました"))
        self.assertFalse(capture_worker.contains_target_language_text("Press START"))
        self.assertTrue(
            capture_worker.contains_target_language_text(
                "Press START", enabled_languages=("ja", "en")
            )
        )
        self.assertTrue(
            capture_worker.contains_target_language_text(
                "¿Nos ha traicionado?", enabled_languages=("es",)
            )
        )
        self.assertFalse(
            capture_worker.contains_target_language_text(
                "안녕하세요", enabled_languages=("es", "en")
            )
        )

    def test_analyzer_splits_blocks_per_language(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "worker.sqlite3"
            connection = db_connect(db_path)
            try:
                connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                analyzer = capture_worker.LanguageSegmentAnalyzer(("ko", "en"))
                block = OcrBlock(text="안녕하세요 Press START", confidence=0.9, bbox=None)
                results = analyzer.analyze_block(connection, block)
            finally:
                connection.close()
        self.assertEqual([payload["language_code"] for payload, _ in results], ["ko", "en"])
        english_candidates = results[1][1]
        self.assertEqual(
            [candidate["surface_form"] for candidate in english_candidates],
            ["Press", "START"],
        )
        self.assertTrue(all(candidate["language_code"] == "en" for candidate in english_candidates))
        source_language, confidence = capture_worker.dominant_block_language(
            [results], default="ko"
        )
        self.assertEqual(source_language, "en")
        self.assertIsNotNone(confidence)


class WebLanguageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "web.sqlite3"
        self._auto_translate = lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = False
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            for language_code, text in (("es", "Vamos a la batalla"), ("en", "Press START")):
                source_id = lang_jp.create_source_record(
                    connection,
                    source_type="text",
                    language_code=language_code,
                    raw_text=text,
                    title=f"{language_code} source",
                )
                lang_jp.analyze_source_text(
                    connection, source_id=source_id, text=text, language=language_code
                )
                lang_jp.approve_source_analysis_candidates(connection, source_id=source_id)
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = self._auto_translate
        self.temp_dir.cleanup()

    def test_terms_page_filters_by_language(self) -> None:
        page = lang_jp_web.terms_page(self.db_path, {"lang": ["es"]})
        self.assertIn("batalla", page)
        self.assertNotIn(">press<", page.lower())
        self.assertIn('class="lang-bar"', page)
        self.assertIn('href="/terms?lang=en"', page)
        # Spanish has no characters, so the kanji link disappears from the nav.
        self.assertNotIn('href="/kanji?lang=es"', page)
        page_all = lang_jp_web.terms_page(self.db_path, {})
        self.assertIn("batalla", page_all)
        self.assertIn("press", page_all.lower())
        self.assertIn('href="/kanji"', page_all)

    def test_api_payloads_carry_language(self) -> None:
        payload = lang_jp_web.api_terms_payload(self.db_path, {"lang": ["en"]})
        self.assertEqual(payload["meta"]["lang"], "en")
        self.assertEqual({term["language_code"] for term in payload["terms"]}, {"en"})
        languages_payload = lang_jp_web.api_languages_payload(self.db_path)
        codes = {entry["code"]: entry for entry in languages_payload["languages"]}
        self.assertEqual(codes["es"]["term_count"], 4)
        self.assertEqual(codes["en"]["term_count"], 2)
        self.assertFalse(codes["es"]["has_characters"])
        self.assertTrue(codes["ja"]["has_characters"])
        sources = lang_jp_web.api_sources_payload(self.db_path, {"lang": ["es"]})
        self.assertEqual([row["language_code"] for row in sources["sources"]], ["es"])
        detail = lang_jp_web.api_term_detail_payload(self.db_path, payload["terms"][0]["id"])
        self.assertEqual(detail["language"]["code"], "en")
        self.assertEqual(len(detail["occurrences"]), 1)

    def test_dashboard_lists_languages(self) -> None:
        page = lang_jp_web.dashboard_page(self.db_path, {"lang": ["es"]})
        self.assertIn("Lang::Spanish", page)
        self.assertIn("Idiomas", page)


if __name__ == "__main__":
    unittest.main()
