"""How well the user knows each term and character.

Three layers, so that no source of fluency data reaches the pages directly:

* A **provider** turns one source (Anki today; the catalog's own SRS and a
  browser extension later) into ``Evidence``: what that source believes about
  one item, in a shape every provider shares.
* ``knowledge_evidence`` keeps the latest evidence per item and provider.
* ``knowledge`` is the consolidated answer, one row per item, and the only
  thing the catalog's views, pages and API read.

The module knows nothing about any particular source: a provider lives in its
own module (``knowledge_anki``) and only has to implement ``KnowledgeProvider``.
Nothing here commits; the caller owns the transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

ITEM_TERM = "term"
ITEM_KANJI = "kanji"
ITEM_TABLES = {ITEM_TERM: "terms", ITEM_KANJI: "kanji"}

# Weakest to strongest. "unknown" is the absence of evidence and is never stored.
STATUS_UNKNOWN = "unknown"
STATUS_LEARNING = "learning"
STATUS_YOUNG = "young"
STATUS_MATURE = "mature"
STATUS_KNOWN = "known"
STATUSES = (STATUS_UNKNOWN, STATUS_LEARNING, STATUS_YOUNG, STATUS_MATURE, STATUS_KNOWN)

# Anki's convention, kept for every spaced-repetition source: a memory expected
# to last three weeks or more is mature.
MATURE_STABILITY_DAYS = 21.0
# Anything studied at all scores above an item nobody has touched.
MIN_STUDIED_SCORE = 0.05

MANUAL_PROVIDER = "manual"
# What the user says outranks what any source infers.
MANUAL_WEIGHT = 1.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_evidence (
    id INTEGER PRIMARY KEY,
    item_kind TEXT NOT NULL CHECK (item_kind IN ('term', 'kanji')),
    item_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('learning', 'young', 'mature', 'known')
    ),
    score REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    weight REAL NOT NULL CHECK (weight >= 0 AND weight <= 1),
    stability_days REAL,
    reviews INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at TEXT,
    due_at TEXT,
    detail TEXT,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(item_kind, item_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_evidence_provider
    ON knowledge_evidence(provider);

CREATE TABLE IF NOT EXISTS knowledge (
    item_kind TEXT NOT NULL CHECK (item_kind IN ('term', 'kanji')),
    item_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('learning', 'young', 'mature', 'known')
    ),
    score REAL NOT NULL,
    confidence REAL NOT NULL,
    provider TEXT NOT NULL,
    last_reviewed_at TEXT,
    due_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (item_kind, item_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_kind_status
    ON knowledge(item_kind, status);
"""

# The two tables point at terms or characters by kind and id, which a foreign
# key cannot express, so a trigger per parent table drops the rows instead.
ITEM_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_knowledge_{table}_delete
AFTER DELETE ON {table}
BEGIN
    DELETE FROM knowledge_evidence WHERE item_kind = '{kind}' AND item_id = OLD.id;
    DELETE FROM knowledge WHERE item_kind = '{kind}' AND item_id = OLD.id;
END;
"""


def ensure_schema(connection: sqlite3.Connection) -> bool:
    """Create the tables and the delete triggers; True when anything was missing.

    Runs on every connection, so an up-to-date database costs one read. A
    parent table created later gets its trigger on the next connection.
    """
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        )
    }
    changed = False
    if not {"knowledge_evidence", "knowledge"} <= existing:
        connection.executescript(SCHEMA_SQL)
        changed = True
    for kind, table in ITEM_TABLES.items():
        if table in existing and f"trg_knowledge_{table}_delete" not in existing:
            connection.executescript(ITEM_DELETE_TRIGGER_SQL.format(table=table, kind=kind))
            changed = True
    return changed


def strength_score(stability_days: float | None) -> float:
    """0..1 for how durable a memory is: 0.5 where it turns mature, 0.9 near six months."""
    stability = max(float(stability_days or 0.0), 0.0)
    return max(MIN_STUDIED_SCORE, stability / (stability + MATURE_STABILITY_DAYS))


def srs_status(stability_days: float | None, *, in_learning: bool) -> str:
    """The status of an item under spaced repetition, whichever system schedules it."""
    if in_learning:
        return STATUS_LEARNING
    if float(stability_days or 0.0) >= MATURE_STABILITY_DAYS:
        return STATUS_MATURE
    return STATUS_YOUNG


def format_timestamp(moment: datetime) -> str:
    """UTC in the form SQLite's CURRENT_TIMESTAMP writes, like every other column."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Evidence:
    """What one provider believes about one item."""

    item_kind: str
    item_id: int
    status: str
    stability_days: float | None = None
    reviews: int = 0
    lapses: int = 0
    last_reviewed_at: str | None = None
    due_at: str | None = None
    # Provider-specific facts worth showing but not worth a column.
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def score(self) -> float:
        if self.status == STATUS_KNOWN:
            return 1.0
        return strength_score(self.stability_days)


class KnowledgeProvider(Protocol):
    """A source of fluency data.

    ``weight`` is how far its evidence is trusted, 0..1: a spaced-repetition
    history is strong, a passive exposure count would be weak. ``collect``
    returns everything the source currently knows, at most one ``Evidence``
    per item; items it has nothing on are simply left out.
    """

    name: str
    weight: float

    def collect(self, connection: sqlite3.Connection) -> Iterable[Evidence]: ...


@dataclass(frozen=True)
class RefreshReport:
    provider: str
    status_counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.status_counts.values())

    def summary(self) -> str:
        parts = [
            f"{self.status_counts[status]} {status}"
            for status in STATUSES
            if self.status_counts.get(status)
        ]
        return f"{self.provider}: {self.total} items" + (f" ({', '.join(parts)})" if parts else "")


def write_evidence(
    connection: sqlite3.Connection,
    provider: str,
    weight: float,
    evidence: Evidence,
) -> None:
    if evidence.item_kind not in ITEM_TABLES:
        raise ValueError(f"Unknown knowledge item kind: {evidence.item_kind}")
    connection.execute(
        """
        INSERT INTO knowledge_evidence (
            item_kind, item_id, provider, status, score, weight, stability_days,
            reviews, lapses, last_reviewed_at, due_at, detail, observed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(item_kind, item_id, provider) DO UPDATE SET
            status = excluded.status,
            score = excluded.score,
            weight = excluded.weight,
            stability_days = excluded.stability_days,
            reviews = excluded.reviews,
            lapses = excluded.lapses,
            last_reviewed_at = excluded.last_reviewed_at,
            due_at = excluded.due_at,
            detail = excluded.detail,
            observed_at = excluded.observed_at
        """,
        (
            evidence.item_kind,
            evidence.item_id,
            provider,
            evidence.status,
            evidence.score,
            weight,
            evidence.stability_days,
            evidence.reviews,
            evidence.lapses,
            evidence.last_reviewed_at,
            evidence.due_at,
            json.dumps(evidence.detail, ensure_ascii=False, sort_keys=True)
            if evidence.detail
            else None,
        ),
    )


def consolidate(
    connection: sqlite3.Connection,
    item: tuple[str, int] | None = None,
) -> None:
    """Rebuild ``knowledge`` from the evidence, for one item or for all of them.

    The most trusted evidence about an item wins, and between equally trusted
    providers the stronger claim does: knowing a word in either of two decks is
    knowing it.
    """
    item_filter = ""
    params: tuple[object, ...] = ()
    if item is not None:
        item_filter = "AND item_kind = ? AND item_id = ?"
        params = item
    connection.execute(
        f"""
        DELETE FROM knowledge
        WHERE NOT EXISTS (
            SELECT 1
            FROM knowledge_evidence AS evidence
            WHERE evidence.item_kind = knowledge.item_kind
                AND evidence.item_id = knowledge.item_id
        )
        {item_filter}
        """,
        params,
    )
    # Rows are only written when they differ, so a refresh that changes nothing
    # leaves no trace for the change log (and the pages) to react to.
    connection.execute(
        f"""
        INSERT INTO knowledge (
            item_kind, item_id, status, score, confidence, provider,
            last_reviewed_at, due_at, updated_at
        )
        SELECT
            item_kind, item_id, status, score, weight, provider,
            last_reviewed_at, due_at, CURRENT_TIMESTAMP
        FROM (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY item_kind, item_id
                    ORDER BY weight DESC, score DESC, id ASC
                ) AS evidence_rank
            FROM knowledge_evidence
            WHERE 1 = 1 {item_filter}
        )
        WHERE evidence_rank = 1
        ON CONFLICT(item_kind, item_id) DO UPDATE SET
            status = excluded.status,
            score = excluded.score,
            confidence = excluded.confidence,
            provider = excluded.provider,
            last_reviewed_at = excluded.last_reviewed_at,
            due_at = excluded.due_at,
            updated_at = excluded.updated_at
        WHERE status IS NOT excluded.status
            OR score IS NOT excluded.score
            OR confidence IS NOT excluded.confidence
            OR provider IS NOT excluded.provider
            OR last_reviewed_at IS NOT excluded.last_reviewed_at
            OR due_at IS NOT excluded.due_at
        """,
        params,
    )


def refresh_provider(
    connection: sqlite3.Connection,
    provider: KnowledgeProvider,
) -> RefreshReport:
    """Replace everything ``provider`` said before with what it says now."""
    # Collect first: a provider that cannot reach its source raises here and
    # the evidence already stored stays as it was.
    collected = list(provider.collect(connection))
    connection.execute("DELETE FROM knowledge_evidence WHERE provider = ?", (provider.name,))
    for evidence in collected:
        write_evidence(connection, provider.name, provider.weight, evidence)
    consolidate(connection)
    return RefreshReport(
        provider=provider.name,
        status_counts=dict(Counter(evidence.status for evidence in collected)),
    )


def item_exists(connection: sqlite3.Connection, item_kind: str, item_id: int) -> bool:
    table = ITEM_TABLES.get(item_kind)
    if table is None:
        raise ValueError(f"Unknown knowledge item kind: {item_kind}")
    row = connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (item_id,)).fetchone()
    return row is not None


def mark_known(connection: sqlite3.Connection, item_kind: str, item_id: int) -> None:
    """Record the user's own word that they know an item."""
    if not item_exists(connection, item_kind, item_id):
        raise ValueError(f"No {item_kind} with id {item_id}.")
    write_evidence(
        connection,
        MANUAL_PROVIDER,
        MANUAL_WEIGHT,
        Evidence(
            item_kind=item_kind,
            item_id=item_id,
            status=STATUS_KNOWN,
            last_reviewed_at=format_timestamp(datetime.now(UTC)),
        ),
    )
    consolidate(connection, (item_kind, item_id))


def clear_mark(connection: sqlite3.Connection, item_kind: str, item_id: int) -> bool:
    """Withdraw a manual mark, leaving the item to what the other providers say."""
    cursor = connection.execute(
        """
        DELETE FROM knowledge_evidence
        WHERE item_kind = ? AND item_id = ? AND provider = ?
        """,
        (item_kind, item_id, MANUAL_PROVIDER),
    )
    consolidate(connection, (item_kind, item_id))
    return cursor.rowcount > 0


def load_status(
    connection: sqlite3.Connection,
    item_kind: str,
    item_id: int,
) -> tuple[str, float]:
    """The consolidated status and score of one item; unknown and 0 without evidence."""
    row = connection.execute(
        "SELECT status, score FROM knowledge WHERE item_kind = ? AND item_id = ?",
        (item_kind, item_id),
    ).fetchone()
    if row is None:
        return STATUS_UNKNOWN, 0.0
    return str(row[0]), float(row[1])


def load_evidence(
    connection: sqlite3.Connection,
    item_kind: str,
    item_id: int,
) -> list[dict[str, object]]:
    """Every provider's evidence about one item, the one that decides first."""
    rows = connection.execute(
        """
        SELECT
            provider, status, score, weight, stability_days, reviews, lapses,
            last_reviewed_at, due_at, detail, observed_at
        FROM knowledge_evidence
        WHERE item_kind = ? AND item_id = ?
        ORDER BY weight DESC, score DESC, id ASC
        """,
        (item_kind, item_id),
    ).fetchall()
    evidence: list[dict[str, object]] = []
    for row in rows:
        entry = dict(row)
        entry["detail"] = json.loads(row["detail"]) if row["detail"] else {}
        evidence.append(entry)
    return evidence
