from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path

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
