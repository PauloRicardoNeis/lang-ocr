from __future__ import annotations

import http.client
import sys
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import lang_jp
import lang_jp_web
from lang_jp import SCHEMA_PATH, db_connect, ensure_runtime_schema


class CatalogWebPaginationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "lang_jp_web_test.sqlite3"
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        connection = db_connect(self.db_path)
        try:
            connection.executescript(schema_sql)
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def insert_term(self, connection, index: int) -> int:
        term_id = connection.execute(
            """
            INSERT INTO terms (canonical_key, lemma, reading, meaning_pt, pos)
            VALUES (?, ?, ?, ?, 'noun')
            """,
            (
                f"term-{index}",
                f"term-{index}",
                f"reading-{index}",
                f"meaning-{index}",
            ),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO term_stats (
                term_id,
                total_occurrences,
                distinct_sources,
                distinct_forms,
                priority_score,
                first_seen_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                term_id,
                index,
                1,
                1,
                float(1000 - index),
                "2026-04-01 10:00:00",
                f"2026-04-{(index % 9) + 1:02d} 10:00:00",
            ),
        )
        return term_id

    def seed_terms(self, count: int) -> None:
        connection = db_connect(self.db_path)
        try:
            for index in range(1, count + 1):
                self.insert_term(connection, index)
            connection.commit()
        finally:
            connection.close()

    def seed_kanji(self, count: int) -> None:
        connection = db_connect(self.db_path)
        try:
            for index in range(1, count + 1):
                kanji_id = connection.execute(
                    """
                    INSERT INTO kanji (literal, meaning_pt, canonical_meanings)
                    VALUES (?, ?, ?)
                    """,
                    (f"K{index}", f"meaning-{index}", f"meaning-{index}"),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO kanji_stats (
                        kanji_id,
                        total_occurrences,
                        distinct_sources,
                        distinct_terms,
                        priority_score,
                        first_seen_at,
                        last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kanji_id,
                        index,
                        1,
                        1,
                        float(500 - index),
                        "2026-04-01 10:00:00",
                        f"2026-04-{(index % 9) + 1:02d} 10:00:00",
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def seed_sources(self, count: int) -> None:
        connection = db_connect(self.db_path)
        try:
            for index in range(1, count + 1):
                connection.execute(
                    """
                    INSERT INTO sources (
                        source_type,
                        title,
                        scene,
                        raw_text,
                        captured_at
                    )
                    VALUES ('manual', ?, ?, ?, ?)
                    """,
                    (
                        f"Source {index}",
                        f"Scene {index}",
                        f"raw text {index}",
                        f"2026-04-{(index % 9) + 1:02d} 10:00:00",
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def seed_flashcards(self, pending_count: int, exported_count: int) -> None:
        connection = db_connect(self.db_path)
        try:
            total_count = pending_count + exported_count
            for index in range(1, total_count + 1):
                term_id = self.insert_term(connection, 1000 + index)
                export_status = "pending" if index <= pending_count else "exported"
                connection.execute(
                    """
                    INSERT INTO flashcards (
                        term_id,
                        template_name,
                        deck_name,
                        note_type,
                        front_text,
                        back_text,
                        export_status
                    )
                    VALUES (?, 'recognition_basic', 'Lang::JP', 'Basic', ?, ?, ?)
                    """,
                    (
                        term_id,
                        f"Front {index}",
                        f"Back {index}",
                        export_status,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def test_terms_page_renders_pagination_summary_and_links(self) -> None:
        self.seed_terms(12)

        page = lang_jp_web.terms_page(
            self.db_path,
            {"limit": ["5"], "page": ["2"]},
        )

        self.assertIn("Mostrando 6 a 10 de 12 resultados.", page)
        self.assertIn("Pagina 2 de 3", page)
        self.assertIn('href="/terms?limit=5"', page)
        self.assertIn('href="/terms?limit=5&amp;page=3"', page)
        self.assertIn("Limpar filtros", page)
        self.assertNotIn("Limpar filtros", lang_jp_web.terms_page(self.db_path, {}))

    def test_kanji_page_clamps_page_number_to_last_page(self) -> None:
        self.seed_kanji(9)

        page = lang_jp_web.kanji_page(
            self.db_path,
            {"limit": ["4"], "page": ["99"]},
        )

        self.assertIn("Mostrando 9 a 9 de 9 resultados.", page)
        self.assertIn("Pagina 3 de 3", page)
        self.assertIn('href="/kanji?limit=4&amp;page=2"', page)

    def test_sources_live_payload_includes_pagination_markup(self) -> None:
        self.seed_sources(11)

        payload = lang_jp_web.sources_live_payload(
            self.db_path,
            {"limit": ["5"], "page": ["2"]},
        )

        self.assertEqual(payload["count_label"], "Mostrando 6 a 10 de 11 resultados.")
        self.assertIn("Pagina 2 de 3", payload["pagination_html"])
        self.assertIn('href="/sources?limit=5&amp;page=3"', payload["pagination_html"])

    def test_flashcards_page_counts_filtered_results(self) -> None:
        self.seed_flashcards(pending_count=7, exported_count=3)

        page = lang_jp_web.flashcards_page(
            self.db_path,
            {"status": ["pending"], "limit": ["4"], "page": ["2"]},
        )

        self.assertIn("Mostrando 5 a 7 de 7 resultados.", page)
        self.assertIn("Pagina 2 de 2", page)
        self.assertIn('href="/flashcards?status=pending&amp;limit=4"', page)
        self.assertIn("Limpar filtros", page)

    def insert_kanji(
        self,
        connection,
        literal: str,
        score: float,
        *,
        meaning_pt: str | None = None,
        canonical_meanings: str | None = None,
    ) -> int:
        kanji_id = connection.execute(
            "INSERT INTO kanji (literal, meaning_pt, canonical_meanings) VALUES (?, ?, ?)",
            (literal, meaning_pt, canonical_meanings),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO kanji_stats (
                kanji_id,
                total_occurrences,
                distinct_sources,
                distinct_terms,
                priority_score,
                first_seen_at,
                last_seen_at
            )
            VALUES (?, 1, 1, 1, ?, '2026-04-01 10:00:00', '2026-04-01 10:00:00')
            """,
            (kanji_id, score),
        )
        return kanji_id

    def insert_source(self, connection, *, captured_at: str | None, created_at: str) -> int:
        return connection.execute(
            """
            INSERT INTO sources (source_type, title, raw_text, captured_at, created_at)
            VALUES ('manual', 'Source', 'raw text', ?, ?)
            """,
            (captured_at, created_at),
        ).lastrowid

    def test_kanji_scores_sort_numerically_not_as_text(self) -> None:
        connection = db_connect(self.db_path)
        try:
            self.insert_kanji(connection, "大", 999.0)
            self.insert_kanji(connection, "川", 14900.0)
            self.insert_kanji(connection, "印", 98.0)
            connection.commit()
        finally:
            connection.close()

        for page in (
            lang_jp_web.kanji_page(self.db_path, {}),
            lang_jp_web.dashboard_page(self.db_path),
        ):
            positions = [page.index(f">{literal}</a>") for literal in ("川", "大", "印")]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("14900.0", page)

    def test_tables_keep_server_order_without_default_client_sort(self) -> None:
        markup = lang_jp_web.table_html(["Termo", "Score"], [["b", "1.0"], ["a", "9.0"]])

        self.assertNotIn("data-default-sort", markup)
        self.assertNotIn("defaultSortColumn", lang_jp_web.PAGE_SCRIPT)
        self.assertLess(markup.index("<td>b</td>"), markup.index("<td>a</td>"))

    def test_sources_with_mixed_timestamp_formats_sort_chronologically(self) -> None:
        connection = db_connect(self.db_path)
        try:
            # 23:30 at -03:00 is 02:30 UTC, later than the naive-UTC 02:14 below,
            # even though it sorts earlier as plain text.
            later_id = self.insert_source(
                connection,
                captured_at="2026-07-08T23:30:00-03:00",
                created_at="2026-07-09 03:00:00",
            )
            earlier_id = self.insert_source(
                connection,
                captured_at=None,
                created_at="2026-07-09 02:14:31",
            )
            connection.commit()
        finally:
            connection.close()

        _, _, _, rows = lang_jp_web.load_sources_rows(self.db_path, {})

        self.assertEqual([row["id"] for row in rows], [later_id, earlier_id])

    def test_flashcard_front_shows_visible_text_and_search_ignores_markup(self) -> None:
        connection = db_connect(self.db_path)
        try:
            term_id = self.insert_term(connection, 1)
            connection.execute(
                """
                INSERT INTO flashcards (term_id, front_text, back_text)
                VALUES (?, ?, ?)
                """,
                (
                    term_id,
                    '<div style="font-size: 2.5em;">川</div><div style="color: #666;">かわ</div>',
                    "<div>river</div>",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        page = lang_jp_web.flashcards_page(self.db_path, {})
        self.assertIn("川 かわ", page)
        self.assertNotIn("&lt;div", page)
        self.assertIn(
            "Nenhum resultado encontrado.",
            lang_jp_web.flashcards_page(self.db_path, {"q": ["div"]}),
        )
        self.assertIn(
            "Mostrando 1 a 1 de 1 resultado.",
            lang_jp_web.flashcards_page(self.db_path, {"q": ["かわ"]}),
        )

    def test_kanji_search_matches_displayed_canonical_meaning(self) -> None:
        connection = db_connect(self.db_path)
        try:
            self.insert_kanji(connection, "川", 10.0, canonical_meanings="river; stream")
            self.insert_kanji(connection, "山", 5.0, canonical_meanings="mountain")
            connection.commit()
        finally:
            connection.close()

        page = lang_jp_web.kanji_page(self.db_path, {"q": ["river"]})

        self.assertIn("Mostrando 1 a 1 de 1 resultado.", page)
        self.assertIn(">川</a>", page)

    def test_detail_pages_cap_long_tables_and_say_so(self) -> None:
        total = lang_jp_web.DETAIL_TABLE_LIMIT + 5
        connection = db_connect(self.db_path)
        try:
            term_id = self.insert_term(connection, 1)
            kanji_id = self.insert_kanji(connection, "川", 10.0)
            for index in range(total):
                source_id = self.insert_source(
                    connection,
                    captured_at=None,
                    created_at=f"2026-04-01 10:{index // 60:02d}:{index % 60:02d}",
                )
                connection.execute(
                    """
                    INSERT INTO source_terms (source_id, term_id, surface_form, count_in_source)
                    VALUES (?, ?, 'term-1', 1)
                    """,
                    (source_id, term_id),
                )
                connection.execute(
                    "INSERT INTO source_kanji (source_id, kanji_id, count_in_source) VALUES (?, ?, 1)",
                    (source_id, kanji_id),
                )
            connection.commit()
        finally:
            connection.close()

        note = f"Mostrando {lang_jp_web.DETAIL_TABLE_LIMIT} de {total}."
        term_page = lang_jp_web.term_detail_page(self.db_path, term_id)
        kanji_page = lang_jp_web.kanji_detail_page(self.db_path, kanji_id)

        self.assertIn(note, term_page)
        self.assertEqual(term_page.count('<a href="/sources/'), lang_jp_web.DETAIL_TABLE_LIMIT)
        self.assertIn(note, kanji_page)
        self.assertEqual(kanji_page.count('<a href="/sources/'), lang_jp_web.DETAIL_TABLE_LIMIT)


class MultilingualPagesTest(unittest.TestCase):
    """Pages and API routes for languages other than Japanese."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "lang_jp_web_multilingual.sqlite3"
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            self.ja_term = self.insert_term(connection, "ja", "回復", reading="かいふく", meaning="cura")
            self.es_term = self.insert_term(connection, "es", "batalla", meaning="batalha")
            self.es_water = self.insert_term(connection, "es", "agua", meaning="ÁGUA")
            self.ru_term = self.insert_term(connection, "ru", "Привет", reading="privet", meaning="olá")
            self.ar_term = self.insert_term(connection, "ar", "مرحبا", meaning="olá")
            self.kanji_id = connection.execute(
                "INSERT INTO kanji (literal, language_code, canonical_meanings) VALUES ('回', 'ja', 'times')"
            ).lastrowid

            # A Spanish capture with one Japanese line, as the polyglot worker files it.
            self.mixed_source = self.insert_source(connection, "es", "¡Vamos a la batalla!\n回復")
            self.link(connection, self.mixed_source, self.es_term, "batalla", "Vamos a la batalla", "es")
            self.link(connection, self.mixed_source, self.ja_term, "回復", "回復", "ja", reading="かいふく")
            connection.execute(
                "INSERT INTO source_kanji (source_id, kanji_id, count_in_source) VALUES (?, ?, 1)",
                (self.mixed_source, self.kanji_id),
            )
            self.es_source = self.insert_source(connection, "es", "agua")
            self.link(connection, self.es_source, self.es_water, "agua", "agua", "es")
            self.ar_source = self.insert_source(connection, "ar", "مرحبا بالعالم")

            spanish_segment = self.insert_analysis_segment(connection, self.mixed_source, 0, "Vamos a la batalla", "es")
            for status in ("approved", "approved", "rejected"):
                self.insert_candidate(connection, spanish_segment, status)
            japanese_segment = self.insert_analysis_segment(connection, self.mixed_source, 1, "回復", "ja")
            self.insert_candidate(connection, japanese_segment, "pending")
            # Approved candidates of another source must not leak into these counts.
            other_segment = self.insert_analysis_segment(connection, self.es_source, 0, "agua", "es")
            for _ in range(4):
                self.insert_candidate(connection, other_segment, "approved")

            for term_id, front in ((self.es_term, "<div>batalla</div>"), (self.ja_term, "<div>回復</div>")):
                connection.execute(
                    "INSERT INTO flashcards (term_id, front_text, back_text) VALUES (?, ?, 'back')",
                    (term_id, front),
                )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def insert_term(
        self,
        connection,
        language_code: str,
        lemma: str,
        *,
        reading: str | None = None,
        meaning: str | None = None,
    ) -> int:
        return connection.execute(
            """
            INSERT INTO terms (canonical_key, language_code, lemma, reading, meaning_pt)
            VALUES (?, ?, ?, ?, ?)
            """,
            (f"{language_code}::{lemma}::{reading or ''}", language_code, lemma, reading, meaning),
        ).lastrowid

    def insert_source(self, connection, language_code: str, raw_text: str) -> int:
        return connection.execute(
            """
            INSERT INTO sources (source_type, language_code, title, raw_text, created_at)
            VALUES ('text', ?, ?, ?, '2026-10-25 10:00:00')
            """,
            (language_code, f"{language_code} source", raw_text),
        ).lastrowid

    def link(
        self,
        connection,
        source_id: int,
        term_id: int,
        surface: str,
        segment_text: str,
        segment_language: str,
        *,
        reading: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO source_terms (source_id, term_id, surface_form, count_in_source) VALUES (?, ?, ?, 1)",
            (source_id, term_id, surface),
        )
        segment_id = connection.execute(
            """
            INSERT INTO source_segments (source_id, segment_key, segment_text, language_code, reading)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, f"{segment_text}::", segment_text, segment_language, reading),
        ).lastrowid
        connection.execute(
            "INSERT INTO source_segment_terms (source_segment_id, term_id, surface_form) VALUES (?, ?, ?)",
            (segment_id, term_id, surface),
        )

    def insert_analysis_segment(self, connection, source_id: int, index: int, text: str, language_code: str) -> int:
        return connection.execute(
            """
            INSERT INTO source_analysis_segments (source_id, segment_index, raw_text, normalized_text, language_code)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, index, text, text, language_code),
        ).lastrowid

    def insert_candidate(self, connection, segment_id: int, status: str) -> None:
        connection.execute(
            """
            INSERT INTO source_analysis_candidates (
                source_analysis_segment_id, candidate_type, surface_form, dictionary_form, approval_status
            )
            VALUES (?, 'term', 'x', 'x', ?)
            """,
            (segment_id, status),
        )

    def test_term_detail_shows_language_and_only_relevant_panels(self) -> None:
        spanish = lang_jp_web.term_detail_page(self.db_path, self.es_term)
        self.assertIn("Idioma: Spanish (es)", spanish)
        self.assertIn('<h2 lang="es" dir="auto">batalla</h2>', spanish)
        self.assertNotIn("Kanji derivados", spanish)
        self.assertNotIn("Leitura:", spanish)
        self.assertNotIn("Leitura do segmento", spanish)
        self.assertIn("Idioma do segmento", spanish)

        japanese = lang_jp_web.term_detail_page(self.db_path, self.ja_term)
        self.assertIn("Idioma: Japanese (ja)", japanese)
        self.assertIn("Kanji derivados", japanese)
        self.assertIn('Leitura: <span lang="ja" dir="auto">かいふく</span>', japanese)
        self.assertIn('>回</a>', japanese)

        russian = lang_jp_web.term_detail_page(self.db_path, self.ru_term)
        self.assertIn('Leitura: <span lang="ru-Latn" dir="auto">privet</span>', russian)
        self.assertNotIn("Kanji derivados", russian)

        arabic = lang_jp_web.term_detail_page(self.db_path, self.ar_term)
        self.assertIn('<h2 lang="ar" dir="auto">مرحبا</h2>', arabic)

    def test_listings_carry_language_through_links_forms_and_pagination(self) -> None:
        page = lang_jp_web.terms_page(self.db_path, {"lang": ["es"], "limit": ["1"]})

        self.assertIn('href="/terms?limit=1&amp;lang=es&amp;page=2"', page)
        self.assertIn(
            f'href="/terms/{self.es_term}?lang=es" lang="es" dir="auto"',
            lang_jp_web.terms_page(self.db_path, {"lang": ["es"]}),
        )
        self.assertIn('<input type="hidden" name="lang" value="es">', page)
        # "Limpar filtros" drops the search but keeps the language.
        self.assertIn('class="secondary-link" href="/terms?lang=es"', page)
        # Spanish has no readings, so the column is gone; it stays for all languages.
        self.assertNotIn(">Leitura<", page)
        self.assertIn(">Leitura<", lang_jp_web.terms_page(self.db_path, {}))
        self.assertIn(">Leitura<", lang_jp_web.terms_page(self.db_path, {"lang": ["ru"]}))

        connection = db_connect(self.db_path)
        try:
            connection.execute("INSERT INTO kanji (literal, language_code) VALUES ('復', 'ja')")
            connection.commit()
        finally:
            connection.close()
        kanji = lang_jp_web.kanji_page(self.db_path, {"lang": ["ja"], "limit": ["1"]})
        self.assertIn('href="/kanji?limit=1&amp;lang=ja&amp;page=2"', kanji)
        self.assertIn('<input type="hidden" name="lang" value="ja">', kanji)

        cards = lang_jp_web.flashcards_page(self.db_path, {"lang": ["es"], "q": ["bat"]})
        self.assertIn('class="secondary-link" href="/flashcards?lang=es"', cards)
        self.assertNotIn(">Leitura<", cards)

        sources = lang_jp_web.sources_live_payload(self.db_path, {"lang": ["es"]})
        self.assertIn(f'href="/sources/{self.mixed_source}?lang=es"', sources["table_html"])

    def test_term_and_source_search_ignore_case_outside_ascii(self) -> None:
        russian = lang_jp_web.terms_page(self.db_path, {"q": ["привет"]})
        self.assertIn("Mostrando 1 a 1 de 1 resultado.", russian)
        self.assertIn(">Привет</a>", russian)

        accented = lang_jp_web.api_terms_payload(self.db_path, {"q": ["água"]})
        self.assertEqual([term["id"] for term in accented["terms"]], [self.es_water])

        japanese = lang_jp_web.api_terms_payload(self.db_path, {"q": ["回復"]})
        self.assertEqual([term["id"] for term in japanese["terms"]], [self.ja_term])

        sources = lang_jp_web.api_sources_payload(self.db_path, {"q": ["¡VAMOS"]})
        self.assertEqual([row["id"] for row in sources["sources"]], [self.mixed_source])

        cards = lang_jp_web.api_flashcards_payload(self.db_path, {"q": ["BATALLA"]})
        self.assertEqual(cards["meta"]["q"], "BATALLA")
        self.assertEqual([card["term_id"] for card in cards["flashcards"]], [self.es_term])

    def test_search_fold_only_changes_case_and_normalization(self) -> None:
        fold = lang_jp_web.search_fold

        self.assertEqual(fold("ÁGUA"), "água")
        self.assertEqual(fold("ПРИВЕТ"), "привет")
        self.assertEqual(fold("Straße"), "strasse")
        # Dakuten, matras, hangul and Arabic stay exactly as they are.
        for value in ("ガギグ", "回復", "हिन्दी", "한국어", "ภาษาไทย", "مرحبا"):
            self.assertEqual(fold(value), value)
        self.assertEqual(fold("が"), "が")
        self.assertEqual(fold(None), "")

    def test_language_attributes_cover_scripts_and_right_to_left(self) -> None:
        self.assertEqual(lang_jp_web.language_attrs("ar"), ' lang="ar" dir="auto"')
        self.assertEqual(lang_jp_web.language_attrs("pt-br"), ' lang="pt-BR" dir="auto"')
        self.assertEqual(lang_jp_web.language_attrs(None), ' dir="auto"')
        self.assertEqual(lang_jp_web.language_attrs('"><script>'), ' dir="auto"')
        self.assertEqual(lang_jp_web.reading_attrs("ja"), ' lang="ja" dir="auto"')
        self.assertEqual(lang_jp_web.reading_attrs("ko"), ' lang="ko-Latn" dir="auto"')

        self.assertEqual(lang_jp_web.current_language({"lang": ["<script>"]}), "all")
        self.assertEqual(lang_jp_web.current_language({"lang": ["ES"]}), "es")
        self.assertEqual(lang_jp_web.current_language({}), "all")

        arabic_source = lang_jp_web.source_detail_page(self.db_path, self.ar_source, {})
        self.assertIn('<pre lang="ar" dir="auto">مرحبا بالعالم</pre>', arabic_source)
        self.assertIn('[lang|="ar"]', lang_jp_web.PAGE_STYLE)
        self.assertIn('pre[dir="auto"] { unicode-bidi: plaintext; }', lang_jp_web.PAGE_STYLE)

    def test_source_detail_shows_each_term_and_segment_language(self) -> None:
        page = lang_jp_web.source_detail_page(self.db_path, self.mixed_source, {"lang": ["es"]})

        self.assertIn(f'href="/terms/{self.es_term}?lang=es" lang="es" dir="auto">batalla</a></td><td>es</td>', page)
        self.assertIn(f'href="/terms/{self.ja_term}?lang=es" lang="ja" dir="auto">回復</a></td><td>ja</td>', page)
        self.assertIn('<td><span lang="es" dir="auto">Vamos a la batalla</span></td><td>es</td>', page)
        self.assertIn('<td><span lang="ja" dir="auto">回復</span></td><td>ja</td>', page)
        # Japanese is linked here, so readings and the kanji panel stay.
        self.assertIn(">Leitura<", page)
        self.assertIn("Kanji ligados a esta fonte", page)
        # Candidate counts are per segment (3 candidates, 2 approved; 1 pending).
        self.assertIn("<td>3</td><td>2</td>", page)
        self.assertIn("<td>1</td><td>0</td>", page)
        self.assertIn('<input type="hidden" name="lang" value="es">', page)

        spanish_only = lang_jp_web.source_detail_page(self.db_path, self.es_source, {})
        self.assertNotIn("Kanji ligados a esta fonte", spanish_only)
        self.assertNotIn("Abrir kanji", spanish_only)
        self.assertNotIn(">Leitura<", spanish_only)
        self.assertIn("<td>4</td><td>4</td>", spanish_only)

    def test_dashboard_for_a_language_without_characters(self) -> None:
        page = lang_jp_web.dashboard_page(self.db_path, {"lang": ["es"]})

        self.assertNotIn('href="/kanji', page)
        self.assertNotIn("Kanji prioritarios", page)
        self.assertNotIn("Buscar kanji", page)
        self.assertNotIn("Japones", page)
        self.assertNotIn(">Leitura<", page)
        self.assertGreaterEqual(page.count('<input type="hidden" name="lang" value="es">'), 3)
        self.assertIn('href="/flashcards?status=pending&amp;lang=es"', page)

        everything = lang_jp_web.dashboard_page(self.db_path, {})
        self.assertIn("Kanji prioritarios", everything)
        self.assertIn('href="/kanji"', everything)
        self.assertNotIn('name="lang"', everything)

    def test_character_pages_name_the_script_per_language(self) -> None:
        spanish = lang_jp_web.kanji_page(self.db_path, {"lang": ["es"]})
        self.assertIn("nao usa caracteres", spanish)

        chinese = lang_jp_web.kanji_page(self.db_path, {"lang": ["zh"]})
        self.assertIn("<h2>Hanzi</h2>", chinese)
        self.assertIn('href="/kanji?lang=zh">Hanzi</a>', chinese)

        detail = lang_jp_web.kanji_detail_page(self.db_path, self.kanji_id, {"lang": ["ja"]})
        self.assertIn("Idioma: Japanese (ja)", detail)
        self.assertIn("On:", detail)
        self.assertIn('href="/kanji?lang=ja">Voltar para kanji</a>', detail)

    def test_capture_page_does_not_claim_every_language_is_captured(self) -> None:
        page = lang_jp_web.capture_page(self.db_path, {})

        self.assertNotIn("todos os idiomas entram", page)
        self.assertIn("nos idiomas marcados", page)
        self.assertIn("(idioma dos significados)", page)

    def test_every_page_title_names_the_catalog(self) -> None:
        # LANG_CAPTURE_SKIP_WINDOW_TITLES="Lang Catalog" skips the catalog only
        # when every tab title names it (plain "Fontes" would be captured).
        pages = {
            "dashboard": lang_jp_web.dashboard_page(self.db_path, {}),
            "terms": lang_jp_web.terms_page(self.db_path, {}),
            "kanji": lang_jp_web.kanji_page(self.db_path, {}),
            "sources": lang_jp_web.sources_page(self.db_path, {"lang": ["ja"]}),
            "flashcards": lang_jp_web.flashcards_page(self.db_path, {}),
            "capture": lang_jp_web.capture_page(self.db_path, {}),
            "kanji_detail": lang_jp_web.kanji_detail_page(self.db_path, self.kanji_id, {}),
        }
        for name, page in pages.items():
            with self.subTest(page=name):
                title = page.split("<title>", 1)[1].split("</title>", 1)[0]
                self.assertTrue(title.endswith("Lang Catalog"), title)
        self.assertEqual(lang_jp_web.page_title("Lang Catalog"), "Lang Catalog")
        self.assertEqual(lang_jp_web.page_title("Fontes"), "Fontes - Lang Catalog")

    def test_table_sort_is_locale_neutral(self) -> None:
        self.assertNotIn('toLocaleLowerCase("pt-BR")', lang_jp_web.PAGE_SCRIPT)
        self.assertIn("sortableText(value)", lang_jp_web.PAGE_SCRIPT)

    def test_source_language_action_keeps_the_selected_language(self) -> None:
        handler = type("TestHandler", (lang_jp_web.CatalogRequestHandler,), {"db_path": self.db_path})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            body = urlencode({"language": "pt", "lang": "es"})
            connection.request(
                "POST",
                f"/actions/sources/{self.es_source}/language",
                body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            location = response.getheader("Location") or ""
            response.read()
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            connection.request("GET", "/terms?lang=ru&q=%D0%9F%D0%A0%D0%98%D0%92%D0%95%D0%A2")
            page = connection.getresponse().read().decode("utf-8")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 303)
        self.assertTrue(location.startswith(f"/sources/{self.es_source}?lang=es&notice="), location)
        self.assertIn(">Привет</a>", page)

    def test_source_detail_offers_delete(self) -> None:
        page = lang_jp_web.source_detail_page(self.db_path, self.es_source, {"lang": ["es"]})
        self.assertIn(f'action="/actions/sources/{self.es_source}/delete"', page)
        self.assertIn("confirm(", page)

    def test_source_delete_action_removes_source_and_refreshes_stats(self) -> None:
        with closing(db_connect(self.db_path)) as connection:
            lang_jp.recalculate_term_stats(connection, self.es_term)
            connection.commit()
        handler = type("TestHandler", (lang_jp_web.CatalogRequestHandler,), {"db_path": self.db_path})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            connection.request(
                "POST",
                f"/actions/sources/{self.mixed_source}/delete",
                body=urlencode({"lang": "es"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            location = response.getheader("Location") or ""
            response.read()
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            connection.request(
                "POST",
                f"/actions/sources/{self.mixed_source}/delete",
                body=urlencode({"lang": "es"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            missing = connection.getresponse()
            missing_location = missing.getheader("Location") or ""
            missing.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 303)
        self.assertTrue(location.startswith("/sources?lang=es&notice="), location)
        self.assertIn("level=error", missing_location)
        with closing(db_connect(self.db_path)) as connection:
            count = lambda sql: connection.execute(sql, (self.mixed_source,)).fetchone()[0]
            self.assertEqual(count("SELECT COUNT(*) FROM sources WHERE id = ?"), 0)
            self.assertEqual(count("SELECT COUNT(*) FROM source_segments WHERE source_id = ?"), 0)
            self.assertEqual(count("SELECT COUNT(*) FROM source_analysis_segments WHERE source_id = ?"), 0)
            self.assertEqual(count("SELECT COUNT(*) FROM source_kanji WHERE source_id = ?"), 0)
            stats = connection.execute(
                "SELECT total_occurrences, distinct_sources FROM term_stats WHERE term_id = ?",
                (self.es_term,),
            ).fetchone()
            self.assertEqual((stats[0], stats[1]), (0, 0))
            # Terms and their cards survive; only the source goes.
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 5)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM flashcards").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM sources WHERE id = ?", (self.es_source,)).fetchone()[0],
                1,
            )

    def test_capture_page_adds_and_removes_skip_rules(self) -> None:
        page = lang_jp_web.capture_page(self.db_path, {})
        self.assertIn("Janelas ignoradas", page)
        self.assertIn("Nenhuma janela ignorada", page)

        handler = type("TestHandler", (lang_jp_web.CatalogRequestHandler,), {"db_path": self.db_path})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(fields: dict[str, str]) -> str:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            connection.request(
                "POST",
                "/actions/capture-skip-rules",
                body=urlencode(fields),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            location = response.getheader("Location") or ""
            response.read()
            connection.close()
            return location

        try:
            added = post({"op": "add", "kind": "process", "pattern": "Brave.exe"})
            invalid = post({"op": "add", "kind": "window", "pattern": "x"})
            page_with_rule = lang_jp_web.capture_page(self.db_path, {})
            removed = post({"op": "remove", "kind": "process", "pattern": "brave.exe"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertIn("level=info", added)
        self.assertIn("level=error", invalid)
        self.assertIn('<span class="mono">brave.exe</span>', page_with_rule)
        self.assertIn("level=info", removed)
        with closing(db_connect(self.db_path)) as connection:
            self.assertEqual(lang_jp.list_capture_skip_rules(connection), [])

    def test_source_detail_offers_on_demand_translation(self) -> None:
        page = lang_jp_web.source_detail_page(self.db_path, self.es_source, {"lang": ["es"]})
        self.assertIn(f'action="/actions/sources/{self.es_source}/translate"', page)
        self.assertIn(">Traduzir</button>", page)

    def test_source_translate_action_stores_translation_and_reports_failures(self) -> None:
        calls: list[str] = []

        def fake_request(text: str, *, source_language: str, target_language: str) -> str:
            calls.append(text)
            return "\n".join(f"{target_language}:{line}" for line in text.split("\n"))

        def failing_request(text: str, *, source_language: str, target_language: str) -> str:
            raise OSError("HTTP Error 429: Too Many Requests")

        handler = type("TestHandler", (lang_jp_web.CatalogRequestHandler,), {"db_path": self.db_path})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post_translate() -> tuple[int, str]:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            connection.request(
                "POST",
                f"/actions/sources/{self.mixed_source}/translate",
                body=urlencode({"lang": "es"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            location = response.getheader("Location") or ""
            response.read()
            connection.close()
            return response.status, location

        original = lang_jp.google_gtx_request
        try:
            lang_jp.google_gtx_request = fake_request
            status, location = post_translate()
            lang_jp.google_gtx_request = failing_request
            failed_status, failed_location = post_translate()
        finally:
            lang_jp.google_gtx_request = original
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(status, 303)
        self.assertTrue(location.startswith(f"/sources/{self.mixed_source}?lang=es&notice="), location)
        self.assertIn("level=info", location)
        # Both source lines go out in a single request.
        self.assertEqual(calls, ["¡Vamos a la batalla!\n回復"])
        self.assertEqual(failed_status, 303)
        self.assertIn("level=error", failed_location)
        with closing(db_connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT status, translated_text, error_message FROM source_translations WHERE source_id = ?",
                (self.mixed_source,),
            ).fetchone()
        # The failed retry is recorded; the worker no longer picks it up on its own.
        self.assertEqual(row["status"], "failed")
        self.assertIn("429", row["error_message"])

    def test_translate_source_lines_falls_back_per_line_when_lines_merge(self) -> None:
        calls: list[str] = []

        def merging_request(text: str, *, source_language: str, target_language: str) -> str:
            calls.append(text)
            return text.replace("\n", " ")

        translated = lang_jp.translate_source_lines(
            ["一行目", "二行目"],
            source_language="ja",
            target_language="en",
            request_translation=merging_request,
        )
        self.assertEqual(translated, "一行目\n二行目")
        self.assertEqual(calls, ["一行目\n二行目", "一行目", "二行目"])

    def test_translate_source_on_demand_keeps_text_already_in_target_language(self) -> None:
        def unexpected_request(text: str, *, source_language: str, target_language: str) -> str:
            raise AssertionError("no request expected")

        with closing(db_connect(self.db_path)) as connection:
            source_id = self.insert_source(connection, "en", "Hello\n  world  ")
            result = lang_jp.translate_source_on_demand(
                connection, source_id=source_id, request_translation=unexpected_request
            )
            text = connection.execute(
                "SELECT translated_text FROM source_translations WHERE source_id = ?", (source_id,)
            ).fetchone()[0]
        self.assertEqual(result["status"], "translated")
        self.assertEqual(text, "Hello\nworld")

    def test_delete_source_only_removes_images_under_data(self) -> None:
        outside = Path(self.temp_dir.name) / "outside.png"
        outside.write_bytes(b"png")
        data_dir = lang_jp.ROOT / "data"
        data_dir.mkdir(exist_ok=True)
        inside = data_dir / f"test-delete-source-{self.es_source}-{id(self)}.png"
        inside.write_bytes(b"png")
        try:
            with closing(db_connect(self.db_path)) as connection:
                connection.execute("UPDATE sources SET source_path = ? WHERE id = ?", (str(outside), self.ar_source))
                connection.execute(
                    "UPDATE sources SET source_path = ? WHERE id = ?",
                    (f"data/{inside.name}", self.es_source),
                )
                kept = lang_jp.delete_source(connection, source_id=self.ar_source, delete_image=True)
                removed = lang_jp.delete_source(connection, source_id=self.es_source, delete_image=True)
                connection.commit()
            self.assertFalse(lang_jp.remove_deleted_source_image(kept))
            self.assertTrue(outside.exists())
            self.assertTrue(lang_jp.remove_deleted_source_image(removed))
            self.assertFalse(inside.exists())
            self.assertEqual(removed["orphaned_terms"], [self.es_water])
        finally:
            inside.unlink(missing_ok=True)


class DisplayFormattingTest(unittest.TestCase):
    BRAZIL = timezone(timedelta(hours=-3))

    def test_format_datetime_display_converts_utc_and_offset_values_to_local_time(self) -> None:
        display = lang_jp_web.format_datetime_display

        self.assertEqual(display("2026-03-27 00:17:48", tz=self.BRAZIL), "26/03/2026 21:17:48")
        self.assertEqual(
            display("2026-07-08T22:55:58.218041600-03:00", tz=self.BRAZIL),
            "08/07/2026 22:55:58",
        )
        self.assertEqual(display("2026-03-26", tz=self.BRAZIL), "26/03/2026")
        self.assertEqual(display(None), "-")
        self.assertEqual(display("not a date"), "not a date")

    def test_datetime_sort_value_normalizes_to_utc(self) -> None:
        later = lang_jp_web.datetime_sort_value("2026-07-08T23:30:00-03:00")
        earlier = lang_jp_web.datetime_sort_value("2026-07-09 02:14:31")

        self.assertEqual(later, "2026-07-09 02:30:00")
        self.assertGreater(later, earlier)

    def test_html_fragment_text_keeps_only_visible_text(self) -> None:
        self.assertEqual(
            lang_jp_web.html_fragment_text('<div style="font-size: 2.5em;">川</div><div>かわ</div>'),
            "川 かわ",
        )
        self.assertEqual(lang_jp_web.html_fragment_text("<b>回</b>復 &amp; more"), "回復 & more")
        self.assertEqual(lang_jp_web.html_fragment_text(None), "")

    def test_format_score_formats_numbers_and_handles_missing(self) -> None:
        self.assertEqual(lang_jp_web.format_score(14900), "14900.0")
        self.assertEqual(lang_jp_web.format_score(None), "-")


class PathResolutionTest(unittest.TestCase):
    def test_resolve_repo_local_path_maps_old_repo_root_into_current_workspace(self) -> None:
        stale_path = "C:/Users/old-user/work/Lang/JP/data/.gitkeep"

        resolved = lang_jp.resolve_repo_local_path(stale_path)

        self.assertEqual(resolved, TESTS_ROOT / "data" / ".gitkeep")
        self.assertTrue(resolved.exists())

    def test_resolve_repo_local_path_supports_repo_relative_paths(self) -> None:
        resolved = lang_jp.resolve_repo_local_path("data/.gitkeep")

        self.assertEqual(resolved, TESTS_ROOT / "data" / ".gitkeep")
        self.assertTrue(resolved.exists())

    def test_normalize_repo_storage_path_relativizes_workspace_files(self) -> None:
        absolute_path = TESTS_ROOT / "data" / ".gitkeep"

        normalized = lang_jp.normalize_repo_storage_path(absolute_path)

        self.assertEqual(normalized, "data/.gitkeep")

    def test_normalize_repo_storage_path_rewrites_old_workspace_roots(self) -> None:
        stale_path = "C:/Users/old-user/work/Lang/JP/data/.gitkeep"

        normalized = lang_jp.normalize_repo_storage_path(stale_path)

        self.assertEqual(normalized, "data/.gitkeep")


class RuntimeSchemaViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "lang_jp_schema_test.sqlite3"
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(schema_sql)
            ensure_runtime_schema(connection)
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ensure_runtime_schema_skips_view_rebuild_when_definition_matches(self) -> None:
        statements: list[str] = []
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(statements.append)
        try:
            ensure_runtime_schema(connection)
        finally:
            connection.set_trace_callback(None)
            connection.close()

        normalized_statements = [statement.lower() for statement in statements]
        self.assertFalse(
            any("drop view if exists v_terms_overview" in statement for statement in normalized_statements)
        )
        self.assertFalse(
            any("drop view if exists v_kanji_overview" in statement for statement in normalized_statements)
        )

    def test_ensure_runtime_schema_refreshes_stale_view_definition(self) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DROP VIEW IF EXISTS v_terms_overview")
            connection.execute("CREATE VIEW v_terms_overview AS SELECT id FROM terms")
            connection.commit()

            ensure_runtime_schema(connection)

            row = connection.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'view' AND name = 'v_terms_overview'
                """
            ).fetchone()
        finally:
            connection.close()

        self.assertIsNotNone(row)
        self.assertIn("coalesce(ts.total_occurrences, 0)", str(row["sql"]).lower())


if __name__ == "__main__":
    unittest.main()
