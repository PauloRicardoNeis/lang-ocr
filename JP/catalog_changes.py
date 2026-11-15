"""Catalog change log: which entities changed, written by SQLite triggers.

Every write to a table the web UI shows appends rows to ``catalog_changes``
through triggers, so the capture worker, the CLI, the web actions and the Anki
sync all report their changes without any of them having to remember to. Each
row names an entity key ("term" 42, "source" 17, ...). A page watches the keys
it shows and refreshes only when one of them appears after the revision it was
rendered at.

``capture_jobs`` only ever logs "job" keys, so job-status churn and rejected
frames never touch a catalog page; only views that watch "job" (the capture
progress) see them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

CATALOG_CHANGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_changes (
    id INTEGER PRIMARY KEY,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_catalog_changes_entity
    ON catalog_changes(entity, entity_id, id);
"""

# The log keeps this many recent rows. A reader whose revision fell out of the
# window is told to refresh everything (see ``changes_since``).
CHANGE_RETENTION = 20_000
TRIGGER_PREFIX = "catalog_change_"
TRIGGER_EVENTS = ("INSERT", "UPDATE", "DELETE")
# Past this many ids of one entity a page watches the whole kind (see Watch.of).
WATCH_MAX_IDS = 150


@dataclass(frozen=True)
class ChangeSpec:
    """Which entity keys a row of ``table`` stands for.

    ``keys`` pairs an entity name with a SQL expression over ``{row}`` (NEW or
    OLD). An expression that yields NULL (a parent row already gone) logs
    nothing; ``None`` as the expression logs a kind-level key with a NULL id,
    which matches every watcher of that entity.
    ``watched_columns`` limits UPDATE triggers to changes of those columns, so
    a stats recalculation that changes nothing logs nothing.
    """

    table: str
    keys: tuple[tuple[str, str | None], ...]
    watched_columns: tuple[str, ...] = ()


STATS_COLUMNS = (
    "total_occurrences",
    "distinct_sources",
    "first_seen_at",
    "last_seen_at",
    "priority_score",
)

CHANGE_SPECS: tuple[ChangeSpec, ...] = (
    ChangeSpec("sources", (("source", "{row}.id"),)),
    ChangeSpec("source_terms", (("source", "{row}.source_id"), ("term", "{row}.term_id"))),
    ChangeSpec("source_segments", (("source", "{row}.source_id"),)),
    ChangeSpec(
        "source_segment_terms",
        (
            ("source", "(SELECT source_id FROM source_segments WHERE id = {row}.source_segment_id)"),
            ("term", "{row}.term_id"),
        ),
    ),
    ChangeSpec("source_analysis_segments", (("source", "{row}.source_id"),)),
    ChangeSpec(
        "source_analysis_candidates",
        (
            (
                "source",
                "(SELECT source_id FROM source_analysis_segments"
                " WHERE id = {row}.source_analysis_segment_id)",
            ),
        ),
    ),
    ChangeSpec("source_translations", (("source", "{row}.source_id"),)),
    ChangeSpec("terms", (("term", "{row}.id"),)),
    ChangeSpec("term_forms", (("term", "{row}.term_id"),)),
    ChangeSpec(
        "term_stats",
        (("term", "{row}.term_id"),),
        watched_columns=(*STATS_COLUMNS, "distinct_forms"),
    ),
    ChangeSpec("kanji", (("kanji", "{row}.id"),)),
    ChangeSpec(
        "kanji_stats",
        (("kanji", "{row}.kanji_id"),),
        watched_columns=(*STATS_COLUMNS, "distinct_terms"),
    ),
    ChangeSpec("source_kanji", (("source", "{row}.source_id"), ("kanji", "{row}.kanji_id"))),
    ChangeSpec("flashcards", (("flashcard", "{row}.id"), ("term", "{row}.term_id"))),
    ChangeSpec("languages", (("language", None),)),
    ChangeSpec("settings", (("setting", None),)),
    ChangeSpec("capture_jobs", (("job", "{row}.id"),)),
)


def trigger_name(table: str, event: str) -> str:
    return f"{TRIGGER_PREFIX}{table}_{event.lower()}"


def _insert_statement(entity: str, value: str, condition: str) -> str:
    return (
        "INSERT INTO catalog_changes (entity, entity_id) "
        f"SELECT '{entity}', {value} WHERE {condition};"
    )


def trigger_sql(spec: ChangeSpec, event: str) -> str:
    """One AFTER trigger of ``spec.table``. UPDATE also logs the old keys when they moved."""
    row = "OLD" if event == "DELETE" else "NEW"
    statements: list[str] = []
    for entity, expression in spec.keys:
        if expression is None:
            statements.append(
                f"INSERT INTO catalog_changes (entity, entity_id) VALUES ('{entity}', NULL);"
            )
            continue
        value = expression.format(row=row)
        statements.append(_insert_statement(entity, value, f"{value} IS NOT NULL"))
    when = ""
    if event == "UPDATE":
        for entity, expression in spec.keys:
            if expression is None:
                continue
            old_value = expression.format(row="OLD")
            new_value = expression.format(row="NEW")
            statements.append(
                _insert_statement(
                    entity,
                    old_value,
                    f"{old_value} IS NOT NULL AND {old_value} IS NOT {new_value}",
                )
            )
        if spec.watched_columns:
            when = " WHEN " + " OR ".join(
                f"OLD.{column} IS NOT NEW.{column}" for column in spec.watched_columns
            )
    body = "\n    ".join(statements)
    return (
        f"CREATE TRIGGER {trigger_name(spec.table, event)}\n"
        f"AFTER {event} ON {spec.table}{when}\n"
        f"BEGIN\n    {body}\nEND"
    )


def prune_trigger_sql() -> str:
    return (
        f"CREATE TRIGGER {TRIGGER_PREFIX}prune\n"
        "AFTER INSERT ON catalog_changes\n"
        "BEGIN\n"
        f"    DELETE FROM catalog_changes WHERE id <= NEW.id - {CHANGE_RETENTION};\n"
        "END"
    )


def wanted_triggers(connection: sqlite3.Connection) -> dict[str, str]:
    """The triggers this database should have: every event of every existing watched table."""
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    definitions = {f"{TRIGGER_PREFIX}prune": prune_trigger_sql()}
    for spec in CHANGE_SPECS:
        if spec.table not in existing_tables:
            continue
        for event in TRIGGER_EVENTS:
            definitions[trigger_name(spec.table, event)] = trigger_sql(spec, event)
    return definitions


def _normalized(sql: str | None) -> str:
    return " ".join(str(sql or "").split()).lower().rstrip(" ;")


def ensure_catalog_change_log(connection: sqlite3.Connection) -> bool:
    """Create the log and bring its triggers in line with ``CHANGE_SPECS``.

    One query reads every existing log trigger; only missing, stale or obsolete
    ones are touched, so an up-to-date database costs two reads per connection.
    A table created later (a fresh database runs this before ``schema.sql``)
    gets its triggers on the next connection.
    """
    connection.executescript(CATALOG_CHANGE_SCHEMA_SQL)
    wanted = wanted_triggers(connection)
    existing = {
        str(row[0]): str(row[1] or "")
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE ?",
            (f"{TRIGGER_PREFIX}%",),
        ).fetchall()
    }
    changed = False
    for name in sorted(set(existing) - set(wanted)):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
        changed = True
    for name, create_sql in wanted.items():
        if _normalized(existing.get(name)) == _normalized(create_sql):
            continue
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
        connection.execute(create_sql)
        changed = True
    return changed


@dataclass(frozen=True)
class Watch:
    """The entity keys a page depends on.

    ``kinds`` match every id of an entity ("term" = any term); ``ids`` match one
    entity id. A kind-level change (NULL id, e.g. "language") matches both.
    """

    kinds: frozenset[str] = field(default_factory=frozenset)
    ids: frozenset[tuple[str, int]] = field(default_factory=frozenset)

    @classmethod
    def of(cls, *kinds: str, **ids: object) -> "Watch":
        """``Watch.of("source", term=[1, 2], kanji=[5])``.

        An entity with more than ``WATCH_MAX_IDS`` ids is watched as a kind
        instead: the page then also wakes for unrelated changes of that kind,
        but its poll URL stays short (a capture can link hundreds of terms).
        """
        all_kinds = set(kinds)
        pairs: set[tuple[str, int]] = set()
        for entity, values in ids.items():
            entity_ids = {int(value) for value in values or () if value is not None}  # type: ignore[union-attr]
            if len(entity_ids) > WATCH_MAX_IDS:
                all_kinds.add(entity)
            else:
                pairs.update((entity, entity_id) for entity_id in entity_ids)
        pairs = {(entity, entity_id) for entity, entity_id in pairs if entity not in all_kinds}
        return cls(kinds=frozenset(all_kinds), ids=frozenset(pairs))

    def __bool__(self) -> bool:
        return bool(self.kinds or self.ids)

    def encode(self) -> str:
        """``source,term:1,term:2``: the wire form pages send back when they poll."""
        parts = sorted(self.kinds)
        parts.extend(f"{entity}:{entity_id}" for entity, entity_id in sorted(self.ids))
        return ",".join(parts)

    @classmethod
    def decode(cls, value: str) -> "Watch":
        """Parse ``encode``'s form; raises ``ValueError`` on anything else."""
        kinds: set[str] = set()
        ids: set[tuple[str, int]] = set()
        for part in (piece.strip() for piece in value.split(",")):
            if not part:
                continue
            entity, _, raw_id = part.partition(":")
            if not entity.isidentifier():
                raise ValueError(f"Invalid watch entity: {part!r}")
            if raw_id:
                ids.add((entity, int(raw_id)))
            else:
                kinds.add(entity)
        return cls(kinds=frozenset(kinds), ids=frozenset(ids))


@dataclass(frozen=True)
class ChangeStatus:
    revision: int
    changed: bool
    reset: bool


def current_revision(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(id) FROM catalog_changes").fetchone()
    return int(row[0] or 0)


def changes_since(connection: sqlite3.Connection, since: int, watch: Watch) -> ChangeStatus:
    """Did anything ``watch`` covers change after revision ``since``?

    ``reset`` means the caller cannot know: rows after ``since`` may have been
    pruned, or ``since`` is ahead of the log (the database was replaced). The
    caller should then refresh everything and take the new revision.
    """
    revision_row = connection.execute(
        "SELECT MAX(id), MIN(id) FROM catalog_changes"
    ).fetchone()
    revision = int(revision_row[0] or 0)
    oldest = int(revision_row[1] or 0)
    if since > revision:
        return ChangeStatus(revision=revision, changed=True, reset=True)
    if since == revision or not watch:
        return ChangeStatus(revision=revision, changed=False, reset=False)
    if oldest and since < oldest - 1:
        return ChangeStatus(revision=revision, changed=True, reset=True)
    clauses: list[str] = []
    params: list[object] = [since]
    if watch.kinds:
        clauses.append(f"entity IN ({', '.join('?' for _ in watch.kinds)})")
        params.extend(sorted(watch.kinds))
    id_entities = sorted({entity for entity, _ in watch.ids})
    for entity in id_entities:
        entity_ids = sorted(entity_id for name, entity_id in watch.ids if name == entity)
        clauses.append(
            f"(entity = ? AND (entity_id IS NULL OR entity_id IN ({', '.join('?' for _ in entity_ids)})))"
        )
        params.append(entity)
        params.extend(entity_ids)
    row = connection.execute(
        f"""
        SELECT 1
        FROM catalog_changes
        WHERE id > ? AND ({' OR '.join(clauses)})
        LIMIT 1
        """,
        params,
    ).fetchone()
    return ChangeStatus(revision=revision, changed=row is not None, reset=False)
