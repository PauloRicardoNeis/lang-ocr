from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lang_jp import (
    DEFAULT_DB_PATH,
    ROOT,
    canonical_key,
    claim_next_capture_job,
    create_source_analysis_candidate,
    create_source_analysis_segment,
    create_source_record,
    db_connect,
    find_source_by_image_sha256,
    form_key,
    is_kanji_char,
    normalize_token,
    stable_text_hash,
    update_capture_job_status,
)


DEFAULT_CAPTURE_DIR = ROOT / "data" / "captures"
DEFAULT_LOG_PATH = ROOT / "data" / "capture_daemon.log"
DEFAULT_POLL_INTERVAL = 2.0
DEBUG_ENABLED = os.environ.get("LANG_JP_CAPTURE_DEBUG") == "1"
DEFAULT_PADDLE_DEVICE = os.environ.get("LANG_JP_PADDLE_DEVICE", "cpu")
DEFAULT_PADDLE_ENABLE_MKLDNN = os.environ.get("LANG_JP_PADDLE_ENABLE_MKLDNN", "0") == "1"
DEFAULT_PADDLE_CPU_THREADS = int(os.environ.get("LANG_JP_PADDLE_CPU_THREADS", "2"))
DEFAULT_PADDLE_ENABLE_HPI = os.environ.get("LANG_JP_PADDLE_ENABLE_HPI", "0") == "1"
DEFAULT_PADDLE_ENABLE_CINN = os.environ.get("LANG_JP_PADDLE_ENABLE_CINN", "0") == "1"
DEFAULT_PADDLE_DISABLE_MODEL_SOURCE_CHECK = (
    os.environ.get("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True").lower() == "true"
)
DEFAULT_OCR_MAX_SIDE = int(os.environ.get("LANG_JP_OCR_MAX_SIDE", "1600"))
DEFAULT_OCR_PROGRESS_INTERVAL = float(os.environ.get("LANG_JP_OCR_PROGRESS_INTERVAL", "5"))
JAPANESE_SCRIPT_RANGES = (
    "\u3040-\u309f",
    "\u30a0-\u30ff",
    "\u3400-\u4dbf",
    "\u4e00-\u9fff",
    "\uf900-\ufaff",
)


@dataclass(slots=True)
class OcrBlock:
    text: str
    confidence: float | None
    bbox: list[list[float]] | None


@dataclass(slots=True)
class DiagnosticSnapshot:
    stage: str
    elapsed_ms: int
    rss_mb: float | None
    private_mb: float | None
    note: str | None


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PSAPI = ctypes.WinDLL("psapi", use_last_error=True)
_KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
_PSAPI.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
    wintypes.DWORD,
]
_PSAPI.GetProcessMemoryInfo.restype = wintypes.BOOL


def current_process_memory_mb() -> tuple[float | None, float | None]:
    if os.name != "nt":
        return None, None
    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    process = _KERNEL32.GetCurrentProcess()
    if not _PSAPI.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        return None, None
    return (
        counters.WorkingSetSize / (1024 * 1024),
        counters.PrivateUsage / (1024 * 1024),
    )


def format_memory_value(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}MB"


class DiagnosticProbe:
    def __init__(self, image_path: Path) -> None:
        self._image_path = image_path
        self._started_at = time.perf_counter()
        self.snapshots: list[DiagnosticSnapshot] = []

    def mark(self, stage: str, note: str | None = None) -> None:
        rss_mb, private_mb = current_process_memory_mb()
        snapshot = DiagnosticSnapshot(
            stage=stage,
            elapsed_ms=int((time.perf_counter() - self._started_at) * 1000),
            rss_mb=rss_mb,
            private_mb=private_mb,
            note=note,
        )
        self.snapshots.append(snapshot)
        parts = [
            f"diagnose stage={snapshot.stage}",
            f"elapsed={snapshot.elapsed_ms}ms",
            f"rss={format_memory_value(snapshot.rss_mb)}",
            f"private={format_memory_value(snapshot.private_mb)}",
        ]
        if note:
            parts.append(note)
        message = " ".join(parts)
        append_worker_log(message)
        print(message)

    def write_json(self, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "image_path": str(self._image_path),
            "snapshots": [asdict(snapshot) for snapshot in self.snapshots],
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def contains_japanese_text(value: str | None) -> bool:
    if not value:
        return False
    return any(char and any(start <= char <= end for start, end in _script_pairs()) for char in value)


def _script_pairs() -> list[tuple[str, str]]:
    return [(chunk[0], chunk[1]) for chunk in (part.split("-") for part in JAPANESE_SCRIPT_RANGES)]


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bbox_json(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return json.dumps(value, ensure_ascii=False)


def build_capture_path(captured_at: str | None, image_sha256: str | None) -> Path:
    now = datetime.now().astimezone()
    if captured_at:
        try:
            now = datetime.fromisoformat(captured_at)
        except ValueError:
            pass
    day_dir = DEFAULT_CAPTURE_DIR / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    suffix = image_sha256[:12] if image_sha256 else now.strftime("%f")
    filename = f"{now.strftime('%Y%m%d-%H%M%S')}-{suffix}.png"
    return day_dir / filename


def delete_if_exists(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def prepare_ocr_input_image(image_path: Path) -> tuple[Path, str | None, Path | None]:
    max_side = DEFAULT_OCR_MAX_SIDE
    if max_side <= 0:
        return image_path, None, None

    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("Pillow is required to prepare OCR input images.") from exc

    with Image.open(image_path) as image:
        width, height = image.size
        longest_side = max(width, height)
        if longest_side <= max_side:
            return (
                image_path,
                f"ocr input kept original size={width}x{height} max_side={max_side}",
                None,
            )

        scale = max_side / float(longest_side)
        resized_width = max(1, int(round(width * scale)))
        resized_height = max(1, int(round(height * scale)))
        resampling = getattr(Image, "Resampling", Image)
        resized = image.resize((resized_width, resized_height), resampling.LANCZOS)

    temp_dir = ROOT / "data" / "ocr_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        suffix=".png",
        prefix=f"{image_path.stem}-ocr-",
        dir=temp_dir,
        delete=False,
    )
    temp_path = Path(temp_file.name)
    temp_file.close()
    resized.save(temp_path, format="PNG")
    return (
        temp_path,
        (
            f"ocr input resized from {width}x{height} "
            f"to {resized_width}x{resized_height} max_side={max_side}"
        ),
        temp_path,
    )


def append_worker_log(message: str) -> None:
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with DEFAULT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def emit_progress_message(message: str, *, echo_stdout: bool = False) -> None:
    append_worker_log(message)
    if echo_stdout:
        print(message)


def run_with_progress_logs(
    label: str,
    func,
    *,
    echo_stdout: bool = False,
    interval_seconds: float = DEFAULT_OCR_PROGRESS_INTERVAL,
):
    started_at = time.perf_counter()
    emit_progress_message(f"{label} started", echo_stdout=echo_stdout)
    stop_event = threading.Event()

    def reporter() -> None:
        while not stop_event.wait(interval_seconds):
            elapsed_seconds = int(time.perf_counter() - started_at)
            rss_mb, private_mb = current_process_memory_mb()
            emit_progress_message(
                (
                    f"{label} in progress elapsed={elapsed_seconds}s "
                    f"rss={format_memory_value(rss_mb)} private={format_memory_value(private_mb)}"
                ),
                echo_stdout=echo_stdout,
            )

    reporter_thread: threading.Thread | None = None
    if interval_seconds > 0:
        reporter_thread = threading.Thread(target=reporter, daemon=True)
        reporter_thread.start()

    try:
        return func()
    finally:
        stop_event.set()
        if reporter_thread is not None:
            reporter_thread.join(timeout=0.2)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        rss_mb, private_mb = current_process_memory_mb()
        emit_progress_message(
            (
                f"{label} finished elapsed={elapsed_ms}ms "
                f"rss={format_memory_value(rss_mb)} private={format_memory_value(private_mb)}"
            ),
            echo_stdout=echo_stdout,
        )


def read_existing_term_match(
    connection,
    *,
    lemma: str,
    reading: str | None,
    surface_form: str,
) -> int | None:
    term_row = connection.execute(
        "SELECT id FROM terms WHERE canonical_key = ?",
        (canonical_key(lemma, reading),),
    ).fetchone()
    if term_row is not None:
        return int(term_row["id"])
    form_row = connection.execute(
        """
        SELECT term_id
        FROM term_forms
        WHERE form_key = ?
        ORDER BY term_id ASC
        LIMIT 1
        """,
        (form_key(surface_form, reading),),
    ).fetchone()
    if form_row is not None:
        return int(form_row["term_id"])
    return None


def read_existing_kanji_match(connection, literal: str) -> int | None:
    row = connection.execute(
        "SELECT id FROM kanji WHERE literal = ?",
        (literal,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


class PaddleOcrAdapter:
    def __init__(self) -> None:
        if DEFAULT_PADDLE_DISABLE_MODEL_SOURCE_CHECK:
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise SystemExit(
                "Missing OCR dependency. Install the capture worker environment with "
                "PaddleOCR/PaddlePaddle before running capture_worker.py."
            ) from exc

        self._engine = PaddleOCR(
            lang="japan",
            device=DEFAULT_PADDLE_DEVICE,
            enable_mkldnn=DEFAULT_PADDLE_ENABLE_MKLDNN,
            cpu_threads=DEFAULT_PADDLE_CPU_THREADS,
            enable_hpi=DEFAULT_PADDLE_ENABLE_HPI,
            enable_cinn=DEFAULT_PADDLE_ENABLE_CINN,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            return_word_box=False,
        )
        append_worker_log(
            "paddle ocr initialized "
            f"device={DEFAULT_PADDLE_DEVICE} "
            f"enable_mkldnn={DEFAULT_PADDLE_ENABLE_MKLDNN} "
            f"cpu_threads={DEFAULT_PADDLE_CPU_THREADS} "
            f"enable_hpi={DEFAULT_PADDLE_ENABLE_HPI} "
            f"enable_cinn={DEFAULT_PADDLE_ENABLE_CINN} "
            "use_doc_orientation_classify=False "
            "use_doc_unwarping=False "
            "use_textline_orientation=False "
            "return_word_box=False"
        )

    def extract_blocks(
        self,
        image_path: Path,
        *,
        prepared: bool = False,
        progress_label: str | None = None,
        echo_stdout: bool = False,
    ) -> list[OcrBlock]:
        cleanup_path: Path | None = None
        if not prepared:
            image_path, note, cleanup_path = prepare_ocr_input_image(image_path)
            if note:
                append_worker_log(note)

        def do_predict():
            if hasattr(self._engine, "predict"):
                return self._engine.predict(str(image_path))
            return self._engine.ocr(str(image_path), cls=False)

        label = progress_label or f"ocr predict image={image_path.name}"
        result = run_with_progress_logs(label, do_predict, echo_stdout=echo_stdout)
        try:
            return normalize_paddle_result(result)
        finally:
            if cleanup_path is not None:
                delete_if_exists(cleanup_path)


def normalize_paddle_result(result: Any) -> list[OcrBlock]:
    blocks: list[OcrBlock] = []
    if result is None:
        return blocks
    if not isinstance(result, list):
        result = [result]
    for item in result:
        if hasattr(item, "json"):
            payload = getattr(item, "json")
            if callable(payload):
                payload = payload()
            if isinstance(payload, dict):
                texts = payload.get("rec_texts") or []
                scores = payload.get("rec_scores") or []
                polys = payload.get("dt_polys") or payload.get("polys") or []
                for index, text in enumerate(texts):
                    normalized = normalize_token(str(text))
                    if not normalized:
                        continue
                    score = scores[index] if index < len(scores) else None
                    bbox = polys[index] if index < len(polys) else None
                    blocks.append(
                        OcrBlock(
                            text=normalized,
                            confidence=optional_float(score),
                            bbox=bbox,
                        )
                    )
                continue
        iterable = item if isinstance(item, list) else [item]
        for line in iterable:
            if (
                isinstance(line, (list, tuple))
                and len(line) >= 2
                and isinstance(line[1], (list, tuple))
                and len(line[1]) >= 1
            ):
                text = normalize_token(str(line[1][0]))
                if not text:
                    continue
                score = line[1][1] if len(line[1]) > 1 else None
                bbox = line[0] if isinstance(line[0], list) else None
                blocks.append(
                    OcrBlock(
                        text=text,
                        confidence=optional_float(score),
                        bbox=bbox,
                    )
                )
    return blocks


class SudachiAnalyzer:
    def __init__(self) -> None:
        try:
            from sudachipy import Dictionary, SplitMode
        except ImportError as exc:
            raise SystemExit(
                "Missing Japanese NLP dependency. Install SudachiPy + sudachidict_core "
                "before running capture_worker.py."
            ) from exc

        self._split_mode = SplitMode
        self._tokenizer = Dictionary().create()

    def analyze_segment(self, connection, block: OcrBlock) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        normalized_text = normalize_token(block.text)
        segment_payload = {
            "raw_text": block.text,
            "normalized_text": normalized_text,
            "reading": None,
            "confidence": block.confidence,
            "bbox_json": bbox_json(block.bbox),
        }
        candidates: list[dict[str, Any]] = []
        if not normalized_text:
            return segment_payload, candidates

        main_tokens = self._tokenizer.tokenize(normalized_text, self._split_mode.C)
        for rank, token in enumerate(main_tokens):
            surface = normalize_token(token.surface())
            lemma = normalize_token(token.dictionary_form()) or surface
            reading = normalize_token(token.reading_form()) or None
            pos = normalize_token(" ".join(token.part_of_speech()[:2])) or None
            if not surface:
                continue
            candidates.append(
                {
                    "key": f"main:{rank}",
                    "parent_key": None,
                    "candidate_type": "term",
                    "surface_form": surface,
                    "dictionary_form": lemma,
                    "reading": reading,
                    "pos": pos,
                    "confidence": block.confidence,
                    "rank": rank,
                    "matched_term_id": read_existing_term_match(
                        connection,
                        lemma=lemma,
                        reading=reading,
                        surface_form=surface,
                    ),
                    "matched_kanji_id": None,
                }
            )
            sub_tokens = token.split(self._split_mode.A)
            for sub_index, sub_token in enumerate(sub_tokens):
                sub_surface = normalize_token(sub_token.surface())
                sub_lemma = normalize_token(sub_token.dictionary_form()) or sub_surface
                sub_reading = normalize_token(sub_token.reading_form()) or None
                sub_pos = normalize_token(" ".join(sub_token.part_of_speech()[:2])) or None
                if not sub_surface or sub_surface == surface:
                    continue
                candidates.append(
                    {
                        "key": f"sub:{rank}:{sub_index}",
                        "parent_key": f"main:{rank}",
                        "candidate_type": "subterm",
                        "surface_form": sub_surface,
                        "dictionary_form": sub_lemma,
                        "reading": sub_reading,
                        "pos": sub_pos,
                        "confidence": block.confidence,
                        "rank": (rank * 100) + sub_index + 1,
                        "matched_term_id": read_existing_term_match(
                            connection,
                            lemma=sub_lemma,
                            reading=sub_reading,
                            surface_form=sub_surface,
                        ),
                        "matched_kanji_id": None,
                    }
                )
            for literal in sorted({char for char in f"{surface}{lemma}" if is_kanji_char(char)}):
                candidates.append(
                    {
                        "key": f"kanji:{rank}:{literal}",
                        "parent_key": f"main:{rank}",
                        "candidate_type": "kanji",
                        "surface_form": literal,
                        "dictionary_form": literal,
                        "reading": None,
                        "pos": "kanji",
                        "confidence": block.confidence,
                        "rank": (rank * 1000) + ord(literal),
                        "matched_term_id": None,
                        "matched_kanji_id": read_existing_kanji_match(connection, literal),
                    }
                )
        return segment_payload, candidates


def process_job_once(db_path: Path, *, ocr: PaddleOcrAdapter, analyzer: SudachiAnalyzer) -> bool:
    with db_connect(db_path) as connection:
        job = claim_next_capture_job(connection)
    if job is None:
        return False

    job_id = int(job["id"])
    temp_image_path = Path(str(job["temp_image_path"]))
    image_sha256 = job["image_sha256"]
    captured_at = job["captured_at"]
    if not temp_image_path.exists():
        with db_connect(db_path) as connection:
            update_capture_job_status(
                connection,
                job_id=job_id,
                status="failed",
                last_error=f"Temp image does not exist: {temp_image_path}",
            )
        append_worker_log(f"capture job #{job_id} failed: temp image missing at {temp_image_path}")
        return True

    try:
        with db_connect(db_path) as connection:
            duplicate_source = find_source_by_image_sha256(connection, image_sha256)
            if duplicate_source is not None:
                update_capture_job_status(
                    connection,
                    job_id=job_id,
                    status="rejected",
                    last_error=f"Duplicate source #{duplicate_source['id']} for image hash.",
                )
                delete_if_exists(temp_image_path)
                append_worker_log(
                    f"capture job #{job_id} rejected: duplicate source #{duplicate_source['id']}"
                )
                return True

        blocks = ocr.extract_blocks(
            temp_image_path,
            progress_label=f"capture job #{job_id} OCR predict",
        )
        normalized_blocks = [block for block in blocks if normalize_token(block.text)]
        raw_text = "\n".join(block.text for block in normalized_blocks)
        ocr_text_hash = stable_text_hash(raw_text)
        if not any(contains_japanese_text(block.text) for block in normalized_blocks):
            with db_connect(db_path) as connection:
                update_capture_job_status(
                    connection,
                    job_id=job_id,
                    status="rejected",
                    last_error="No Japanese text detected in OCR output.",
                    raw_text=raw_text,
                    ocr_text_hash=ocr_text_hash,
                )
            delete_if_exists(temp_image_path)
            append_worker_log(f"capture job #{job_id} rejected: no Japanese text detected")
            return True

        final_path = build_capture_path(captured_at, image_sha256)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            final_path = final_path.with_name(
                f"{final_path.stem}-{int(time.time())}{final_path.suffix}"
            )
        shutil.move(str(temp_image_path), final_path)

        with db_connect(db_path) as connection:
            source_id = create_source_record(
                connection,
                source_type="image",
                language_code="ja",
                ingest_mode="auto_capture",
                review_status="pending",
                source_path=str(final_path),
                raw_text=raw_text,
                notes="capture_worker_auto_ingest",
                captured_at=captured_at,
                image_sha256=image_sha256,
                ocr_text_hash=ocr_text_hash,
                capture_job_id=job_id,
            )
            for segment_index, block in enumerate(normalized_blocks):
                segment_payload, candidates = analyzer.analyze_segment(connection, block)
                segment_id = create_source_analysis_segment(
                    connection,
                    source_id=source_id,
                    segment_index=segment_index,
                    raw_text=segment_payload["raw_text"],
                    normalized_text=segment_payload["normalized_text"],
                    reading=segment_payload["reading"],
                    confidence=segment_payload["confidence"],
                    bbox_json=segment_payload["bbox_json"],
                )
                inserted_ids: dict[str, int] = {}
                for candidate in candidates:
                    parent_id = (
                        inserted_ids.get(candidate["parent_key"])
                        if candidate["parent_key"] is not None
                        else None
                    )
                    candidate_id = create_source_analysis_candidate(
                        connection,
                        source_analysis_segment_id=segment_id,
                        candidate_type=candidate["candidate_type"],
                        surface_form=candidate["surface_form"],
                        dictionary_form=candidate["dictionary_form"],
                        reading=candidate["reading"],
                        pos=candidate["pos"],
                        confidence=candidate["confidence"],
                        rank=candidate["rank"],
                        parent_candidate_id=parent_id,
                        matched_term_id=candidate["matched_term_id"],
                        matched_kanji_id=candidate["matched_kanji_id"],
                    )
                    inserted_ids[candidate["key"]] = candidate_id
            update_capture_job_status(
                connection,
                job_id=job_id,
                status="accepted",
                final_image_path=str(final_path),
                raw_text=raw_text,
                ocr_text_hash=ocr_text_hash,
            )
        append_worker_log(
            f"capture job #{job_id} accepted: source #{source_id} saved to {final_path}"
        )
    except Exception as exc:
        with db_connect(db_path) as connection:
            update_capture_job_status(
                connection,
                job_id=job_id,
                status="failed",
                last_error=str(exc),
            )
        append_worker_log(f"capture job #{job_id} failed: {exc}")
        return True

    return True


def diagnose_image(
    image_path: Path,
    *,
    with_analysis: bool,
    output_json: Path | None,
) -> None:
    probe = DiagnosticProbe(image_path)
    probe.mark("start", note=f"image={image_path}")
    if not image_path.exists():
        raise SystemExit(f"Image does not exist: {image_path}")

    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("Pillow is required for diagnose mode.") from exc

    with Image.open(image_path) as image:
        probe.mark("image_opened", note=f"size={image.width}x{image.height} mode={image.mode}")
        image.load()
        probe.mark("image_decoded", note=f"size={image.width}x{image.height} mode={image.mode}")

    probe.mark("before_ocr_init")
    ocr = PaddleOcrAdapter()
    probe.mark("after_ocr_init")

    probe.mark("before_ocr_predict")
    ocr_input_path, resize_note, cleanup_path = prepare_ocr_input_image(image_path)
    if resize_note:
        probe.mark("ocr_input_prepared", note=resize_note)
    try:
        blocks = ocr.extract_blocks(
            ocr_input_path,
            prepared=True,
            progress_label=f"diagnose OCR predict image={ocr_input_path.name}",
            echo_stdout=True,
        )
    finally:
        if cleanup_path is not None:
            delete_if_exists(cleanup_path)
    normalized_blocks = [block for block in blocks if normalize_token(block.text)]
    raw_text = "\n".join(block.text for block in normalized_blocks)
    japanese_detected = any(contains_japanese_text(block.text) for block in normalized_blocks)
    probe.mark(
        "after_ocr_predict",
        note=(
            f"blocks={len(blocks)} normalized_blocks={len(normalized_blocks)} "
            f"japanese_detected={japanese_detected} raw_text_len={len(raw_text)}"
        ),
    )

    if with_analysis:
        probe.mark("before_analyzer_init")
        analyzer = SudachiAnalyzer()
        probe.mark("after_analyzer_init")
        candidate_total = 0
        with db_connect(DEFAULT_DB_PATH) as connection:
            for block in normalized_blocks:
                _, candidates = analyzer.analyze_segment(connection, block)
                candidate_total += len(candidates)
        probe.mark(
            "after_analysis",
            note=f"segments={len(normalized_blocks)} candidates={candidate_total}",
        )

    probe.mark("done")
    if output_json is not None:
        probe.write_json(output_json)
        print(f"Wrote diagnostic report to {output_json}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture job worker for the JP passive OCR pipeline."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="Seconds to wait between queue polls in loop mode.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    once_parser = subparsers.add_parser("once", help="Process a single pending capture job.")
    once_parser.set_defaults(mode="once")

    loop_parser = subparsers.add_parser("loop", help="Process capture jobs continuously.")
    loop_parser.set_defaults(mode="loop")

    diagnose_parser = subparsers.add_parser(
        "diagnose",
        help="Run OCR memory diagnostics for a single image without touching the queue.",
    )
    diagnose_parser.add_argument(
        "--image",
        type=Path,
        required=True,
        help="Absolute path to the image that should be profiled.",
    )
    diagnose_parser.add_argument(
        "--with-analysis",
        action="store_true",
        help="Also initialize Sudachi and run analysis on OCR blocks.",
    )
    diagnose_parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path for a JSON report with stage-by-stage memory snapshots.",
    )
    diagnose_parser.set_defaults(mode="diagnose")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    append_worker_log(
        f"capture worker booted mode={args.command} db={args.db} debug={DEBUG_ENABLED}"
    )
    if args.mode == "diagnose":
        diagnose_image(
            args.image,
            with_analysis=args.with_analysis,
            output_json=args.output_json,
        )
        return

    ocr = PaddleOcrAdapter()
    analyzer = SudachiAnalyzer()

    if args.mode == "once":
        processed = process_job_once(args.db, ocr=ocr, analyzer=analyzer)
        if not processed:
            print("No pending capture jobs.")
            if DEBUG_ENABLED:
                append_worker_log("capture worker idle: no pending capture jobs")
        return

    idle_polls = 0
    while True:
        processed = process_job_once(args.db, ocr=ocr, analyzer=analyzer)
        if not processed:
            idle_polls += 1
            if DEBUG_ENABLED and idle_polls >= 15:
                append_worker_log("capture worker idle: still waiting for pending capture jobs")
                idle_polls = 0
            time.sleep(max(0.25, args.poll_interval))
        else:
            idle_polls = 0


if __name__ == "__main__":
    main()
