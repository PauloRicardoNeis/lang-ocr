from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "lang_jp.sqlite3"
SCHEMA_PATH = ROOT / "schema.sql"
DEFAULT_ANKI_EXPORT_PATH = ROOT / "exports" / "anki_basic.tsv"
DEFAULT_JP_DECK_NAME = "Lang::Japanese"
DEFAULT_JP_KANJI_DECK_NAME = DEFAULT_JP_DECK_NAME
DEFAULT_UI_PORT = 8766
DEFAULT_ANKI_CONNECT_HOST = "127.0.0.1"
DEFAULT_ANKI_CONNECT_PORT = 8765
ANKI_CONNECT_VERSION = 6
DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = (
    os.environ.get("LANG_JP_AUTO_TRANSLATE_TERM_MEANINGS", "1") != "0"
)
DEFAULT_TERM_TRANSLATION_TARGET = os.environ.get(
    "LANG_JP_TERM_TRANSLATION_TARGET",
    "pt",
)
DEFAULT_TERM_TRANSLATION_TIMEOUT_SECONDS = float(
    os.environ.get("LANG_JP_TERM_TRANSLATION_TIMEOUT_SECONDS", "10")
)
KANJI_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kanji (
    id INTEGER PRIMARY KEY,
    literal TEXT NOT NULL UNIQUE,
    meaning_pt TEXT,
    canonical_meanings TEXT,
    on_readings TEXT,
    kun_readings TEXT,
    name_readings TEXT,
    metadata_updated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_kanji (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kanji_id INTEGER NOT NULL REFERENCES kanji(id) ON DELETE CASCADE,
    count_in_source INTEGER NOT NULL DEFAULT 1 CHECK (count_in_source > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, kanji_id)
);

CREATE INDEX IF NOT EXISTS idx_source_kanji_kanji_id
    ON source_kanji(kanji_id);

CREATE INDEX IF NOT EXISTS idx_source_kanji_source_id
    ON source_kanji(source_id);

CREATE TABLE IF NOT EXISTS kanji_stats (
    kanji_id INTEGER PRIMARY KEY REFERENCES kanji(id) ON DELETE CASCADE,
    total_occurrences INTEGER NOT NULL DEFAULT 0,
    distinct_sources INTEGER NOT NULL DEFAULT 0,
    distinct_terms INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT,
    last_seen_at TEXT,
    priority_score REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

KANJI_FLASHCARD_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kanji_flashcards (
    id INTEGER PRIMARY KEY,
    kanji_id INTEGER NOT NULL REFERENCES kanji(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    template_name TEXT NOT NULL DEFAULT 'kanji_basic',
    deck_name TEXT NOT NULL DEFAULT 'Lang::Japanese',
    note_type TEXT NOT NULL DEFAULT 'Basic',
    front_text TEXT NOT NULL,
    back_text TEXT NOT NULL,
    tags TEXT,
    anki_note_id INTEGER,
    export_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        export_status IN ('pending', 'exported')
    ),
    exported_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kanji_id, template_name)
);

CREATE INDEX IF NOT EXISTS idx_kanji_flashcards_export_status
    ON kanji_flashcards(export_status);

CREATE INDEX IF NOT EXISTS idx_kanji_flashcards_anki_note_id
    ON kanji_flashcards(anki_note_id);
"""

KANJI_VIEW_SQL = """
DROP VIEW IF EXISTS v_kanji_overview;

CREATE VIEW v_kanji_overview AS
SELECT
    k.id,
    k.literal,
    k.meaning_pt,
    k.canonical_meanings,
    k.on_readings,
    k.kun_readings,
    k.name_readings,
    k.metadata_updated_at,
    COALESCE(ks.total_occurrences, 0) AS total_occurrences,
    COALESCE(ks.distinct_sources, 0) AS distinct_sources,
    COALESCE(ks.distinct_terms, 0) AS distinct_terms,
    COALESCE(ks.priority_score, 0) AS priority_score,
    ks.first_seen_at,
    ks.last_seen_at,
    k.created_at,
    k.updated_at
FROM kanji AS k
LEFT JOIN kanji_stats AS ks
    ON ks.kanji_id = k.id;
"""

SOURCE_SEGMENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_segments (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segment_key TEXT NOT NULL,
    segment_text TEXT NOT NULL,
    reading TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, segment_key)
);

CREATE INDEX IF NOT EXISTS idx_source_segments_source_id
    ON source_segments(source_id);

CREATE TABLE IF NOT EXISTS source_segment_terms (
    id INTEGER PRIMARY KEY,
    source_segment_id INTEGER NOT NULL REFERENCES source_segments(id) ON DELETE CASCADE,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    term_form_id INTEGER REFERENCES term_forms(id) ON DELETE SET NULL,
    surface_form TEXT NOT NULL,
    count_in_segment INTEGER NOT NULL DEFAULT 1 CHECK (count_in_segment > 0),
    confidence REAL CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_segment_id, term_id, surface_form)
);

CREATE INDEX IF NOT EXISTS idx_source_segment_terms_segment_id
    ON source_segment_terms(source_segment_id);

CREATE INDEX IF NOT EXISTS idx_source_segment_terms_term_id
    ON source_segment_terms(term_id);
"""

CAPTURE_JOB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS capture_jobs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    captured_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'accepted', 'rejected', 'failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    temp_image_path TEXT,
    final_image_path TEXT,
    image_sha256 TEXT,
    image_phash TEXT,
    raw_text TEXT,
    ocr_text_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_capture_jobs_status_created_at
    ON capture_jobs(status, created_at);

CREATE INDEX IF NOT EXISTS idx_capture_jobs_image_sha256
    ON capture_jobs(image_sha256);
"""

SOURCE_ANALYSIS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_analysis_segments (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    reading TEXT,
    confidence REAL CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    bbox_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_source_analysis_segments_source_id
    ON source_analysis_segments(source_id);

CREATE TABLE IF NOT EXISTS source_analysis_candidates (
    id INTEGER PRIMARY KEY,
    source_analysis_segment_id INTEGER NOT NULL REFERENCES source_analysis_segments(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL CHECK (
        candidate_type IN ('term', 'subterm', 'kanji')
    ),
    surface_form TEXT NOT NULL,
    dictionary_form TEXT NOT NULL,
    reading TEXT,
    pos TEXT,
    confidence REAL CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    rank INTEGER NOT NULL DEFAULT 0,
    parent_candidate_id INTEGER REFERENCES source_analysis_candidates(id) ON DELETE SET NULL,
    matched_term_id INTEGER REFERENCES terms(id) ON DELETE SET NULL,
    matched_kanji_id INTEGER REFERENCES kanji(id) ON DELETE SET NULL,
    approval_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        approval_status IN ('pending', 'approved', 'rejected')
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_source_analysis_candidates_segment_id
    ON source_analysis_candidates(source_analysis_segment_id);

CREATE INDEX IF NOT EXISTS idx_source_analysis_candidates_approval_status
    ON source_analysis_candidates(approval_status);
"""

SOURCE_TRANSLATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_translations (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_text_hash TEXT,
    target_language TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'translated', 'failed', 'reviewed')
    ),
    translated_text TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, target_language, provider)
);

CREATE INDEX IF NOT EXISTS idx_source_translations_source_id
    ON source_translations(source_id);

CREATE INDEX IF NOT EXISTS idx_source_translations_target_language
    ON source_translations(target_language);
"""

TERM_VIEW_SQL = """
DROP VIEW IF EXISTS v_terms_overview;

CREATE VIEW v_terms_overview AS
SELECT
    t.id,
    t.lemma,
    t.reading,
    t.meaning_pt,
    t.pos,
    COALESCE(ts.total_occurrences, 0) AS total_occurrences,
    COALESCE(ts.distinct_sources, 0) AS distinct_sources,
    COALESCE(ts.distinct_forms, 0) AS distinct_forms,
    COALESCE(ts.priority_score, 0) AS priority_score,
    ts.first_seen_at,
    ts.last_seen_at
FROM terms AS t
LEFT JOIN term_stats AS ts
    ON ts.term_id = t.id;
"""

TERM_PRIORITY_ORDER_SQL = """
        priority_score DESC,
        COALESCE(last_seen_at, first_seen_at) DESC,
        lemma COLLATE NOCASE ASC,
        id ASC
"""

TERM_PRIORITY_ORDER_WITH_TS_SQL = """
            COALESCE(ts.priority_score, 0) DESC,
            COALESCE(ts.last_seen_at, ts.first_seen_at) DESC,
            t.lemma COLLATE NOCASE ASC,
            t.id ASC
"""

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def db_connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    ensure_runtime_schema(connection)
    return connection


def normalize_token(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().split())


BLACKLISTED_SINGLE_SYMBOL_TERMS = frozenset(
    {
        "ー",
        "ｰ",
        "〜",
        "～",
        "…",
    }
)


class BlacklistedTermError(ValueError):
    """Raised when a term should not be cataloged."""


def is_single_kana_character(value: str) -> bool:
    normalized = normalize_token(value)
    if len(normalized) != 1:
        return False

    codepoint = ord(normalized)
    return (
        0x3041 <= codepoint <= 0x3096
        or codepoint in (0x309D, 0x309E, 0x309F)
        or 0x30A1 <= codepoint <= 0x30FA
        or codepoint in (0x30FD, 0x30FE)
        or 0xFF66 <= codepoint <= 0xFF9D
    )


def term_blacklist_reason(
    lemma: str | None,
    *,
    reading: str | None = None,
    pos: str | None = None,
) -> str | None:
    del reading
    del pos
    normalized_lemma = normalize_token(lemma)
    if not normalized_lemma:
        return None
    if len(normalized_lemma) == 1 and (
        unicodedata.category(normalized_lemma).startswith("P")
        or normalized_lemma in BLACKLISTED_SINGLE_SYMBOL_TERMS
    ):
        return "placeholder_or_punctuation"
    if is_single_kana_character(normalized_lemma):
        return "single_kana_syllable"
    return None


def ensure_term_not_blacklisted(
    lemma: str | None,
    *,
    reading: str | None = None,
    pos: str | None = None,
) -> None:
    reason = term_blacklist_reason(lemma, reading=reading, pos=pos)
    if reason is None:
        return
    normalized_lemma = normalize_token(lemma) or "<empty>"
    raise BlacklistedTermError(f"Blocked blacklisted term '{normalized_lemma}' ({reason}).")


def filter_blacklisted_term_rows(rows: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    visible_rows: list[sqlite3.Row] = []
    for row in rows:
        keys = set(row.keys())
        reason = term_blacklist_reason(
            row["lemma"] if "lemma" in keys else None,
            reading=row["reading"] if "reading" in keys else None,
            pos=row["pos"] if "pos" in keys else None,
        )
        if reason is None:
            visible_rows.append(row)
    return visible_rows


def blacklisted_term_entries(
    connection: sqlite3.Connection,
) -> list[tuple[sqlite3.Row, str]]:
    rows = connection.execute(
        """
        SELECT
            t.id,
            t.lemma,
            t.reading,
            t.pos,
            COALESCE(ts.total_occurrences, 0) AS total_occurrences,
            COALESCE(ts.priority_score, 0) AS priority_score,
            (
                SELECT COUNT(*)
                FROM flashcards AS f
                WHERE f.term_id = t.id
            ) AS flashcard_count
        FROM terms AS t
        LEFT JOIN term_stats AS ts
            ON ts.term_id = t.id
        ORDER BY
            COALESCE(ts.priority_score, 0) DESC,
            t.id ASC
        """
    ).fetchall()
    entries: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        reason = term_blacklist_reason(
            row["lemma"],
            reading=row["reading"],
            pos=row["pos"],
        )
        if reason is not None:
            entries.append((row, reason))
    return entries


def is_proper_name_meaning(value: str | None) -> bool:
    normalized = normalize_token(value).lower()
    return normalized.startswith("nome") and "proprio" in normalized


def canonical_key(lemma: str, reading: str | None) -> str:
    return f"{normalize_token(lemma)}::{normalize_token(reading)}"


def form_key(surface_form: str, reading: str | None) -> str:
    return f"{normalize_token(surface_form)}::{normalize_token(reading)}"


def segment_key(segment_text: str, reading: str | None) -> str:
    return f"{normalize_token(segment_text)}::{normalize_token(reading)}"


def ensure_db_parent(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


def ensure_output_parent(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def ensure_runtime_schema(connection: sqlite3.Connection) -> None:
    changed = False

    connection.executescript(CAPTURE_JOB_SCHEMA_SQL)
    if table_exists(connection, "flashcards"):
        flashcard_columns = table_columns(connection, "flashcards")
        if "anki_note_id" not in flashcard_columns:
            connection.execute("ALTER TABLE flashcards ADD COLUMN anki_note_id INTEGER")
            changed = True

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_flashcards_anki_note_id
                ON flashcards(anki_note_id)
            """
        )

    connection.executescript(KANJI_SCHEMA_SQL)
    connection.executescript(KANJI_FLASHCARD_SCHEMA_SQL)
    connection.executescript(SOURCE_SEGMENT_SCHEMA_SQL)
    connection.executescript(SOURCE_ANALYSIS_SCHEMA_SQL)
    connection.executescript(SOURCE_TRANSLATION_SCHEMA_SQL)
    if table_exists(connection, "sources"):
        source_columns = table_columns(connection, "sources")
        if "ingest_mode" not in source_columns:
            connection.execute(
                """
                ALTER TABLE sources
                ADD COLUMN ingest_mode TEXT NOT NULL DEFAULT 'manual'
                """
            )
            changed = True
        if "review_status" not in source_columns:
            connection.execute(
                """
                ALTER TABLE sources
                ADD COLUMN review_status TEXT NOT NULL DEFAULT 'reviewed'
                """
            )
            changed = True
        if "image_sha256" not in source_columns:
            connection.execute("ALTER TABLE sources ADD COLUMN image_sha256 TEXT")
            changed = True
        if "image_width" not in source_columns:
            connection.execute("ALTER TABLE sources ADD COLUMN image_width INTEGER")
            changed = True
        if "image_height" not in source_columns:
            connection.execute("ALTER TABLE sources ADD COLUMN image_height INTEGER")
            changed = True
        if "ocr_text_hash" not in source_columns:
            connection.execute("ALTER TABLE sources ADD COLUMN ocr_text_hash TEXT")
            changed = True
        if "capture_job_id" not in source_columns:
            connection.execute("ALTER TABLE sources ADD COLUMN capture_job_id INTEGER")
            changed = True
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sources_review_status_created_at
                ON sources(review_status, created_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sources_image_sha256
                ON sources(image_sha256)
            """
        )
    if table_exists(connection, "kanji"):
        kanji_columns = table_columns(connection, "kanji")
        if "meaning_pt" not in kanji_columns:
            connection.execute("ALTER TABLE kanji ADD COLUMN meaning_pt TEXT")
            changed = True
        if "canonical_meanings" not in kanji_columns:
            connection.execute("ALTER TABLE kanji ADD COLUMN canonical_meanings TEXT")
            changed = True
        if "on_readings" not in kanji_columns:
            connection.execute("ALTER TABLE kanji ADD COLUMN on_readings TEXT")
            changed = True
        if "kun_readings" not in kanji_columns:
            connection.execute("ALTER TABLE kanji ADD COLUMN kun_readings TEXT")
            changed = True
        if "name_readings" not in kanji_columns:
            connection.execute("ALTER TABLE kanji ADD COLUMN name_readings TEXT")
            changed = True
        if "metadata_updated_at" not in kanji_columns:
            connection.execute("ALTER TABLE kanji ADD COLUMN metadata_updated_at TEXT")
            changed = True
    if table_exists(connection, "kanji_stats"):
        kanji_stats_columns = table_columns(connection, "kanji_stats")
        if "distinct_terms" not in kanji_stats_columns:
            connection.execute(
                """
                ALTER TABLE kanji_stats
                ADD COLUMN distinct_terms INTEGER NOT NULL DEFAULT 0
                """
            )
            changed = True
    connection.executescript(TERM_VIEW_SQL)
    connection.executescript(KANJI_VIEW_SQL)

    if changed:
        connection.commit()


def init_db(db_path: Path) -> None:
    ensure_db_parent(db_path)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with db_connect(db_path) as connection:
        connection.executescript(schema_sql)
    print(f"Initialized database at {db_path}")


def stable_text_hash(value: str | None) -> str | None:
    normalized = normalize_token(value)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_image_dimension(value: object | None) -> int | None:
    try:
        normalized = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if normalized is None or normalized <= 0:
        return None
    return normalized


def read_source_image_dimensions(
    source_path: str | None,
) -> tuple[int | None, int | None]:
    if not source_path:
        return (None, None)
    path = Path(str(source_path))
    if not path.exists():
        return (None, None)
    try:
        from PIL import Image
    except ImportError:
        return (None, None)
    try:
        with Image.open(path) as image:
            return (int(image.width), int(image.height))
    except Exception:
        return (None, None)


def ensure_source_image_dimensions(
    connection: sqlite3.Connection,
    *,
    source_id: int | None,
    source_path: str | None,
    image_width: object | None,
    image_height: object | None,
) -> tuple[int | None, int | None]:
    width = normalize_image_dimension(image_width)
    height = normalize_image_dimension(image_height)
    if width is not None and height is not None:
        return (width, height)

    resolved_width, resolved_height = read_source_image_dimensions(source_path)
    if resolved_width is None or resolved_height is None:
        return (width, height)

    width = width or resolved_width
    height = height or resolved_height
    if source_id is not None:
        connection.execute(
            """
            UPDATE sources
            SET
                image_width = COALESCE(image_width, ?),
                image_height = COALESCE(image_height, ?)
            WHERE id = ?
            """,
            (width, height, source_id),
        )
    return (width, height)


def format_source_resolution(
    image_width: object | None,
    image_height: object | None,
) -> str | None:
    width = normalize_image_dimension(image_width)
    height = normalize_image_dimension(image_height)
    if width is None or height is None:
        return None
    return f"{width}x{height}"


@lru_cache(maxsize=512)
def google_gtx_translate_text(
    text: str,
    *,
    source_language: str,
    target_language: str,
) -> str | None:
    normalized = normalize_token(text)
    if not normalized:
        return None
    params = urlencode(
        {
            "client": "gtx",
            "sl": source_language,
            "tl": target_language,
            "dt": "t",
            "q": normalized,
        }
    )
    request = Request(
        f"https://translate.googleapis.com/translate_a/single?{params}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=DEFAULT_TERM_TRANSLATION_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    segments = payload[0] if isinstance(payload, list) and payload else []
    translated = normalize_token(
        "".join(
            str(segment[0])
            for segment in segments
            if isinstance(segment, list) and segment and segment[0] is not None
        )
    )
    if not translated or translated == normalized:
        return None
    return translated


def auto_translate_term_meaning_pt(
    lemma: str,
    *,
    reading: str | None = None,
) -> str | None:
    del reading
    if not DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS:
        return None
    try:
        return google_gtx_translate_text(
            lemma,
            source_language="ja",
            target_language=DEFAULT_TERM_TRANSLATION_TARGET,
        )
    except (OSError, TimeoutError, URLError, ValueError):
        return None


def find_source_by_image_sha256(
    connection: sqlite3.Connection,
    image_sha256: str | None,
) -> sqlite3.Row | None:
    if not image_sha256:
        return None
    return connection.execute(
        "SELECT * FROM sources WHERE image_sha256 = ? ORDER BY id DESC LIMIT 1",
        (image_sha256,),
    ).fetchone()


def create_source_record(
    connection: sqlite3.Connection,
    *,
    source_type: str,
    language_code: str = "ja",
    ingest_mode: str = "manual",
    review_status: str = "reviewed",
    title: str | None = None,
    game: str | None = None,
    scene: str | None = None,
    source_path: str | None = None,
    raw_text: str | None = None,
    notes: str | None = None,
    captured_at: str | None = None,
    image_sha256: str | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
    ocr_text_hash: str | None = None,
    capture_job_id: int | None = None,
) -> int:
    if source_type == "image":
        image_width, image_height = ensure_source_image_dimensions(
            connection,
            source_id=None,
            source_path=source_path,
            image_width=image_width,
            image_height=image_height,
        )
    cursor = connection.execute(
        """
        INSERT INTO sources (
            source_type,
            language_code,
            ingest_mode,
            review_status,
            title,
            game,
            scene,
            source_path,
            raw_text,
            notes,
            captured_at,
            image_sha256,
            image_width,
            image_height,
            ocr_text_hash,
            capture_job_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_type,
            language_code,
            ingest_mode,
            review_status,
            title,
            game,
            scene,
            source_path,
            raw_text,
            notes,
            captured_at,
            image_sha256,
            image_width,
            image_height,
            ocr_text_hash or stable_text_hash(raw_text),
            capture_job_id,
        ),
    )
    return int(cursor.lastrowid)


def update_source_record(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    raw_text: str | None = None,
    scene: str | None = None,
    review_status: str | None = None,
    notes: str | None = None,
) -> None:
    existing = connection.execute(
        "SELECT raw_text, scene, review_status, notes FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    if existing is None:
        raise SystemExit(f"Source #{source_id} does not exist.")

    new_raw_text = existing["raw_text"] if raw_text is None else raw_text
    connection.execute(
        """
        UPDATE sources
        SET
            raw_text = ?,
            scene = COALESCE(?, scene),
            review_status = COALESCE(?, review_status),
            notes = COALESCE(?, notes),
            ocr_text_hash = ?,
            captured_at = captured_at
        WHERE id = ?
        """,
        (
            new_raw_text,
            scene,
            review_status,
            notes,
            stable_text_hash(new_raw_text),
            source_id,
        ),
    )


def add_source(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        source_id = create_source_record(
            connection,
            source_type=args.source_type,
            language_code=args.language_code,
            ingest_mode=args.ingest_mode,
            review_status=args.review_status,
            title=args.title,
            game=args.game,
            scene=args.scene,
            source_path=args.path,
            raw_text=args.raw_text,
            notes=args.notes,
            captured_at=args.captured_at,
            image_sha256=args.image_sha256,
            ocr_text_hash=args.ocr_text_hash,
            capture_job_id=args.capture_job_id,
        )
    print(f"Created source #{source_id}")


def create_capture_job(
    connection: sqlite3.Connection,
    *,
    captured_at: str | None,
    temp_image_path: str,
    image_sha256: str | None,
    image_phash: str | None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO capture_jobs (
            captured_at,
            temp_image_path,
            image_sha256,
            image_phash
        )
        VALUES (?, ?, ?, ?)
        """,
        (captured_at, temp_image_path, image_sha256, image_phash),
    )
    return int(cursor.lastrowid)


def claim_next_capture_job(
    connection: sqlite3.Connection,
    *,
    max_attempts: int = 3,
) -> sqlite3.Row | None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT *
            FROM capture_jobs
            WHERE status IN ('pending', 'failed')
              AND attempt_count < ?
            ORDER BY
                CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                COALESCE(captured_at, created_at) DESC,
                id DESC
            LIMIT 1
            """,
            (max_attempts,),
        ).fetchone()
        if row is None:
            connection.commit()
            return None

        connection.execute(
            """
            UPDATE capture_jobs
            SET
                status = 'processing',
                attempt_count = attempt_count + 1,
                last_error = NULL
            WHERE id = ?
            """,
            (row["id"],),
        )
        connection.commit()
        return connection.execute(
            "SELECT * FROM capture_jobs WHERE id = ?",
            (row["id"],),
        ).fetchone()
    except Exception:
        connection.rollback()
        raise


def update_capture_job_status(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    status: str,
    last_error: str | None = None,
    final_image_path: str | None = None,
    raw_text: str | None = None,
    ocr_text_hash: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE capture_jobs
        SET
            status = ?,
            last_error = ?,
            final_image_path = COALESCE(?, final_image_path),
            raw_text = COALESCE(?, raw_text),
            ocr_text_hash = COALESCE(?, ocr_text_hash)
        WHERE id = ?
        """,
        (status, last_error, final_image_path, raw_text, ocr_text_hash, job_id),
    )


def claim_next_source_translation(
    connection: sqlite3.Connection,
    *,
    target_language: str | None = None,
    provider: str | None = None,
) -> sqlite3.Row | None:
    where_clauses = [
        "st.status IN ('pending', 'failed')",
        "COALESCE(s.raw_text, '') <> ''",
    ]
    params: list[object] = []
    if target_language is not None:
        where_clauses.append("st.target_language = ?")
        params.append(target_language)
    if provider is not None:
        where_clauses.append("st.provider = ?")
        params.append(provider)

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            f"""
            SELECT
                st.*,
                s.raw_text,
                s.ocr_text_hash AS current_source_text_hash
            FROM source_translations AS st
            JOIN sources AS s
              ON s.id = st.source_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY
                CASE st.status WHEN 'pending' THEN 0 ELSE 1 END,
                st.updated_at DESC,
                st.id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        connection.commit()
        return row
    except Exception:
        connection.rollback()
        raise


def clear_source_analysis(connection: sqlite3.Connection, source_id: int) -> None:
    connection.execute(
        """
        DELETE FROM source_analysis_candidates
        WHERE source_analysis_segment_id IN (
            SELECT id
            FROM source_analysis_segments
            WHERE source_id = ?
        )
        """,
        (source_id,),
    )
    connection.execute(
        "DELETE FROM source_analysis_segments WHERE source_id = ?",
        (source_id,),
    )


def create_source_analysis_segment(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    segment_index: int,
    raw_text: str,
    normalized_text: str,
    reading: str | None,
    confidence: float | None,
    bbox_json: str | None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO source_analysis_segments (
            source_id,
            segment_index,
            raw_text,
            normalized_text,
            reading,
            confidence,
            bbox_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, segment_index) DO UPDATE SET
            raw_text = excluded.raw_text,
            normalized_text = excluded.normalized_text,
            reading = excluded.reading,
            confidence = excluded.confidence,
            bbox_json = excluded.bbox_json,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            source_id,
            segment_index,
            raw_text,
            normalized_text,
            reading,
            confidence,
            bbox_json,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise SystemExit("Could not upsert source analysis segment.")
    return int(row["id"])


def create_source_analysis_candidate(
    connection: sqlite3.Connection,
    *,
    source_analysis_segment_id: int,
    candidate_type: str,
    surface_form: str,
    dictionary_form: str,
    reading: str | None,
    pos: str | None,
    confidence: float | None,
    rank: int,
    parent_candidate_id: int | None,
    matched_term_id: int | None,
    matched_kanji_id: int | None,
    approval_status: str = "pending",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO source_analysis_candidates (
            source_analysis_segment_id,
            candidate_type,
            surface_form,
            dictionary_form,
            reading,
            pos,
            confidence,
            rank,
            parent_candidate_id,
            matched_term_id,
            matched_kanji_id,
            approval_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_analysis_segment_id,
            candidate_type,
            surface_form,
            dictionary_form,
            reading,
            pos,
            confidence,
            rank,
            parent_candidate_id,
            matched_term_id,
            matched_kanji_id,
            approval_status,
        ),
    )
    return int(cursor.lastrowid)


def upsert_source_translation(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    source_text_hash: str | None,
    target_language: str,
    provider: str,
    status: str,
    translated_text: str | None,
    error_message: str | None,
) -> int:
    existing = connection.execute(
        """
        SELECT id
        FROM source_translations
        WHERE source_id = ? AND target_language = ? AND provider = ?
        """,
        (source_id, target_language, provider),
    ).fetchone()
    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO source_translations (
                source_id,
                source_text_hash,
                target_language,
                provider,
                status,
                translated_text,
                error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                source_text_hash,
                target_language,
                provider,
                status,
                translated_text,
                error_message,
            ),
        )
        return int(cursor.lastrowid)

    connection.execute(
        """
        UPDATE source_translations
        SET
            source_text_hash = ?,
            status = ?,
            translated_text = ?,
            error_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            source_text_hash,
            status,
            translated_text,
            error_message,
            existing["id"],
        ),
    )
    return int(existing["id"])


def review_source_rows(
    connection: sqlite3.Connection,
    *,
    status: str,
    limit: int,
) -> list[sqlite3.Row]:
    where_clause = ""
    params: list[object] = []
    if status != "all":
        where_clause = "WHERE s.review_status = ?"
        params.append(status)
    params.append(limit)
    return connection.execute(
        f"""
        SELECT
            s.id,
            s.review_status,
            s.ingest_mode,
            COALESCE(s.scene, '-') AS scene,
            s.source_path,
            s.image_width,
            s.image_height,
            COALESCE(s.captured_at, s.created_at) AS seen_at,
            COALESCE(s.raw_text, '') AS raw_text,
            (
                SELECT COUNT(*)
                FROM source_analysis_candidates AS sac
                JOIN source_analysis_segments AS sas
                    ON sas.id = sac.source_analysis_segment_id
                WHERE sas.source_id = s.id
                  AND sac.approval_status = 'pending'
            ) AS pending_candidates
        FROM sources AS s
        {where_clause}
        ORDER BY s.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def source_review_payload(
    connection: sqlite3.Connection,
    source_id: int,
) -> tuple[sqlite3.Row, list[sqlite3.Row], list[sqlite3.Row]]:
    source_row = connection.execute(
        """
        SELECT
            id,
            source_type,
            language_code,
            ingest_mode,
            review_status,
            title,
            game,
            scene,
            source_path,
            raw_text,
            notes,
            captured_at,
            image_sha256,
            image_width,
            image_height,
            ocr_text_hash,
            capture_job_id,
            (
                SELECT translated_text
                FROM source_translations AS st
                WHERE st.source_id = sources.id
                  AND st.target_language = 'en'
                  AND st.status = 'translated'
                ORDER BY st.updated_at DESC, st.id DESC
                LIMIT 1
            ) AS translated_text_en,
            (
                SELECT status
                FROM source_translations AS st
                WHERE st.source_id = sources.id
                  AND st.target_language = 'en'
                ORDER BY st.updated_at DESC, st.id DESC
                LIMIT 1
            ) AS translated_text_en_status,
            (
                SELECT provider
                FROM source_translations AS st
                WHERE st.source_id = sources.id
                  AND st.target_language = 'en'
                ORDER BY st.updated_at DESC, st.id DESC
                LIMIT 1
            ) AS translated_text_en_provider,
            created_at
        FROM sources
        WHERE id = ?
        """,
        (source_id,),
    ).fetchone()
    if source_row is None:
        raise SystemExit(f"Source #{source_id} does not exist.")

    segments = connection.execute(
        """
        SELECT *
        FROM source_analysis_segments
        WHERE source_id = ?
        ORDER BY segment_index ASC, id ASC
        """,
        (source_id,),
    ).fetchall()
    candidates = connection.execute(
        """
        SELECT
            sac.*,
            sas.segment_index,
            sas.raw_text AS segment_raw_text,
            sas.reading AS segment_reading
        FROM source_analysis_candidates AS sac
        JOIN source_analysis_segments AS sas
            ON sas.id = sac.source_analysis_segment_id
        WHERE sas.source_id = ?
        ORDER BY sas.segment_index ASC, sac.rank ASC, sac.id ASC
        """,
        (source_id,),
    ).fetchall()
    return source_row, segments, candidates


def pending_analysis_candidates(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    candidate_ids: list[int] | None = None,
) -> list[sqlite3.Row]:
    conditions = [
        "sas.source_id = ?",
        "sac.approval_status = 'pending'",
    ]
    params: list[object] = [source_id]
    if candidate_ids:
        placeholders = ", ".join("?" for _ in candidate_ids)
        conditions.append(f"sac.id IN ({placeholders})")
        params.extend(candidate_ids)
    return connection.execute(
        f"""
        SELECT
            sac.*,
            sas.segment_index,
            sas.raw_text AS segment_raw_text,
            sas.reading AS segment_reading
        FROM source_analysis_candidates AS sac
        JOIN source_analysis_segments AS sas
            ON sas.id = sac.source_analysis_segment_id
        WHERE {' AND '.join(conditions)}
        ORDER BY sas.segment_index ASC, sac.rank ASC, sac.id ASC
        """,
        params,
    ).fetchall()


def approve_source_analysis_candidates(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    candidate_ids: list[int] | None = None,
    mark_reviewed: bool = True,
) -> tuple[int, int]:
    source_row = connection.execute(
        "SELECT id FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    if source_row is None:
        raise SystemExit(f"Source #{source_id} does not exist.")

    candidates = pending_analysis_candidates(
        connection,
        source_id=source_id,
        candidate_ids=candidate_ids,
    )
    if not candidates:
        return (0, 0)

    promoted_terms = 0
    promoted_candidates = 0
    touched_term_ids: set[int] = set()
    for row in candidates:
        candidate_id = int(row["id"])
        if row["candidate_type"] == "kanji":
            connection.execute(
                """
                UPDATE source_analysis_candidates
                SET approval_status = 'approved', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (candidate_id,),
            )
            promoted_candidates += 1
            continue

        lemma = normalize_token(row["dictionary_form"]) or normalize_token(
            row["surface_form"]
        )
        surface_form = normalize_token(row["surface_form"]) or lemma
        reading = normalize_token(row["reading"])
        if not lemma or not surface_form:
            continue
        blacklist_reason = term_blacklist_reason(
            lemma,
            reading=reading or None,
            pos=row["pos"],
        )
        if blacklist_reason is not None:
            connection.execute(
                """
                UPDATE source_analysis_candidates
                SET approval_status = 'rejected', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (candidate_id,),
            )
            continue

        term_id = row["matched_term_id"]
        if term_id is None:
            term_id = upsert_term_record(
                connection,
                lemma=lemma,
                reading=reading or None,
                meaning_pt=None,
                pos=row["pos"],
                notes="approved_from_source_analysis",
            )
        else:
            term_id = int(term_id)

        term_form_id = ensure_term_form(
            connection,
            term_id=term_id,
            surface_form=surface_form,
            reading=reading or None,
        )
        source_segment_id = ensure_source_segment(
            connection,
            source_id=source_id,
            segment_text=str(row["segment_raw_text"]),
            reading=row["segment_reading"],
            notes="approved_from_source_analysis",
        )
        upsert_source_term_link(
            connection,
            source_id=source_id,
            term_id=term_id,
            term_form_id=term_form_id,
            surface_form=surface_form,
            count=1,
            confidence=row["confidence"],
            notes="approved_from_source_analysis",
            mode="add",
        )
        upsert_source_segment_term_link(
            connection,
            source_segment_id=source_segment_id,
            term_id=term_id,
            term_form_id=term_form_id,
            surface_form=surface_form,
            count=1,
            confidence=row["confidence"],
            notes="approved_from_source_analysis",
            mode="add",
        )
        connection.execute(
            """
            UPDATE source_analysis_candidates
            SET approval_status = 'approved', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (candidate_id,),
        )
        touched_term_ids.add(term_id)
        promoted_terms += 1
        promoted_candidates += 1

    for term_id in sorted(touched_term_ids):
        recalculate_term_stats(connection, term_id)
    if touched_term_ids:
        refresh_source_kanji(connection, source_id)
    if mark_reviewed:
        connection.execute(
            """
            UPDATE sources
            SET review_status = 'reviewed'
            WHERE id = ?
            """,
            (source_id,),
        )
    return (promoted_terms, promoted_candidates)


def get_term_by_key(
    connection: sqlite3.Connection,
    key: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM terms WHERE canonical_key = ?",
        (key,),
    ).fetchone()


def upsert_term_record(
    connection: sqlite3.Connection,
    *,
    lemma: str,
    reading: str | None,
    meaning_pt: str | None,
    pos: str | None,
    notes: str | None,
) -> int:
    lemma = normalize_token(lemma)
    reading = normalize_token(reading)
    meaning_pt = normalize_token(meaning_pt) or None
    ensure_term_not_blacklisted(lemma, reading=reading or None, pos=pos)
    key = canonical_key(lemma, reading)
    existing = get_term_by_key(connection, key)
    if meaning_pt is None and (
        existing is None or not normalize_token(existing["meaning_pt"])
    ):
        meaning_pt = auto_translate_term_meaning_pt(lemma, reading=reading or None)
    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO terms (
                canonical_key,
                lemma,
                reading,
                meaning_pt,
                pos,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, lemma, reading or None, meaning_pt, pos, notes),
        )
        return int(cursor.lastrowid)

    connection.execute(
        """
        UPDATE terms
        SET
            meaning_pt = COALESCE(?, meaning_pt),
            pos = COALESCE(?, pos),
            notes = COALESCE(?, notes),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (meaning_pt, pos, notes, existing["id"]),
    )
    return int(existing["id"])


def ensure_term_form(
    connection: sqlite3.Connection,
    *,
    term_id: int,
    surface_form: str,
    reading: str | None,
) -> int:
    surface_form = normalize_token(surface_form)
    reading = normalize_token(reading)
    key = form_key(surface_form, reading)
    row = connection.execute(
        """
        SELECT id
        FROM term_forms
        WHERE term_id = ? AND form_key = ?
        """,
        (term_id, key),
    ).fetchone()
    if row is not None:
        return int(row["id"])

    cursor = connection.execute(
        """
        INSERT INTO term_forms (
            term_id,
            form_key,
            surface_form,
            reading
        )
        VALUES (?, ?, ?, ?)
        """,
        (term_id, key, surface_form, reading or None),
    )
    return int(cursor.lastrowid)


def ensure_source_segment(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    segment_text: str,
    reading: str | None,
    notes: str | None,
) -> int:
    segment_text = normalize_token(segment_text)
    if not segment_text:
        raise SystemExit("Segment text cannot be empty.")

    reading = normalize_token(reading)
    key = segment_key(segment_text, reading)
    existing = connection.execute(
        """
        SELECT id
        FROM source_segments
        WHERE source_id = ? AND segment_key = ?
        """,
        (source_id, key),
    ).fetchone()
    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO source_segments (
                source_id,
                segment_key,
                segment_text,
                reading,
                notes
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, key, segment_text, reading or None, notes),
        )
        return int(cursor.lastrowid)

    connection.execute(
        """
        UPDATE source_segments
        SET
            reading = COALESCE(?, reading),
            notes = COALESCE(?, notes),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (reading or None, notes, existing["id"]),
    )
    return int(existing["id"])


def upsert_source_term_link(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    term_id: int,
    term_form_id: int,
    surface_form: str,
    count: int,
    confidence: float | None,
    notes: str | None,
    mode: str,
) -> None:
    existing = connection.execute(
        """
        SELECT id, count_in_source
        FROM source_terms
        WHERE source_id = ? AND term_id = ? AND surface_form = ?
        """,
        (source_id, term_id, surface_form),
    ).fetchone()

    if existing is None:
        connection.execute(
            """
            INSERT INTO source_terms (
                source_id,
                term_id,
                term_form_id,
                surface_form,
                count_in_source,
                confidence,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                term_id,
                term_form_id,
                surface_form,
                count,
                confidence,
                notes,
            ),
        )
        return

    new_count = count
    if mode == "add":
        new_count = int(existing["count_in_source"]) + count

    connection.execute(
        """
        UPDATE source_terms
        SET
            term_form_id = ?,
            count_in_source = ?,
            confidence = COALESCE(?, confidence),
            notes = COALESCE(?, notes),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            term_form_id,
            new_count,
            confidence,
            notes,
            existing["id"],
        ),
    )


def upsert_source_segment_term_link(
    connection: sqlite3.Connection,
    *,
    source_segment_id: int,
    term_id: int,
    term_form_id: int,
    surface_form: str,
    count: int,
    confidence: float | None,
    notes: str | None,
    mode: str,
) -> None:
    existing = connection.execute(
        """
        SELECT id, count_in_segment
        FROM source_segment_terms
        WHERE source_segment_id = ? AND term_id = ? AND surface_form = ?
        """,
        (source_segment_id, term_id, surface_form),
    ).fetchone()

    if existing is None:
        connection.execute(
            """
            INSERT INTO source_segment_terms (
                source_segment_id,
                term_id,
                term_form_id,
                surface_form,
                count_in_segment,
                confidence,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_segment_id,
                term_id,
                term_form_id,
                surface_form,
                count,
                confidence,
                notes,
            ),
        )
        return

    new_count = count
    if mode == "add":
        new_count = int(existing["count_in_segment"]) + count

    connection.execute(
        """
        UPDATE source_segment_terms
        SET
            term_form_id = ?,
            count_in_segment = ?,
            confidence = COALESCE(?, confidence),
            notes = COALESCE(?, notes),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            term_form_id,
            new_count,
            confidence,
            notes,
            existing["id"],
        ),
    )


def recalculate_term_stats(connection: sqlite3.Connection, term_id: int) -> None:
    row = connection.execute(
        """
        SELECT
            COALESCE(SUM(st.count_in_source), 0) AS total_occurrences,
            COUNT(DISTINCT st.source_id) AS distinct_sources,
            COUNT(DISTINCT st.surface_form) AS distinct_forms,
            MIN(COALESCE(s.captured_at, s.created_at)) AS first_seen_at,
            MAX(COALESCE(s.captured_at, s.created_at)) AS last_seen_at
        FROM source_terms AS st
        JOIN sources AS s
            ON s.id = st.source_id
        WHERE st.term_id = ?
        """,
        (term_id,),
    ).fetchone()

    total_occurrences = int(row["total_occurrences"])
    distinct_sources = int(row["distinct_sources"])
    distinct_forms = int(row["distinct_forms"])
    base_priority_score = (
        total_occurrences
        + (distinct_sources * 3)
        + (distinct_forms * 0.5)
    )
    meaning_row = connection.execute(
        "SELECT meaning_pt FROM terms WHERE id = ?",
        (term_id,),
    ).fetchone()
    priority_score = (
        0.0
        if is_proper_name_meaning(meaning_row["meaning_pt"] if meaning_row else None)
        else base_priority_score
    )

    connection.execute(
        """
        INSERT INTO term_stats (
            term_id,
            total_occurrences,
            distinct_sources,
            distinct_forms,
            first_seen_at,
            last_seen_at,
            priority_score,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(term_id) DO UPDATE SET
            total_occurrences = excluded.total_occurrences,
            distinct_sources = excluded.distinct_sources,
            distinct_forms = excluded.distinct_forms,
            first_seen_at = excluded.first_seen_at,
            last_seen_at = excluded.last_seen_at,
            priority_score = excluded.priority_score,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            term_id,
            total_occurrences,
            distinct_sources,
            distinct_forms,
            row["first_seen_at"],
            row["last_seen_at"],
            priority_score,
        ),
    )


def is_kanji_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def extract_kanji_counts(value: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not value:
        return counts
    for char in normalize_token(value):
        if is_kanji_char(char):
            counts[char] = counts.get(char, 0) + 1
    return counts


def extract_kanji_sequence(*values: str | None) -> list[str]:
    sequence: list[str] = []
    seen: set[str] = set()
    for value in values:
        for char in normalize_token(value):
            if not is_kanji_char(char) or char in seen:
                continue
            seen.add(char)
            sequence.append(char)
    return sequence


def preferred_kanji_text(surface_form: str | None, lemma: str | None) -> str:
    if extract_kanji_sequence(lemma):
        return normalize_token(lemma)
    return normalize_token(surface_form)


def upsert_kanji_record(connection: sqlite3.Connection, literal: str) -> int:
    literal = normalize_token(literal)
    row = connection.execute(
        "SELECT id FROM kanji WHERE literal = ?",
        (literal,),
    ).fetchone()
    if row is not None:
        connection.execute(
            """
            UPDATE kanji
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (row["id"],),
        )
        return int(row["id"])

    cursor = connection.execute(
        """
        INSERT INTO kanji (literal)
        VALUES (?)
        """,
        (literal,),
    )
    return int(cursor.lastrowid)


def recalculate_kanji_stats(connection: sqlite3.Connection, kanji_id: int) -> None:
    row = connection.execute(
        """
        SELECT
            COALESCE(SUM(sk.count_in_source), 0) AS total_occurrences,
            COUNT(DISTINCT sk.source_id) AS distinct_sources,
            MIN(COALESCE(s.captured_at, s.created_at)) AS first_seen_at,
            MAX(COALESCE(s.captured_at, s.created_at)) AS last_seen_at
        FROM source_kanji AS sk
        JOIN sources AS s
            ON s.id = sk.source_id
        WHERE sk.kanji_id = ?
        """,
        (kanji_id,),
    ).fetchone()

    total_occurrences = int(row["total_occurrences"])
    distinct_sources = int(row["distinct_sources"])
    related_terms = related_terms_for_kanji(connection, kanji_id, limit=12)
    meaning_pt = derive_kanji_meaning_pt(related_terms)
    distinct_terms = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT st.term_id) AS distinct_terms
            FROM kanji AS k
            JOIN source_kanji AS sk
                ON sk.kanji_id = k.id
            JOIN source_terms AS st
                ON st.source_id = sk.source_id
            JOIN terms AS t
                ON t.id = st.term_id
            WHERE
                k.id = ?
                AND (
                    instr(st.surface_form, k.literal) > 0
                    OR instr(t.lemma, k.literal) > 0
                )
            """,
            (kanji_id,),
        ).fetchone()["distinct_terms"]
    )
    priority_score = total_occurrences + (distinct_sources * 3) + distinct_terms

    connection.execute(
        """
        INSERT INTO kanji_stats (
            kanji_id,
            total_occurrences,
            distinct_sources,
            distinct_terms,
            first_seen_at,
            last_seen_at,
            priority_score,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(kanji_id) DO UPDATE SET
            total_occurrences = excluded.total_occurrences,
            distinct_sources = excluded.distinct_sources,
            distinct_terms = excluded.distinct_terms,
            first_seen_at = excluded.first_seen_at,
            last_seen_at = excluded.last_seen_at,
            priority_score = excluded.priority_score,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            kanji_id,
            total_occurrences,
            distinct_sources,
            distinct_terms,
            row["first_seen_at"],
            row["last_seen_at"],
            priority_score,
        ),
    )
    connection.execute(
        """
        UPDATE kanji
        SET
            meaning_pt = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (meaning_pt, kanji_id),
    )


def refresh_source_kanji(connection: sqlite3.Connection, source_id: int) -> None:
    previous_kanji_ids = {
        int(row["kanji_id"])
        for row in connection.execute(
            """
            SELECT kanji_id
            FROM source_kanji
            WHERE source_id = ?
            """,
            (source_id,),
        ).fetchall()
    }

    connection.execute(
        """
        DELETE FROM source_kanji
        WHERE source_id = ?
        """,
        (source_id,),
    )

    counts_by_kanji: dict[str, int] = {}
    source_rows = connection.execute(
        """
        SELECT
            st.surface_form,
            st.count_in_source,
            t.lemma
        FROM source_terms AS st
        JOIN terms AS t
            ON t.id = st.term_id
        WHERE st.source_id = ?
        """,
        (source_id,),
    ).fetchall()

    for row in source_rows:
        kanji_text = preferred_kanji_text(row["surface_form"], row["lemma"])
        for literal, count in extract_kanji_counts(kanji_text).items():
            counts_by_kanji[literal] = (
                counts_by_kanji.get(literal, 0)
                + (count * int(row["count_in_source"]))
            )

    current_kanji_ids: set[int] = set()
    for literal, count_in_source in counts_by_kanji.items():
        kanji_id = upsert_kanji_record(connection, literal)
        current_kanji_ids.add(kanji_id)
        connection.execute(
            """
            INSERT INTO source_kanji (
                source_id,
                kanji_id,
                count_in_source
            )
            VALUES (?, ?, ?)
            """,
            (source_id, kanji_id, count_in_source),
        )

    for kanji_id in sorted(previous_kanji_ids | current_kanji_ids):
        recalculate_kanji_stats(connection, kanji_id)


def source_ids_for_derived_refresh(connection: sqlite3.Connection) -> list[int]:
    return [
        int(row["source_id"])
        for row in connection.execute(
            """
            SELECT source_id
            FROM source_terms
            UNION
            SELECT source_id
            FROM source_kanji
            ORDER BY source_id
            """
        ).fetchall()
    ]


def refresh_all_source_kanji(connection: sqlite3.Connection) -> int:
    source_ids = source_ids_for_derived_refresh(connection)
    for source_id in source_ids:
        refresh_source_kanji(connection, source_id)
    return len(source_ids)


def upsert_term_command(args: argparse.Namespace) -> None:
    try:
        with db_connect(args.db) as connection:
            term_id = upsert_term_record(
                connection,
                lemma=args.lemma,
                reading=args.reading,
                meaning_pt=args.meaning_pt,
                pos=args.pos,
                notes=args.notes,
            )
    except BlacklistedTermError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Upserted term #{term_id}")


def link_term(args: argparse.Namespace) -> None:
    surface_form = normalize_token(args.surface_form or args.lemma)
    try:
        with db_connect(args.db) as connection:
            source = connection.execute(
                "SELECT id FROM sources WHERE id = ?",
                (args.source_id,),
            ).fetchone()
            if source is None:
                raise SystemExit(f"Source #{args.source_id} does not exist.")

            term_id = upsert_term_record(
                connection,
                lemma=args.lemma,
                reading=args.reading,
                meaning_pt=args.meaning_pt,
                pos=args.pos,
                notes=args.term_notes,
            )
            term_form_id = ensure_term_form(
                connection,
                term_id=term_id,
                surface_form=surface_form,
                reading=args.reading,
            )
            upsert_source_term_link(
                connection,
                source_id=args.source_id,
                term_id=term_id,
                term_form_id=term_form_id,
                surface_form=surface_form,
                count=args.count,
                confidence=args.confidence,
                notes=args.link_notes,
                mode=args.mode,
            )

            segment_text = normalize_token(args.segment_text)
            if segment_text:
                source_segment_id = ensure_source_segment(
                    connection,
                    source_id=args.source_id,
                    segment_text=segment_text,
                    reading=args.segment_reading,
                    notes=args.segment_notes,
                )
                upsert_source_segment_term_link(
                    connection,
                    source_segment_id=source_segment_id,
                    term_id=term_id,
                    term_form_id=term_form_id,
                    surface_form=surface_form,
                    count=args.count,
                    confidence=args.confidence,
                    notes=args.link_notes,
                    mode=args.mode,
                )
            elif args.segment_reading or args.segment_notes:
                raise SystemExit(
                    "--segment-reading and --segment-notes require --segment-text."
                )

            recalculate_term_stats(connection, term_id)
            refresh_source_kanji(connection, args.source_id)
    except BlacklistedTermError as exc:
        raise SystemExit(str(exc)) from exc

    segment_suffix = f", segment='{segment_text}'" if segment_text else ""
    print(
        "Linked term "
        f"#{term_id} to source #{args.source_id} "
        f"as '{surface_form}' (mode={args.mode}, count={args.count}{segment_suffix})"
    )


def rebuild_stats(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        term_ids = [
            int(row["term_id"])
            for row in connection.execute(
                "SELECT DISTINCT term_id FROM source_terms ORDER BY term_id"
            )
        ]
        for term_id in term_ids:
            recalculate_term_stats(connection, term_id)

        source_count = refresh_all_source_kanji(connection)

        kanji_count = int(
            connection.execute("SELECT COUNT(*) FROM kanji").fetchone()[0]
        )

    print(
        f"Rebuilt stats for {len(term_ids)} terms, "
        f"{kanji_count} kanji, {source_count} sources"
    )


def list_sources(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        raw_rows = connection.execute(
            """
            SELECT
                id,
                source_type,
                review_status,
                COALESCE(title, '(untitled)') AS title,
                COALESCE(scene, '-') AS scene,
                source_path,
                image_width,
                image_height,
                COALESCE(captured_at, created_at) AS seen_at
            FROM sources
            ORDER BY seen_at DESC, id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        rows = []
        for row in raw_rows:
            image_width, image_height = ensure_source_image_dimensions(
                connection,
                source_id=int(row["id"]),
                source_path=row["source_path"],
                image_width=row["image_width"],
                image_height=row["image_height"],
            )
            rows.append(
                {
                    "id": row["id"],
                    "source_type": row["source_type"],
                    "review_status": row["review_status"],
                    "title": row["title"],
                    "resolution": format_source_resolution(image_width, image_height) or "-",
                    "scene": row["scene"],
                    "seen_at": row["seen_at"],
                }
            )
    print_rows(
        rows,
        ("id", "source_type", "review_status", "title", "resolution", "scene", "seen_at"),
    )


def preview_text(value: str | None, size: int = 48) -> str:
    normalized = normalize_token(value)
    if len(normalized) <= size:
        return normalized or "-"
    return f"{normalized[: size - 1]}..."


def review_sources(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        rows = review_source_rows(
            connection,
            status=args.status,
            limit=args.limit,
        )
        normalized_rows = []
        for row in rows:
            image_width, image_height = ensure_source_image_dimensions(
                connection,
                source_id=int(row["id"]),
                source_path=row["source_path"],
                image_width=row["image_width"],
                image_height=row["image_height"],
            )
            normalized_rows.append(
                {
                    "id": row["id"],
                    "review_status": row["review_status"],
                    "ingest_mode": row["ingest_mode"],
                    "resolution": format_source_resolution(image_width, image_height) or "-",
                    "scene": row["scene"],
                    "seen_at": row["seen_at"],
                    "pending_candidates": row["pending_candidates"],
                    "raw_preview": preview_text(row["raw_text"], size=64),
                }
            )
    print_rows(
        normalized_rows,
        (
            "id",
            "review_status",
            "ingest_mode",
            "resolution",
            "scene",
            "seen_at",
            "pending_candidates",
            "raw_preview",
        ),
    )


def print_source_review_details(
    source_row: sqlite3.Row,
    segments: list[sqlite3.Row],
    candidates: list[sqlite3.Row],
) -> None:
    image_width = normalize_image_dimension(source_row["image_width"])
    image_height = normalize_image_dimension(source_row["image_height"])
    if image_width is None or image_height is None:
        image_width, image_height = read_source_image_dimensions(source_row["source_path"])
    print(f"Source #{source_row['id']}")
    print(f"  type={source_row['source_type']}")
    print(f"  ingest_mode={source_row['ingest_mode']}")
    print(f"  review_status={source_row['review_status']}")
    print(f"  language={source_row['language_code']}")
    print(f"  resolution={format_source_resolution(image_width, image_height) or '-'}")
    if normalize_token(source_row["game"]):
        print(f"  game={source_row['game']}")
    print(f"  scene={source_row['scene'] or '-'}")
    print(f"  source_path={source_row['source_path'] or '-'}")
    print(f"  capture_job_id={source_row['capture_job_id'] or '-'}")
    print(f"  captured_at={source_row['captured_at'] or source_row['created_at']}")
    print(f"  image_sha256={source_row['image_sha256'] or '-'}")
    print(f"  raw_text={source_row['raw_text'] or '-'}")
    if source_row["translated_text_en"]:
        print(f"  translated_text_en={source_row['translated_text_en']}")
        print(
            "  translated_text_en_meta="
            f"{source_row['translated_text_en_provider'] or '-'} "
            f"status={source_row['translated_text_en_status'] or '-'}"
        )
    elif source_row["translated_text_en_status"]:
        print(
            "  translated_text_en_meta="
            f"{source_row['translated_text_en_provider'] or '-'} "
            f"status={source_row['translated_text_en_status'] or '-'}"
        )
    if source_row["notes"]:
        print(f"  notes={source_row['notes']}")
    print("")
    if not segments:
        print("(no source analysis segments)")
    else:
        print("Segments:")
        for segment in segments:
            confidence = segment["confidence"]
            confidence_text = "-" if confidence is None else f"{confidence:.3f}"
            print(
                "  "
                f"[segment {segment['segment_index']}] "
                f"text={segment['raw_text']} "
                f"normalized={segment['normalized_text']} "
                f"reading={segment['reading'] or '-'} "
                f"confidence={confidence_text}"
            )
    print("")
    if not candidates:
        print("(no source analysis candidates)")
        return
    print("Candidates:")
    for candidate in candidates:
        confidence = candidate["confidence"]
        confidence_text = "-" if confidence is None else f"{confidence:.3f}"
        print(
            "  "
            f"#{candidate['id']} "
            f"segment={candidate['segment_index']} "
            f"type={candidate['candidate_type']} "
            f"status={candidate['approval_status']} "
            f"surface={candidate['surface_form']} "
            f"lemma={candidate['dictionary_form']} "
            f"reading={candidate['reading'] or '-'} "
            f"pos={candidate['pos'] or '-'} "
            f"rank={candidate['rank']} "
            f"confidence={confidence_text} "
            f"matched_term={candidate['matched_term_id'] or '-'} "
            f"matched_kanji={candidate['matched_kanji_id'] or '-'}"
        )


def review_source_command(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        if (
            args.raw_text is not None
            or args.scene is not None
            or args.status is not None
            or args.notes is not None
        ):
            update_source_record(
                connection,
                source_id=args.source_id,
                raw_text=args.raw_text,
                scene=args.scene,
                review_status=args.status,
                notes=args.notes,
            )
        source_row, segments, candidates = source_review_payload(
            connection,
            args.source_id,
        )
    print_source_review_details(source_row, segments, candidates)


def ignore_source_command(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        update_source_record(
            connection,
            source_id=args.source_id,
            review_status="ignored",
            notes=args.notes,
        )
        source_row, segments, candidates = source_review_payload(
            connection,
            args.source_id,
        )
    print_source_review_details(source_row, segments, candidates)


def approve_analysis_command(args: argparse.Namespace) -> None:
    candidate_ids = args.candidate_id or None
    with db_connect(args.db) as connection:
        promoted_terms, promoted_candidates = approve_source_analysis_candidates(
            connection,
            source_id=args.source_id,
            candidate_ids=candidate_ids,
        )
        source_row, segments, candidates = source_review_payload(
            connection,
            args.source_id,
        )
    print(
        f"Approved source analysis for source #{args.source_id}: "
        f"promoted_terms={promoted_terms}, promoted_candidates={promoted_candidates}"
    )
    print_source_review_details(source_row, segments, candidates)


def list_terms(args: argparse.Namespace) -> None:
    order_clause = {
        "priority": TERM_PRIORITY_ORDER_SQL.strip(),
        "recent": "last_seen_at DESC, id DESC",
        "lemma": "lemma ASC, id ASC",
    }[args.order]
    query = f"""
        SELECT
            id,
            lemma,
            COALESCE(reading, '-') AS reading,
            COALESCE(meaning_pt, '-') AS meaning_pt,
            total_occurrences,
            distinct_sources,
            priority_score
        FROM v_terms_overview
        ORDER BY {order_clause}
        LIMIT ?
    """
    with db_connect(args.db) as connection:
        rows = connection.execute(query, (args.limit,)).fetchall()
    rows = filter_blacklisted_term_rows(rows)
    print_rows(
        rows,
        (
            "id",
            "lemma",
            "reading",
            "meaning_pt",
            "total_occurrences",
            "distinct_sources",
            "priority_score",
        ),
    )


def list_kanji(args: argparse.Namespace) -> None:
    order_clause = {
        "priority": "priority_score DESC, id DESC",
        "recent": "last_seen_at DESC, id DESC",
        "literal": "literal ASC, id ASC",
    }[args.order]
    query = f"""
        SELECT
            id,
            literal,
            COALESCE(canonical_meanings, meaning_pt, '-') AS meaning,
            total_occurrences,
            distinct_sources,
            distinct_terms,
            priority_score
        FROM v_kanji_overview
        ORDER BY {order_clause}
        LIMIT ?
    """
    with db_connect(args.db) as connection:
        rows = connection.execute(query, (args.limit,)).fetchall()
    print_rows(
        rows,
        (
            "id",
            "literal",
            "meaning",
            "total_occurrences",
            "distinct_sources",
            "distinct_terms",
            "priority_score",
        ),
    )


def refresh_kanji_metadata_command(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        updated, failed = refresh_kanji_metadata(
            connection,
            force=args.force,
            kanji_id=args.kanji_id,
            limit=args.limit,
        )
    print(f"Refreshed kanji metadata: updated={updated}, failed={failed}")


def make_tag(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", value.strip().lower()).strip("_")
    return cleaned or None


def best_source_for_term(
    connection: sqlite3.Connection,
    term_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
            st.surface_form,
            st.count_in_source,
            s.id AS source_id,
            s.title,
            s.game,
            s.scene,
            s.raw_text
        FROM source_terms AS st
        JOIN sources AS s
            ON s.id = st.source_id
        WHERE st.term_id = ?
        ORDER BY
            st.count_in_source DESC,
            COALESCE(s.captured_at, s.created_at) DESC,
            st.id DESC
        LIMIT 1
        """,
        (term_id,),
    ).fetchone()


def build_source_label(source_row: sqlite3.Row | None) -> str:
    if source_row is None:
        return ""
    parts = [
        source_row["title"],
        source_row["game"],
        source_row["scene"],
    ]
    return " | ".join(part for part in parts if part)


def html_text(value: str | None) -> str:
    if not value:
        return ""
    return html.escape(value).replace("\n", "<br>")


def katakana_to_hiragana(value: str | None) -> str:
    if not value:
        return ""
    chars = []
    for char in value:
        codepoint = ord(char)
        if 0x30A1 <= codepoint <= 0x30F6:
            chars.append(chr(codepoint - 0x60))
        else:
            chars.append(char)
    return "".join(chars)


ROMAJI_DIGRAPHS = {
    "きゃ": "kya",
    "きゅ": "kyu",
    "きょ": "kyo",
    "ぎゃ": "gya",
    "ぎゅ": "gyu",
    "ぎょ": "gyo",
    "しゃ": "sha",
    "しゅ": "shu",
    "しょ": "sho",
    "じゃ": "ja",
    "じゅ": "ju",
    "じょ": "jo",
    "ちゃ": "cha",
    "ちゅ": "chu",
    "ちょ": "cho",
    "にゃ": "nya",
    "にゅ": "nyu",
    "にょ": "nyo",
    "ひゃ": "hya",
    "ひゅ": "hyu",
    "ひょ": "hyo",
    "びゃ": "bya",
    "びゅ": "byu",
    "びょ": "byo",
    "ぴゃ": "pya",
    "ぴゅ": "pyu",
    "ぴょ": "pyo",
    "みゃ": "mya",
    "みゅ": "myu",
    "みょ": "myo",
    "りゃ": "rya",
    "りゅ": "ryu",
    "りょ": "ryo",
    "ゔぁ": "va",
    "ゔぃ": "vi",
    "ゔぇ": "ve",
    "ゔぉ": "vo",
    "ゔゅ": "vyu",
    "てぃ": "ti",
    "でぃ": "di",
    "とぅ": "tu",
    "どぅ": "du",
    "ふぁ": "fa",
    "ふぃ": "fi",
    "ふぇ": "fe",
    "ふぉ": "fo",
    "しぇ": "she",
    "じぇ": "je",
    "ちぇ": "che",
    "つぁ": "tsa",
    "つぃ": "tsi",
    "つぇ": "tse",
    "つぉ": "tso",
    "うぃ": "wi",
    "うぇ": "we",
    "うぉ": "wo",
}

ROMAJI_MONOGRAPHS = {
    "あ": "a",
    "い": "i",
    "う": "u",
    "え": "e",
    "お": "o",
    "か": "ka",
    "き": "ki",
    "く": "ku",
    "け": "ke",
    "こ": "ko",
    "が": "ga",
    "ぎ": "gi",
    "ぐ": "gu",
    "げ": "ge",
    "ご": "go",
    "さ": "sa",
    "し": "shi",
    "す": "su",
    "せ": "se",
    "そ": "so",
    "ざ": "za",
    "じ": "ji",
    "ず": "zu",
    "ぜ": "ze",
    "ぞ": "zo",
    "た": "ta",
    "ち": "chi",
    "つ": "tsu",
    "て": "te",
    "と": "to",
    "だ": "da",
    "ぢ": "ji",
    "づ": "zu",
    "で": "de",
    "ど": "do",
    "な": "na",
    "に": "ni",
    "ぬ": "nu",
    "ね": "ne",
    "の": "no",
    "は": "ha",
    "ひ": "hi",
    "ふ": "fu",
    "へ": "he",
    "ほ": "ho",
    "ば": "ba",
    "び": "bi",
    "ぶ": "bu",
    "べ": "be",
    "ぼ": "bo",
    "ぱ": "pa",
    "ぴ": "pi",
    "ぷ": "pu",
    "ぺ": "pe",
    "ぽ": "po",
    "ま": "ma",
    "み": "mi",
    "む": "mu",
    "め": "me",
    "も": "mo",
    "や": "ya",
    "ゆ": "yu",
    "よ": "yo",
    "ら": "ra",
    "り": "ri",
    "る": "ru",
    "れ": "re",
    "ろ": "ro",
    "わ": "wa",
    "を": "o",
    "ん": "n",
    "ぁ": "a",
    "ぃ": "i",
    "ぅ": "u",
    "ぇ": "e",
    "ぉ": "o",
    "ゃ": "ya",
    "ゅ": "yu",
    "ょ": "yo",
    "ゎ": "wa",
    "ゔ": "vu",
}


def romanize_kana(value: str | None) -> str:
    text = katakana_to_hiragana(normalize_token(value))
    if not text:
        return ""

    parts: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]

        if char in {" ", "/", "(", ")", "[", "]", ",", ";", ":"}:
            parts.append(char)
            index += 1
            continue
        if char in {".", "・"}:
            index += 1
            continue
        if char == "ー":
            last_vowel = next(
                (letter for letter in reversed("".join(parts)) if letter in "aeiou"),
                "",
            )
            if last_vowel:
                parts.append(last_vowel)
            index += 1
            continue

        pair = text[index : index + 2]
        next_romaji = ROMAJI_DIGRAPHS.get(pair)
        consumed = 2 if next_romaji else 1
        if next_romaji is None:
            next_romaji = ROMAJI_MONOGRAPHS.get(char, char)

        if char == "っ":
            lookahead_pair = text[index + 1 : index + 3]
            lookahead = ROMAJI_DIGRAPHS.get(lookahead_pair)
            if lookahead is None and index + 1 < len(text):
                lookahead = ROMAJI_MONOGRAPHS.get(text[index + 1], "")
            if lookahead:
                parts.append(lookahead[0])
            index += 1
            continue

        if char == "ん":
            lookahead_pair = text[index + 1 : index + 3]
            lookahead = ROMAJI_DIGRAPHS.get(lookahead_pair)
            if lookahead is None and index + 1 < len(text):
                lookahead = ROMAJI_MONOGRAPHS.get(text[index + 1], "")
            if lookahead and lookahead[0] in "aiueoy":
                next_romaji = "n'"

        parts.append(next_romaji)
        index += consumed

    return normalize_token("".join(parts))


def build_card_front_text(
    main_text: str,
    reading: str | None = None,
    *,
    extra_lines: list[str] | None = None,
) -> str:
    reading_hiragana = katakana_to_hiragana(normalize_token(reading))
    front_lines = [
        '<div style="font-size: 2.5em; line-height: 1.1;">'
        f"{html_text(main_text)}"
        "</div>"
    ]
    if reading_hiragana:
        front_lines.append(
            '<div style="font-size: 0.8em; color: #666; margin-top: 0.2em;">'
            f"{html_text(reading_hiragana)}"
            "</div>"
        )
    for line in extra_lines or []:
        normalized = normalize_token(line)
        if not normalized:
            continue
        front_lines.append(
            '<div style="font-size: 0.55em; color: #666; margin-top: 0.2em; line-height: 1.25;">'
            f"{html_text(normalized)}"
            "</div>"
        )
    return "".join(front_lines)


def reading_with_romaji(label: str, reading: str | None) -> str | None:
    normalized = normalize_token(reading)
    if not normalized or normalized == "-":
        return None
    romaji = romanize_kana(normalized)
    if romaji:
        return f"{label}: {normalized} ({romaji})"
    return f"{label}: {normalized}"


def build_front_text(surface_form: str, reading: str | None) -> str:
    return build_card_front_text(surface_form, reading)


def build_kanji_back_text(surface_form: str, lemma: str) -> str:
    kanji_chars = extract_kanji_sequence(
        preferred_kanji_text(surface_form, lemma),
        surface_form,
    )
    if not kanji_chars:
        return ""
    return " ".join(kanji_chars)


def split_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [tag for tag in value.split() if tag]


def split_meaning_glosses(value: str | None) -> list[str]:
    normalized = normalize_token(value)
    if not normalized or normalized == "-":
        return []
    parts = re.split(r"\s*[;,/]\s*|\s+-\s+", normalized)
    glosses: list[str] = []
    seen: set[str] = set()
    for part in parts:
        gloss = normalize_token(part).strip(". ")
        if not gloss:
            continue
        key = gloss.lower()
        if key in seen:
            continue
        seen.add(key)
        glosses.append(gloss)
    return glosses


def kanji_gloss_candidates(
    related_terms: list[sqlite3.Row],
    *,
    limit: int = 6,
) -> list[str]:
    ranked: dict[str, tuple[float, int]] = {}
    for index, row in enumerate(related_terms):
        weight = float(row["priority_score"]) + float(row["total_occurrences"])
        for gloss in split_meaning_glosses(row["meaning_pt"]):
            previous = ranked.get(gloss)
            if previous is None or weight > previous[0]:
                ranked[gloss] = (weight, index)
    ordered = sorted(
        ranked.items(),
        key=lambda item: (-item[1][0], item[1][1], item[0].lower()),
    )
    return [gloss for gloss, _ in ordered[:limit]]


def derive_kanji_meaning_pt(related_terms: list[sqlite3.Row]) -> str | None:
    glosses = kanji_gloss_candidates(related_terms, limit=1)
    if not glosses:
        return None
    return glosses[0]


def join_unique_text(values: list[object], *, separator: str = "; ") -> str | None:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = normalize_token(str(value) if value is not None else "")
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    if not ordered:
        return None
    return separator.join(ordered)


def fetch_kanji_metadata(literal: str) -> dict[str, str | None]:
    request = Request(
        f"https://kanjiapi.dev/v1/kanji/{quote(literal)}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise SystemExit(f"Could not fetch kanji metadata for {literal}: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit(f"Unexpected kanji metadata payload for {literal}.")

    meanings = payload.get("meanings")
    on_readings = payload.get("on_readings")
    kun_readings = payload.get("kun_readings")
    name_readings = payload.get("name_readings")

    return {
        "canonical_meanings": join_unique_text(
            meanings if isinstance(meanings, list) else []
        ),
        "on_readings": join_unique_text(
            on_readings if isinstance(on_readings, list) else [],
            separator=" / ",
        ),
        "kun_readings": join_unique_text(
            kun_readings if isinstance(kun_readings, list) else [],
            separator=" / ",
        ),
        "name_readings": join_unique_text(
            name_readings if isinstance(name_readings, list) else [],
            separator=" / ",
        ),
    }


def refresh_kanji_metadata(
    connection: sqlite3.Connection,
    *,
    force: bool = False,
    kanji_id: int | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    filters = []
    params: list[object] = []
    if kanji_id is not None:
        filters.append("id = ?")
        params.append(kanji_id)
    if not force:
        filters.append(
            """
            (
                COALESCE(canonical_meanings, '') = ''
                OR COALESCE(on_readings, '') = ''
                OR COALESCE(kun_readings, '') = ''
            )
            """
        )

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(part.strip() for part in filters)

    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)

    rows = connection.execute(
        f"""
        SELECT id, literal
        FROM kanji
        {where_clause}
        ORDER BY id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()

    updated = 0
    failed = 0
    for row in rows:
        try:
            metadata = fetch_kanji_metadata(str(row["literal"]))
        except SystemExit:
            failed += 1
            continue
        connection.execute(
            """
            UPDATE kanji
            SET
                canonical_meanings = ?,
                on_readings = ?,
                kun_readings = ?,
                name_readings = ?,
                metadata_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                metadata["canonical_meanings"],
                metadata["on_readings"],
                metadata["kun_readings"],
                metadata["name_readings"],
                row["id"],
            ),
        )
        updated += 1

    return updated, failed


def kanji_related_term_text(row: sqlite3.Row) -> str:
    chunk = html_text(str(row["lemma"]))
    reading = normalize_token(row["reading"])
    meaning = normalize_token(row["meaning_pt"])
    if reading and reading != "-":
        chunk += f" [{html_text(reading)}]"
    if meaning and meaning != "-":
        chunk += f" ({html_text(meaning)})"
    return chunk


def row_identity_tag(row: sqlite3.Row) -> tuple[str, int]:
    keys = set(row.keys())
    if "term_id" in keys and row["term_id"] is not None:
        return ("term", int(row["term_id"]))
    if "kanji_id" in keys and row["kanji_id"] is not None:
        return ("kanji", int(row["kanji_id"]))
    raise SystemExit("Missing identity column for Anki note lookup.")


def build_flashcard_payload(
    term_row: sqlite3.Row,
    source_row: sqlite3.Row | None,
    *,
    deck_name: str,
    note_type: str,
    template_name: str,
) -> dict[str, str | int | None]:
    preferred_surface_form = (
        source_row["surface_form"]
        if source_row and source_row["surface_form"]
        else term_row["lemma"]
    )
    surface_form = (
        term_row["lemma"]
        if term_blacklist_reason(preferred_surface_form) is not None
        else preferred_surface_form
    )
    front_text = build_front_text(surface_form, term_row["reading"])
    back_parts = []
    reading = normalize_token(term_row["reading"])
    reading_romaji = romanize_kana(reading)
    kanji_text = preferred_kanji_text(surface_form, str(term_row["lemma"]))
    display_text = normalize_token(surface_form)
    if extract_kanji_sequence(kanji_text) and kanji_text != display_text:
        back_parts.append(f"Kanji: {html_text(kanji_text)}")

    if reading:
        back_parts.append(f"Reading: {html_text(reading)}")
    if reading_romaji:
        back_parts.append(f"Romaji: {html_text(reading_romaji)}")
    if term_row["meaning_pt"]:
        back_parts.append(f"Meaning: {html_text(term_row['meaning_pt'])}")

    tags = [
        "jp",
        "auto_generated",
        f"term_{term_row['id']}",
        f"template_{template_name}",
    ]
    pos_tag = make_tag(term_row["pos"])
    if pos_tag:
        tags.append(f"pos_{pos_tag}")
    if source_row is not None:
        tags.append(f"source_{source_row['source_id']}")

    return {
        "term_id": int(term_row["id"]),
        "source_id": int(source_row["source_id"]) if source_row else None,
        "template_name": template_name,
        "deck_name": deck_name,
        "note_type": note_type,
        "front_text": front_text,
        "back_text": "<br>".join(back_parts),
        "tags": " ".join(tags),
    }


def best_source_for_kanji(
    connection: sqlite3.Connection,
    kanji_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
            sk.source_id,
            sk.count_in_source,
            s.title,
            s.game,
            s.scene,
            s.raw_text,
            s.captured_at,
            s.created_at
        FROM source_kanji AS sk
        JOIN sources AS s
            ON s.id = sk.source_id
        WHERE sk.kanji_id = ?
        ORDER BY
            sk.count_in_source DESC,
            COALESCE(s.captured_at, s.created_at) DESC,
            sk.id DESC
        LIMIT 1
        """,
        (kanji_id,),
    ).fetchone()


def related_terms_for_kanji(
    connection: sqlite3.Connection,
    kanji_id: int,
    *,
    limit: int = 6,
) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT
            t.id,
            t.lemma,
            COALESCE(t.reading, '-') AS reading,
            COALESCE(t.meaning_pt, '-') AS meaning_pt,
            SUM(st.count_in_source) AS total_occurrences,
            COALESCE(ts.priority_score, 0) AS priority_score
        FROM kanji AS k
        JOIN source_kanji AS sk
            ON sk.kanji_id = k.id
        JOIN source_terms AS st
            ON st.source_id = sk.source_id
        JOIN terms AS t
            ON t.id = st.term_id
        LEFT JOIN term_stats AS ts
            ON ts.term_id = t.id
        WHERE
            k.id = ?
            AND (
                instr(st.surface_form, k.literal) > 0
                OR instr(t.lemma, k.literal) > 0
            )
        GROUP BY
            t.id,
            t.lemma,
            t.reading,
            t.meaning_pt,
            ts.priority_score
        ORDER BY
            SUM(st.count_in_source) DESC,
            COALESCE(ts.priority_score, 0) DESC,
            t.lemma COLLATE NOCASE ASC
        LIMIT ?
        """,
        (kanji_id, limit),
    ).fetchall()
    return filter_blacklisted_term_rows(rows)


def build_kanji_flashcard_payload(
    kanji_row: sqlite3.Row,
    related_terms: list[sqlite3.Row],
    source_row: sqlite3.Row | None,
    *,
    deck_name: str,
    note_type: str,
    template_name: str,
) -> dict[str, str | int | None]:
    canonical_meanings = normalize_token(kanji_row["canonical_meanings"])
    derived_meaning = normalize_token(kanji_row["meaning_pt"])
    on_readings = normalize_token(kanji_row["on_readings"])
    kun_readings = normalize_token(kanji_row["kun_readings"])
    front_text = build_card_front_text(
        str(kanji_row["literal"]),
        extra_lines=[
            line
            for line in (
                reading_with_romaji("On", on_readings),
                reading_with_romaji("Kun", kun_readings),
            )
            if line
        ],
    )

    back_parts = []
    if canonical_meanings:
        back_parts.append(f"Meaning: {html_text(canonical_meanings)}")
    elif derived_meaning and derived_meaning != "-":
        back_parts.append(f"Meaning: {html_text(derived_meaning)}")
    if not back_parts:
        if on_readings:
            back_parts.append(f"On: {html_text(on_readings)}")
        if kun_readings:
            back_parts.append(f"Kun: {html_text(kun_readings)}")

    tags = [
        "jp",
        "kanji",
        "auto_generated",
        f"kanji_{kanji_row['id']}",
        f"template_{template_name}",
    ]
    if source_row is not None:
        tags.append(f"source_{source_row['source_id']}")

    return {
        "kanji_id": int(kanji_row["id"]),
        "source_id": int(source_row["source_id"]) if source_row else None,
        "template_name": template_name,
        "deck_name": deck_name,
        "note_type": note_type,
        "front_text": front_text,
        "back_text": "<br>".join(back_parts),
        "tags": " ".join(tags),
    }


FLASHCARD_PRIORITY_ORDER_SQL = """
            COALESCE(ts.priority_score, 0) DESC,
            COALESCE(ts.last_seen_at, ts.first_seen_at) DESC,
            t.lemma COLLATE NOCASE ASC,
            f.id ASC
"""

KANJI_FLASHCARD_PRIORITY_ORDER_SQL = """
            COALESCE(ks.priority_score, 0) DESC,
            COALESCE(ks.last_seen_at, ks.first_seen_at) DESC,
            k.literal COLLATE NOCASE ASC,
            f.id ASC
"""


def flashcard_candidates(args: argparse.Namespace) -> tuple[str, list[object]]:
    filters = [
        "COALESCE(ts.total_occurrences, 0) > 0",
        "COALESCE(ts.priority_score, 0) >= ?",
    ]
    params: list[object] = [args.min_priority]

    if args.term_id is not None:
        filters.append("t.id = ?")
        params.append(args.term_id)

    if args.existing_only:
        filters.append(
            """
            EXISTS (
                SELECT 1
                FROM flashcards AS f
                WHERE f.term_id = t.id AND f.template_name = ?
            )
            """
        )
        params.append(args.template_name)

    if not args.refresh_existing:
        filters.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM flashcards AS f
                WHERE f.term_id = t.id AND f.template_name = ?
            )
            """
        )
        params.append(args.template_name)

    where_clause = " AND ".join(filter_text.strip() for filter_text in filters)
    limit_clause = ""
    if args.limit is not None:
        limit_clause = "LIMIT ?"
        params.append(args.limit)

    query = f"""
        SELECT
            t.id,
            t.lemma,
            t.reading,
            t.meaning_pt,
            t.pos,
            COALESCE(ts.total_occurrences, 0) AS total_occurrences,
            COALESCE(ts.distinct_sources, 0) AS distinct_sources,
            COALESCE(ts.priority_score, 0) AS priority_score
        FROM terms AS t
        LEFT JOIN term_stats AS ts
            ON ts.term_id = t.id
        WHERE {where_clause}
        ORDER BY
        """ + TERM_PRIORITY_ORDER_WITH_TS_SQL + """
        """ + limit_clause + """
    """
    return query, params


def generate_flashcards(args: argparse.Namespace) -> None:
    created = 0
    updated = 0
    with db_connect(args.db) as connection:
        created, updated = upsert_flashcards(connection, args)

    print(
        f"Generated flashcards: created={created}, "
        f"updated={updated}, template={args.template_name}"
    )


def upsert_flashcards(
    connection: sqlite3.Connection,
    args: argparse.Namespace,
) -> tuple[int, int]:
    created = 0
    updated = 0
    refresh_all_source_kanji(connection)
    query, params = flashcard_candidates(args)
    term_rows = connection.execute(query, params).fetchall()
    term_rows = filter_blacklisted_term_rows(term_rows)

    for term_row in term_rows:
        source_row = best_source_for_term(connection, int(term_row["id"]))
        payload = build_flashcard_payload(
            term_row,
            source_row,
            deck_name=args.deck_name,
            note_type=args.note_type,
            template_name=args.template_name,
        )
        existing = connection.execute(
            """
            SELECT id
            FROM flashcards
            WHERE term_id = ? AND template_name = ?
            """,
            (payload["term_id"], args.template_name),
        ).fetchone()

        conflicting_front = connection.execute(
            """
            SELECT id
            FROM flashcards
            WHERE
                term_id != ?
                AND template_name = ?
                AND deck_name = ?
                AND note_type = ?
                AND front_text = ?
            LIMIT 1
            """,
            (
                payload["term_id"],
                payload["template_name"],
                payload["deck_name"],
                payload["note_type"],
                payload["front_text"],
            ),
        ).fetchone()
        if conflicting_front is not None:
            continue

        if existing is None:
            connection.execute(
                """
                INSERT INTO flashcards (
                    term_id,
                    source_id,
                    template_name,
                    deck_name,
                    note_type,
                    front_text,
                    back_text,
                    tags
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["term_id"],
                    payload["source_id"],
                    payload["template_name"],
                    payload["deck_name"],
                    payload["note_type"],
                    payload["front_text"],
                    payload["back_text"],
                    payload["tags"],
                ),
            )
            created += 1
            continue

        if args.refresh_existing:
            connection.execute(
                """
                UPDATE flashcards
                SET
                    source_id = ?,
                    deck_name = ?,
                    note_type = ?,
                    front_text = ?,
                    back_text = ?,
                    tags = ?,
                    export_status = 'pending',
                    exported_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    payload["source_id"],
                    payload["deck_name"],
                    payload["note_type"],
                    payload["front_text"],
                    payload["back_text"],
                    payload["tags"],
                    existing["id"],
                ),
            )
            updated += 1

    return created, updated


def kanji_flashcard_candidates(args: argparse.Namespace) -> tuple[str, list[object]]:
    filters = [
        "COALESCE(ks.total_occurrences, 0) > 0",
        "COALESCE(ks.priority_score, 0) >= ?",
    ]
    params: list[object] = [args.min_priority]

    if args.kanji_id is not None:
        filters.append("k.id = ?")
        params.append(args.kanji_id)

    if args.existing_only:
        filters.append(
            """
            EXISTS (
                SELECT 1
                FROM kanji_flashcards AS f
                WHERE f.kanji_id = k.id AND f.template_name = ?
            )
            """
        )
        params.append(args.template_name)

    if not args.refresh_existing:
        filters.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM kanji_flashcards AS f
                WHERE f.kanji_id = k.id AND f.template_name = ?
            )
            """
        )
        params.append(args.template_name)

    where_clause = " AND ".join(filter_text.strip() for filter_text in filters)
    query = f"""
        SELECT
            k.id,
            k.literal,
            COALESCE(k.meaning_pt, '') AS meaning_pt,
            COALESCE(k.canonical_meanings, '') AS canonical_meanings,
            COALESCE(k.on_readings, '') AS on_readings,
            COALESCE(k.kun_readings, '') AS kun_readings,
            COALESCE(ks.total_occurrences, 0) AS total_occurrences,
            COALESCE(ks.distinct_sources, 0) AS distinct_sources,
            COALESCE(ks.distinct_terms, 0) AS distinct_terms,
            COALESCE(ks.priority_score, 0) AS priority_score
        FROM kanji AS k
        LEFT JOIN kanji_stats AS ks
            ON ks.kanji_id = k.id
        WHERE {where_clause}
        ORDER BY
            COALESCE(ks.priority_score, 0) DESC,
            COALESCE(ks.last_seen_at, ks.first_seen_at) DESC,
            k.literal COLLATE NOCASE ASC,
            k.id ASC
        LIMIT ?
    """
    params.append(args.limit)
    return query, params


def generate_kanji_flashcards(args: argparse.Namespace) -> None:
    created = 0
    updated = 0
    with db_connect(args.db) as connection:
        created, updated = upsert_kanji_flashcards(connection, args)

    print(
        f"Generated kanji flashcards: created={created}, "
        f"updated={updated}, template={args.template_name}"
    )


def upsert_kanji_flashcards(
    connection: sqlite3.Connection,
    args: argparse.Namespace,
) -> tuple[int, int]:
    created = 0
    updated = 0
    refresh_all_source_kanji(connection)
    refresh_kanji_metadata(
        connection,
        force=getattr(args, "refresh_metadata", False),
        kanji_id=getattr(args, "kanji_id", None),
        limit=getattr(args, "limit", None),
    )
    query, params = kanji_flashcard_candidates(args)
    kanji_rows = connection.execute(query, params).fetchall()

    for kanji_row in kanji_rows:
        source_row = best_source_for_kanji(connection, int(kanji_row["id"]))
        related_terms = related_terms_for_kanji(connection, int(kanji_row["id"]))
        payload = build_kanji_flashcard_payload(
            kanji_row,
            related_terms,
            source_row,
            deck_name=args.deck_name,
            note_type=args.note_type,
            template_name=args.template_name,
        )
        existing = connection.execute(
            """
            SELECT id
            FROM kanji_flashcards
            WHERE kanji_id = ? AND template_name = ?
            """,
            (payload["kanji_id"], args.template_name),
        ).fetchone()

        if existing is None:
            connection.execute(
                """
                INSERT INTO kanji_flashcards (
                    kanji_id,
                    source_id,
                    template_name,
                    deck_name,
                    note_type,
                    front_text,
                    back_text,
                    tags
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["kanji_id"],
                    payload["source_id"],
                    payload["template_name"],
                    payload["deck_name"],
                    payload["note_type"],
                    payload["front_text"],
                    payload["back_text"],
                    payload["tags"],
                ),
            )
            created += 1
            continue

        if args.refresh_existing:
            connection.execute(
                """
                UPDATE kanji_flashcards
                SET
                    source_id = ?,
                    deck_name = ?,
                    note_type = ?,
                    front_text = ?,
                    back_text = ?,
                    tags = ?,
                    export_status = 'pending',
                    exported_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    payload["source_id"],
                    payload["deck_name"],
                    payload["note_type"],
                    payload["front_text"],
                    payload["back_text"],
                    payload["tags"],
                    existing["id"],
                ),
            )
            updated += 1

    return created, updated


def list_flashcards(args: argparse.Namespace) -> None:
    filters = []
    params: list[object] = []
    if args.status != "all":
        filters.append("export_status = ?")
        params.append(args.status)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(filters)

    query = f"""
        SELECT
            f.id,
            t.lemma,
            printf('%.1f', COALESCE(ts.priority_score, 0)) AS priority_score,
            f.template_name,
            f.deck_name,
            f.export_status,
            f.front_text
        FROM flashcards AS f
        JOIN terms AS t
            ON t.id = f.term_id
        LEFT JOIN term_stats AS ts
            ON ts.term_id = f.term_id
        {where_clause}
        ORDER BY
{FLASHCARD_PRIORITY_ORDER_SQL}
        LIMIT ?
    """
    params.append(args.limit)
    with db_connect(args.db) as connection:
        rows = connection.execute(query, params).fetchall()
    rows = filter_blacklisted_term_rows(rows)
    print_rows(
        rows,
        (
            "id",
            "lemma",
            "priority_score",
            "template_name",
            "deck_name",
            "export_status",
            "front_text",
        ),
    )


def export_anki_tsv(args: argparse.Namespace) -> None:
    ensure_output_parent(args.output)
    filters = []
    params: list[object] = []
    if args.status != "all":
        filters.append("export_status = ?")
        params.append(args.status)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(filters)

    query = f"""
        SELECT
            f.id,
            t.lemma,
            f.front_text,
            f.back_text,
            f.tags
        FROM flashcards AS f
        JOIN terms AS t
            ON t.id = f.term_id
        LEFT JOIN term_stats AS ts
            ON ts.term_id = f.term_id
        {where_clause}
        ORDER BY
{FLASHCARD_PRIORITY_ORDER_SQL}
    """
    with db_connect(args.db) as connection:
        rows = connection.execute(query, params).fetchall()
        rows = filter_blacklisted_term_rows(rows)
        with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            if args.include_header:
                writer.writerow(("Front", "Back", "Tags"))
            for row in rows:
                writer.writerow((row["front_text"], row["back_text"], row["tags"] or ""))

        if args.mark_exported and rows:
            ids = [row["id"] for row in rows]
            placeholders = ", ".join("?" for _ in ids)
            connection.execute(
                f"""
                UPDATE flashcards
                SET
                    export_status = 'exported',
                    exported_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                ids,
            )

    print(f"Exported {len(rows)} flashcards to {args.output}")


def anki_connect_request(
    *,
    host: str,
    port: int,
    action: str,
    params: dict[str, object] | None = None,
) -> object:
    payload: dict[str, object] = {
        "action": action,
        "version": ANKI_CONNECT_VERSION,
    }
    if params is not None:
        payload["params"] = params

    request = Request(
        f"http://{host}:{port}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise SystemExit(
            f"Could not reach AnkiConnect at http://{host}:{port}. "
            f"Is Anki open? ({reason})"
        ) from exc

    if not isinstance(response_data, dict):
        raise SystemExit(f"Unexpected response from AnkiConnect action {action}.")
    if response_data.get("error") is not None:
        error_text = str(response_data["error"])
        if action == "addNote" and "duplicate" in error_text.lower():
            return None
        raise SystemExit(
            f"AnkiConnect action {action} failed: {error_text}"
        )
    if "result" not in response_data:
        raise SystemExit(f"Missing result in AnkiConnect action {action}.")
    return response_data["result"]


def anki_find_existing_note_id(
    row: sqlite3.Row,
    *,
    host: str,
    port: int,
) -> int | None:
    entity_type, entity_id = row_identity_tag(row)
    deck_name = str(row["deck_name"]).replace('"', '\\"')
    query = (
        f'deck:"{deck_name}" '
        f'tag:{entity_type}_{entity_id} '
        f'tag:template_{row["template_name"]}'
    )
    note_ids = anki_connect_request(
        host=host,
        port=port,
        action="findNotes",
        params={"query": query},
    )
    if not isinstance(note_ids, list):
        raise SystemExit("AnkiConnect returned an invalid findNotes payload.")
    if len(note_ids) > 1:
        raise SystemExit(
            "Found multiple Anki notes for the same flashcard tags. "
            f"Resolve duplicates for {entity_type} #{entity_id} in deck {row['deck_name']}."
        )
    if not note_ids:
        return None
    return int(note_ids[0])


def anki_note_info(
    note_id: int,
    *,
    host: str,
    port: int,
) -> dict[str, object] | None:
    result = anki_connect_request(
        host=host,
        port=port,
        action="notesInfo",
        params={"notes": [note_id]},
    )
    if not isinstance(result, list) or not result:
        return None
    note = result[0]
    if not isinstance(note, dict):
        return None
    return note


def anki_front_note_lookup(
    rows: Iterable[sqlite3.Row],
    *,
    host: str,
    port: int,
    front_field: str,
) -> dict[tuple[str, str, str], int]:
    by_scope: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (str(row["deck_name"]), str(row["note_type"]))
        by_scope.setdefault(key, []).append(row)

    lookup: dict[tuple[str, str, str], int] = {}
    for (deck_name, note_type), scoped_rows in by_scope.items():
        note_ids = anki_connect_request(
            host=host,
            port=port,
            action="findNotes",
            params={"query": f'deck:"{deck_name}" note:"{note_type}"'},
        )
        if not isinstance(note_ids, list) or not note_ids:
            continue

        wanted_fronts = {
            str(row["front_text"]).strip()
            for row in scoped_rows
            if row["front_text"] is not None
        }
        if not wanted_fronts:
            continue

        for start in range(0, len(note_ids), 100):
            note_batch = note_ids[start : start + 100]
            notes = anki_connect_request(
                host=host,
                port=port,
                action="notesInfo",
                params={"notes": note_batch},
            )
            if not isinstance(notes, list):
                raise SystemExit("AnkiConnect returned an invalid notesInfo payload.")

            for note in notes:
                if not isinstance(note, dict):
                    continue
                fields = note.get("fields")
                if not isinstance(fields, dict):
                    continue
                front = fields.get(front_field)
                if not isinstance(front, dict):
                    continue
                front_value = str(front.get("value") or "").strip()
                if front_value not in wanted_fronts:
                    continue
                key = (deck_name, note_type, front_value)
                if key not in lookup:
                    lookup[key] = int(note["noteId"])

    return lookup


def anki_sync_tags(
    note_id: int,
    desired_tags: list[str],
    *,
    existing_tags: list[str],
    host: str,
    port: int,
) -> None:
    current = set(existing_tags)
    desired = set(desired_tags)
    tags_to_add = sorted(desired - current)
    tags_to_remove = sorted(current - desired)

    if tags_to_add:
        anki_connect_request(
            host=host,
            port=port,
            action="addTags",
            params={"notes": [note_id], "tags": " ".join(tags_to_add)},
        )
    if tags_to_remove:
        anki_connect_request(
            host=host,
            port=port,
            action="removeTags",
            params={"notes": [note_id], "tags": " ".join(tags_to_remove)},
        )


def batch_values(values: list[int], size: int) -> Iterable[list[int]]:
    if size <= 0:
        raise ValueError("Batch size must be positive.")

    for index in range(0, len(values), size):
        yield values[index : index + size]


def anki_available_actions(*, host: str, port: int) -> set[str]:
    try:
        result = anki_connect_request(
            host=host,
            port=port,
            action="apiReflect",
            params={"scopes": ["actions"]},
        )
    except SystemExit:
        return set()

    if not isinstance(result, dict):
        return set()

    actions = result.get("actions")
    if not isinstance(actions, list):
        return set()

    return {str(action) for action in actions}


def anki_notes_info(
    note_ids: list[int],
    *,
    host: str,
    port: int,
) -> dict[int, dict[str, object]]:
    notes: dict[int, dict[str, object]] = {}
    for batch in batch_values(note_ids, 250):
        result = anki_connect_request(
            host=host,
            port=port,
            action="notesInfo",
            params={"notes": batch},
        )
        if not isinstance(result, list):
            raise SystemExit("AnkiConnect returned an invalid notesInfo payload.")

        for note in result:
            if not isinstance(note, dict):
                continue
            note_id = note.get("noteId")
            if note_id is None:
                continue
            notes[int(note_id)] = note

    return notes


def anki_cards_info(
    card_ids: list[int],
    *,
    host: str,
    port: int,
) -> dict[int, dict[str, object]]:
    cards: dict[int, dict[str, object]] = {}
    for batch in batch_values(card_ids, 250):
        result = anki_connect_request(
            host=host,
            port=port,
            action="cardsInfo",
            params={"cards": batch},
        )
        if not isinstance(result, list):
            raise SystemExit("AnkiConnect returned an invalid cardsInfo payload.")

        for card in result:
            if not isinstance(card, dict):
                continue
            card_id = card.get("cardId")
            if card_id is None:
                continue
            cards[int(card_id)] = card

    return cards


def is_anki_new_card(card: dict[str, object] | None) -> bool:
    if not isinstance(card, dict):
        return False

    card_type = card.get("type")
    try:
        return int(card_type) == 0
    except (TypeError, ValueError):
        return False


def parse_seen_sort_value(value: str | None) -> float:
    if not value:
        return 0.0
    normalized = str(value).strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def flashcard_surface_form(
    connection: sqlite3.Connection,
    *,
    term_id: int,
    source_id: int | None,
    fallback_lemma: str,
) -> str:
    if source_id is None:
        return fallback_lemma
    row = connection.execute(
        """
        SELECT COALESCE(surface_form, '') AS surface_form
        FROM source_terms
        WHERE source_id = ? AND term_id = ?
        ORDER BY
            count_in_source DESC,
            id DESC
        LIMIT 1
        """,
        (source_id, term_id),
    ).fetchone()
    surface_form = normalize_token(row["surface_form"]) if row is not None else ""
    return surface_form or fallback_lemma


def reprioritize_anki_learning_path(
    connection: sqlite3.Connection,
    *,
    host: str,
    port: int,
) -> int:
    term_rows = connection.execute(
        """
        SELECT
            f.id AS flashcard_id,
            f.term_id,
            f.source_id,
            f.anki_note_id,
            t.lemma,
            COALESCE(ts.priority_score, 0) AS priority_score,
            COALESCE(ts.last_seen_at, ts.first_seen_at) AS seen_at
        FROM flashcards AS f
        JOIN terms AS t
            ON t.id = f.term_id
        LEFT JOIN term_stats AS ts
            ON ts.term_id = f.term_id
        WHERE
            f.deck_name = ?
            AND f.anki_note_id IS NOT NULL
        """,
        (DEFAULT_JP_DECK_NAME,),
    ).fetchall()
    term_rows = filter_blacklisted_term_rows(term_rows)
    kanji_rows = connection.execute(
        """
        SELECT
            f.id AS flashcard_id,
            f.kanji_id,
            f.anki_note_id,
            k.literal,
            COALESCE(ks.priority_score, 0) AS priority_score,
            COALESCE(ks.last_seen_at, ks.first_seen_at) AS seen_at
        FROM kanji_flashcards AS f
        JOIN kanji AS k
            ON k.id = f.kanji_id
        LEFT JOIN kanji_stats AS ks
            ON ks.kanji_id = f.kanji_id
        WHERE
            f.deck_name = ?
            AND f.anki_note_id IS NOT NULL
        """,
        (DEFAULT_JP_KANJI_DECK_NAME,),
    ).fetchall()

    note_ids = list(
        dict.fromkeys(
            int(row["anki_note_id"])
            for row in [*term_rows, *kanji_rows]
            if row["anki_note_id"] is not None
        )
    )
    if not note_ids:
        return 0

    note_infos = anki_notes_info(note_ids, host=host, port=port)
    note_to_new_cards: dict[int, list[int]] = {}
    all_card_ids: list[int] = []
    for note_id in note_ids:
        note = note_infos.get(note_id)
        if note is None:
            continue
        cards = note.get("cards")
        if not isinstance(cards, list):
            continue
        all_card_ids.extend(int(card_id) for card_id in cards)

    if not all_card_ids:
        return 0

    card_infos = anki_cards_info(list(dict.fromkeys(all_card_ids)), host=host, port=port)
    for note_id in note_ids:
        note = note_infos.get(note_id)
        if note is None:
            continue
        cards = note.get("cards")
        if not isinstance(cards, list):
            continue
        new_cards = [
            int(card_id)
            for card_id in cards
            if is_anki_new_card(card_infos.get(int(card_id)))
        ]
        if new_cards:
            note_to_new_cards[note_id] = list(dict.fromkeys(new_cards))

    if not note_to_new_cards:
        return 0

    nodes: dict[tuple[str, int], dict[str, object]] = {}
    kanji_nodes_by_literal: dict[str, list[tuple[str, int]]] = {}

    for row in kanji_rows:
        note_id = int(row["anki_note_id"])
        card_ids = note_to_new_cards.get(note_id)
        if not card_ids:
            continue
        node_id = ("kanji", int(row["flashcard_id"]))
        literal = str(row["literal"])
        nodes[node_id] = {
            "card_ids": card_ids,
            "deps": set(),
            "sort_key": (
                -float(row["priority_score"]),
                -parse_seen_sort_value(row["seen_at"]),
                0,
                literal,
                int(row["flashcard_id"]),
            ),
        }
        kanji_nodes_by_literal.setdefault(literal, []).append(node_id)

    for row in term_rows:
        note_id = int(row["anki_note_id"])
        card_ids = note_to_new_cards.get(note_id)
        if not card_ids:
            continue
        term_id = int(row["term_id"])
        lemma = str(row["lemma"])
        surface_form = flashcard_surface_form(
            connection,
            term_id=term_id,
            source_id=int(row["source_id"]) if row["source_id"] is not None else None,
            fallback_lemma=lemma,
        )
        dependency_literals = extract_kanji_sequence(
            preferred_kanji_text(surface_form, lemma),
            surface_form,
        )
        deps: set[tuple[str, int]] = set()
        for literal in dependency_literals:
            deps.update(kanji_nodes_by_literal.get(literal, []))
        node_id = ("term", int(row["flashcard_id"]))
        nodes[node_id] = {
            "card_ids": card_ids,
            "deps": deps,
            "sort_key": (
                -float(row["priority_score"]),
                -parse_seen_sort_value(row["seen_at"]),
                1,
                lemma,
                int(row["flashcard_id"]),
            ),
        }

    if not nodes:
        return 0

    incoming_counts: dict[tuple[str, int], int] = {}
    dependents: dict[tuple[str, int], set[tuple[str, int]]] = {
        node_id: set() for node_id in nodes
    }
    for node_id, payload in nodes.items():
        deps = {
            dep
            for dep in payload["deps"]
            if dep in nodes
        }
        payload["deps"] = deps
        incoming_counts[node_id] = len(deps)
        for dep in deps:
            dependents[dep].add(node_id)

    ready = [node_id for node_id, count in incoming_counts.items() if count == 0]
    ordered_nodes: list[tuple[str, int]] = []
    while ready:
        ready.sort(key=lambda node_id: nodes[node_id]["sort_key"])
        current = ready.pop(0)
        ordered_nodes.append(current)
        for dependent in dependents[current]:
            incoming_counts[dependent] -= 1
            if incoming_counts[dependent] == 0:
                ready.append(dependent)

    if len(ordered_nodes) != len(nodes):
        ordered_nodes = sorted(nodes, key=lambda node_id: nodes[node_id]["sort_key"])

    ordered_card_ids: list[int] = []
    for node_id in ordered_nodes:
        ordered_card_ids.extend(int(card_id) for card_id in nodes[node_id]["card_ids"])

    if not ordered_card_ids:
        return 0

    result = anki_connect_request(
        host=host,
        port=port,
        action="repositionNewCards",
        params={
            "cards": ordered_card_ids,
            "startingPosition": 1,
            "stepSize": 1,
            "randomize": False,
            "shiftExisting": True,
        },
    )
    if isinstance(result, dict):
        count = result.get("count")
        if isinstance(count, int):
            return count
    if isinstance(result, int):
        return result
    return len(ordered_card_ids)


def reprioritize_anki_deck(
    connection: sqlite3.Connection,
    *,
    deck_name: str,
    host: str,
    port: int,
) -> int:
    rows = connection.execute(
        f"""
        SELECT
            f.anki_note_id,
            t.lemma
        FROM flashcards AS f
        JOIN terms AS t
            ON t.id = f.term_id
        LEFT JOIN term_stats AS ts
            ON ts.term_id = f.term_id
        WHERE f.deck_name = ? AND f.anki_note_id IS NOT NULL
        ORDER BY
{FLASHCARD_PRIORITY_ORDER_SQL}
        """,
        (deck_name,),
    ).fetchall()
    rows = filter_blacklisted_term_rows(rows)

    if not rows:
        return 0

    note_ids = list(dict.fromkeys(int(row["anki_note_id"]) for row in rows))
    note_infos = anki_notes_info(note_ids, host=host, port=port)

    ordered_card_ids: list[int] = []
    for note_id in note_ids:
        note = note_infos.get(note_id)
        if note is None:
            continue
        cards = note.get("cards")
        if not isinstance(cards, list):
            continue
        for card_id in cards:
            ordered_card_ids.append(int(card_id))

    if not ordered_card_ids:
        return 0

    ordered_card_ids = list(dict.fromkeys(ordered_card_ids))
    card_infos = anki_cards_info(ordered_card_ids, host=host, port=port)
    new_card_ids = [
        card_id
        for card_id in ordered_card_ids
        if is_anki_new_card(card_infos.get(card_id))
    ]

    if not new_card_ids:
        return 0

    result = anki_connect_request(
        host=host,
        port=port,
        action="repositionNewCards",
        params={
            "cards": new_card_ids,
            "startingPosition": 1,
            "stepSize": 1,
            "randomize": False,
            "shiftExisting": True,
        },
    )
    if isinstance(result, dict):
        count = result.get("count")
        if isinstance(count, int):
            return count
    if isinstance(result, int):
        return result
    return len(new_card_ids)


def reprioritize_anki_kanji_deck(
    connection: sqlite3.Connection,
    *,
    deck_name: str,
    host: str,
    port: int,
) -> int:
    rows = connection.execute(
        f"""
        SELECT
            f.anki_note_id
        FROM kanji_flashcards AS f
        JOIN kanji AS k
            ON k.id = f.kanji_id
        LEFT JOIN kanji_stats AS ks
            ON ks.kanji_id = f.kanji_id
        WHERE f.deck_name = ? AND f.anki_note_id IS NOT NULL
        ORDER BY
{KANJI_FLASHCARD_PRIORITY_ORDER_SQL}
        """,
        (deck_name,),
    ).fetchall()

    if not rows:
        return 0

    note_ids = list(dict.fromkeys(int(row["anki_note_id"]) for row in rows))
    note_infos = anki_notes_info(note_ids, host=host, port=port)

    ordered_card_ids: list[int] = []
    for note_id in note_ids:
        note = note_infos.get(note_id)
        if note is None:
            continue
        cards = note.get("cards")
        if not isinstance(cards, list):
            continue
        for card_id in cards:
            ordered_card_ids.append(int(card_id))

    if not ordered_card_ids:
        return 0

    ordered_card_ids = list(dict.fromkeys(ordered_card_ids))
    card_infos = anki_cards_info(ordered_card_ids, host=host, port=port)
    new_card_ids = [
        card_id
        for card_id in ordered_card_ids
        if is_anki_new_card(card_infos.get(card_id))
    ]

    if not new_card_ids:
        return 0

    result = anki_connect_request(
        host=host,
        port=port,
        action="repositionNewCards",
        params={
            "cards": new_card_ids,
            "startingPosition": 1,
            "stepSize": 1,
            "randomize": False,
            "shiftExisting": True,
        },
    )
    if isinstance(result, dict):
        count = result.get("count")
        if isinstance(count, int):
            return count
    if isinstance(result, int):
        return int(result)
    return len(new_card_ids)


def sync_anki(args: argparse.Namespace) -> None:
    anki_version = anki_connect_request(
        host=args.host,
        port=args.port,
        action="version",
    )
    if not isinstance(anki_version, int):
        raise SystemExit("AnkiConnect returned a non-numeric version.")

    filters = []
    params: list[object] = []
    if args.status != "all":
        filters.append("export_status = ?")
        params.append(args.status)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(filters)

    limit_clause = ""
    if args.limit is not None:
        limit_clause = "LIMIT ?"
        params.append(args.limit)

    with db_connect(args.db) as connection:
        rows = connection.execute(
            f"""
            SELECT
                f.id,
                f.term_id,
                t.lemma,
                f.template_name,
                f.deck_name,
                f.note_type,
                f.front_text,
                f.back_text,
                f.tags,
                f.anki_note_id
            FROM flashcards AS f
            JOIN terms AS t
                ON t.id = f.term_id
            LEFT JOIN term_stats AS ts
                ON ts.term_id = f.term_id
            {where_clause}
            ORDER BY
{FLASHCARD_PRIORITY_ORDER_SQL}
            {limit_clause}
            """,
            params,
        ).fetchall()
        rows = filter_blacklisted_term_rows(rows)

        sync_deck_names = sorted({str(row["deck_name"]) for row in rows})
        front_note_lookup = anki_front_note_lookup(
            rows,
            host=args.host,
            port=args.port,
            front_field=args.front_field,
        )
        reprioritize_deck_names = sorted(
            {
                str(row["deck_name"])
                for row in connection.execute(
                """
                SELECT DISTINCT
                    deck_name,
                    t.lemma
                FROM flashcards
                JOIN terms AS t
                    ON t.id = flashcards.term_id
                WHERE anki_note_id IS NOT NULL
                ORDER BY deck_name COLLATE NOCASE ASC
                """
            ).fetchall()
                if term_blacklist_reason(row["lemma"]) is None
            }
        )

        created = 0
        updated = 0
        skipped_duplicates = 0

        if rows:
            model_names = anki_connect_request(
                host=args.host,
                port=args.port,
                action="modelNames",
            )
            if not isinstance(model_names, list):
                raise SystemExit("AnkiConnect returned an invalid modelNames payload.")
            missing_models = sorted(
                {
                    str(row["note_type"])
                    for row in rows
                    if str(row["note_type"]) not in model_names
                }
            )
            if missing_models:
                raise SystemExit(
                    "Missing Anki note types: "
                    + ", ".join(missing_models)
                    + ". Create them in Anki or regenerate cards with a different note type."
                )

            for deck_name in sync_deck_names:
                anki_connect_request(
                    host=args.host,
                    port=args.port,
                    action="createDeck",
                    params={"deck": deck_name},
                )

        for row in rows:
            note_id = (
                int(row["anki_note_id"])
                if row["anki_note_id"] is not None
                else anki_find_existing_note_id(
                    row,
                    host=args.host,
                    port=args.port,
                )
            )
            if note_id is None:
                note_id = front_note_lookup.get(
                    (
                        str(row["deck_name"]),
                        str(row["note_type"]),
                        str(row["front_text"]).strip(),
                    )
                )
            desired_tags = split_tags(row["tags"])

            if note_id is None:
                note_id_result = anki_connect_request(
                    host=args.host,
                    port=args.port,
                    action="addNote",
                    params={
                        "note": {
                            "deckName": row["deck_name"],
                            "modelName": row["note_type"],
                            "fields": {
                                args.front_field: row["front_text"],
                                args.back_field: row["back_text"],
                            },
                            "tags": desired_tags,
                            "options": {"allowDuplicate": False},
                        }
                    },
                )
                if note_id_result is None:
                    skipped_duplicates += 1
                    continue
                note_id = int(note_id_result)
                created += 1
                front_note_lookup[
                    (
                        str(row["deck_name"]),
                        str(row["note_type"]),
                        str(row["front_text"]).strip(),
                    )
                ] = note_id
            else:
                note = anki_note_info(
                    note_id,
                    host=args.host,
                    port=args.port,
                )
                if note is None:
                    note_id_result = anki_connect_request(
                        host=args.host,
                        port=args.port,
                        action="addNote",
                        params={
                            "note": {
                                "deckName": row["deck_name"],
                                "modelName": row["note_type"],
                                "fields": {
                                    args.front_field: row["front_text"],
                                    args.back_field: row["back_text"],
                                },
                                "tags": desired_tags,
                                "options": {"allowDuplicate": False},
                            }
                        },
                    )
                    if note_id_result is None:
                        skipped_duplicates += 1
                        continue
                    note_id = int(note_id_result)
                    created += 1
                else:
                    note_id_result = anki_connect_request(
                        host=args.host,
                        port=args.port,
                        action="updateNoteFields",
                        params={
                            "note": {
                                "id": note_id,
                                "fields": {
                                    args.front_field: row["front_text"],
                                    args.back_field: row["back_text"],
                                },
                            }
                        }
                    )

                    cards = note.get("cards")
                    if isinstance(cards, list) and cards:
                        anki_connect_request(
                            host=args.host,
                            port=args.port,
                            action="changeDeck",
                            params={"cards": cards, "deck": row["deck_name"]},
                        )

                    note_tags = note.get("tags")
                    existing_tags = (
                        [str(tag) for tag in note_tags]
                        if isinstance(note_tags, list)
                        else []
                    )
                    anki_sync_tags(
                        note_id,
                        desired_tags,
                        existing_tags=existing_tags,
                        host=args.host,
                        port=args.port,
                    )
                    updated += 1

            connection.execute(
                """
                UPDATE flashcards
                SET
                    anki_note_id = ?,
                    export_status = 'exported',
                    exported_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (note_id, row["id"]),
            )
            connection.commit()

        actions = anki_available_actions(host=args.host, port=args.port)
        reprioritized_cards = 0
        reprioritized_decks = 0
        reprioritization_warning = None

        if "repositionNewCards" in actions:
            learning_path_cards = reprioritize_anki_learning_path(
                connection,
                host=args.host,
                port=args.port,
            )
            if learning_path_cards > 0:
                reprioritized_cards += learning_path_cards
                reprioritized_decks += 1

            for deck_name in reprioritize_deck_names:
                if deck_name == DEFAULT_JP_DECK_NAME:
                    continue
                count = reprioritize_anki_deck(
                    connection,
                    deck_name=deck_name,
                    host=args.host,
                    port=args.port,
                )
                if count > 0:
                    reprioritized_cards += count
                    reprioritized_decks += 1
        else:
            reprioritization_warning = (
                "Warning: current AnkiConnect does not expose repositionNewCards; "
                "existing Anki new-card order was left unchanged."
            )

    print(
        f"Synced {len(rows)} flashcards with Anki "
        f"(created={created}, updated={updated}, "
        f"skipped_duplicates={skipped_duplicates}, "
        f"reprioritized_new_cards={reprioritized_cards}, "
        f"reprioritized_decks={reprioritized_decks}, version={anki_version})."
    )
    if reprioritization_warning is not None:
        print(reprioritization_warning)


def sync_anki_words(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        generated_created, generated_updated = upsert_flashcards(connection, args)

    print(
        f"Prepared flashcards: created={generated_created}, "
        f"updated={generated_updated}, template={args.template_name}."
    )
    sync_anki(args)


def prune_blacklisted_terms(
    connection: sqlite3.Connection,
    *,
    delete_anki_notes: bool = False,
    host: str = DEFAULT_ANKI_CONNECT_HOST,
    port: int = DEFAULT_ANKI_CONNECT_PORT,
) -> dict[str, object]:
    entries = blacklisted_term_entries(connection)
    if not entries:
        return {
            "term_count": 0,
            "flashcard_count": 0,
            "anki_note_count": 0,
            "source_term_count": 0,
            "source_segment_term_count": 0,
            "term_form_count": 0,
            "sample": [],
        }

    term_ids = [int(row["id"]) for row, _ in entries]
    placeholders = ", ".join("?" for _ in term_ids)
    sample = [str(row["lemma"]) for row, _ in entries[:10]]
    flashcard_count = connection.execute(
        f"SELECT COUNT(*) FROM flashcards WHERE term_id IN ({placeholders})",
        term_ids,
    ).fetchone()[0]
    source_term_count = connection.execute(
        f"SELECT COUNT(*) FROM source_terms WHERE term_id IN ({placeholders})",
        term_ids,
    ).fetchone()[0]
    source_segment_term_count = connection.execute(
        f"SELECT COUNT(*) FROM source_segment_terms WHERE term_id IN ({placeholders})",
        term_ids,
    ).fetchone()[0]
    term_form_count = connection.execute(
        f"SELECT COUNT(*) FROM term_forms WHERE term_id IN ({placeholders})",
        term_ids,
    ).fetchone()[0]
    anki_note_ids = [
        int(row["anki_note_id"])
        for row in connection.execute(
            f"""
            SELECT DISTINCT anki_note_id
            FROM flashcards
            WHERE
                term_id IN ({placeholders})
                AND anki_note_id IS NOT NULL
            ORDER BY anki_note_id ASC
            """,
            term_ids,
        ).fetchall()
    ]
    if delete_anki_notes and anki_note_ids:
        anki_connect_request(
            host=host,
            port=port,
            action="deleteNotes",
            params={"notes": anki_note_ids},
        )
    connection.execute(
        f"DELETE FROM terms WHERE id IN ({placeholders})",
        term_ids,
    )
    return {
        "term_count": len(term_ids),
        "flashcard_count": flashcard_count,
        "anki_note_count": len(anki_note_ids),
        "source_term_count": source_term_count,
        "source_segment_term_count": source_segment_term_count,
        "term_form_count": term_form_count,
        "sample": sample,
    }


def prune_blacklisted_terms_command(args: argparse.Namespace) -> None:
    with db_connect(args.db) as connection:
        entries = blacklisted_term_entries(connection)
        if args.dry_run:
            print(
                "Blacklisted terms queued for removal: "
                f"{len(entries)}"
            )
            preview_rows = [
                {
                    "id": int(row["id"]),
                    "lemma": row["lemma"],
                    "reading": normalize_token(row["reading"]) or "-",
                    "pos": normalize_token(row["pos"]) or "-",
                    "reason": reason,
                    "priority_score": row["priority_score"],
                    "flashcard_count": row["flashcard_count"],
                }
                for row, reason in entries[: args.limit]
            ]
            print_rows(
                preview_rows,
                (
                    "id",
                    "lemma",
                    "reading",
                    "pos",
                    "reason",
                    "priority_score",
                    "flashcard_count",
                ),
            )
            return

        summary = prune_blacklisted_terms(
            connection,
            delete_anki_notes=args.delete_anki_notes,
            host=args.host,
            port=args.port,
        )
    sample = ", ".join(summary["sample"]) if summary["sample"] else "-"
    anki_note_label = (
        "anki_notes_deleted" if args.delete_anki_notes else "anki_notes_linked"
    )
    print(
        "Pruned blacklisted terms: "
        f"terms={summary['term_count']}, "
        f"flashcards={summary['flashcard_count']}, "
        f"source_links={summary['source_term_count']}, "
        f"segment_links={summary['source_segment_term_count']}, "
        f"term_forms={summary['term_form_count']}, "
        f"{anki_note_label}={summary['anki_note_count']}, "
        f"sample={sample}"
    )


def sync_anki_kanji(args: argparse.Namespace) -> None:
    anki_version = anki_connect_request(
        host=args.host,
        port=args.port,
        action="version",
    )
    if not isinstance(anki_version, int):
        raise SystemExit("AnkiConnect returned a non-numeric version.")

    filters = []
    params: list[object] = []
    if args.status != "all":
        filters.append("export_status = ?")
        params.append(args.status)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(filters)

    limit_clause = ""
    if args.limit is not None:
        limit_clause = "LIMIT ?"
        params.append(args.limit)

    with db_connect(args.db) as connection:
        generated_created = 0
        generated_updated = 0
        if not getattr(args, "skip_generate", False):
            generated_created, generated_updated = upsert_kanji_flashcards(
                connection,
                args,
            )

        rows = connection.execute(
            f"""
            SELECT
                f.id,
                f.kanji_id,
                f.template_name,
                f.deck_name,
                f.note_type,
                f.front_text,
                f.back_text,
                f.tags,
                f.anki_note_id
            FROM kanji_flashcards AS f
            JOIN kanji AS k
                ON k.id = f.kanji_id
            LEFT JOIN kanji_stats AS ks
                ON ks.kanji_id = f.kanji_id
            {where_clause}
            ORDER BY
{KANJI_FLASHCARD_PRIORITY_ORDER_SQL}
            {limit_clause}
            """,
            params,
        ).fetchall()

        sync_deck_names = sorted({str(row["deck_name"]) for row in rows})
        reprioritize_deck_names = [
            str(row["deck_name"])
            for row in connection.execute(
                """
                SELECT DISTINCT deck_name
                FROM kanji_flashcards
                WHERE anki_note_id IS NOT NULL
                ORDER BY deck_name COLLATE NOCASE ASC
                """
            ).fetchall()
        ]

        created = 0
        updated = 0

        if rows:
            model_names = anki_connect_request(
                host=args.host,
                port=args.port,
                action="modelNames",
            )
            if not isinstance(model_names, list):
                raise SystemExit("AnkiConnect returned an invalid modelNames payload.")
            missing_models = sorted(
                {
                    str(row["note_type"])
                    for row in rows
                    if str(row["note_type"]) not in {str(name) for name in model_names}
                }
            )
            if missing_models:
                raise SystemExit(
                    "Missing Anki note type(s) for kanji sync: "
                    + ", ".join(missing_models)
                )

            for deck_name in sync_deck_names:
                anki_connect_request(
                    host=args.host,
                    port=args.port,
                    action="createDeck",
                    params={"deck": deck_name},
                )

        for row in rows:
            note_id = (
                int(row["anki_note_id"])
                if row["anki_note_id"] is not None
                else anki_find_existing_note_id(
                    row,
                    host=args.host,
                    port=args.port,
                )
            )
            desired_tags = split_tags(row["tags"])

            if note_id is None:
                note_id_result = anki_connect_request(
                    host=args.host,
                    port=args.port,
                    action="addNote",
                    params={
                        "note": {
                            "deckName": row["deck_name"],
                            "modelName": row["note_type"],
                            "fields": {
                                args.front_field: row["front_text"],
                                args.back_field: row["back_text"],
                            },
                            "tags": desired_tags,
                            "options": {"allowDuplicate": False},
                        }
                    },
                )
                if note_id_result is None:
                    raise SystemExit(
                        f"Anki rejected kanji flashcard #{row['id']} as a duplicate."
                    )
                note_id = int(note_id_result)
                created += 1
            else:
                note = anki_note_info(note_id, host=args.host, port=args.port)
                if note is None:
                    note_id_result = anki_connect_request(
                        host=args.host,
                        port=args.port,
                        action="addNote",
                        params={
                            "note": {
                                "deckName": row["deck_name"],
                                "modelName": row["note_type"],
                                "fields": {
                                    args.front_field: row["front_text"],
                                    args.back_field: row["back_text"],
                                },
                                "tags": desired_tags,
                                "options": {"allowDuplicate": False},
                            }
                        },
                    )
                    if note_id_result is None:
                        raise SystemExit(
                            f"Anki rejected kanji flashcard #{row['id']} as a duplicate."
                        )
                    note_id = int(note_id_result)
                    created += 1
                else:
                    anki_connect_request(
                        host=args.host,
                        port=args.port,
                        action="updateNoteFields",
                        params={
                            "note": {
                                "id": note_id,
                                "fields": {
                                    args.front_field: row["front_text"],
                                    args.back_field: row["back_text"],
                                },
                            }
                        }
                    )

                    cards = note.get("cards")
                    if isinstance(cards, list) and cards:
                        anki_connect_request(
                            host=args.host,
                            port=args.port,
                            action="changeDeck",
                            params={"cards": cards, "deck": row["deck_name"]},
                        )

                    note_tags = note.get("tags")
                    existing_tags = (
                        [str(tag) for tag in note_tags]
                        if isinstance(note_tags, list)
                        else []
                    )
                    anki_sync_tags(
                        note_id,
                        desired_tags,
                        existing_tags=existing_tags,
                        host=args.host,
                        port=args.port,
                    )
                    updated += 1

            connection.execute(
                """
                UPDATE kanji_flashcards
                SET
                    anki_note_id = ?,
                    export_status = 'exported',
                    exported_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (note_id, row["id"]),
            )

        reprioritized_cards = 0
        reprioritized_decks = 0
        actions = anki_available_actions(host=args.host, port=args.port)
        reprioritization_warning = None

        if "repositionNewCards" in actions:
            learning_path_cards = reprioritize_anki_learning_path(
                connection,
                host=args.host,
                port=args.port,
            )
            if learning_path_cards > 0:
                reprioritized_cards += learning_path_cards
                reprioritized_decks += 1

            for deck_name in reprioritize_deck_names:
                if deck_name == DEFAULT_JP_KANJI_DECK_NAME:
                    continue
                count = reprioritize_anki_kanji_deck(
                    connection,
                    deck_name=deck_name,
                    host=args.host,
                    port=args.port,
                )
                if count > 0:
                    reprioritized_cards += count
                    reprioritized_decks += 1
        else:
            reprioritization_warning = (
                "Warning: current AnkiConnect does not expose repositionNewCards; "
                "existing Anki new-card order was left unchanged."
            )

    print(
        f"Prepared kanji flashcards: created={generated_created}, "
        f"updated={generated_updated}, template={args.template_name}."
    )
    print(
        f"Synced {len(rows)} kanji flashcards with Anki "
        f"(created={created}, updated={updated}, "
        f"reprioritized_new_cards={reprioritized_cards}, "
        f"reprioritized_decks={reprioritized_decks}, version={anki_version})."
    )
    if reprioritization_warning is not None:
        print(reprioritization_warning)


def browse_catalog(args: argparse.Namespace) -> None:
    from lang_jp_web import serve_catalog_ui

    serve_catalog_ui(
        db_path=args.db,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open_browser,
    )


def print_rows(rows: Iterable[sqlite3.Row], columns: tuple[str, ...]) -> None:
    rows = list(rows)
    if not rows:
        print("No rows.")
        return

    widths = {column: len(column) for column in columns}
    for row in rows:
        for column in columns:
            widths[column] = max(widths[column], len(str(row[column])))

    header = " | ".join(column.ljust(widths[column]) for column in columns)
    separator = "-+-".join("-" * widths[column] for column in columns)
    print(header)
    print(separator)
    for row in rows:
        print(" | ".join(str(row[column]).ljust(widths[column]) for column in columns))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="JP vocabulary catalog for sources, terms, and recurrence."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database (default: {DEFAULT_DB_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-db", help="Create the SQLite schema.")
    init_parser.set_defaults(func=lambda args: init_db(args.db))

    source_parser = subparsers.add_parser(
        "add-source",
        help="Register a raw source such as an image or text snippet.",
    )
    source_parser.add_argument(
        "--source-type",
        choices=("image", "text", "manual"),
        required=True,
    )
    source_parser.add_argument("--language-code", default="ja")
    source_parser.add_argument(
        "--ingest-mode",
        choices=("manual", "auto_capture", "image_import"),
        default="manual",
    )
    source_parser.add_argument(
        "--review-status",
        choices=("pending", "reviewed", "ignored"),
        default="reviewed",
    )
    source_parser.add_argument("--title")
    source_parser.add_argument("--game")
    source_parser.add_argument("--scene")
    source_parser.add_argument("--path")
    source_parser.add_argument("--raw-text")
    source_parser.add_argument("--notes")
    source_parser.add_argument("--image-sha256")
    source_parser.add_argument("--ocr-text-hash")
    source_parser.add_argument("--capture-job-id", type=int)
    source_parser.add_argument(
        "--captured-at",
        help="Optional ISO timestamp, for example 2026-03-22T10:15:00-03:00",
    )
    source_parser.set_defaults(func=add_source)

    term_parser = subparsers.add_parser(
        "upsert-term",
        help="Create or update a canonical term.",
    )
    term_parser.add_argument("--lemma", required=True)
    term_parser.add_argument("--reading")
    term_parser.add_argument("--meaning-pt")
    term_parser.add_argument("--pos")
    term_parser.add_argument("--notes")
    term_parser.set_defaults(func=upsert_term_command)

    link_parser = subparsers.add_parser(
        "link-term",
        help="Link a term to a source and refresh its aggregate stats.",
    )
    link_parser.add_argument("--source-id", type=int, required=True)
    link_parser.add_argument("--lemma", required=True)
    link_parser.add_argument("--surface-form")
    link_parser.add_argument("--reading")
    link_parser.add_argument("--meaning-pt")
    link_parser.add_argument("--pos")
    link_parser.add_argument("--count", type=int, default=1)
    link_parser.add_argument("--confidence", type=float)
    link_parser.add_argument("--term-notes")
    link_parser.add_argument("--link-notes")
    link_parser.add_argument(
        "--segment-text",
        help="Optional raw source segment shared by one or more linked terms.",
    )
    link_parser.add_argument("--segment-reading")
    link_parser.add_argument("--segment-notes")
    link_parser.add_argument(
        "--mode",
        choices=("set", "add"),
        default="set",
        help="set replaces the source count; add increments it.",
    )
    link_parser.set_defaults(func=link_term)

    rebuild_parser = subparsers.add_parser(
        "rebuild-stats",
        help="Recalculate term_stats from source_terms.",
    )
    rebuild_parser.set_defaults(func=rebuild_stats)

    prune_blacklisted_terms_parser = subparsers.add_parser(
        "prune-blacklisted-terms",
        help="Remove placeholder and single-kana terms plus their derived links/cards.",
    )
    prune_blacklisted_terms_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview blacklisted terms without deleting anything.",
    )
    prune_blacklisted_terms_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Preview row limit for --dry-run.",
    )
    prune_blacklisted_terms_parser.add_argument(
        "--delete-anki-notes",
        action="store_true",
        help="Also delete linked Anki notes for removed flashcards via AnkiConnect.",
    )
    prune_blacklisted_terms_parser.add_argument(
        "--host",
        default=DEFAULT_ANKI_CONNECT_HOST,
    )
    prune_blacklisted_terms_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_ANKI_CONNECT_PORT,
    )
    prune_blacklisted_terms_parser.set_defaults(func=prune_blacklisted_terms_command)

    list_sources_parser = subparsers.add_parser(
        "list-sources",
        help="Show recent sources.",
    )
    list_sources_parser.add_argument("--limit", type=int, default=20)
    list_sources_parser.set_defaults(func=list_sources)

    review_sources_parser = subparsers.add_parser(
        "review-sources",
        help="Show source drafts waiting for review.",
    )
    review_sources_parser.add_argument("--limit", type=int, default=20)
    review_sources_parser.add_argument(
        "--status",
        choices=("all", "pending", "reviewed", "ignored"),
        default="pending",
    )
    review_sources_parser.set_defaults(func=review_sources)

    review_source_parser = subparsers.add_parser(
        "review-source",
        help="Inspect or update a single source draft.",
    )
    review_source_parser.add_argument("--source-id", type=int, required=True)
    review_source_parser.add_argument("--raw-text")
    review_source_parser.add_argument("--scene")
    review_source_parser.add_argument(
        "--status",
        choices=("pending", "reviewed", "ignored"),
    )
    review_source_parser.add_argument("--notes")
    review_source_parser.set_defaults(func=review_source_command)

    ignore_source_parser = subparsers.add_parser(
        "ignore-source",
        help="Mark a source draft as ignored without deleting it.",
    )
    ignore_source_parser.add_argument("--source-id", type=int, required=True)
    ignore_source_parser.add_argument("--notes")
    ignore_source_parser.set_defaults(func=ignore_source_command)

    approve_analysis_parser = subparsers.add_parser(
        "approve-analysis",
        help="Promote pending source analysis candidates to official source links.",
    )
    approve_analysis_parser.add_argument("--source-id", type=int, required=True)
    approve_analysis_parser.add_argument(
        "--candidate-id",
        type=int,
        action="append",
        help="Optional candidate ids to approve. Default approves all pending candidates.",
    )
    approve_analysis_parser.set_defaults(func=approve_analysis_command)

    list_terms_parser = subparsers.add_parser(
        "list-terms",
        help="Show terms ordered by priority, recency, or lemma.",
    )
    list_terms_parser.add_argument("--limit", type=int, default=20)
    list_terms_parser.add_argument(
        "--order",
        choices=("priority", "recent", "lemma"),
        default="priority",
    )
    list_terms_parser.set_defaults(func=list_terms)

    list_kanji_parser = subparsers.add_parser(
        "list-kanji",
        help="Show kanji ordered by priority, recency, or literal.",
    )
    list_kanji_parser.add_argument("--limit", type=int, default=20)
    list_kanji_parser.add_argument(
        "--order",
        choices=("priority", "recent", "literal"),
        default="priority",
    )
    list_kanji_parser.set_defaults(func=list_kanji)

    refresh_kanji_metadata_parser = subparsers.add_parser(
        "refresh-kanji-metadata",
        help="Fetch canonical kanji readings and meanings from the external metadata API.",
    )
    refresh_kanji_metadata_parser.add_argument("--limit", type=int)
    refresh_kanji_metadata_parser.add_argument("--kanji-id", type=int)
    refresh_kanji_metadata_parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh even when metadata is already present.",
    )
    refresh_kanji_metadata_parser.set_defaults(func=refresh_kanji_metadata_command)

    generate_cards_parser = subparsers.add_parser(
        "generate-flashcards",
        help="Create or refresh flashcards from cataloged terms.",
    )
    generate_cards_parser.add_argument(
        "--template-name",
        default="recognition_basic",
    )
    generate_cards_parser.add_argument(
        "--deck-name",
        default=DEFAULT_JP_DECK_NAME,
    )
    generate_cards_parser.add_argument(
        "--note-type",
        default="Basic",
    )
    generate_cards_parser.add_argument("--min-priority", type=float, default=0.0)
    generate_cards_parser.add_argument("--limit", type=int, default=100)
    generate_cards_parser.add_argument("--term-id", type=int)
    generate_cards_parser.add_argument(
        "--existing-only",
        action="store_true",
        help="Only refresh cards that already exist for the same template.",
    )
    generate_cards_parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Update cards if they already exist for the same template.",
    )
    generate_cards_parser.set_defaults(func=generate_flashcards)

    generate_kanji_cards_parser = subparsers.add_parser(
        "generate-kanji-flashcards",
        help="Create or refresh flashcards from cataloged kanji.",
    )
    generate_kanji_cards_parser.add_argument(
        "--template-name",
        default="kanji_basic",
    )
    generate_kanji_cards_parser.add_argument(
        "--deck-name",
        default=DEFAULT_JP_KANJI_DECK_NAME,
    )
    generate_kanji_cards_parser.add_argument(
        "--note-type",
        default="Basic",
    )
    generate_kanji_cards_parser.add_argument("--min-priority", type=float, default=0.0)
    generate_kanji_cards_parser.add_argument("--limit", type=int, default=5000)
    generate_kanji_cards_parser.add_argument("--kanji-id", type=int)
    generate_kanji_cards_parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Refetch canonical kanji metadata even when the database already has it.",
    )
    generate_kanji_cards_parser.add_argument(
        "--existing-only",
        action="store_true",
        help="Only refresh kanji cards that already exist for the same template.",
    )
    generate_kanji_cards_parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Update kanji cards if they already exist for the same template.",
    )
    generate_kanji_cards_parser.set_defaults(func=generate_kanji_flashcards)

    list_cards_parser = subparsers.add_parser(
        "list-flashcards",
        help="Show generated flashcards.",
    )
    list_cards_parser.add_argument("--limit", type=int, default=50)
    list_cards_parser.add_argument(
        "--status",
        choices=("all", "pending", "exported"),
        default="all",
    )
    list_cards_parser.set_defaults(func=list_flashcards)

    export_cards_parser = subparsers.add_parser(
        "export-anki-tsv",
        help="Export flashcards to a TSV file for Anki import.",
    )
    export_cards_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ANKI_EXPORT_PATH,
        help=f"TSV export path (default: {DEFAULT_ANKI_EXPORT_PATH})",
    )
    export_cards_parser.add_argument(
        "--status",
        choices=("all", "pending", "exported"),
        default="pending",
    )
    export_cards_parser.add_argument(
        "--include-header",
        action="store_true",
        help="Write Front, Back, Tags as the first TSV row.",
    )
    export_cards_parser.add_argument(
        "--mark-exported",
        action="store_true",
        help="Mark exported cards as exported after writing the TSV.",
    )
    export_cards_parser.set_defaults(func=export_anki_tsv)

    sync_cards_parser = subparsers.add_parser(
        "sync-anki",
        help="Create or update flashcards directly in Anki via AnkiConnect.",
    )
    sync_cards_parser.add_argument(
        "--host",
        default=DEFAULT_ANKI_CONNECT_HOST,
    )
    sync_cards_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_ANKI_CONNECT_PORT,
    )
    sync_cards_parser.add_argument(
        "--status",
        choices=("all", "pending", "exported"),
        default="pending",
    )
    sync_cards_parser.add_argument("--limit", type=int)
    sync_cards_parser.add_argument("--front-field", default="Front")
    sync_cards_parser.add_argument("--back-field", default="Back")
    sync_cards_parser.set_defaults(func=sync_anki)

    sync_words_parser = subparsers.add_parser(
        "sync-anki-words",
        help="Generate or refresh vocabulary flashcards, then sync them to Anki via AnkiConnect.",
    )
    sync_words_parser.add_argument(
        "--host",
        default=DEFAULT_ANKI_CONNECT_HOST,
    )
    sync_words_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_ANKI_CONNECT_PORT,
    )
    sync_words_parser.add_argument(
        "--status",
        choices=("all", "pending", "exported"),
        default="pending",
    )
    sync_words_parser.add_argument("--limit", type=int)
    sync_words_parser.add_argument("--front-field", default="Front")
    sync_words_parser.add_argument("--back-field", default="Back")
    sync_words_parser.add_argument(
        "--template-name",
        default="recognition_basic",
    )
    sync_words_parser.add_argument(
        "--deck-name",
        default=DEFAULT_JP_DECK_NAME,
    )
    sync_words_parser.add_argument(
        "--note-type",
        default="Basic",
    )
    sync_words_parser.add_argument("--min-priority", type=float, default=0.0)
    sync_words_parser.add_argument("--term-id", type=int)
    sync_words_parser.add_argument(
        "--existing-only",
        action="store_true",
        help="Limit pre-sync regeneration to vocabulary cards that already exist for the same template.",
    )
    sync_words_parser.set_defaults(
        func=sync_anki_words,
        refresh_existing=True,
    )

    sync_kanji_cards_parser = subparsers.add_parser(
        "sync-anki-kanji",
        help="Create or update kanji flashcards directly in Anki via AnkiConnect.",
    )
    sync_kanji_cards_parser.add_argument(
        "--host",
        default=DEFAULT_ANKI_CONNECT_HOST,
    )
    sync_kanji_cards_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_ANKI_CONNECT_PORT,
    )
    sync_kanji_cards_parser.add_argument(
        "--status",
        choices=("all", "pending", "exported"),
        default="pending",
    )
    sync_kanji_cards_parser.add_argument("--limit", type=int, default=5000)
    sync_kanji_cards_parser.add_argument("--front-field", default="Front")
    sync_kanji_cards_parser.add_argument("--back-field", default="Back")
    sync_kanji_cards_parser.add_argument(
        "--template-name",
        default="kanji_basic",
    )
    sync_kanji_cards_parser.add_argument(
        "--deck-name",
        default=DEFAULT_JP_KANJI_DECK_NAME,
    )
    sync_kanji_cards_parser.add_argument(
        "--note-type",
        default="Basic",
    )
    sync_kanji_cards_parser.add_argument("--min-priority", type=float, default=0.0)
    sync_kanji_cards_parser.add_argument("--kanji-id", type=int)
    sync_kanji_cards_parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Refetch canonical kanji metadata even when the database already has it.",
    )
    sync_kanji_cards_parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="Sync only existing kanji_flashcards rows without regenerating payloads first.",
    )
    sync_kanji_cards_parser.add_argument(
        "--existing-only",
        action="store_true",
        help="Limit pre-sync regeneration to kanji cards that already exist for the same template.",
    )
    sync_kanji_cards_parser.set_defaults(
        func=sync_anki_kanji,
        refresh_existing=True,
    )

    browse_parser = subparsers.add_parser(
        "browse",
        help="Open a local browser UI for terms, sources, and flashcards.",
    )
    browse_parser.add_argument("--host", default="127.0.0.1")
    browse_parser.add_argument("--port", type=int, default=DEFAULT_UI_PORT)
    browse_parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Start the server without opening a browser tab automatically.",
    )
    browse_parser.set_defaults(func=browse_catalog)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
