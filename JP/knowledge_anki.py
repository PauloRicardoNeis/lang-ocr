"""Anki as a source of fluency data.

Reads the review state of the cards the catalog exported (the notes linked by
``flashcards.anki_note_id`` and ``kanji_flashcards.anki_note_id``) and reports
it as knowledge evidence. It never writes to Anki.

Only studied cards are fetched: a collection can hold tens of thousands of new
cards that say nothing about fluency and would each cost a full card payload.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import anki_connect
from knowledge import (
    ITEM_KANJI,
    ITEM_TERM,
    STATUS_LEARNING,
    Evidence,
    format_timestamp,
    srs_status,
)

# Anki's card.type values.
CARD_TYPE_NEW = 0
CARD_TYPE_LEARNING = 1
CARD_TYPE_REVIEW = 2
CARD_TYPE_RELEARNING = 3
# Anki's card.queue values this module tells apart.
CARD_QUEUE_SUSPENDED = -1
CARD_QUEUE_LEARNING = 1

LINKED_NOTES_SQL = """
SELECT '{kind}' AS item_kind, {id_column} AS item_id, anki_note_id, deck_name
FROM {table}
WHERE anki_note_id IS NOT NULL
"""


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def card_evidence(
    item_kind: str,
    item_id: int,
    card: dict[str, object],
    reviews: list[dict[str, object]],
) -> Evidence | None:
    """One card's state as evidence, or None for a card never studied."""
    card_type = as_int(card.get("type"), CARD_TYPE_NEW)
    if card_type == CARD_TYPE_NEW:
        return None

    in_learning = card_type in (CARD_TYPE_LEARNING, CARD_TYPE_RELEARNING)
    # Anki schedules a review for when recall should have dropped to the
    # desired retention, so the interval in days stands in for the memory's
    # stability. A card still in its learning steps reports seconds as a
    # negative interval, or zero: no stability yet.
    stability_days = float(max(as_int(card.get("interval")), 0))
    if card_type == CARD_TYPE_LEARNING:
        stability_days = 0.0

    last_review: datetime | None = None
    if reviews:
        # A review's id is the moment it happened, in milliseconds.
        last_review = datetime.fromtimestamp(as_int(reviews[-1].get("id")) / 1000, UTC)

    due: datetime | None = None
    queue = as_int(card.get("queue"))
    if queue == CARD_QUEUE_LEARNING:
        # Intraday learning cards carry their due time as epoch seconds.
        due = datetime.fromtimestamp(as_int(card.get("due")), UTC)
    elif card_type == CARD_TYPE_REVIEW and last_review is not None:
        # A review card's due is a day count from the collection's creation,
        # which AnkiConnect does not expose; the last review plus the interval
        # is the same day.
        due = last_review + timedelta(days=stability_days)

    detail: dict[str, object] = {
        "card_id": as_int(card.get("cardId")),
        "deck": str(card.get("deckName") or ""),
        "ease": as_int(card.get("factor")) / 1000 or None,
    }
    if card_type == CARD_TYPE_RELEARNING:
        detail["relearning"] = True
    if queue == CARD_QUEUE_SUSPENDED:
        detail["suspended"] = True

    return Evidence(
        item_kind=item_kind,
        item_id=item_id,
        status=srs_status(stability_days, in_learning=in_learning),
        stability_days=stability_days,
        reviews=as_int(card.get("reps")),
        lapses=as_int(card.get("lapses")),
        last_reviewed_at=format_timestamp(last_review) if last_review else None,
        due_at=format_timestamp(due) if due else None,
        detail={key: value for key, value in detail.items() if value is not None},
    )


def evidence_rank(evidence: Evidence) -> tuple[bool, float]:
    """Orders an item's cards: one out of learning beats one in it, then by strength."""
    return (evidence.status != STATUS_LEARNING, evidence.score)


class AnkiKnowledgeProvider:
    name = "anki"
    # A review history is the best evidence there is short of the user's word.
    weight = 0.9

    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def linked_notes(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[dict[int, tuple[str, int]], list[str]]:
        """The catalog item behind each exported note, and the decks they went to."""
        items: dict[int, tuple[str, int]] = {}
        decks: list[str] = []
        for kind, table, id_column in (
            (ITEM_TERM, "flashcards", "term_id"),
            (ITEM_KANJI, "kanji_flashcards", "kanji_id"),
        ):
            for row in connection.execute(
                LINKED_NOTES_SQL.format(kind=kind, table=table, id_column=id_column)
            ):
                items[int(row["anki_note_id"])] = (str(row["item_kind"]), int(row["item_id"]))
                deck_name = str(row["deck_name"])
                if deck_name not in decks:
                    decks.append(deck_name)
        return items, decks

    def studied_card_ids(self, decks: list[str]) -> list[int]:
        actions: list[dict[str, object]] = [
            {
                "action": "findCards",
                "params": {"query": f"{anki_connect.deck_search(deck_name)} -is:new"},
            }
            for deck_name in decks
        ]
        card_ids: list[int] = []
        for action, outcome in zip(
            actions, anki_connect.multi(actions, host=self.host, port=self.port)
        ):
            result = anki_connect.action_result(action, outcome)
            if not isinstance(result, list):
                raise anki_connect.AnkiConnectError(
                    "AnkiConnect returned an invalid findCards payload."
                )
            card_ids.extend(int(card_id) for card_id in result)
        # A deck and its subdeck can both be export targets.
        return list(dict.fromkeys(card_ids))

    def collect(self, connection: sqlite3.Connection) -> list[Evidence]:
        items, decks = self.linked_notes(connection)
        if not items:
            return []

        card_ids = self.studied_card_ids(decks)
        cards = anki_connect.cards_info(card_ids, host=self.host, port=self.port)
        linked_cards = {
            card_id: card for card_id, card in cards.items() if as_int(card.get("note")) in items
        }
        reviews = anki_connect.reviews_of_cards(
            list(linked_cards), host=self.host, port=self.port
        )

        # An item can have several cards (one per template, or several per
        # note): it is as well known as its best one.
        best: dict[tuple[str, int], Evidence] = {}
        card_counts: dict[tuple[str, int], int] = {}
        for card_id, card in linked_cards.items():
            item = items[as_int(card.get("note"))]
            evidence = card_evidence(*item, card, reviews.get(card_id, []))
            if evidence is None:
                continue
            card_counts[item] = card_counts.get(item, 0) + 1
            current = best.get(item)
            if current is None or evidence_rank(evidence) > evidence_rank(current):
                best[item] = evidence

        return [
            Evidence(
                item_kind=evidence.item_kind,
                item_id=evidence.item_id,
                status=evidence.status,
                stability_days=evidence.stability_days,
                reviews=evidence.reviews,
                lapses=evidence.lapses,
                last_reviewed_at=evidence.last_reviewed_at,
                due_at=evidence.due_at,
                detail={**evidence.detail, "studied_cards": card_counts[item]},
            )
            for item, evidence in best.items()
        ]
