"""Transport for AnkiConnect, the HTTP add-on that exposes a running Anki.

Everything that talks to Anki goes through ``request``: the flashcard sync in
``lang_jp`` (which writes notes) and the knowledge provider in
``knowledge_anki`` (which only reads review state). This module knows the wire
format and the payload shapes, nothing about the catalog.

AnkiConnect answers from a timer on Anki's UI thread, so every request costs a
fixed ~60 ms however little it carries. Callers should batch: ``multi`` runs
many actions in one request and the ``*_info`` helpers take id lists.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import TypeVar
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
API_VERSION = 6
TIMEOUT_SECONDS = 10
# A batch can carry thousands of notes and Anki answers it in one go.
BATCH_TIMEOUT_SECONDS = 120
NOTES_INFO_BATCH_SIZE = 1000
CARDS_INFO_BATCH_SIZE = 250
REVIEWS_BATCH_SIZE = 500

T = TypeVar("T")


class AnkiConnectError(SystemExit):
    """Anki could not be reached or refused an action.

    A ``SystemExit`` so a CLI command that does not handle it ends with the
    message, as every other fatal error in the catalog does.
    """


def request(
    *,
    host: str,
    port: int,
    action: str,
    params: dict[str, object] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> object:
    payload: dict[str, object] = {
        "action": action,
        "version": API_VERSION,
    }
    if params is not None:
        payload["params"] = params

    http_request = Request(
        f"http://{host}:{port}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(http_request, timeout=timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AnkiConnectError(
            f"Could not reach AnkiConnect at http://{host}:{port}. "
            f"Is Anki open? ({reason})"
        ) from exc

    if not isinstance(response_data, dict):
        raise AnkiConnectError(f"Unexpected response from AnkiConnect action {action}.")
    if response_data.get("error") is not None:
        error_text = str(response_data["error"])
        if action == "addNote" and "duplicate" in error_text.lower():
            return None
        raise AnkiConnectError(f"AnkiConnect action {action} failed: {error_text}")
    if "result" not in response_data:
        raise AnkiConnectError(f"Missing result in AnkiConnect action {action}.")
    return response_data["result"]


def multi(
    actions: list[dict[str, object]],
    *,
    host: str,
    port: int,
) -> list[tuple[object, str | None]]:
    """Run ``actions`` in one AnkiConnect request: one ``(result, error)`` each, in order."""
    if not actions:
        return []
    replies = request(
        host=host,
        port=port,
        action="multi",
        params={
            # Without its own version a nested action answers with a bare result
            # on success, which cannot be told apart from an error object.
            "actions": [{**action, "version": API_VERSION} for action in actions]
        },
        timeout=BATCH_TIMEOUT_SECONDS,
    )
    if not isinstance(replies, list) or len(replies) != len(actions):
        raise AnkiConnectError("AnkiConnect returned an invalid multi payload.")

    outcomes: list[tuple[object, str | None]] = []
    for reply in replies:
        if not isinstance(reply, dict) or "error" not in reply:
            raise AnkiConnectError("AnkiConnect returned an invalid multi payload.")
        error = reply["error"]
        outcomes.append((reply.get("result"), None if error is None else str(error)))
    return outcomes


def action_result(
    action: dict[str, object],
    outcome: tuple[object, str | None],
) -> object:
    result, error = outcome
    if error is not None:
        raise AnkiConnectError(f"AnkiConnect action {action['action']} failed: {error}")
    return result


def batch_values(values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    if size <= 0:
        raise ValueError("Batch size must be positive.")

    for index in range(0, len(values), size):
        yield values[index : index + size]


def deck_search(deck_name: str, *, include_subdecks: bool = True) -> str:
    escaped = deck_name.replace('"', '\\"')
    if include_subdecks:
        return f'deck:"{escaped}"'
    return f'deck:"{escaped}" -deck:"{escaped}::*"'


def note_tags(note: dict[str, object]) -> list[str]:
    tags = note.get("tags")
    return [str(tag) for tag in tags] if isinstance(tags, list) else []


def note_field(note: dict[str, object], field_name: str) -> str | None:
    fields = note.get("fields")
    if not isinstance(fields, dict):
        return None
    field_payload = fields.get(field_name)
    if not isinstance(field_payload, dict):
        return None
    return str(field_payload.get("value") or "")


def available_actions(*, host: str, port: int) -> set[str]:
    try:
        result = request(
            host=host,
            port=port,
            action="apiReflect",
            params={"scopes": ["actions"]},
        )
    except AnkiConnectError:
        return set()

    if not isinstance(result, dict):
        return set()

    actions = result.get("actions")
    if not isinstance(actions, list):
        return set()

    return {str(action) for action in actions}


def notes_info(
    note_ids: list[int],
    *,
    host: str,
    port: int,
) -> dict[int, dict[str, object]]:
    notes: dict[int, dict[str, object]] = {}
    for batch in batch_values(note_ids, NOTES_INFO_BATCH_SIZE):
        result = request(
            host=host,
            port=port,
            action="notesInfo",
            params={"notes": list(batch)},
            timeout=BATCH_TIMEOUT_SECONDS,
        )
        if not isinstance(result, list):
            raise AnkiConnectError("AnkiConnect returned an invalid notesInfo payload.")

        for note in result:
            if not isinstance(note, dict):
                continue
            note_id = note.get("noteId")
            if note_id is None:
                continue
            notes[int(note_id)] = note

    return notes


def cards_info(
    card_ids: list[int],
    *,
    host: str,
    port: int,
) -> dict[int, dict[str, object]]:
    cards: dict[int, dict[str, object]] = {}
    for batch in batch_values(card_ids, CARDS_INFO_BATCH_SIZE):
        result = request(
            host=host,
            port=port,
            action="cardsInfo",
            params={"cards": list(batch)},
        )
        if not isinstance(result, list):
            raise AnkiConnectError("AnkiConnect returned an invalid cardsInfo payload.")

        for card in result:
            if not isinstance(card, dict):
                continue
            card_id = card.get("cardId")
            if card_id is None:
                continue
            cards[int(card_id)] = card

    return cards


def find_cards(query: str, *, host: str, port: int) -> list[int]:
    result = request(
        host=host,
        port=port,
        action="findCards",
        params={"query": query},
        timeout=BATCH_TIMEOUT_SECONDS,
    )
    if not isinstance(result, list):
        raise AnkiConnectError("AnkiConnect returned an invalid findCards payload.")
    return [int(card_id) for card_id in result]


def reviews_of_cards(
    card_ids: list[int],
    *,
    host: str,
    port: int,
) -> dict[int, list[dict[str, object]]]:
    """The review log of each card, oldest first. A review's ``id`` is its time in ms."""
    reviews: dict[int, list[dict[str, object]]] = {}
    for batch in batch_values(card_ids, REVIEWS_BATCH_SIZE):
        result = request(
            host=host,
            port=port,
            action="getReviewsOfCards",
            params={"cards": list(batch)},
            timeout=BATCH_TIMEOUT_SECONDS,
        )
        if not isinstance(result, dict):
            raise AnkiConnectError("AnkiConnect returned an invalid getReviewsOfCards payload.")
        for card_id, entries in result.items():
            if not isinstance(entries, list):
                continue
            reviews[int(card_id)] = sorted(
                (entry for entry in entries if isinstance(entry, dict)),
                key=lambda entry: int(entry.get("id") or 0),
            )
    return reviews
