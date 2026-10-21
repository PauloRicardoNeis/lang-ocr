from __future__ import annotations

import argparse
import json
import random
import sqlite3
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from lang_jp import DEFAULT_DB_PATH, resolve_repo_local_path


@dataclass(slots=True)
class SampleResult:
    bucket_index: int
    source_id: int
    ingest_mode: str
    review_status: str
    created_at: str | None
    captured_at: str | None
    stored_path: str
    resolved_path: str
    exists: bool
    suffix: str
    detected_format: str
    compressed: bool
    path_rewritten: bool


REPORT_WIDTH = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sample image sources across the JP catalog and report whether the "
            "underlying files are compressed (for example, stored as WebP)."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=30,
        help="Number of distributed buckets to sample (default: 30)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for reproducible bucket picks.",
    )
    parser.add_argument(
        "--include-manual",
        action="store_true",
        help="Also include manual image sources with stored paths.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path for a JSON report with the sampled rows.",
    )
    return parser.parse_args()


def detect_image_format(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return "other"


def fetch_candidate_rows(
    connection: sqlite3.Connection,
    *,
    include_manual: bool,
) -> list[sqlite3.Row]:
    query = """
        SELECT
            id,
            ingest_mode,
            review_status,
            created_at,
            captured_at,
            source_path
        FROM sources
        WHERE source_type = 'image'
          AND source_path IS NOT NULL
          AND TRIM(source_path) <> ''
    """
    if not include_manual:
        query += " AND ingest_mode = 'auto_capture'"
    query += " ORDER BY id ASC"
    return connection.execute(query).fetchall()


def pick_bucket_sample(
    rows: list[sqlite3.Row],
    *,
    sample_size: int,
    rng: random.Random,
) -> list[sqlite3.Row]:
    if sample_size <= 0:
        raise ValueError("--sample-size must be greater than zero.")
    if not rows:
        return []

    sampled_rows: list[sqlite3.Row] = []
    total = len(rows)
    for bucket_index in range(sample_size):
        start = (bucket_index * total) // sample_size
        end = ((bucket_index + 1) * total) // sample_size
        bucket = rows[start:end]
        if not bucket:
            continue
        sampled_rows.append(bucket[rng.randrange(len(bucket))])
    return sampled_rows


def build_sample_results(
    sampled_rows: list[sqlite3.Row],
) -> list[SampleResult]:
    results: list[SampleResult] = []
    for bucket_offset, row in enumerate(sampled_rows, start=1):
        stored_path = str(row["source_path"]).strip()
        resolved = resolve_repo_local_path(stored_path)
        resolved_path = resolved if resolved is not None else Path(stored_path)
        detected_format = detect_image_format(resolved_path)
        results.append(
            SampleResult(
                bucket_index=bucket_offset,
                source_id=int(row["id"]),
                ingest_mode=str(row["ingest_mode"]),
                review_status=str(row["review_status"]),
                created_at=str(row["created_at"]) if row["created_at"] is not None else None,
                captured_at=str(row["captured_at"]) if row["captured_at"] is not None else None,
                stored_path=stored_path,
                resolved_path=str(resolved_path),
                exists=resolved_path.exists(),
                suffix=resolved_path.suffix.lower(),
                detected_format=detected_format,
                compressed=detected_format == "webp",
                path_rewritten=stored_path != str(resolved_path),
            )
        )
    return results


def box_border(char: str = "-") -> str:
    return f"+{char * (REPORT_WIDTH - 2)}+"


def box_line(text: str = "") -> str:
    inner_width = REPORT_WIDTH - 4
    normalized = text[:inner_width]
    return f"| {normalized:<{inner_width}} |"


def print_box(title: str, rows: list[tuple[str, str]]) -> None:
    label_width = max((len(label) for label, _ in rows), default=0)
    value_width = max(10, REPORT_WIDTH - 6 - label_width - 4)

    print(box_border("="))
    for line in textwrap.wrap(title, width=REPORT_WIDTH - 4) or [""]:
        print(box_line(line))
    print(box_border("="))

    for label, value in rows:
        wrapped = textwrap.wrap(str(value), width=value_width) or [""]
        for index, line in enumerate(wrapped):
            current_label = label if index == 0 else ""
            if index == 0:
                formatted = f"{current_label:<{label_width}} : {line}"
            else:
                formatted = f"{current_label:<{label_width}}   {line}"
            print(box_line(formatted))
    print(box_border())


def summarize_results(
    results: list[SampleResult],
) -> tuple[dict[str, int], int, int, int]:
    format_counts: dict[str, int] = {}
    compressed_count = 0
    missing_count = 0
    rewritten_count = 0
    for result in results:
        format_counts[result.detected_format] = format_counts.get(result.detected_format, 0) + 1
        if result.compressed:
            compressed_count += 1
        if not result.exists:
            missing_count += 1
        if result.path_rewritten:
            rewritten_count += 1
    return format_counts, compressed_count, missing_count, rewritten_count


def status_label(result: SampleResult) -> str:
    if result.compressed:
        return "WEBP"
    return result.detected_format.upper()


def print_report(
    results: list[SampleResult],
    *,
    total_candidates: int,
    sample_size: int,
    seed: int,
) -> None:
    format_counts, compressed_count, missing_count, rewritten_count = summarize_results(results)

    if format_counts:
        ordered_formats = ", ".join(
            f"{name}={count}" for name, count in sorted(format_counts.items())
        )
    else:
        ordered_formats = "-"

    print_box(
        "JP Source Compression Sample",
        [
            ("Candidates", str(total_candidates)),
            ("Buckets", str(sample_size)),
            ("Sampled", str(len(results))),
            ("Seed", str(seed)),
            ("Compressed", f"{compressed_count}/{len(results)}"),
            ("Missing", str(missing_count)),
            ("Rewritten", str(rewritten_count)),
            ("Formats", ordered_formats),
        ],
    )
    print("")

    for result in results:
        status = f"{status_label(result)}{' (compressed)' if result.compressed else ''}"
        file_state = "present" if result.exists else "missing"
        rewrite_state = "yes" if result.path_rewritten else "no"
        rows = [
            ("Status", status),
            ("File", file_state),
            ("Mode", result.ingest_mode),
            ("Review", result.review_status),
            ("Created", result.created_at or "-"),
            ("Captured", result.captured_at or "-"),
            ("Suffix", result.suffix or "-"),
            ("Resolved", result.resolved_path),
            ("Path Fixup", rewrite_state),
        ]
        if result.path_rewritten:
            rows.append(("Stored", result.stored_path))
        print_box(
            f"Bucket {result.bucket_index:02d} / Source #{result.source_id}",
            rows,
        )
        print("")

    print_box(
        "Quick Summary",
        [
            (
                f"[{result.bucket_index:02d}]",
                (
                    f"source#{result.source_id} "
                    f"{status_label(result)} "
                    f"{'present' if result.exists else 'missing'} "
                    f"mode={result.ingest_mode} "
                    f"review={result.review_status}"
                ),
            )
            for result in results
        ],
    )
    print("")

    print_box(
        "Final Summary",
        [
            (
                "Result",
                (
                    f"{compressed_count} compressed, "
                    f"{len(results) - compressed_count - missing_count} not compressed, "
                    f"{missing_count} missing"
                ),
            ),
            ("Coverage", f"{len(results)} sampled across {sample_size} distributed buckets"),
            ("Candidates", str(total_candidates)),
            ("Formats", ordered_formats),
            ("Path Fixups", str(rewritten_count)),
            ("Seed", str(seed)),
        ],
    )


def main() -> None:
    args = parse_args()
    seed = args.seed if args.seed is not None else int(datetime.now().strftime("%Y%m%d"))
    rng = random.Random(seed)

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        candidate_rows = fetch_candidate_rows(
            connection,
            include_manual=args.include_manual,
        )
    finally:
        connection.close()

    if not candidate_rows:
        print("No image sources matched the current filters.")
        return

    sampled_rows = pick_bucket_sample(
        candidate_rows,
        sample_size=args.sample_size,
        rng=rng,
    )
    results = build_sample_results(sampled_rows)
    print_report(
        results,
        total_candidates=len(candidate_rows),
        sample_size=args.sample_size,
        seed=seed,
    )

    if args.json_output is not None:
        payload = {
            "seed": seed,
            "total_candidates": len(candidate_rows),
            "sample_size": args.sample_size,
            "sampled_count": len(results),
            "results": [asdict(result) for result in results],
        }
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("")
        print(f"Wrote JSON report to {args.json_output}")


if __name__ == "__main__":
    main()
