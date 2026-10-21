from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from compress_source_images import fetch_candidate_images, update_paths_in_db


class CompressSourceImagesTest(unittest.TestCase):
    def test_fetch_candidate_images_groups_alias_paths_by_normalized_storage_path(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_path TEXT,
                ingest_mode TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO sources (id, source_type, source_path, ingest_mode)
            VALUES (?, 'image', ?, 'auto_capture')
            """,
            [
                (1, r"C:\Users\paulorn\work\Lang\JP\data\captures\same.webp"),
                (2, "data/captures/same.webp"),
            ],
        )

        resolved_path = Path(r"C:\Users\paulo\Documents\work\Lang\JP\data\captures\same.webp")
        storage_path = "data/captures/same.webp"
        with (
            patch(
                "compress_source_images.resolve_repo_local_path",
                return_value=resolved_path,
            ),
            patch(
                "compress_source_images.normalize_repo_storage_path",
                return_value=storage_path,
            ),
        ):
            candidates = fetch_candidate_images(
                connection,
                include_manual=False,
                limit=None,
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_path, resolved_path)
        self.assertEqual(candidates[0].stored_paths, [
            r"C:\Users\paulorn\work\Lang\JP\data\captures\same.webp",
            "data/captures/same.webp",
        ])
        self.assertEqual(candidates[0].source_ids, [1, 2])

    def test_update_paths_in_db_updates_sources_and_capture_jobs_for_all_aliases(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY,
                source_path TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE capture_jobs (
                id INTEGER PRIMARY KEY,
                final_image_path TEXT
            )
            """
        )
        old_paths = [
            r"C:\Users\paulorn\work\Lang\JP\data\captures\same.webp",
            "data/captures/same.webp",
        ]
        new_path = "data/captures/same-normalized.webp"
        connection.executemany(
            "INSERT INTO sources (id, source_path) VALUES (?, ?)",
            [(1, old_paths[0]), (2, old_paths[1])],
        )
        connection.executemany(
            "INSERT INTO capture_jobs (id, final_image_path) VALUES (?, ?)",
            [(10, old_paths[0]), (11, old_paths[1])],
        )

        update_paths_in_db(
            connection,
            old_paths=old_paths,
            new_path=new_path,
        )

        source_paths = [
            row["source_path"]
            for row in connection.execute("SELECT source_path FROM sources ORDER BY id ASC")
        ]
        capture_job_paths = [
            row["final_image_path"]
            for row in connection.execute(
                "SELECT final_image_path FROM capture_jobs ORDER BY id ASC"
            )
        ]
        self.assertEqual(source_paths, [new_path, new_path])
        self.assertEqual(capture_job_paths, [new_path, new_path])


if __name__ == "__main__":
    unittest.main()
