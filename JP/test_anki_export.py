"""Anki export and sync across languages: decks, identity, tags, filters, RTL.

AnkiConnect is replaced by an in-memory fake that mimics Anki's duplicate
check (collection-wide by default, per deck with ``duplicateScope: deck``), so
nothing here talks to a running Anki.
"""

from __future__ import annotations

import argparse
import csv
import gc
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import lang_jp
from lang_jp import SCHEMA_PATH, db_connect


def strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()


class FakeAnki:
    """Just enough of AnkiConnect for sync_anki and sync_anki_kanji."""

    def __init__(self) -> None:
        self.notes: dict[int, dict[str, object]] = {}
        self.decks: set[str] = set()
        self.add_note_options: list[dict[str, object]] = []
        self.repositioned: list[list[int]] = []
        self.next_id = 1000

    def add_existing(self, *, deck: str, front: str, back: str, tags: list[str]) -> int:
        note_id = self.next_id
        self.next_id += 1
        self.notes[note_id] = {
            "deck": deck,
            "model": "Basic",
            "fields": {"Front": front, "Back": back},
            "tags": list(tags),
            "cards": [note_id * 10],
        }
        return note_id

    def _matches(self, note: dict[str, object], query: str) -> bool:
        deck = re.search(r'deck:"((?:[^"\\]|\\.)*)"', query)
        if deck:
            name = deck.group(1).replace('\\"', '"')
            if note["deck"] != name and not str(note["deck"]).startswith(name + "::"):
                return False
        model = re.search(r'note:"([^"]*)"', query)
        if model and note["model"] != model.group(1):
            return False
        tags = {str(tag).lower() for tag in note["tags"]}
        for tag in re.findall(r"tag:(\S+)", query):
            if tag.lower() not in tags:
                return False
        return True

    def _is_duplicate(self, note: dict[str, object]) -> bool:
        options = note.get("options") or {}
        front = strip_html(str(note["fields"]["Front"]))
        scope_deck = None
        if options.get("duplicateScope") == "deck":
            scope_deck = (options.get("duplicateScopeOptions") or {}).get("deckName") or note["deckName"]
        for existing in self.notes.values():
            if existing["model"] != note["modelName"]:
                continue
            if scope_deck is not None and existing["deck"] != scope_deck:
                continue
            if strip_html(str(existing["fields"]["Front"])) == front:
                return True
        return False

    def __call__(self, *, host: str, port: int, action: str, params: dict | None = None) -> object:
        del host, port
        params = params or {}
        if action == "version":
            return 6
        if action == "modelNames":
            return ["Basic"]
        if action == "createDeck":
            self.decks.add(params["deck"])
            return 1
        if action == "apiReflect":
            return {"actions": ["repositionNewCards"]}
        if action == "findNotes":
            return [note_id for note_id, note in self.notes.items() if self._matches(note, params["query"])]
        if action == "notesInfo":
            return [
                {
                    "noteId": note_id,
                    "modelName": self.notes[note_id]["model"],
                    "tags": list(self.notes[note_id]["tags"]),
                    "fields": {
                        name: {"value": value, "order": index}
                        for index, (name, value) in enumerate(self.notes[note_id]["fields"].items())
                    },
                    "cards": list(self.notes[note_id]["cards"]),
                }
                for note_id in params["notes"]
                if note_id in self.notes
            ]
        if action == "cardsInfo":
            return [{"cardId": card_id, "type": 0} for card_id in params["cards"]]
        if action == "addNote":
            note = params["note"]
            self.add_note_options.append(dict(note.get("options") or {}))
            if self._is_duplicate(note):
                return None  # anki_connect_request turns the duplicate error into None
            note_id = self.next_id
            self.next_id += 1
            self.notes[note_id] = {
                "deck": note["deckName"],
                "model": note["modelName"],
                "fields": dict(note["fields"]),
                "tags": list(note["tags"]),
                "cards": [note_id * 10],
            }
            return note_id
        if action == "updateNoteFields":
            self.notes[params["note"]["id"]]["fields"].update(params["note"]["fields"])
            return None
        if action == "changeDeck":
            for note in self.notes.values():
                if set(note["cards"]) & set(params["cards"]):
                    note["deck"] = params["deck"]
            return None
        if action == "addTags":
            for note_id in params["notes"]:
                self.notes[note_id]["tags"].extend(params["tags"].split())
            return None
        if action == "removeTags":
            removed = set(params["tags"].split())
            for note_id in params["notes"]:
                self.notes[note_id]["tags"] = [
                    tag for tag in self.notes[note_id]["tags"] if tag not in removed
                ]
            return None
        if action == "repositionNewCards":
            self.repositioned.append(list(params["cards"]))
            return len(params["cards"])
        raise AssertionError(f"Unexpected AnkiConnect action {action}")

    def deck_of(self, note_id: int) -> str:
        return str(self.notes[note_id]["deck"])


class AnkiExportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # The CLI functions use `with db_connect(...)`, which commits but does not
        # close; on Windows the file stays locked until the connection is collected.
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "anki_export.sqlite3"
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        finally:
            connection.close()

    def tearDown(self) -> None:
        gc.collect()
        self.temp_dir.cleanup()

    def connect(self) -> sqlite3.Connection:
        return db_connect(self.db_path)

    def add_term(
        self,
        connection: sqlite3.Connection,
        *,
        language_code: str,
        lemma: str,
        reading: str | None = None,
        meaning: str | None = None,
        surface: str | None = None,
        priority: float = 5.0,
    ) -> int:
        source_id = connection.execute(
            "INSERT INTO sources (source_type, language_code, raw_text) VALUES ('text', ?, ?)",
            (language_code or "ja", surface or lemma),
        ).lastrowid
        term_id = connection.execute(
            """
            INSERT INTO terms (canonical_key, language_code, lemma, reading, meaning_pt)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                lang_jp.canonical_key(lemma, reading, language_code or "ja"),
                language_code,
                lemma,
                reading,
                meaning,
            ),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO source_terms (source_id, term_id, surface_form, count_in_source)
            VALUES (?, ?, ?, 1)
            """,
            (source_id, term_id, surface or lemma),
        )
        connection.execute(
            """
            INSERT INTO term_stats (term_id, total_occurrences, distinct_sources, priority_score)
            VALUES (?, 1, 1, ?)
            """,
            (term_id, priority),
        )
        return int(term_id)

    def generate(self, connection: sqlite3.Connection, *, language: str = "all") -> int:
        args = argparse.Namespace(
            template_name="recognition_basic",
            deck_name=None,
            note_type="Basic",
            min_priority=0.0,
            limit=None,
            term_id=None,
            language=language,
            existing_only=False,
            refresh_existing=False,
        )
        created, _ = lang_jp.upsert_flashcards(connection, args)
        connection.commit()
        return created

    def export(self, name: str, **overrides: object) -> Path:
        output = Path(self.temp_dir.name) / name
        args = argparse.Namespace(
            db=self.db_path,
            output=output,
            language="all",
            status="all",
            include_header=False,
            mark_exported=False,
            format="anki",
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        lang_jp.export_anki_tsv(args)
        return output

    def read_anki_tsv(self, path: Path) -> tuple[list[str], list[dict[str, str]]]:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        headers = [line for line in lines if line.startswith("#")]
        body = [line for line in lines if not line.startswith("#")]
        records = [
            dict(zip(lang_jp.ANKI_TSV_COLUMNS, row))
            for row in csv.reader(body, delimiter="\t")
        ]
        return headers, records

    def sync_args(self, **overrides: object) -> argparse.Namespace:
        args = argparse.Namespace(
            db=self.db_path,
            host="127.0.0.1",
            port=1,
            status="pending",
            limit=None,
            language="all",
            front_field="Front",
            back_field="Back",
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args


class TsvExportTest(AnkiExportTestCase):
    def test_same_spelling_in_two_languages_gets_two_decks_and_two_guids(self) -> None:
        connection = self.connect()
        try:
            self.add_term(connection, language_code="es", lemma="no", meaning="não")
            self.add_term(connection, language_code="en", lemma="no", meaning="não")
            self.add_term(connection, language_code="de", lemma="die", meaning="a")
            self.add_term(connection, language_code="en", lemma="die", meaning="morrer")
            self.add_term(connection, language_code="ja", lemma="山", reading="ヤマ", meaning="montanha")
            self.assertEqual(self.generate(connection), 5)
        finally:
            connection.close()

        headers, records = self.read_anki_tsv(self.export("all.tsv"))
        self.assertEqual(
            headers,
            [
                "#separator:tab",
                "#html:true",
                "#columns:Front\tBack\tTags\tDeck\tNotetype\tGUID",
                "#tags column:3",
                "#deck column:4",
                "#notetype column:5",
                "#guid column:6",
            ],
        )
        self.assertEqual(len(records), 5)
        by_deck_front = {(row["Deck"], strip_html(row["Front"])): row for row in records}
        self.assertEqual(
            set(by_deck_front),
            {
                ("Lang::Spanish", "no"),
                ("Lang::English", "no"),
                ("Lang::German", "die"),
                ("Lang::English", "die"),
                ("Lang::Japanese", "山やま"),
            },
        )
        guids = [row["GUID"] for row in records if row["Deck"] != "Lang::Japanese"]
        self.assertEqual(len(set(guids)), 4)
        self.assertTrue(by_deck_front[("Lang::Spanish", "no")]["GUID"].startswith("lang-es-"))
        self.assertTrue(by_deck_front[("Lang::English", "no")]["GUID"].startswith("lang-en-"))
        # Japanese keeps the old identity: no GUID, Anki matches on the first field.
        self.assertEqual(by_deck_front[("Lang::Japanese", "山やま")]["GUID"], "")
        for row in records:
            self.assertEqual(row["Notetype"], "Basic")

        # GUIDs are stable between exports.
        _, again = self.read_anki_tsv(self.export("again.tsv"))
        self.assertEqual(sorted(r["GUID"] for r in records), sorted(r["GUID"] for r in again))

    def test_single_language_export_does_not_leak_other_languages(self) -> None:
        connection = self.connect()
        try:
            self.add_term(connection, language_code="es", lemma="batalla", meaning="batalha")
            self.add_term(connection, language_code="en", lemma="battle", meaning="batalha")
            self.add_term(connection, language_code="ja", lemma="戦い", reading="タタカイ")
            self.generate(connection)
        finally:
            connection.close()

        _, records = self.read_anki_tsv(self.export("es.tsv", language="es"))
        self.assertEqual([strip_html(row["Front"]) for row in records], ["batalla"])
        self.assertEqual(records[0]["Deck"], "Lang::Spanish")
        self.assertIn("lang_es", records[0]["Tags"].split())

    def test_blank_language_counts_as_japanese(self) -> None:
        connection = self.connect()
        try:
            term_id = self.add_term(connection, language_code="ja", lemma="猫", reading="ネコ")
            self.add_term(connection, language_code="es", lemma="gato")
            self.generate(connection)
            # A legacy row whose language was never filled in.
            connection.execute("UPDATE terms SET language_code = '' WHERE id = ?", (term_id,))
            connection.commit()
            expression = lang_jp.term_language_sql("NULL")
            self.assertEqual(connection.execute(f"SELECT {expression}").fetchone()[0], "ja")
        finally:
            connection.close()

        _, records = self.read_anki_tsv(self.export("ja.tsv", language="ja"))
        self.assertEqual([strip_html(row["Front"]) for row in records], ["猫ねこ"])
        self.assertEqual(records[0]["GUID"], "")
        self.assertIn("lang_ja", records[0]["Tags"].split())

    def test_basic_format_keeps_the_old_three_columns(self) -> None:
        connection = self.connect()
        try:
            self.add_term(connection, language_code="ja", lemma="山", reading="ヤマ", meaning="montanha")
            self.generate(connection)
            # Cards generated before the polyglot change only carry "jp".
            connection.execute(
                "UPDATE flashcards SET tags = 'jp auto_generated term_1 template_recognition_basic'"
            )
            connection.commit()
        finally:
            connection.close()

        path = self.export("basic.tsv", format="basic", include_header=True)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle, delimiter="\t"))
        self.assertEqual(rows[0], ["Front", "Back", "Tags"])
        self.assertEqual(len(rows[1]), 3)
        self.assertEqual(rows[1][0], lang_jp.build_front_text("山", "ヤマ"))
        self.assertEqual(
            rows[1][2],
            "jp lang_ja auto_generated term_1 template_recognition_basic",
        )

    def test_mark_exported_is_per_language(self) -> None:
        connection = self.connect()
        try:
            self.add_term(connection, language_code="es", lemma="casa")
            self.add_term(connection, language_code="en", lemma="house")
            self.generate(connection)
        finally:
            connection.close()

        self.export("es.tsv", language="es", status="pending", mark_exported=True)
        _, pending = self.read_anki_tsv(self.export("pending.tsv", status="pending"))
        self.assertEqual([strip_html(row["Front"]) for row in pending], ["house"])


class CardPayloadTest(AnkiExportTestCase):
    def test_japanese_front_is_unchanged_and_other_languages_get_lang_and_dir(self) -> None:
        japanese = lang_jp.build_flashcard_payload(
            {"id": 1, "language_code": "ja", "lemma": "山", "reading": "ヤマ", "meaning_pt": "montanha", "pos": None},
            None,
            deck_name="Lang::Japanese",
            note_type="Basic",
            template_name="recognition_basic",
        )
        self.assertEqual(japanese["front_text"], lang_jp.build_front_text("山", "ヤマ"))
        self.assertNotIn("dir=", japanese["front_text"] + japanese["back_text"])
        self.assertEqual(japanese["back_text"], "Reading: ヤマ<br>Romaji: yama<br>Meaning: montanha")
        self.assertEqual(japanese["tags"].split()[:2], ["jp", "lang_ja"])

        arabic = lang_jp.build_flashcard_payload(
            {"id": 2, "language_code": "ar", "lemma": "كتاب", "reading": None, "meaning_pt": "livro & <caderno>", "pos": None},
            {"source_id": 3, "surface_form": "الكتاب"},
            deck_name="Lang::Arabic",
            note_type="Basic",
            template_name="recognition_basic",
        )
        self.assertIn('<div lang="ar" dir="auto" ', arabic["front_text"])
        self.assertIn("الكتاب", arabic["front_text"])
        self.assertIn('Lemma: <span lang="ar" dir="auto">كتاب</span>', arabic["back_text"])
        self.assertIn("livro &amp; &lt;caderno&gt;", arabic["back_text"])
        self.assertEqual(arabic["tags"].split()[0], "lang_ar")
        self.assertNotIn("jp", arabic["tags"].split())

    def test_readings_follow_the_language(self) -> None:
        cases = {
            "ko": ("안녕하세요", "annyeonghaseyo", "Romanization: annyeonghaseyo"),
            "zh": ("你好", "nǐ hǎo", "Pinyin: nǐ hǎo"),
            "ru": ("привет", "privet", "Transliteration: privet"),
        }
        for code, (lemma, reading, expected) in cases.items():
            with self.subTest(code=code):
                payload = lang_jp.build_flashcard_payload(
                    {"id": 9, "language_code": code, "lemma": lemma, "reading": reading, "meaning_pt": "oi", "pos": None},
                    None,
                    deck_name="Lang::X",
                    note_type="Basic",
                    template_name="recognition_basic",
                )
                self.assertIn(expected, payload["back_text"])
                self.assertNotIn("Romaji", payload["back_text"])
                self.assertIn(reading, payload["front_text"])
        spanish = lang_jp.build_flashcard_payload(
            {"id": 10, "language_code": "es", "lemma": "casa", "reading": "kasa", "meaning_pt": "casa", "pos": None},
            None,
            deck_name="Lang::Spanish",
            note_type="Basic",
            template_name="recognition_basic",
        )
        # Languages without readings never show one, even when a row has a value.
        self.assertNotIn("kasa", spanish["front_text"] + spanish["back_text"])

    def test_generation_skips_the_gloss_language_and_disabled_languages(self) -> None:
        connection = self.connect()
        try:
            self.add_term(connection, language_code="pt", lemma="estado", meaning="estado")
            self.add_term(connection, language_code="es", lemma="estado", meaning="estado")
            self.add_term(connection, language_code="fr", lemma="état", meaning="estado")
            lang_jp.ensure_language_row(connection, "fr", enabled=False)
            self.assertEqual(self.generate(connection), 1)
            languages_with_cards = [
                row[0]
                for row in connection.execute(
                    "SELECT t.language_code FROM flashcards f JOIN terms t ON t.id = f.term_id"
                )
            ]
            self.assertEqual(languages_with_cards, ["es"])
            # Asking for the language explicitly still works.
            self.assertEqual(self.generate(connection, language="pt"), 1)
            self.assertEqual(self.generate(connection, language="fr"), 1)
        finally:
            connection.close()


class KanjiCardTest(AnkiExportTestCase):
    def kanji_args(self, **overrides: object) -> argparse.Namespace:
        args = argparse.Namespace(
            template_name="kanji_basic",
            deck_name=None,
            note_type="Basic",
            min_priority=0.0,
            limit=5000,
            kanji_id=None,
            language="all",
            refresh_metadata=False,
            existing_only=False,
            refresh_existing=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_characters_go_to_their_language_deck(self) -> None:
        metadata = {
            "canonical_meanings": "mountain",
            "on_readings": "サン",
            "kun_readings": "やま",
            "name_readings": None,
        }
        connection = self.connect()
        try:
            self.add_term(connection, language_code="ja", lemma="山", reading="ヤマ")
            self.add_term(connection, language_code="zh", lemma="书", reading="shū")
            self.add_term(connection, language_code="en", lemma="book")
            lang_jp.refresh_all_source_kanji(connection)
            # A stray character row in a language without characters never gets a card.
            connection.execute("INSERT INTO kanji (literal, language_code) VALUES ('X', 'en')")
            with mock.patch.object(lang_jp, "fetch_kanji_metadata", return_value=metadata):
                created, _ = lang_jp.upsert_kanji_flashcards(connection, self.kanji_args())
            connection.commit()
            rows = {
                row["literal"]: row
                for row in connection.execute(
                    """
                    SELECT k.literal, f.deck_name, f.front_text, f.back_text, f.tags
                    FROM kanji_flashcards AS f
                    JOIN kanji AS k ON k.id = f.kanji_id
                    """
                ).fetchall()
            }
        finally:
            connection.close()

        self.assertEqual(created, 2)
        self.assertEqual(set(rows), {"山", "书"})
        self.assertEqual(rows["山"]["deck_name"], "Lang::Japanese")
        self.assertEqual(rows["山"]["tags"].split()[:3], ["jp", "lang_ja", "kanji"])
        self.assertIn("On: サン (san)", rows["山"]["front_text"])
        self.assertNotIn("dir=", rows["山"]["front_text"])
        self.assertEqual(rows["书"]["deck_name"], "Lang::Chinese")
        self.assertEqual(rows["书"]["tags"].split()[:2], ["lang_zh", "kanji"])
        self.assertNotIn("jp", rows["书"]["tags"].split())
        self.assertIn('lang="zh"', rows["书"]["front_text"])
        # Japanese on/kun readings never show on a Chinese character.
        self.assertNotIn("On:", rows["书"]["front_text"] + rows["书"]["back_text"])
        self.assertNotIn("Kun:", rows["书"]["front_text"] + rows["书"]["back_text"])
        self.assertEqual(rows["书"]["back_text"], "Meaning: mountain")

    def test_language_filter_on_kanji_cards(self) -> None:
        connection = self.connect()
        try:
            self.add_term(connection, language_code="ja", lemma="山", reading="ヤマ")
            self.add_term(connection, language_code="zh", lemma="书", reading="shū")
            lang_jp.refresh_all_source_kanji(connection)
            connection.execute(
                "UPDATE kanji SET canonical_meanings = 'x', on_readings = 'x', kun_readings = 'x'"
            )
            created, _ = lang_jp.upsert_kanji_flashcards(connection, self.kanji_args(language="zh"))
            literals = [
                row[0]
                for row in connection.execute(
                    "SELECT k.literal FROM kanji_flashcards f JOIN kanji k ON k.id = f.kanji_id"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(created, 1)
        self.assertEqual(literals, ["书"])


class AnkiConnectSyncTest(AnkiExportTestCase):
    def run_sync(self, fake: FakeAnki, **overrides: object) -> None:
        with mock.patch.object(lang_jp, "anki_connect_request", fake), mock.patch("builtins.print"):
            lang_jp.sync_anki(self.sync_args(**overrides))

    def card_note_ids(self) -> dict[tuple[str, str], int | None]:
        connection = self.connect()
        try:
            return {
                (row["language_code"], row["lemma"]): row["anki_note_id"]
                for row in connection.execute(
                    """
                    SELECT t.language_code, t.lemma, f.anki_note_id
                    FROM flashcards AS f JOIN terms AS t ON t.id = f.term_id
                    """
                ).fetchall()
            }
        finally:
            connection.close()

    def test_same_spelling_in_two_languages_creates_two_notes(self) -> None:
        connection = self.connect()
        try:
            self.add_term(connection, language_code="es", lemma="ser", meaning="ser")
            self.add_term(connection, language_code="it", lemma="ser", meaning="senhor")
            self.add_term(connection, language_code="ja", lemma="山", reading="ヤマ")
            self.generate(connection)
        finally:
            connection.close()

        fake = FakeAnki()
        self.run_sync(fake)
        note_ids = self.card_note_ids()
        self.assertTrue(all(note_ids.values()), note_ids)
        self.assertEqual(fake.deck_of(note_ids[("es", "ser")]), "Lang::Spanish")
        self.assertEqual(fake.deck_of(note_ids[("it", "ser")]), "Lang::Italian")
        self.assertNotEqual(note_ids[("es", "ser")], note_ids[("it", "ser")])
        options_by_deck = {
            options.get("duplicateScopeOptions", {}).get("deckName", "collection"): options
            for options in fake.add_note_options
        }
        # Japanese keeps Anki's collection-wide duplicate check.
        self.assertEqual(options_by_deck["collection"], {"allowDuplicate": False})
        self.assertEqual(options_by_deck["Lang::Spanish"]["duplicateScope"], "deck")
        self.assertIn("lang_it", fake.notes[note_ids[("it", "ser")]]["tags"])

    def test_front_lookup_never_links_a_note_of_another_language(self) -> None:
        fake = FakeAnki()
        connection = self.connect()
        try:
            # Two languages sharing one deck by configuration.
            lang_jp.ensure_language_row(connection, "de", deck_name="Lang::Mixed")
            lang_jp.ensure_language_row(connection, "en", deck_name="Lang::Mixed")
            self.add_term(connection, language_code="de", lemma="die", meaning="a")
            self.generate(connection)
            front = connection.execute("SELECT front_text FROM flashcards").fetchone()[0]
        finally:
            connection.close()
        english_note = fake.add_existing(
            deck="Lang::Mixed",
            front=front,
            back="Meaning: morrer",
            tags=["lang_en", "auto_generated", "term_999"],
        )

        self.run_sync(fake)
        note_ids = self.card_note_ids()
        self.assertNotEqual(note_ids[("de", "die")], english_note)
        self.assertEqual(fake.notes[english_note]["fields"]["Back"], "Meaning: morrer")
        self.assertEqual(fake.notes[english_note]["tags"], ["lang_en", "auto_generated", "term_999"])

    def test_legacy_japanese_note_is_found_and_updated_not_duplicated(self) -> None:
        fake = FakeAnki()
        connection = self.connect()
        try:
            self.add_term(connection, language_code="ja", lemma="山", reading="ヤマ", meaning="montanha")
            self.generate(connection)
            row = connection.execute("SELECT front_text, back_text FROM flashcards").fetchone()
            # Stored before languages existed: only the "jp" tag.
            connection.execute(
                "UPDATE flashcards SET tags = 'jp auto_generated term_1 template_recognition_basic'"
            )
            connection.commit()
        finally:
            connection.close()
        # The user's existing note, imported earlier without catalog tags.
        existing = fake.add_existing(
            deck="Lang::Japanese",
            front=row["front_text"],
            back=row["back_text"],
            tags=["jp"],
        )

        self.run_sync(fake)
        self.assertEqual(len(fake.notes), 1)
        self.assertEqual(self.card_note_ids()[("ja", "山")], existing)
        self.assertIn("lang_ja", fake.notes[existing]["tags"])

    def test_single_language_sync_only_reorders_its_own_deck(self) -> None:
        connection = self.connect()
        try:
            self.add_term(connection, language_code="es", lemma="casa")
            self.add_term(connection, language_code="ja", lemma="山", reading="ヤマ")
            self.generate(connection)
        finally:
            connection.close()

        fake = FakeAnki()
        self.run_sync(fake)
        note_ids = self.card_note_ids()
        japanese_cards = set(fake.notes[note_ids[("ja", "山")]]["cards"])
        connection = self.connect()
        try:
            connection.execute("UPDATE flashcards SET export_status = 'pending'")
            connection.commit()
        finally:
            connection.close()

        fake.repositioned.clear()
        self.run_sync(fake, language="es")
        self.assertTrue(fake.repositioned)
        for cards in fake.repositioned:
            self.assertFalse(japanese_cards & set(cards))


if __name__ == "__main__":
    unittest.main()
