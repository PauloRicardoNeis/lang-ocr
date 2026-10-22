from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import lang_jp
from lang_jp import (
    AUTO_PROMOTION_NOTES,
    SCHEMA_PATH,
    create_source_record,
    db_connect,
    ensure_source_segment,
    ensure_term_form,
    stable_text_hash,
    upsert_source_segment_term_link,
    upsert_source_term_link,
    upsert_term_record,
)
import rebuild_auto_sources
from ocr_backends import OcrBlock
from rebuild_auto_sources import (
    SourceOutcome,
    append_progress,
    fill_missing_term_meanings,
    is_live_db_path,
    load_finished_sources,
    rebuild_source,
    remove_auto_promoted_links,
    select_rebuild_sources,
)


class StubOcr:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def extract_blocks(self, image_path: Path, **_: object) -> list[OcrBlock]:
        return [OcrBlock(text=line, confidence=0.9, bbox=None) for line in self.lines]


class StubAnalyzer:
    """One term candidate per space-separated word, like Sudachi's main tokens."""

    def analyze_segment(self, connection, block: OcrBlock):
        segment = {
            "raw_text": block.text,
            "normalized_text": block.text,
            "reading": None,
            "confidence": block.confidence,
            "bbox_json": None,
        }
        candidates = [
            {
                "key": f"main:{rank}",
                "parent_key": None,
                "candidate_type": "term",
                "surface_form": word,
                "dictionary_form": word,
                "reading": None,
                "pos": "名詞 普通名詞",
                "confidence": block.confidence,
                "rank": rank,
                "matched_term_id": None,
                "matched_kanji_id": None,
            }
            for rank, word in enumerate(block.text.split())
        ]
        return segment, candidates


class RebuildSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "catalog.sqlite3"
        self._original_auto_translate = lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = False
        self.connection = db_connect(self.db_path)
        self.connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.connection.close()
        lang_jp.DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS = self._original_auto_translate
        self.temp_dir.cleanup()

    def add_source(
        self,
        *,
        ingest_mode: str = "auto_capture",
        review_status: str = "pending",
        source_type: str = "image",
        raw_text: str = "川 日",
        source_path: str | None = None,
    ) -> int:
        source_id = create_source_record(
            self.connection,
            source_type=source_type,
            ingest_mode=ingest_mode,
            review_status=review_status,
            source_path=source_path,
            raw_text=raw_text,
        )
        self.connection.commit()
        return source_id

    def link(self, source_id: int, lemma: str, *, notes: str) -> int:
        term_id = upsert_term_record(
            self.connection,
            lemma=lemma,
            reading=None,
            meaning_pt=None,
            pos=None,
            notes=notes,
        )
        form_id = ensure_term_form(self.connection, term_id=term_id, surface_form=lemma, reading=None)
        segment_id = ensure_source_segment(
            self.connection,
            source_id=source_id,
            segment_text=f"{lemma} segment",
            reading=None,
            notes=notes,
        )
        upsert_source_term_link(
            self.connection,
            source_id=source_id,
            term_id=term_id,
            term_form_id=form_id,
            surface_form=lemma,
            count=1,
            confidence=None,
            notes=notes,
            mode="add",
        )
        upsert_source_segment_term_link(
            self.connection,
            source_segment_id=segment_id,
            term_id=term_id,
            term_form_id=form_id,
            surface_form=lemma,
            count=1,
            confidence=None,
            notes=notes,
            mode="add",
        )
        self.connection.commit()
        return term_id

    def selected_ids(self, **kwargs) -> list[int]:
        return [int(row["id"]) for row in select_rebuild_sources(self.connection, **kwargs)]

    def test_selects_only_pending_auto_captured_images(self) -> None:
        pending = self.add_source()
        self.add_source(ingest_mode="manual", review_status="reviewed")
        self.add_source(ingest_mode="manual", review_status="pending")
        self.add_source(review_status="reviewed")
        self.add_source(review_status="ignored")
        self.add_source(source_type="text")

        self.assertEqual(self.selected_ids(), [pending])

    def test_source_id_filter_cannot_pull_in_manual_or_reviewed_sources(self) -> None:
        pending = self.add_source()
        manual = self.add_source(ingest_mode="manual", review_status="reviewed")
        reviewed = self.add_source(review_status="reviewed")

        self.assertEqual(self.selected_ids(source_ids=[pending, manual, reviewed]), [pending])

    def test_finished_sources_are_skipped_only_while_their_text_hash_matches(self) -> None:
        done = self.add_source(raw_text="新しい")
        restored = self.add_source(raw_text="古い")
        todo = self.add_source()
        finished = {
            done: stable_text_hash("新しい"),
            # A backup restored after the run has the old text again.
            restored: stable_text_hash("新しい"),
        }

        self.assertEqual(self.selected_ids(finished=finished), [restored, todo])

    def test_limit_applies_after_finished_sources_are_dropped(self) -> None:
        first = self.add_source(raw_text="一")
        second = self.add_source(raw_text="二")
        third = self.add_source(raw_text="三")

        selected = self.selected_ids(finished={first: stable_text_hash("一")}, limit=1)

        self.assertEqual(selected, [second])
        self.assertNotIn(third, selected)

    def test_sample_is_deterministic_and_stable_when_resumed(self) -> None:
        source_ids = [self.add_source(raw_text=f"文{index}") for index in range(20)]

        sample = self.selected_ids(sample=5, seed=7)
        self.assertEqual(sample, self.selected_ids(sample=5, seed=7))
        self.assertEqual(sample, sorted(sample))
        self.assertTrue(set(sample) <= set(source_ids))

        finished = {sample[0]: stable_text_hash(f"文{source_ids.index(sample[0])}")}
        self.assertEqual(self.selected_ids(sample=5, seed=7, finished=finished), sample[1:])

    def test_load_finished_sources_keeps_the_last_record_per_source(self) -> None:
        progress_path = self.root / "progress.jsonl"
        append_progress(progress_path, SourceOutcome(source_id=1, status="rebuilt", ocr_text_hash="a"))
        append_progress(progress_path, SourceOutcome(source_id=2, status="rebuilt", ocr_text_hash="b"))
        append_progress(progress_path, SourceOutcome(source_id=2, status="failed", error="boom"))
        append_progress(progress_path, SourceOutcome(source_id=3, status="no_text"))
        append_progress(progress_path, SourceOutcome(source_id=4, status="failed", error="boom"))
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write('{"source_id": 5, "status": "rebu')  # cut short by an interruption

        self.assertEqual(load_finished_sources(progress_path), {1: "a", 3: None})
        self.assertEqual(load_finished_sources(self.root / "missing.jsonl"), {})

    def test_live_database_paths_are_recognised(self) -> None:
        jp_root = self.root / "JP"
        (jp_root / "data").mkdir(parents=True)
        live_db = jp_root / "data" / "lang_jp.sqlite3"
        live_db.write_bytes(b"")

        self.assertTrue(is_live_db_path(lang_jp.DEFAULT_DB_PATH, jp_root=jp_root))
        self.assertTrue(is_live_db_path(live_db, jp_root=jp_root))
        self.assertTrue(is_live_db_path(jp_root / "data" / ".." / "data" / "lang_jp.sqlite3", jp_root=jp_root))
        self.assertFalse(is_live_db_path(self.db_path, jp_root=jp_root))

    def test_remove_auto_promoted_links_keeps_links_made_by_hand(self) -> None:
        source_id = self.add_source()
        auto_term = self.link(source_id, "川", notes=AUTO_PROMOTION_NOTES)
        manual_term = self.link(source_id, "冒険", notes="manual_review")

        term_ids, removed = remove_auto_promoted_links(self.connection, source_id)

        self.assertEqual((term_ids, removed), ([auto_term], 1))
        remaining = self.connection.execute(
            "SELECT term_id FROM source_terms WHERE source_id = ?", (source_id,)
        ).fetchall()
        self.assertEqual([int(row["term_id"]) for row in remaining], [manual_term])
        segments = self.connection.execute(
            "SELECT segment_text FROM source_segments WHERE source_id = ?", (source_id,)
        ).fetchall()
        self.assertEqual([row["segment_text"] for row in segments], ["冒険 segment"])

    def test_rebuild_source_replaces_text_and_auto_links_only(self) -> None:
        image = self.root / "JP" / "data" / "captures" / "shot.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"not read by the stub OCR")
        source_id = self.add_source(raw_text="川 日", source_path="data/captures/shot.png")
        manual_source = self.add_source(ingest_mode="manual", review_status="reviewed")
        old_term = self.link(source_id, "川", notes=AUTO_PROMOTION_NOTES)
        self.link(manual_source, "川", notes="manual_review")
        hand_term = self.link(source_id, "冒険", notes="manual_review")
        row = select_rebuild_sources(self.connection)[0]

        outcome, raw_text = rebuild_source(
            self.connection,
            row,
            ocr=StubOcr(["魔法 使い"]),
            analyzer=StubAnalyzer(),
            jp_root=self.root / "JP",
            dry_run=False,
            requeue_translations=True,
            ignore_empty=False,
        )

        self.assertEqual((outcome.status, raw_text), ("rebuilt", "魔法 使い"))
        self.assertEqual((outcome.removed_links, outcome.promoted_terms), (1, 2))
        source = self.connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        self.assertEqual(source["raw_text"], "魔法 使い")
        self.assertEqual(source["ocr_text_hash"], stable_text_hash("魔法 使い"))
        self.assertEqual(source["review_status"], "pending")
        lemmas = {
            row["lemma"]
            for row in self.connection.execute(
                """
                SELECT t.lemma FROM source_terms AS st JOIN terms AS t ON t.id = st.term_id
                WHERE st.source_id = ?
                """,
                (source_id,),
            )
        }
        self.assertEqual(lemmas, {"魔法", "使い", "冒険"})
        self.assertIn(
            hand_term,
            [row["term_id"] for row in self.connection.execute("SELECT term_id FROM source_terms")],
        )
        # The old term keeps only the manual source, and its stats say so.
        stats = self.connection.execute(
            "SELECT distinct_sources FROM term_stats WHERE term_id = ?", (old_term,)
        ).fetchone()
        self.assertEqual(stats["distinct_sources"], 1)

    def test_rebuild_source_dry_run_rolls_back(self) -> None:
        image = self.root / "JP" / "data" / "captures" / "shot.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"")
        source_id = self.add_source(raw_text="川 日", source_path="data/captures/shot.png")
        self.link(source_id, "川", notes=AUTO_PROMOTION_NOTES)
        row = select_rebuild_sources(self.connection)[0]

        outcome, raw_text = rebuild_source(
            self.connection,
            row,
            ocr=StubOcr(["Settings"]),
            analyzer=StubAnalyzer(),
            jp_root=self.root / "JP",
            dry_run=True,
            requeue_translations=True,
            ignore_empty=True,
        )

        self.assertEqual((outcome.status, raw_text), ("no_text", ""))
        source = self.connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        self.assertEqual((source["raw_text"], source["review_status"]), ("川 日", "pending"))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM source_terms WHERE source_id = ?", (source_id,)
            ).fetchone()[0],
            1,
        )

    def test_fill_missing_term_meanings_fills_terms_of_the_given_sources(self) -> None:
        source_id = self.add_source()
        other_source = self.add_source()
        river = self.link(source_id, "川", notes=AUTO_PROMOTION_NOTES)
        mountain = self.link(other_source, "山", notes=AUTO_PROMOTION_NOTES)

        with patch.object(rebuild_auto_sources, "auto_translate_term_meaning_pt", return_value="rio"):
            result = fill_missing_term_meanings(self.connection, [source_id])

        self.assertEqual(result, (1, 1, True))
        meanings = dict(self.connection.execute("SELECT id, meaning_pt FROM terms").fetchall())
        self.assertEqual((meanings[river], meanings[mountain]), ("rio", None))

    def test_fill_missing_term_meanings_stops_when_the_translator_is_down(self) -> None:
        source_id = self.add_source()
        for index in range(25):
            self.link(source_id, f"語{index}", notes=AUTO_PROMOTION_NOTES)

        with (
            patch.object(rebuild_auto_sources, "auto_translate_term_meaning_pt", return_value=None),
            patch.object(rebuild_auto_sources, "translator_reachable", return_value=False),
        ):
            result = fill_missing_term_meanings(self.connection, [source_id])

        self.assertEqual(result, (rebuild_auto_sources.TRANSLATOR_PROBE_AFTER_EMPTY, 0, False))

    def test_missing_image_leaves_the_source_alone(self) -> None:
        self.add_source(source_path="data/captures/gone.png")
        row = select_rebuild_sources(self.connection)[0]

        outcome, _ = rebuild_source(
            self.connection,
            row,
            ocr=StubOcr(["魔法"]),
            analyzer=StubAnalyzer(),
            jp_root=self.root / "JP",
            dry_run=False,
            requeue_translations=True,
            ignore_empty=False,
        )

        self.assertEqual(outcome.status, "missing_image")
        self.assertEqual(outcome.ocr_text_hash, row["ocr_text_hash"])
        self.assertIn("gone.png", outcome.error)

    def test_ignore_missing_images_drops_auto_links_and_ignores_the_source(self) -> None:
        source_id = self.add_source(source_path="data/captures/gone.png")
        river = self.link(source_id, "川", notes=AUTO_PROMOTION_NOTES)
        self.link(source_id, "冒険", notes="manual_review")
        row = select_rebuild_sources(self.connection)[0]

        outcome, raw_text = rebuild_source(
            self.connection,
            row,
            ocr=StubOcr(["魔法"]),
            analyzer=StubAnalyzer(),
            jp_root=self.root / "JP",
            dry_run=False,
            requeue_translations=False,
            ignore_empty=False,
            ignore_missing_images=True,
        )

        self.assertEqual((outcome.status, outcome.removed_links, raw_text), ("missing_image", 1, "川 日"))
        source = self.connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        self.assertEqual((source["review_status"], source["raw_text"]), ("ignored", "川 日"))
        lemmas = [
            row["lemma"]
            for row in self.connection.execute(
                "SELECT t.lemma FROM source_terms st JOIN terms t ON t.id = st.term_id WHERE st.source_id = ?",
                (source_id,),
            )
        ]
        self.assertEqual(lemmas, ["冒険"])
        stats = self.connection.execute(
            "SELECT distinct_sources FROM term_stats WHERE term_id = ?", (river,)
        ).fetchone()
        self.assertEqual(stats["distinct_sources"], 0)
        self.assertEqual(select_rebuild_sources(self.connection), [])


if __name__ == "__main__":
    unittest.main()
