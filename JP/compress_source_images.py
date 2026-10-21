from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageOps

from lang_jp import (
    DEFAULT_DB_PATH,
    normalize_repo_storage_path,
    resolve_repo_local_path,
)


@dataclass
class CandidateImage:
    stored_paths: list[str]
    source_path: Path
    source_ids: list[int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compress image sources already stored in the JP catalog database. "
            "By default, this converts auto-captured source images to WebP."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=45,
        help="WebP quality from 0 to 100 (default: 45)",
    )
    parser.add_argument(
        "--method",
        type=int,
        default=6,
        help="WebP encoder method from 0 to 6 (default: 6)",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=None,
        help="Optional resize cap for width before encoding",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=None,
        help="Optional resize cap for height before encoding",
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=0,
        help="Skip files smaller than this size in bytes",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many distinct image paths",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without touching files or the database",
    )
    parser.add_argument(
        "--include-manual",
        action="store_true",
        help="Also include manual image sources that have a file path stored",
    )
    parser.add_argument(
        "--keep-png",
        action="store_true",
        help="Keep PNG files after creating WebP replacements",
    )
    return parser.parse_args()


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def fetch_candidate_images(
    connection: sqlite3.Connection,
    *,
    include_manual: bool,
    limit: int | None,
) -> list[CandidateImage]:
    query = """
        SELECT id, source_path
        FROM sources
        WHERE source_type = 'image'
          AND source_path IS NOT NULL
    """
    if not include_manual:
        query += " AND ingest_mode = 'auto_capture'"
    query += " ORDER BY id ASC"

    grouped: dict[str, CandidateImage] = {}
    for row in connection.execute(query):
        raw_path = str(row["source_path"]).strip()
        if not raw_path:
            continue
        resolved_path = resolve_repo_local_path(raw_path)
        source_path = resolved_path if resolved_path is not None else Path(raw_path)
        grouping_key = normalize_repo_storage_path(raw_path) or raw_path
        candidate = grouped.get(grouping_key)
        if candidate is None:
            grouped[grouping_key] = CandidateImage(
                stored_paths=[raw_path],
                source_path=source_path,
                source_ids=[int(row["id"])],
            )
        else:
            if raw_path not in candidate.stored_paths:
                candidate.stored_paths.append(raw_path)
            candidate.source_ids.append(int(row["id"]))
        if limit is not None and len(grouped) >= limit:
            break
    return list(grouped.values())


def build_output_path(source_path: Path) -> Path:
    return source_path.with_suffix(".webp")


def maybe_resize_image(
    image: Image.Image,
    *,
    max_width: int | None,
    max_height: int | None,
) -> Image.Image:
    if max_width is None and max_height is None:
        return image

    width, height = image.size
    target_width = max_width or width
    target_height = max_height or height
    if width <= target_width and height <= target_height:
        return image

    resized = image.copy()
    resized.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
    return resized


def encode_webp(
    source_path: Path,
    *,
    quality: int,
    method: int,
    max_width: int | None,
    max_height: int | None,
) -> tuple[Path, tuple[int, int], tuple[int, int]]:
    with Image.open(source_path) as opened_image:
        normalized = ImageOps.exif_transpose(opened_image)
        original_size = normalized.size
        prepared = maybe_resize_image(
            normalized,
            max_width=max_width,
            max_height=max_height,
        )
        prepared.load()
        encoded_size = prepared.size
        with NamedTemporaryFile(
            suffix=".webp",
            prefix=f"{source_path.stem}-compressed-",
            dir=source_path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
        prepared.save(
            temp_path,
            format="WEBP",
            quality=quality,
            method=method,
        )
    return temp_path, original_size, encoded_size


def update_paths_in_db(
    connection: sqlite3.Connection,
    *,
    old_paths: list[str],
    new_path: str,
) -> None:
    for old_path in old_paths:
        if old_path == new_path:
            continue
        connection.execute(
            "UPDATE sources SET source_path = ? WHERE source_path = ?",
            (new_path, old_path),
        )
        if table_exists(connection, "capture_jobs"):
            connection.execute(
                "UPDATE capture_jobs SET final_image_path = ? WHERE final_image_path = ?",
                (new_path, old_path),
            )


def canonical_storage_path(path: Path) -> str:
    return normalize_repo_storage_path(path) or str(path)


def format_size(size_in_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_in_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_in_bytes} B"


def main() -> None:
    args = parse_args()
    if not 0 <= args.quality <= 100:
        raise SystemExit("--quality must be between 0 and 100.")
    if not 0 <= args.method <= 6:
        raise SystemExit("--method must be between 0 and 6.")

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    try:
        candidates = fetch_candidate_images(
            connection,
            include_manual=args.include_manual,
            limit=args.limit,
        )
        if not candidates:
            print("No image sources matched the current filters.")
            return

        processed = 0
        path_fixed = 0
        skipped = 0
        missing = 0
        total_before = 0
        total_after = 0

        for candidate in candidates:
            source_path = candidate.source_path
            current_storage_path = canonical_storage_path(source_path)
            output_path = build_output_path(source_path)
            output_storage_path = canonical_storage_path(output_path)
            if not source_path.exists():
                if source_path.suffix.lower() != ".webp" and output_path.exists():
                    print(
                        "[path-fix-existing-webp] "
                        f"{candidate.stored_paths[0]} -> {output_storage_path}"
                    )
                    if not args.dry_run:
                        update_paths_in_db(
                            connection,
                            old_paths=candidate.stored_paths,
                            new_path=output_storage_path,
                        )
                        connection.commit()
                    processed += 1
                    path_fixed += 1
                    continue
                missing += 1
                print(f"[missing] {source_path}")
                continue
            if source_path.suffix.lower() == ".webp":
                if any(stored_path != current_storage_path for stored_path in candidate.stored_paths):
                    print(
                        "[path-fix-webp] "
                        f"{candidate.stored_paths[0]} -> {current_storage_path}"
                    )
                    if not args.dry_run:
                        update_paths_in_db(
                            connection,
                            old_paths=candidate.stored_paths,
                            new_path=current_storage_path,
                        )
                        connection.commit()
                    processed += 1
                    path_fixed += 1
                else:
                    skipped += 1
                    print(f"[skip-webp] {source_path}")
                continue

            before_size = source_path.stat().st_size
            if before_size < args.min_bytes:
                skipped += 1
                print(f"[skip-small] {source_path} ({format_size(before_size)})")
                continue

            temp_path: Path | None = None
            output_created = False
            try:
                temp_path, original_dimensions, encoded_dimensions = encode_webp(
                    source_path,
                    quality=args.quality,
                    method=args.method,
                    max_width=args.max_width,
                    max_height=args.max_height,
                )
                after_size = temp_path.stat().st_size
                if after_size >= before_size:
                    skipped += 1
                    print(
                        "[skip-no-gain] "
                        f"{source_path} ({format_size(before_size)} -> {format_size(after_size)})"
                    )
                    temp_path.unlink(missing_ok=True)
                    continue

                reduction_percent = 100.0 - ((after_size / before_size) * 100.0)
                print(
                    "[plan] "
                    f"{source_path} "
                    f"{original_dimensions[0]}x{original_dimensions[1]} -> "
                    f"{encoded_dimensions[0]}x{encoded_dimensions[1]} "
                    f"{format_size(before_size)} -> {format_size(after_size)} "
                    f"({reduction_percent:.1f}% smaller)"
                )

                total_before += before_size
                total_after += after_size
                if args.dry_run:
                    temp_path.unlink(missing_ok=True)
                    processed += 1
                    continue

                temp_path.replace(output_path)
                output_created = True
                update_paths_in_db(
                    connection,
                    old_paths=candidate.stored_paths,
                    new_path=output_storage_path,
                )
                connection.commit()

                if not args.keep_png and output_path != source_path:
                    source_path.unlink(missing_ok=True)
                processed += 1
            except Exception as exc:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                if output_created and output_path != source_path and output_path.exists():
                    output_path.unlink(missing_ok=True)
                connection.rollback()
                skipped += 1
                print(f"[error] {source_path}: {exc}")

        print("")
        print(f"Candidates: {len(candidates)}")
        print(f"Processed: {processed}")
        print(f"Path fixes: {path_fixed}")
        print(f"Skipped: {skipped}")
        print(f"Missing: {missing}")
        print(f"Bytes before: {format_size(total_before)}")
        print(f"Bytes after: {format_size(total_after)}")
        if total_before and total_after:
            saved = total_before - total_after
            saved_percent = (saved / total_before) * 100.0
            print(f"Saved: {format_size(saved)} ({saved_percent:.1f}%)")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
