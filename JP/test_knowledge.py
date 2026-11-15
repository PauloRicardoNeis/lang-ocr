"""The knowledge layer and its Anki provider.

Both are tested against a bare SQLite database holding only the tables they
read, and AnkiConnect is replaced by a fake, so nothing here needs the rest of
the catalog or a running Anki.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import anki_connect
import knowledge
from knowledge import ITEM_KANJI, ITEM_TERM, Evidence
from knowledge_anki import AnkiKnowledgeProvider

CATALOG_TABLES_SQL = """
CREATE TABLE terms (id INTEGER PRIMARY KEY, lemma TEXT NOT NULL);
CREATE TABLE kanji (id INTEGER PRIMARY KEY, literal TEXT NOT NULL);
CREATE TABLE flashcards (
    id INTEGER PRIMARY KEY,
    term_id INTEGER NOT NULL,
    template_name TEXT NOT NULL DEFAULT 'recognition_basic',
    deck_name TEXT NOT NULL DEFAULT 'Lang::Japanese',
    anki_note_id INTEGER
);
CREATE TABLE kanji_flashcards (
    id INTEGER PRIMARY KEY,
    kanji_id INTEGER NOT NULL,
    deck_name TEXT NOT NULL DEFAULT 'Lang::Japanese',
    anki_note_id INTEGER
);
"""

DAY_MS = 86_400_000
# 2026-01-01 00:00:00 UTC
EPOCH_MS = 1_767_225_600_000


class StaticProvider:
    """A provider that reports whatever it was built with."""

    def __init__(self, name: str, weight: float, evidence: list[Evidence]) -> None:
        self.name = name
        self.weight = weight
        self.evidence = evidence

    def collect(self, connection: sqlite3.Connection) -> list[Evidence]:
        return self.evidence


class FailingProvider:
    name = "srs"
    weight = 0.9

    def collect(self, connection: sqlite3.Connection) -> list[Evidence]:
        raise anki_connect.AnkiConnectError("offline")


class FakeAnki:
    """Just enough of AnkiConnect for the knowledge provider: it only ever reads."""

    def __init__(self) -> None:
        self.cards: dict[int, dict[str, object]] = {}
        self.reviews: dict[int, list[dict[str, object]]] = {}
        self.actions: list[str] = []

    def add_card(
        self,
        card_id: int,
        *,
        note: int,
        card_type: int,
        interval: int = 0,
        queue: int | None = None,
        deck: str = "Lang::Japanese",
        reps: int = 0,
        lapses: int = 0,
        due: int = 0,
        review_days: tuple[int, ...] = (),
    ) -> None:
        self.cards[card_id] = {
            "cardId": card_id,
            "note": note,
            "type": card_type,
            "queue": card_type if queue is None else queue,
            "interval": interval,
            "factor": 2500,
            "reps": reps or len(review_days),
            "lapses": lapses,
            "due": due,
            "deckName": deck,
        }
        self.reviews[card_id] = [
            {"id": EPOCH_MS + day * DAY_MS, "ease": 3} for day in review_days
        ]

    def find_cards(self, query: str) -> list[int]:
        return [
            card_id
            for card_id, card in self.cards.items()
            if f'deck:"{card["deckName"]}"' in query
            and not ("-is:new" in query and card["type"] == 0)
        ]

    def __call__(
        self,
        *,
        host: str,
        port: int,
        action: str,
        params: dict[str, object] | None = None,
        timeout: float = 0,
    ) -> object:
        params = params or {}
        self.actions.append(action)
        if action == "multi":
            return [
                {"result": self(host=host, port=port, action=nested["action"], params=nested["params"]), "error": None}
                for nested in params["actions"]  # type: ignore[union-attr]
            ]
        if action == "findCards":
            return self.find_cards(str(params["query"]))
        if action == "cardsInfo":
            return [self.cards[card_id] for card_id in params["cards"]]  # type: ignore[union-attr]
        if action == "getReviewsOfCards":
            return {str(card_id): self.reviews[card_id] for card_id in params["cards"]}  # type: ignore[union-attr]
        raise AssertionError(f"The knowledge provider must not call {action}")


class KnowledgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(CATALOG_TABLES_SQL)
        knowledge.ensure_schema(self.connection)
        self.connection.executemany(
            "INSERT INTO terms (id, lemma) VALUES (?, ?)",
            [(1, "回復"), (2, "猫"), (3, "犬")],
        )
        self.connection.execute("INSERT INTO kanji (id, literal) VALUES (1, '回')")
        self.addCleanup(self.connection.close)

    def consolidated(self, item_kind: str = ITEM_TERM) -> dict[int, sqlite3.Row]:
        return {
            row["item_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM knowledge WHERE item_kind = ?", (item_kind,)
            )
        }


class ScoreTest(unittest.TestCase):
    def test_strength_grows_with_stability_and_is_half_at_maturity(self) -> None:
        self.assertEqual(knowledge.strength_score(0), knowledge.MIN_STUDIED_SCORE)
        self.assertEqual(knowledge.strength_score(21), 0.5)
        self.assertLess(knowledge.strength_score(3), knowledge.strength_score(30))
        self.assertLess(knowledge.strength_score(10_000), 1.0)

    def test_status_follows_learning_state_then_stability(self) -> None:
        self.assertEqual(knowledge.srs_status(40, in_learning=True), "learning")
        self.assertEqual(knowledge.srs_status(20, in_learning=False), "young")
        self.assertEqual(knowledge.srs_status(21, in_learning=False), "mature")

    def test_known_evidence_scores_one(self) -> None:
        self.assertEqual(Evidence(ITEM_TERM, 1, "known").score, 1.0)


class ConsolidationTest(KnowledgeTestCase):
    def test_refresh_stores_evidence_and_consolidates(self) -> None:
        provider = StaticProvider(
            "srs",
            0.9,
            [
                Evidence(ITEM_TERM, 1, "mature", stability_days=42, reviews=7, lapses=1),
                Evidence(ITEM_KANJI, 1, "learning"),
            ],
        )
        report = knowledge.refresh_provider(self.connection, provider)

        self.assertEqual(report.status_counts, {"mature": 1, "learning": 1})
        self.assertEqual(report.summary(), "srs: 2 items (1 learning, 1 mature)")
        term = self.consolidated()[1]
        self.assertEqual((term["status"], term["provider"], term["confidence"]), ("mature", "srs", 0.9))
        self.assertAlmostEqual(term["score"], 42 / 63)
        self.assertEqual(self.consolidated(ITEM_KANJI)[1]["status"], "learning")
        self.assertNotIn(2, self.consolidated())

    def test_refresh_replaces_what_the_provider_said_before(self) -> None:
        knowledge.refresh_provider(
            self.connection,
            StaticProvider("srs", 0.9, [Evidence(ITEM_TERM, 1, "young", stability_days=3)]),
        )
        knowledge.refresh_provider(
            self.connection,
            StaticProvider("srs", 0.9, [Evidence(ITEM_TERM, 2, "young", stability_days=3)]),
        )
        self.assertEqual(list(self.consolidated()), [2])

    def test_failed_collect_leaves_stored_evidence_alone(self) -> None:
        knowledge.refresh_provider(
            self.connection,
            StaticProvider("srs", 0.9, [Evidence(ITEM_TERM, 1, "young", stability_days=3)]),
        )
        with self.assertRaises(anki_connect.AnkiConnectError):
            knowledge.refresh_provider(self.connection, FailingProvider())
        self.assertEqual(list(self.consolidated()), [1])

    def test_more_trusted_provider_wins_over_a_stronger_claim(self) -> None:
        knowledge.refresh_provider(
            self.connection,
            StaticProvider("exposure", 0.2, [Evidence(ITEM_TERM, 1, "mature", stability_days=400)]),
        )
        knowledge.refresh_provider(
            self.connection,
            StaticProvider("srs", 0.9, [Evidence(ITEM_TERM, 1, "young", stability_days=3)]),
        )
        self.assertEqual(self.consolidated()[1]["provider"], "srs")
        self.assertEqual(self.consolidated()[1]["status"], "young")

    def test_equally_trusted_providers_resolve_to_the_stronger_claim(self) -> None:
        knowledge.refresh_provider(
            self.connection,
            StaticProvider("srs_a", 0.9, [Evidence(ITEM_TERM, 1, "young", stability_days=3)]),
        )
        knowledge.refresh_provider(
            self.connection,
            StaticProvider("srs_b", 0.9, [Evidence(ITEM_TERM, 1, "mature", stability_days=90)]),
        )
        self.assertEqual(self.consolidated()[1]["provider"], "srs_b")

    def test_deleting_an_item_drops_its_knowledge(self) -> None:
        knowledge.refresh_provider(
            self.connection,
            StaticProvider(
                "srs",
                0.9,
                [Evidence(ITEM_TERM, 1, "young", stability_days=3), Evidence(ITEM_KANJI, 1, "young")],
            ),
        )
        self.connection.execute("DELETE FROM terms WHERE id = 1")
        self.assertEqual(self.consolidated(), {})
        self.assertEqual(knowledge.load_evidence(self.connection, ITEM_TERM, 1), [])
        # Same id, other kind: untouched.
        self.assertIn(1, self.consolidated(ITEM_KANJI))


class ManualMarkTest(KnowledgeTestCase):
    def test_mark_known_overrides_other_evidence_and_clearing_restores_it(self) -> None:
        knowledge.refresh_provider(
            self.connection,
            StaticProvider("srs", 0.9, [Evidence(ITEM_TERM, 1, "young", stability_days=3)]),
        )
        knowledge.mark_known(self.connection, ITEM_TERM, 1)
        row = self.consolidated()[1]
        self.assertEqual((row["status"], row["score"], row["provider"]), ("known", 1.0, "manual"))
        self.assertEqual(
            [entry["provider"] for entry in knowledge.load_evidence(self.connection, ITEM_TERM, 1)],
            ["manual", "srs"],
        )

        self.assertTrue(knowledge.clear_mark(self.connection, ITEM_TERM, 1))
        self.assertEqual(self.consolidated()[1]["status"], "young")
        self.assertFalse(knowledge.clear_mark(self.connection, ITEM_TERM, 1))

    def test_a_manual_mark_survives_a_provider_refresh(self) -> None:
        knowledge.mark_known(self.connection, ITEM_TERM, 1)
        knowledge.refresh_provider(self.connection, StaticProvider("srs", 0.9, []))
        self.assertEqual(self.consolidated()[1]["status"], "known")

    def test_clearing_the_only_evidence_makes_the_item_unknown_again(self) -> None:
        knowledge.mark_known(self.connection, ITEM_KANJI, 1)
        knowledge.clear_mark(self.connection, ITEM_KANJI, 1)
        self.assertEqual(self.consolidated(ITEM_KANJI), {})

    def test_marking_a_missing_item_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            knowledge.mark_known(self.connection, ITEM_TERM, 99)
        with self.assertRaises(ValueError):
            knowledge.mark_known(self.connection, "phrase", 1)


class AnkiProviderTest(KnowledgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.anki = FakeAnki()
        self.connection.executemany(
            "INSERT INTO flashcards (term_id, template_name, anki_note_id) VALUES (?, ?, ?)",
            [(1, "recognition_basic", 101), (1, "production_basic", 102), (2, "recognition_basic", 103), (3, "recognition_basic", None)],
        )
        self.connection.execute(
            "INSERT INTO kanji_flashcards (kanji_id, anki_note_id) VALUES (1, 201)"
        )

    def collect(self) -> dict[tuple[str, int], Evidence]:
        provider = AnkiKnowledgeProvider(host="127.0.0.1", port=8765)
        with mock.patch.object(anki_connect, "request", self.anki):
            return {(e.item_kind, e.item_id): e for e in provider.collect(self.connection)}

    def test_review_card_becomes_young_or_mature_evidence(self) -> None:
        self.anki.add_card(1, note=103, card_type=2, interval=30, lapses=2, review_days=(0, 3, 10))
        self.anki.add_card(2, note=201, card_type=2, interval=5, review_days=(1,))
        evidence = self.collect()

        cat = evidence[(ITEM_TERM, 2)]
        self.assertEqual((cat.status, cat.stability_days, cat.reviews, cat.lapses), ("mature", 30.0, 3, 2))
        self.assertEqual(cat.last_reviewed_at, "2026-01-11 00:00:00")
        self.assertEqual(cat.due_at, "2026-02-10 00:00:00")
        self.assertEqual(cat.detail, {"card_id": 1, "deck": "Lang::Japanese", "ease": 2.5, "studied_cards": 1})
        self.assertEqual(evidence[(ITEM_KANJI, 1)].status, "young")

    def test_learning_and_relearning_cards_are_learning(self) -> None:
        # A learning card reports its step as a negative interval, in seconds.
        self.anki.add_card(1, note=103, card_type=1, interval=-600, due=EPOCH_MS // 1000, review_days=(0,))
        self.anki.add_card(2, note=201, card_type=3, interval=40, lapses=1, review_days=(0, 5))
        evidence = self.collect()

        learning = evidence[(ITEM_TERM, 2)]
        self.assertEqual((learning.status, learning.stability_days), ("learning", 0.0))
        self.assertEqual(learning.due_at, "2026-01-01 00:00:00")
        relearning = evidence[(ITEM_KANJI, 1)]
        self.assertEqual(relearning.status, "learning")
        self.assertIs(relearning.detail["relearning"], True)
        self.assertIsNone(relearning.due_at)

    def test_new_and_unlinked_cards_are_not_evidence(self) -> None:
        self.anki.add_card(1, note=103, card_type=0)
        self.anki.add_card(2, note=999, card_type=2, interval=50, review_days=(0,))
        self.assertEqual(self.collect(), {})
        # The unlinked card's review log is never asked for.
        self.assertNotIn("getReviewsOfCards", self.anki.actions)

    def test_item_with_several_cards_takes_the_best_one(self) -> None:
        self.anki.add_card(1, note=101, card_type=2, interval=4, review_days=(0,))
        self.anki.add_card(2, note=102, card_type=2, interval=60, review_days=(0, 9))
        self.anki.add_card(3, note=102, card_type=1, review_days=(0,))
        evidence = self.collect()[(ITEM_TERM, 1)]
        self.assertEqual((evidence.status, evidence.stability_days), ("mature", 60.0))
        self.assertEqual(evidence.detail["card_id"], 2)
        self.assertEqual(evidence.detail["studied_cards"], 3)

    def test_a_graduated_card_beats_a_relearning_one_with_a_longer_interval(self) -> None:
        self.anki.add_card(1, note=101, card_type=3, interval=90, review_days=(0,))
        self.anki.add_card(2, note=102, card_type=2, interval=10, review_days=(0,))
        self.assertEqual(self.collect()[(ITEM_TERM, 1)].status, "young")

    def test_suspended_card_still_counts_and_is_flagged(self) -> None:
        self.anki.add_card(1, note=103, card_type=2, queue=-1, interval=25, review_days=(0,))
        evidence = self.collect()[(ITEM_TERM, 2)]
        self.assertEqual(evidence.status, "mature")
        self.assertIs(evidence.detail["suspended"], True)

    def test_every_export_deck_is_searched(self) -> None:
        self.connection.execute(
            "INSERT INTO flashcards (term_id, deck_name, anki_note_id) VALUES (3, 'Lang::Spanish', 104)"
        )
        self.anki.add_card(1, note=104, card_type=2, interval=8, deck="Lang::Spanish", review_days=(0,))
        self.assertIn((ITEM_TERM, 3), self.collect())

    def test_nothing_exported_means_no_request(self) -> None:
        self.connection.execute("DELETE FROM flashcards")
        self.connection.execute("DELETE FROM kanji_flashcards")
        self.assertEqual(self.collect(), {})
        self.assertEqual(self.anki.actions, [])

    def test_collected_evidence_flows_into_the_consolidated_table(self) -> None:
        self.anki.add_card(1, note=103, card_type=2, interval=30, review_days=(0,))
        provider = AnkiKnowledgeProvider(host="127.0.0.1", port=8765)
        with mock.patch.object(anki_connect, "request", self.anki):
            report = knowledge.refresh_provider(self.connection, provider)
        self.assertEqual(report.summary(), "anki: 1 items (1 mature)")
        self.assertEqual(self.consolidated()[2]["status"], "mature")


if __name__ == "__main__":
    unittest.main()
