"""The knowledge layer wired into the catalog: schema, change log, CLI, pages and API.

``test_knowledge`` covers the layer by itself; this file covers what the rest
of the catalog does with it. AnkiConnect is a fake throughout.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import http.client
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import anki_connect
import knowledge
import lang_jp
import lang_jp_web
from catalog_changes import Watch, changes_since, current_revision
from knowledge import ITEM_KANJI, ITEM_TERM, Evidence
from lang_jp import SCHEMA_PATH, db_connect
from test_knowledge import FakeAnki, StaticProvider


class KnowledgeCatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # CLI commands leave their connection to the collector; Windows will not
        # delete a database that is still open.
        self.addCleanup(gc.collect)
        self.db_path = Path(self.temp_dir.name) / "knowledge.sqlite3"
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()
        with self.connect() as connection:
            for term_id, lemma, score in ((1, "回復", 30), (2, "猫", 20), (3, "犬", 10)):
                connection.execute(
                    "INSERT INTO terms (id, canonical_key, language_code, lemma) VALUES (?, ?, 'ja', ?)",
                    (term_id, f"ja::{lemma}", lemma),
                )
                connection.execute(
                    "INSERT INTO term_stats (term_id, priority_score) VALUES (?, ?)",
                    (term_id, score),
                )
            connection.execute("INSERT INTO kanji (id, literal, language_code) VALUES (1, '回', 'ja')")
            connection.execute(
                """
                INSERT INTO flashcards (term_id, front_text, back_text, anki_note_id)
                VALUES (2, '猫', 'gato', 103)
                """
            )

    @contextlib.contextmanager
    def connect(self):
        connection = db_connect(self.db_path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def refresh(self, *evidence: Evidence) -> None:
        with self.connect() as connection:
            knowledge.refresh_provider(connection, StaticProvider("anki", 0.9, list(evidence)))

    def run_command(self, func, **arguments: object) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            func(argparse.Namespace(db=self.db_path, **arguments))
        return output.getvalue()


class SchemaTest(KnowledgeCatalogTestCase):
    def test_fresh_and_upgraded_databases_agree(self) -> None:
        def objects(connection: sqlite3.Connection) -> dict[str, str]:
            return {
                row["name"]: lang_jp.normalize_schema_sql(row["sql"])
                for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name LIKE '%knowledge%' AND name NOT LIKE 'catalog_change_%' AND sql IS NOT NULL"
                )
            }

        upgraded_path = Path(self.temp_dir.name) / "upgraded.sqlite3"
        # A catalog from before the knowledge layer: only the parent tables.
        upgraded = sqlite3.connect(upgraded_path)
        upgraded.row_factory = sqlite3.Row
        try:
            upgraded.executescript(
                "CREATE TABLE terms (id INTEGER PRIMARY KEY); CREATE TABLE kanji (id INTEGER PRIMARY KEY);"
            )
            knowledge.ensure_schema(upgraded)
            with self.connect() as fresh:
                self.assertEqual(objects(upgraded), objects(fresh))
                self.assertFalse(knowledge.ensure_schema(fresh))
        finally:
            upgraded.close()

    def test_overview_views_report_unknown_without_evidence(self) -> None:
        self.refresh(Evidence(ITEM_TERM, 1, "mature", stability_days=42))
        with self.connect() as connection:
            terms = {
                row["id"]: (row["knowledge_status"], round(row["knowledge_score"], 3))
                for row in connection.execute("SELECT * FROM v_terms_overview")
            }
            kanji = connection.execute("SELECT knowledge_status FROM v_kanji_overview").fetchone()
        self.assertEqual(terms, {1: ("mature", 0.667), 2: ("unknown", 0), 3: ("unknown", 0)})
        self.assertEqual(kanji["knowledge_status"], "unknown")


class ChangeLogTest(KnowledgeCatalogTestCase):
    def test_only_items_whose_knowledge_changed_are_logged(self) -> None:
        self.refresh(
            Evidence(ITEM_TERM, 1, "young", stability_days=3),
            Evidence(ITEM_KANJI, 1, "young", stability_days=3),
        )
        with self.connect() as connection:
            revision = current_revision(connection)
        # Same evidence again, except term 1 matured.
        self.refresh(
            Evidence(ITEM_TERM, 1, "mature", stability_days=30),
            Evidence(ITEM_KANJI, 1, "young", stability_days=3),
        )
        with self.connect() as connection:
            self.assertTrue(changes_since(connection, revision, Watch.of(term=[1])).changed)
            self.assertFalse(changes_since(connection, revision, Watch.of("kanji", term=[2])).changed)

    def test_manual_mark_wakes_the_item_page(self) -> None:
        with self.connect() as connection:
            revision = current_revision(connection)
            knowledge.mark_known(connection, ITEM_KANJI, 1)
            self.assertTrue(changes_since(connection, revision, Watch.of(kanji=[1])).changed)
            self.assertFalse(changes_since(connection, revision, Watch.of("term")).changed)


class CommandTest(KnowledgeCatalogTestCase):
    def test_sync_knowledge_reads_anki_and_reports(self) -> None:
        anki = FakeAnki()
        anki.add_card(1, note=103, card_type=2, interval=30, review_days=(0, 4))
        with mock.patch.object(anki_connect, "request", anki):
            output = self.run_command(lang_jp.sync_knowledge_command, host="127.0.0.1", port=8765)
        self.assertEqual(output.strip(), "Knowledge from anki: 1 items (1 mature)")
        with self.connect() as connection:
            self.assertEqual(knowledge.load_status(connection, ITEM_TERM, 2)[0], "mature")

    def test_mark_known_and_clear(self) -> None:
        self.run_command(lang_jp.mark_known_command, term_id=3, kanji_id=None, clear=False)
        with self.connect() as connection:
            self.assertEqual(knowledge.load_status(connection, ITEM_TERM, 3), ("known", 1.0))
        output = self.run_command(lang_jp.mark_known_command, term_id=3, kanji_id=None, clear=True)
        self.assertIn("Cleared", output)
        with self.connect() as connection:
            self.assertEqual(knowledge.load_status(connection, ITEM_TERM, 3), ("unknown", 0.0))
        with self.assertRaises(SystemExit):
            self.run_command(lang_jp.mark_known_command, term_id=None, kanji_id=99, clear=False)

    def test_list_terms_filters_by_knowledge(self) -> None:
        self.refresh(Evidence(ITEM_TERM, 1, "young", stability_days=3))
        output = self.run_command(
            lang_jp.list_terms, order="priority", language="all", limit=10, knowledge="unknown"
        )
        self.assertNotIn("回復", output)
        # What is left is the next to learn, highest score first.
        self.assertLess(output.index("猫"), output.index("犬"))


class WebTest(KnowledgeCatalogTestCase):
    def setUp(self) -> None:
        super().setUp()
        handler = type("TestHandler", (lang_jp_web.CatalogRequestHandler,), {"db_path": self.db_path})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.refresh(
            Evidence(
                ITEM_TERM, 1, "learning", stability_days=40, reviews=9, lapses=2,
                detail={"relearning": True, "studied_cards": 2},
            ),
            Evidence(ITEM_KANJI, 1, "mature", stability_days=60),
        )

    def request(self, method: str, path: str, fields: dict[str, str] | None = None, *, accept: str = "text/html"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        try:
            headers = {"Accept": accept}
            body = None
            if fields is not None:
                body = urlencode(fields)
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Location") or "", response.read().decode("utf-8")
        finally:
            connection.close()

    def test_terms_page_shows_and_filters_knowledge(self) -> None:
        _, _, page = self.request("GET", "/terms")
        self.assertIn("Conhecimento", page)
        self.assertIn("knowledge-learning", page)

        _, _, filtered = self.request("GET", "/terms?knowledge=unknown")
        self.assertIn("Mostrando 1 a 2 de 2 resultados", filtered)
        self.assertNotIn("/terms/1\"", filtered)
        self.assertIn('<option value="unknown" selected>', filtered)
        # An unrecognised value is no filter at all.
        _, _, unfiltered = self.request("GET", "/terms?knowledge=bogus")
        self.assertIn("de 3 resultados", unfiltered)

    def test_kanji_page_filters_knowledge(self) -> None:
        _, _, page = self.request("GET", "/kanji?knowledge=mature")
        self.assertIn("Mostrando 1 a 1 de 1 resultado", page)
        _, _, empty = self.request("GET", "/kanji?knowledge=known")
        self.assertIn("Nenhum resultado encontrado", empty)

    def test_detail_pages_show_the_evidence(self) -> None:
        _, _, term_page = self.request("GET", "/terms/1")
        self.assertIn("Forca: 66%", term_page)
        self.assertIn("reaprendendo, 2 cards", term_page)
        self.assertIn("Marcar como conhecido", term_page)
        _, _, untouched = self.request("GET", "/terms/3")
        self.assertIn("Nenhuma fonte de estudo tem dados", untouched)
        _, _, kanji_page = self.request("GET", "/kanji/1")
        self.assertIn("knowledge-mature", kanji_page)

    def test_mark_action_round_trip(self) -> None:
        fields = {"kind": "term", "id": "3", "op": "mark", "next": "/terms/3?lang=ja"}
        status, location, _ = self.request("POST", "/actions/knowledge", fields)
        self.assertEqual(status, 303)
        parsed = urlparse(location)
        self.assertEqual(parsed.path, "/terms/3")
        self.assertEqual(parse_qs(parsed.query)["notice"], ["Marcado como conhecido."])
        _, _, page = self.request("GET", location)
        self.assertIn("Marcado por voce", page)
        self.assertIn("Remover marcacao de conhecido", page)

        status, _, body = self.request(
            "POST", "/actions/knowledge", {**fields, "op": "clear"}, accept="application/json"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "unknown")

    def test_invalid_actions_are_refused(self) -> None:
        for fields in (
            {"kind": "phrase", "id": "1", "op": "mark"},
            {"kind": "term", "id": "1", "op": "delete"},
            {"kind": "term", "id": "abc", "op": "mark"},
            {"kind": "term", "id": "99", "op": "mark"},
        ):
            status, _, _ = self.request("POST", "/actions/knowledge", fields)
            self.assertEqual(status, 404, fields)

    def test_api_exposes_knowledge(self) -> None:
        _, _, body = self.request("GET", "/api/terms?knowledge=learning")
        payload = json.loads(body)
        self.assertEqual(payload["meta"]["knowledge"], "learning")
        self.assertEqual(
            [(term["id"], term["knowledge_status"]) for term in payload["terms"]], [(1, "learning")]
        )

        _, _, body = self.request("GET", "/api/terms/1")
        detail = json.loads(body)
        self.assertNotIn("knowledge_status", detail)
        self.assertEqual(detail["knowledge"]["status"], "learning")
        self.assertEqual(detail["knowledge"]["evidence"][0]["provider"], "anki")
        self.assertIs(detail["knowledge"]["evidence"][0]["detail"]["relearning"], True)


if __name__ == "__main__":
    unittest.main()
