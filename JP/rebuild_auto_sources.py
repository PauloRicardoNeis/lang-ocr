"""Rebuild auto-captured sources that are still pending review with the current pipeline.

For each source with ingest_mode='auto_capture' and review_status='pending', this re-runs
the capture worker's meikiocr OCR on the stored image, replaces raw_text and the source
analysis, drops only the source links that auto-promotion created, and promotes again the
way capture_worker.py does. Manual and reviewed sources are never selected.

Every source is one transaction. Finished sources go to a JSONL progress file next to the
database, so rerunning the same command after an interruption resumes where it stopped.
The live database is refused unless --allow-live-db is given.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

import capture_worker
import lang_jp
from capture_worker import (
    MeikiOcrAdapter,
    SudachiAnalyzer,
    auto_promote_source_terms,
    blocks_with_text,
    build_log_event,
    contains_japanese_text,
    store_source_analysis,
)
from lang_jp import (
    AUTO_PROMOTION_NOTES,
    DEFAULT_DB_PATH,
    ROOT,
    auto_translate_term_meaning_pt,
    clear_source_analysis,
    db_connect,
    google_gtx_translate_text,
    normalize_token,
    recalculate_term_stats,
    refresh_source_kanji,
    resolve_repo_local_path,
    stable_text_hash,
    term_blacklist_reason,
    update_source_record,
    upsert_source_translation,
)


REBUILD_POPULATION_SQL = """
    SELECT id, source_path, raw_text, ocr_text_hash
    FROM sources
    WHERE ingest_mode = 'auto_capture'
      AND review_status = 'pending'
      AND source_type = 'image'
    ORDER BY id ASC
"""
# Outcomes that count as done on resume, as long as the source text still matches.
FINISHED_STATUSES = frozenset({"rebuilt", "no_text", "missing_image"})
MEANING_COMMIT_INTERVAL = 50
# After this many empty meanings in a row, check whether the translator is down.
TRANSLATOR_PROBE_AFTER_EMPTY = 20
SQL_CHUNK_SIZE = 500


@dataclass(slots=True)
class SourceOutcome:
    source_id: int
    status: str
    ocr_text_hash: str | None = None
    ocr_seconds: float = 0.0
    seconds: float = 0.0
    old_text_len: int = 0
    new_text_len: int = 0
    removed_links: int = 0
    promoted_terms: int = 0
    segments: int = 0
    requeued_translations: int = 0
    error: str | None = None


def default_progress_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}.rebuild-progress.jsonl")


def default_log_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}.rebuild.log")


def is_live_db_path(db_path: Path, *, jp_root: Path) -> bool:
    """True for this checkout's catalog DB or the one that sits next to the captures we read."""
    target = Path(db_path)
    for live_path in (DEFAULT_DB_PATH, Path(jp_root) / "data" / DEFAULT_DB_PATH.name):
        if os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(live_path)):
            return True
        try:
            if target.exists() and live_path.exists() and os.path.samefile(target, live_path):
                return True
        except OSError:
            pass
    return False


def load_finished_sources(progress_path: Path) -> dict[int, str | None]:
    """Map source id -> text hash for sources whose last progress record is finished."""
    finished: dict[int, str | None] = {}
    if not progress_path.exists():
        return finished
    with progress_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # A line cut short by an interruption.
            source_id = int(record["source_id"])
            if record.get("status") in FINISHED_STATUSES:
                finished[source_id] = record.get("ocr_text_hash")
            else:
                finished.pop(source_id, None)
    return finished


def append_progress(progress_path: Path, outcome: SourceOutcome) -> None:
    record = {**asdict(outcome), "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    with progress_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def is_finished(row: sqlite3.Row, finished: Mapping[int, str | None]) -> bool:
    # A restored backup has the old text hash again, so its sources are selected again.
    source_id = int(row["id"])
    return source_id in finished and finished[source_id] == row["ocr_text_hash"]


def select_rebuild_sources(
    connection: sqlite3.Connection,
    *,
    finished: Mapping[int, str | None] | None = None,
    source_ids: Iterable[int] | None = None,
    sample: int | None = None,
    seed: int = 0,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Pending auto-captured image sources to rebuild, in id order.

    The sample is drawn from the whole population before finished sources are dropped,
    so a resumed sampled run keeps the same sample.
    """
    rows = connection.execute(REBUILD_POPULATION_SQL).fetchall()
    if source_ids is not None:
        wanted = {int(source_id) for source_id in source_ids}
        rows = [row for row in rows if int(row["id"]) in wanted]
    if sample is not None and sample < len(rows):
        rows = sorted(random.Random(seed).sample(rows, sample), key=lambda row: int(row["id"]))
    if finished:
        rows = [row for row in rows if not is_finished(row, finished)]
    if limit is not None:
        rows = rows[: max(0, limit)]
    return rows


def remove_auto_promoted_links(
    connection: sqlite3.Connection,
    source_id: int,
) -> tuple[list[int], int]:
    """Delete the links auto-promotion made for a source; keep any made by hand.

    Returns the term ids that lost a link (their stats need recalculating) and the
    number of source_terms rows removed.
    """
    term_ids = [
        int(row["term_id"])
        for row in connection.execute(
            """
            SELECT DISTINCT term_id
            FROM source_terms
            WHERE source_id = ? AND notes = ?
            ORDER BY term_id
            """,
            (source_id, AUTO_PROMOTION_NOTES),
        ).fetchall()
    ]
    connection.execute(
        """
        DELETE FROM source_segment_terms
        WHERE notes = ?
          AND source_segment_id IN (
              SELECT id FROM source_segments WHERE source_id = ?
          )
        """,
        (AUTO_PROMOTION_NOTES, source_id),
    )
    removed_links = connection.execute(
        "DELETE FROM source_terms WHERE source_id = ? AND notes = ?",
        (source_id, AUTO_PROMOTION_NOTES),
    ).rowcount
    connection.execute(
        """
        DELETE FROM source_segments
        WHERE source_id = ?
          AND notes = ?
          AND NOT EXISTS (
              SELECT 1
              FROM source_segment_terms AS sst
              WHERE sst.source_segment_id = source_segments.id
          )
        """,
        (source_id, AUTO_PROMOTION_NOTES),
    )
    return term_ids, int(removed_links)


def requeue_source_translations(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    raw_text: str,
) -> int:
    """Queue stale source translations again, as the worker does for a new source."""
    text_hash = stable_text_hash(raw_text)
    requeued = 0
    for row in connection.execute(
        """
        SELECT target_language, provider, source_text_hash
        FROM source_translations
        WHERE source_id = ?
        """,
        (source_id,),
    ).fetchall():
        if row["source_text_hash"] == text_hash:
            continue
        upsert_source_translation(
            connection,
            source_id=source_id,
            source_text_hash=text_hash,
            target_language=row["target_language"],
            provider=row["provider"],
            status="pending" if raw_text else "failed",
            translated_text=None,
            error_message=None if raw_text else "Source has no text to translate.",
        )
        requeued += 1
    return requeued


def finish_transaction(connection: sqlite3.Connection, *, dry_run: bool) -> None:
    if dry_run:
        connection.rollback()
    else:
        connection.commit()


def unlink_source(connection: sqlite3.Connection, source_id: int) -> int:
    """Drop a source's auto-promoted links and update the stats they fed."""
    old_term_ids, removed_links = remove_auto_promoted_links(connection, source_id)
    refresh_source_kanji(connection, source_id)
    for term_id in old_term_ids:
        recalculate_term_stats(connection, term_id)
    return removed_links


def rebuild_source(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    ocr: MeikiOcrAdapter,
    analyzer: SudachiAnalyzer,
    jp_root: Path,
    dry_run: bool,
    requeue_translations: bool,
    ignore_empty: bool,
    ignore_missing_images: bool = False,
) -> tuple[SourceOutcome, str]:
    """Rebuild one source in its own transaction. Returns the outcome and the new raw_text."""
    source_id = int(row["id"])
    old_raw_text = str(row["raw_text"] or "")
    started_at = time.perf_counter()
    image_path = resolve_repo_local_path(row["source_path"], root=jp_root)
    if image_path is None or not image_path.is_file():
        removed_links = 0
        if ignore_missing_images:
            # Without the image the old OCR text cannot be checked, so stop counting it.
            try:
                update_source_record(connection, source_id=source_id, review_status="ignored")
                removed_links = unlink_source(connection, source_id)
            except BaseException:
                connection.rollback()
                raise
            finish_transaction(connection, dry_run=dry_run)
        return (
            SourceOutcome(
                source_id=source_id,
                status="missing_image",
                ocr_text_hash=row["ocr_text_hash"],
                old_text_len=len(old_raw_text),
                new_text_len=len(old_raw_text),
                removed_links=removed_links,
                error=f"Image not found: {image_path}",
            ),
            old_raw_text,
        )

    blocks = ocr.extract_blocks(
        image_path,
        progress_label=build_log_event("rebuild", source_id=source_id, step="ocr"),
    )
    ocr_seconds = time.perf_counter() - started_at
    blocks, raw_text = blocks_with_text(blocks)
    if not any(contains_japanese_text(block.text) for block in blocks):
        # The worker rejects such a capture; here the source is kept but left empty.
        blocks, raw_text = [], ""

    try:
        old_term_ids, removed_links = remove_auto_promoted_links(connection, source_id)
        clear_source_analysis(connection, source_id)
        update_source_record(
            connection,
            source_id=source_id,
            raw_text=raw_text,
            review_status="ignored" if ignore_empty and not raw_text else None,
        )
        segments, _ = store_source_analysis(
            connection,
            source_id=source_id,
            blocks=blocks,
            analyzer=analyzer,
        )
        promoted_terms, _ = auto_promote_source_terms(connection, source_id)
        if promoted_terms == 0:
            # Promotion only refreshes source_kanji when it linked something.
            refresh_source_kanji(connection, source_id)
        for term_id in old_term_ids:
            recalculate_term_stats(connection, term_id)
        requeued = (
            requeue_source_translations(connection, source_id=source_id, raw_text=raw_text)
            if requeue_translations
            else 0
        )
    except BaseException:
        connection.rollback()
        raise
    finish_transaction(connection, dry_run=dry_run)

    return (
        SourceOutcome(
            source_id=source_id,
            status="rebuilt" if raw_text else "no_text",
            ocr_text_hash=stable_text_hash(raw_text),
            ocr_seconds=round(ocr_seconds, 3),
            seconds=round(time.perf_counter() - started_at, 3),
            old_text_len=len(old_raw_text),
            new_text_len=len(raw_text),
            removed_links=removed_links,
            promoted_terms=promoted_terms,
            segments=segments,
            requeued_translations=requeued,
        ),
        raw_text,
    )


def chunked(values: list[int], size: int = SQL_CHUNK_SIZE) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def terms_missing_meanings(
    connection: sqlite3.Connection,
    source_ids: list[int],
) -> list[sqlite3.Row]:
    rows: dict[int, sqlite3.Row] = {}
    for chunk in chunked(sorted(source_ids)):
        placeholders = ", ".join("?" for _ in chunk)
        for row in connection.execute(
            f"""
            SELECT DISTINCT t.id, t.lemma, t.reading, t.pos
            FROM terms AS t
            JOIN source_terms AS st
                ON st.term_id = t.id
            WHERE st.source_id IN ({placeholders})
              AND COALESCE(TRIM(t.meaning_pt), '') = ''
            """,
            chunk,
        ).fetchall():
            rows[int(row["id"])] = row
    return [rows[term_id] for term_id in sorted(rows)]


def translator_reachable() -> bool:
    """Probe the EN->PT translator the JMdict path uses, bypassing its cache."""
    try:
        google_gtx_translate_text.__wrapped__(
            "water",
            source_language="en",
            target_language="pt",
        )
    except (OSError, TimeoutError, ValueError):
        return False
    return True


def fill_missing_term_meanings(
    connection: sqlite3.Connection,
    source_ids: list[int],
) -> tuple[int, int, bool]:
    """Fill empty meanings of terms linked to rebuilt sources through the JMdict path.

    Stops early, returning complete=False, when a run of empty results turns out to be
    the translator failing (for example HTTP 429), so a rerun can finish the job later.
    """
    attempted = 0
    filled = 0
    empty_in_a_row = 0
    for row in terms_missing_meanings(connection, source_ids):
        if term_blacklist_reason(row["lemma"], reading=row["reading"], pos=row["pos"]):
            continue
        attempted += 1
        meaning_pt = normalize_token(
            auto_translate_term_meaning_pt(row["lemma"], reading=row["reading"], pos=row["pos"])
        )
        if not meaning_pt:
            empty_in_a_row += 1
            if empty_in_a_row >= TRANSLATOR_PROBE_AFTER_EMPTY:
                if not translator_reachable():
                    connection.commit()
                    return attempted, filled, False
                empty_in_a_row = 0
            continue
        empty_in_a_row = 0
        connection.execute(
            """
            UPDATE terms
            SET meaning_pt = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND COALESCE(TRIM(meaning_pt), '') = ''
            """,
            (meaning_pt, row["id"]),
        )
        # A proper-name meaning zeroes the priority score.
        recalculate_term_stats(connection, int(row["id"]))
        filled += 1
        if filled % MEANING_COMMIT_INTERVAL == 0:
            connection.commit()
    connection.commit()
    return attempted, filled, True


def preview_lines(value: str, *, limit: int = 3) -> str:
    lines = [line for line in value.splitlines() if line.strip()]
    shown = " / ".join(lines[:limit])
    if len(lines) > limit:
        shown += f" / ... (+{len(lines) - limit} lines)"
    return shown or "(empty)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-run OCR, analysis and auto-promotion for auto-captured sources that are "
            "still pending review. Run it on a copy of the catalog first."
        )
    )
    parser.add_argument("--db", type=Path, required=True, help="Catalog database to rebuild.")
    parser.add_argument(
        "--allow-live-db",
        action="store_true",
        help="Allow the live catalog database (back it up first).",
    )
    parser.add_argument(
        "--jp-root",
        type=Path,
        default=ROOT,
        help=f"JP folder that stored image paths are relative to (default: {ROOT})",
    )
    parser.add_argument(
        "--jmdict-db",
        type=Path,
        help="JMdict database for term meanings (default: <jp-root>/data/jmdict.sqlite3).",
    )
    parser.add_argument(
        "--progress-file",
        type=Path,
        help="JSONL progress file (default: <db>.rebuild-progress.jsonl).",
    )
    parser.add_argument("--log-file", type=Path, help="OCR log (default: <db>.rebuild.log).")
    parser.add_argument("--fresh", action="store_true", help="Ignore the progress file.")
    parser.add_argument("--limit", type=int, help="Rebuild at most this many sources in this run.")
    parser.add_argument(
        "--sample",
        type=int,
        help="Rebuild a random sample of this size from the pending population.",
    )
    parser.add_argument("--seed", type=int, default=0, help="Seed for --sample (default: 0).")
    parser.add_argument(
        "--source-id",
        type=int,
        action="append",
        help="Only rebuild this source (repeatable); it must still be pending and auto-captured.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run OCR and the rebuild, print old and new text, then roll every source back.",
    )
    parser.add_argument(
        "--ignore-empty",
        action="store_true",
        help="Mark sources with no Japanese text after OCR as ignored.",
    )
    parser.add_argument(
        "--ignore-missing-images",
        action="store_true",
        help="Mark sources whose image is gone as ignored and drop their auto-promoted links.",
    )
    parser.add_argument(
        "--requeue-translations",
        action="store_true",
        help=(
            "Queue source translations of the old text again for the worker. Off by "
            "default: one Google request per line of every rebuilt source."
        ),
    )
    parser.add_argument(
        "--skip-meanings",
        action="store_true",
        help=(
            "Skip filling missing term meanings. New terms then stay without a meaning "
            "until a later run without this flag."
        ),
    )
    parser.add_argument(
        "--skip-rebuild-stats",
        action="store_true",
        help="Skip the final rebuild-stats pass (run it once after the last batch).",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    db_path: Path = args.db
    jp_root: Path = args.jp_root
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")
    if is_live_db_path(db_path, jp_root=jp_root) and not args.allow_live_db:
        raise SystemExit(
            f"Refusing to open the live database {db_path}. Run on a copy, or back it up "
            "and pass --allow-live-db."
        )
    jmdict_db = args.jmdict_db or jp_root / "data" / "jmdict.sqlite3"
    if not jmdict_db.is_file() and not (args.dry_run or args.skip_meanings):
        raise SystemExit(f"JMdict database not found: {jmdict_db}")
    lang_jp.JMDICT_DB_PATH = jmdict_db
    # Promotion would look up each new term's meaning as it creates it; the meaning step
    # after the loop does that once per term instead and stops if the translator is down.
    auto_translate_term_meanings = lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS
    lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = False
    progress_path = args.progress_file or default_progress_path(db_path)
    capture_worker.DEFAULT_LOG_PATH = args.log_file or default_log_path(db_path)

    finished = {} if args.fresh else load_finished_sources(progress_path)
    connection = db_connect(db_path)
    try:
        rows = select_rebuild_sources(
            connection,
            finished=finished,
            source_ids=args.source_id,
            sample=args.sample,
            seed=args.seed,
            limit=args.limit,
        )
        print(
            f"Selected {len(rows)} sources to rebuild "
            f"({len(finished)} already finished in {progress_path.name})."
            + (" Dry run: nothing is saved." if args.dry_run else ""),
            flush=True,
        )
        outcomes: list[SourceOutcome] = []
        if rows:
            ocr = MeikiOcrAdapter()
            analyzer = SudachiAnalyzer()
            run_started_at = time.perf_counter()
            for index, row in enumerate(rows, start=1):
                try:
                    outcome, new_raw_text = rebuild_source(
                        connection,
                        row,
                        ocr=ocr,
                        analyzer=analyzer,
                        jp_root=jp_root,
                        dry_run=args.dry_run,
                        requeue_translations=args.requeue_translations,
                        ignore_empty=args.ignore_empty,
                        ignore_missing_images=args.ignore_missing_images,
                    )
                except KeyboardInterrupt:
                    print("Interrupted. Run the same command again to resume.", flush=True)
                    raise
                except Exception as exc:
                    connection.rollback()
                    outcome = SourceOutcome(source_id=int(row["id"]), status="failed", error=str(exc))
                    new_raw_text = ""
                outcomes.append(outcome)
                if not args.dry_run:
                    append_progress(progress_path, outcome)
                print(
                    f"[{index}/{len(rows)}] source #{outcome.source_id} {outcome.status} "
                    f"ocr={outcome.ocr_seconds:.2f}s total={outcome.seconds:.2f}s "
                    f"links -{outcome.removed_links} +{outcome.promoted_terms} "
                    f"text {outcome.old_text_len}->{outcome.new_text_len}"
                    + (f" error={outcome.error}" if outcome.error else ""),
                    flush=True,
                )
                if args.dry_run:
                    print(f"    old: {preview_lines(str(row['raw_text'] or ''))}")
                    print(f"    new: {preview_lines(new_raw_text)}", flush=True)
            elapsed = time.perf_counter() - run_started_at
            ocr_runs = [outcome for outcome in outcomes if outcome.ocr_seconds > 0]
            counts: dict[str, int] = {}
            for outcome in outcomes:
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
            print(
                f"Rebuilt {len(outcomes)} sources in {elapsed:.1f}s: "
                + ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
            )
            if ocr_runs:
                print(
                    f"Per image: OCR {sum(o.ocr_seconds for o in ocr_runs) / len(ocr_runs):.2f}s, "
                    f"total {sum(o.seconds for o in ocr_runs) / len(ocr_runs):.2f}s"
                )

        if args.dry_run:
            return
        if not args.skip_meanings:
            population_ids = {
                int(row["id"]) for row in connection.execute(REBUILD_POPULATION_SQL)
            }
            rebuilt_ids = [
                source_id
                for source_id in load_finished_sources(progress_path)
                if source_id in population_ids
            ]
            started_at = time.perf_counter()
            lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = auto_translate_term_meanings
            attempted, filled, complete = fill_missing_term_meanings(connection, rebuilt_ids)
            print(
                f"Filled {filled} of {attempted} missing term meanings "
                f"in {time.perf_counter() - started_at:.1f}s"
            )
            if not complete:
                print(
                    "Stopped filling meanings: the translator is not answering. Fill the rest "
                    "later with --limit 0 --skip-rebuild-stats."
                )
    finally:
        connection.close()

    if not args.skip_rebuild_stats:
        started_at = time.perf_counter()
        lang_jp.rebuild_stats(argparse.Namespace(db=db_path))
        print(f"rebuild-stats took {time.perf_counter() - started_at:.1f}s")


if __name__ == "__main__":
    main()
