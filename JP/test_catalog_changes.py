from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import catalog_changes
import lang_jp
from catalog_changes import Watch, changes_since, current_revision
from lang_jp import SCHEMA_PATH, db_connect, ensure_runtime_schema


class CatalogChangeLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "changes.sqlite3"
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()
        # The tables schema.sql created get their triggers on the next connection.
        self.connection = db_connect(self.db_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def keys_after(self, revision: int) -> list[tuple[str, int | None]]:
        return [
            (row["entity"], row["entity_id"])
            for row in self.connection.execute(
                "SELECT entity, entity_id FROM catalog_changes WHERE id > ? ORDER BY id",
                (revision,),
            )
        ]

    def insert_source(self, title: str = "screen") -> int:
        return int(
            self.connection.execute(
                "INSERT INTO sources (source_type, language_code, title, raw_text) VALUES ('text', 'en', ?, 'hi')",
                (title,),
            ).lastrowid
        )

    def insert_term(self, lemma: str) -> int:
        return int(
            self.connection.execute(
                "INSERT INTO terms (canonical_key, language_code, lemma) VALUES (?, 'en', ?)",
                (f"en::{lemma}", lemma),
            ).lastrowid
        )

    def test_every_watched_table_gets_one_trigger_per_event(self) -> None:
        names = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'catalog_change_%'"
            )
        }
        for spec in catalog_changes.CHANGE_SPECS:
            for event in catalog_changes.TRIGGER_EVENTS:
                self.assertIn(catalog_changes.trigger_name(spec.table, event), names)
        self.assertIn("catalog_change_prune", names)

    def test_up_to_date_database_is_left_alone(self) -> None:
        self.assertFalse(catalog_changes.ensure_catalog_change_log(self.connection))

    def test_stale_and_obsolete_triggers_are_replaced(self) -> None:
        name = catalog_changes.trigger_name("terms", "INSERT")
        self.connection.execute(f"DROP TRIGGER {name}")
        self.connection.execute(f"CREATE TRIGGER {name} AFTER INSERT ON terms BEGIN SELECT 1; END")
        self.connection.execute(
            "CREATE TRIGGER catalog_change_retired AFTER INSERT ON terms BEGIN SELECT 1; END"
        )
        self.assertTrue(catalog_changes.ensure_catalog_change_log(self.connection))
        triggers = dict(
            self.connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'catalog_change_%'"
            ).fetchall()
        )
        self.assertNotIn("catalog_change_retired", triggers)
        self.assertIn("INSERT INTO catalog_changes", triggers[name])

    def test_database_without_the_log_gets_it(self) -> None:
        self.connection.execute("DROP TABLE catalog_changes")
        for (name,) in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'catalog_change_%'"
        ).fetchall():
            self.connection.execute(f"DROP TRIGGER {name}")
        self.connection.commit()
        ensure_runtime_schema(self.connection)
        self.insert_term("again")
        self.assertEqual([entity for entity, _ in self.keys_after(0)], ["term"])

    def test_source_and_term_links_log_both_sides(self) -> None:
        start = current_revision(self.connection)
        source_id = self.insert_source()
        term_id = self.insert_term("hello")
        self.connection.execute(
            "INSERT INTO source_terms (source_id, term_id, surface_form) VALUES (?, ?, 'hello')",
            (source_id, term_id),
        )
        segment_id = self.connection.execute(
            "INSERT INTO source_segments (source_id, segment_key, segment_text) VALUES (?, 'k', 'hello')",
            (source_id,),
        ).lastrowid
        start_segment_terms = current_revision(self.connection)
        self.connection.execute(
            "INSERT INTO source_segment_terms (source_segment_id, term_id, surface_form) VALUES (?, ?, 'hello')",
            (segment_id, term_id),
        )
        self.connection.execute(
            """
            INSERT INTO source_translations (source_id, target_language, provider, status)
            VALUES (?, 'en', 'test', 'pending')
            """,
            (source_id,),
        )
        keys = self.keys_after(start)
        self.assertEqual(keys[:4], [("source", source_id), ("term", term_id), ("source", source_id), ("term", term_id)])
        self.assertEqual(
            self.keys_after(start_segment_terms),
            [("source", source_id), ("term", term_id), ("source", source_id)],
        )

    def test_update_that_moves_a_link_logs_old_and_new_keys(self) -> None:
        source_id = self.insert_source()
        first = self.insert_term("one")
        second = self.insert_term("two")
        link_id = self.connection.execute(
            "INSERT INTO source_terms (source_id, term_id, surface_form) VALUES (?, ?, 'x')",
            (source_id, first),
        ).lastrowid
        start = current_revision(self.connection)
        self.connection.execute("UPDATE source_terms SET term_id = ? WHERE id = ?", (second, link_id))
        self.assertEqual(
            sorted(self.keys_after(start)),
            sorted([("source", source_id), ("term", second), ("term", first)]),
        )

    def test_capture_jobs_log_only_job_keys(self) -> None:
        start = current_revision(self.connection)
        job_id = lang_jp.create_capture_job(
            self.connection,
            captured_at="2026-11-14T10:00:00-03:00",
            temp_image_path="x.png",
            image_sha256="sha",
            image_phash=None,
        )
        lang_jp.update_capture_job_status(self.connection, job_id=job_id, status="rejected", last_error="no text")
        self.assertEqual({entity for entity, _ in self.keys_after(start)}, {"job"})
        status = changes_since(self.connection, start, Watch.of("source", "term", "kanji", "flashcard", "language"))
        self.assertFalse(status.changed)

    def test_stats_recalculation_that_changes_nothing_logs_nothing(self) -> None:
        source_id = self.insert_source()
        term_id = self.insert_term("hello")
        self.connection.execute(
            "INSERT INTO source_terms (source_id, term_id, surface_form) VALUES (?, ?, 'hello')",
            (source_id, term_id),
        )
        lang_jp.recalculate_term_stats(self.connection, term_id)
        start = current_revision(self.connection)
        lang_jp.recalculate_term_stats(self.connection, term_id)
        self.assertEqual(self.keys_after(start), [])
        self.connection.execute(
            "UPDATE source_terms SET count_in_source = 3 WHERE term_id = ?", (term_id,)
        )
        start = current_revision(self.connection)
        lang_jp.recalculate_term_stats(self.connection, term_id)
        self.assertEqual(self.keys_after(start), [("term", term_id)])

    def test_deleting_a_source_logs_the_terms_its_links_pointed_at(self) -> None:
        source_id = self.insert_source()
        term_id = self.insert_term("hello")
        self.connection.execute(
            "INSERT INTO source_terms (source_id, term_id, surface_form) VALUES (?, ?, 'hello')",
            (source_id, term_id),
        )
        self.connection.commit()
        start = current_revision(self.connection)
        lang_jp.delete_source(self.connection, source_id=source_id)
        keys = self.keys_after(start)
        self.assertIn(("source", source_id), keys)
        self.assertIn(("term", term_id), keys)

    def test_languages_log_a_kind_level_key(self) -> None:
        start = current_revision(self.connection)
        lang_jp.set_capture_languages(self.connection, ["ja"])
        self.assertIn(("language", None), self.keys_after(start))
        self.assertTrue(changes_since(self.connection, start, Watch.of("language")).changed)

    def test_log_is_pruned_to_its_retention_window(self) -> None:
        self.connection.executemany(
            "INSERT INTO catalog_changes (entity, entity_id) VALUES ('term', ?)",
            [(index,) for index in range(catalog_changes.CHANGE_RETENTION + 50)],
        )
        count, oldest = self.connection.execute(
            "SELECT COUNT(*), MIN(id) FROM catalog_changes"
        ).fetchone()
        self.assertEqual(count, catalog_changes.CHANGE_RETENTION)
        status = changes_since(self.connection, oldest - 10, Watch.of(term=[1]))
        self.assertTrue(status.reset)
        self.assertFalse(changes_since(self.connection, oldest - 1, Watch.of(term=[-5])).reset)


class ChangesSinceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(catalog_changes.CATALOG_CHANGE_SCHEMA_SQL)
        self.connection.executemany(
            "INSERT INTO catalog_changes (entity, entity_id) VALUES (?, ?)",
            [("term", 1), ("source", 7), ("job", 3)],
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_watched_ids_and_kinds(self) -> None:
        self.assertTrue(changes_since(self.connection, 0, Watch.of(term=[1])).changed)
        self.assertFalse(changes_since(self.connection, 0, Watch.of(term=[2])).changed)
        self.assertTrue(changes_since(self.connection, 0, Watch.of("source")).changed)
        self.assertFalse(changes_since(self.connection, 1, Watch.of(term=[1])).changed)
        self.assertFalse(changes_since(self.connection, 0, Watch.of("kanji", flashcard=[1])).changed)

    def test_up_to_date_and_empty_watch(self) -> None:
        status = changes_since(self.connection, 3, Watch.of("term"))
        self.assertEqual((status.revision, status.changed, status.reset), (3, False, False))
        self.assertFalse(changes_since(self.connection, 0, Watch()).changed)

    def test_revision_ahead_of_the_log_resets(self) -> None:
        status = changes_since(self.connection, 99, Watch.of("term"))
        self.assertEqual((status.revision, status.changed, status.reset), (3, True, True))

    def test_watch_round_trips_and_rejects_junk(self) -> None:
        watch = Watch.of("source", "language", term=[3, 1], kanji=[9])
        self.assertEqual(watch.encode(), "language,source,kanji:9,term:1,term:3")
        self.assertEqual(Watch.decode(watch.encode()), watch)
        self.assertEqual(Watch.decode(""), Watch())
        for junk in ("term:x", "drop table;", "term:1:2"):
            with self.assertRaises(ValueError):
                Watch.decode(junk)


if __name__ == "__main__":
    unittest.main()
