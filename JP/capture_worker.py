from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import languages
from lang_jp import (
    CAPTURE_LANGUAGES_ENV,
    DEFAULT_DB_PATH,
    DEFAULT_LANGUAGE_CODE,
    ROOT,
    approve_source_analysis_candidates,
    build_segment_candidates,
    capture_language_codes,
    capture_profile_languages,
    claim_next_source_translation,
    claim_next_capture_job,
    create_source_analysis_segment,
    create_source_record,
    db_connect,
    find_source_by_image_sha256,
    match_capture_profile,
    normalize_token,
    read_existing_kanji_match,
    read_existing_term_match,
    recover_stale_capture_jobs,
    resolve_repo_local_path,
    stable_text_hash,
    store_segment_candidates,
    upsert_source_translation,
    update_capture_job_status,
)
from ocr_backends import (
    EasyOcrBackend,
    OcrBlock,
    OcrEnginePlan,
    contains_japanese_chars,
    filter_ocr_line_boxes,
    get_easyocr_backend,
    get_meikiocr_backend,
    group_ocr_blocks_into_regions,
    is_horizontal_bbox,
    merge_engine_blocks,
    ocr_line_boxes_to_blocks,
    plan_ocr_engines,
    recognize_with_shared_detection,
    resolve_capture_ocr_backend,
    scale_ocr_blocks,
    sort_ocr_blocks_reading_order,
)


# The database decides which languages are captured (languages.capture_enabled);
# LANG_CAPTURE_LANGUAGES is only an override for one-off runs. This module-level
# default is what callers without a database connection (tests, diagnostics) get.
DEFAULT_CAPTURE_LANGUAGES = languages.enabled_capture_languages(
    os.environ.get(CAPTURE_LANGUAGES_ENV),
    default=DEFAULT_LANGUAGE_CODE,
)
DEFAULT_CAPTURE_DIR = ROOT / "data" / "captures"
DEFAULT_LOG_PATH = ROOT / "data" / "capture_daemon.log"
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_STALE_CAPTURE_JOB_HOURS = float(
    os.environ.get("LANG_JP_CAPTURE_STALE_JOB_HOURS", "4")
)
DEBUG_ENABLED = os.environ.get("LANG_JP_CAPTURE_DEBUG") == "1"
# LANG_JP_OCR_BACKEND picks the engine (meikiocr by default, easyocr for comparison).
# The downscale, confidence, orientation and region settings below apply to EasyOCR only.
DEFAULT_OCR_MAX_SIDE = int(os.environ.get("LANG_JP_OCR_MAX_SIDE", "1600"))
DEFAULT_OCR_PROGRESS_INTERVAL = float(os.environ.get("LANG_JP_OCR_PROGRESS_INTERVAL", "5"))
DEFAULT_OCR_MIN_CONFIDENCE = float(os.environ.get("LANG_JP_OCR_MIN_CONFIDENCE", "0.30"))
# A box read with at least this confidence by one EasyOCR reader is not sent to the next ones.
DEFAULT_OCR_CONFIDENT_READ = float(os.environ.get("LANG_JP_OCR_CONFIDENT_READ", "0.7"))
DEFAULT_OCR_HORIZONTAL_MIN_RATIO = float(os.environ.get("LANG_JP_OCR_HORIZONTAL_MIN_RATIO", "1.2"))
DEFAULT_OCR_HORIZONTAL_MAX_ANGLE = float(os.environ.get("LANG_JP_OCR_HORIZONTAL_MAX_ANGLE", "18"))
DEFAULT_OCR_REGION_PADDING = int(os.environ.get("LANG_JP_OCR_REGION_PADDING", "12"))
DEFAULT_OCR_MAX_REGIONS = int(os.environ.get("LANG_JP_OCR_MAX_REGIONS", "3"))
# Non-Japanese OCR lines read below this confidence keep their segment but give
# no candidates: on real screens they are mostly garbage ("Lcqallc", "Googic").
DEFAULT_MIN_TERM_CONFIDENCE = float(os.environ.get("LANG_JP_MIN_TERM_CONFIDENCE", "0.5"))
DEFAULT_AUTO_PROMOTE_SOURCE_TERMS = (
    os.environ.get("LANG_JP_AUTO_PROMOTE_SOURCE_TERMS", "1") != "0"
)
DEFAULT_ENABLE_SOURCE_TRANSLATION = (
    os.environ.get("LANG_JP_ENABLE_SOURCE_TRANSLATION", "1") != "0"
)
DEFAULT_SOURCE_TRANSLATION_TARGET = os.environ.get(
    "LANG_JP_SOURCE_TRANSLATION_TARGET", "en"
)
JOB_PROGRESS_BAR_WIDTH = 12
JOB_PROGRESS_PERCENT_BY_STEP_STATUS: dict[tuple[str, str | None], int] = {
    ("start", None): 0,
    ("languages", None): 4,
    ("ocr_input", None): 10,
    ("ocr", "start"): 18,
    ("ocr", "running"): 24,
    ("ocr", "done"): 36,
    ("ocr", "filter"): 42,
    ("ocr", "select"): 48,
    ("ocr_result", None): 56,
    ("image_saved", None): 62,
    ("source_saved", None): 70,
    ("analyze", None): 80,
    ("promote", None): 88,
    ("translate", None): 96,
    ("done", None): 100,
    ("source_complete", None): 100,
}


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


def short_hash(value: str | None, *, length: int = 12) -> str | None:
    if value in (None, ""):
        return None
    return str(value)[:length]


def short_path(value: str | Path | None) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    name = Path(text).name
    return name or text


def format_log_value(key: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, Path):
        return value.name
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    text = str(value)
    if key in {"image", "temp_image", "final_image"} or key.endswith("_path"):
        return short_path(text)
    if key in {"sha", "image_sha256", "ocr_text_hash"}:
        return short_hash(text)
    if any(character.isspace() for character in text):
        return json.dumps(text, ensure_ascii=False)
    return text


def build_log_event(*parts: str, **fields: Any) -> str:
    items = [part for part in parts if part]
    for key, value in fields.items():
        formatted = format_log_value(key, value)
        if formatted is None:
            continue
        items.append(f"{key}={formatted}")
    return " ".join(items)


def job_log_prefix(job_id: int, step: str) -> str:
    return f"job={job_id} step={step}"


def prefix_log_context(context: str | None, event: str) -> str:
    if not context:
        return event
    if context.startswith("job="):
        return f"{context.split(' ', 1)[0]} {event}"
    if context.startswith("diagnose"):
        return f"diagnose {event}"
    return event


def extract_log_field(message: str, field_name: str) -> str | None:
    match = re.search(rf"(?:^|\s){re.escape(field_name)}=([^\s]+)", message)
    if match is None:
        return None
    return match.group(1)


def job_progress_percent(step: str | None, status: str | None = None) -> int | None:
    if not step:
        return None
    percent = JOB_PROGRESS_PERCENT_BY_STEP_STATUS.get((step, status))
    if percent is not None:
        return percent
    return JOB_PROGRESS_PERCENT_BY_STEP_STATUS.get((step, None))


def render_job_progress(percent: int, *, width: int = JOB_PROGRESS_BAR_WIDTH) -> str:
    bounded = max(0, min(100, int(percent)))
    filled = min(width, max(0, round((bounded / 100) * width)))
    return f"[{'#' * filled}{'-' * (width - filled)}]({bounded}%)"


def enrich_job_progress_message(message: str) -> str:
    progress_value = extract_log_field(message, "progress")
    if progress_value is None:
        if extract_log_field(message, "job") is None:
            return message
        step = extract_log_field(message, "step")
        percent = job_progress_percent(step, extract_log_field(message, "status"))
        if percent is None:
            return message
        progress_value = render_job_progress(percent)
    message_without_progress = re.sub(r"(?:^|\s)progress=\S+", "", message, count=1).strip()
    if not message_without_progress:
        return f"progress={progress_value}"
    return f"progress={progress_value} {message_without_progress}"


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


class WorkerJobProbe:
    def __init__(self, job_id: int) -> None:
        self._job_id = job_id
        self._started_at = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started_at) * 1000)

    def elapsed_seconds_label(self) -> str:
        seconds = self.elapsed_ms() / 1000.0
        return f"{seconds:.1f}".replace(".", ",") + "s"

    def mark(
        self,
        step: str,
        *,
        include_memory: bool = False,
        **fields: Any,
    ) -> None:
        elapsed_ms = self.elapsed_ms()
        log_fields: dict[str, Any] = {"t": f"{elapsed_ms}ms", **fields}
        if include_memory:
            rss_mb, private_mb = current_process_memory_mb()
            log_fields["rss"] = format_memory_value(rss_mb)
            log_fields["private"] = format_memory_value(private_mb)
        append_worker_log(build_log_event(job_log_prefix(self._job_id, step), **log_fields))


class GoogleGtxSourceTranslator:
    provider_name = "google_gtx"

    def __init__(self, *, target_language: str = DEFAULT_SOURCE_TRANSLATION_TARGET) -> None:
        self.target_language = target_language

    def translate_lines(self, lines: list[str], *, source_language: str = "ja") -> str:
        translated_lines: list[str] = []
        for normalized_line in split_source_text_lines(lines):
            translated_lines.append(
                self.translate_text(normalized_line, source_language=source_language)
            )
        return "\n".join(line for line in translated_lines if line)

    def translate_text(self, text: str, *, source_language: str = "ja") -> str:
        params = urlencode(
            {
                "client": "gtx",
                "sl": source_language,
                "tl": self.target_language,
                "dt": "t",
                "q": text,
            }
        )
        request = Request(
            f"https://translate.googleapis.com/translate_a/single?{params}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        segments = payload[0] if isinstance(payload, list) and payload else []
        translated = "".join(
            str(segment[0])
            for segment in segments
            if isinstance(segment, list) and segment and segment[0] is not None
        ).strip()
        return translated or text


def split_source_text_lines(chunks: list[str]) -> list[str]:
    lines: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        normalized_chunk = str(chunk).replace("\r\n", "\n").replace("\r", "\n")
        for raw_line in normalized_chunk.split("\n"):
            normalized_line = normalize_token(raw_line)
            if normalized_line:
                lines.append(normalized_line)
    return lines


def contains_japanese_text(value: str | None) -> bool:
    if not value:
        return False
    return contains_japanese_chars(value)


def resolve_capture_languages(connection) -> tuple[str, ...]:
    """Languages the worker keeps right now: env override, else the database flags."""
    return capture_language_codes(connection)


def resolve_job_languages(
    connection,
    *,
    window_title: str | None,
    process_name: str | None,
) -> tuple[tuple[str, ...], Any, bool]:
    """``(languages, profile_row, forced)`` for one capture.

    A matching capture profile pins the languages: one code skips detection
    entirely (``forced``), several restrict it. Otherwise the capture set from
    the database applies and every block is detected against it.
    """
    profile = match_capture_profile(
        connection,
        window_title=window_title,
        process_name=process_name,
    )
    if profile is not None:
        codes = capture_profile_languages(profile)
        if codes:
            return codes, profile, len(codes) == 1
    return resolve_capture_languages(connection), None, False


def contains_target_language_text(
    value: str | None,
    *,
    enabled_languages: tuple[str, ...] = DEFAULT_CAPTURE_LANGUAGES,
) -> bool:
    """True when the text looks like one of the languages enabled for capture.

    With the default (``ja`` only) this is the old Japanese-only check. For
    other sets the script of the text has to match one enabled language, and for
    Latin-script text the detector has to pick an enabled language too.
    """
    if not value or not normalize_token(value):
        return False
    enabled = {languages.base_language_code(code) for code in enabled_languages}
    if enabled == {DEFAULT_LANGUAGE_CODE}:
        return contains_japanese_chars(value)
    histogram = languages.script_histogram(value)
    if not histogram:
        return False
    for code in enabled:
        profile = languages.get_language(code)
        if profile.script == languages.SCRIPT_JAPANESE and (
            histogram.get(languages.SCRIPT_JAPANESE) or histogram.get(languages.SCRIPT_HAN)
        ):
            return True
        if profile.script == languages.SCRIPT_LATIN and histogram.get(languages.SCRIPT_LATIN):
            guess = languages.detect_language(value, candidates=enabled_languages)
            if languages.base_language_code(guess.code) in enabled:
                return True
        elif histogram.get(profile.script):
            return True
    return False


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
                build_log_event(
                    "step=ocr_input",
                    status="kept",
                    size=f"{width}x{height}",
                    limit=max_side,
                ),
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
        build_log_event(
            "step=ocr_input",
            status="resized",
            source_size=f"{width}x{height}",
            size=f"{resized_width}x{resized_height}",
            limit=max_side,
        ),
        temp_path,
    )


def append_worker_log(message: str) -> None:
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    normalized_message = enrich_job_progress_message(message)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with DEFAULT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {normalized_message}\n")


def recover_stale_capture_jobs_on_startup(
    db_path: Path,
    *,
    older_than_hours: float,
) -> int:
    if older_than_hours <= 0:
        return 0

    connection = db_connect(db_path)
    try:
        stale_rows = recover_stale_capture_jobs(
            connection,
            older_than_hours=older_than_hours,
            to_status="failed",
            note=(
                "Worker startup recovered a stale capture job "
                f"after exceeding the {older_than_hours:g}h threshold."
            ),
        )
        if stale_rows:
            connection.commit()
    finally:
        connection.close()

    recovered_count = len(stale_rows)
    if recovered_count:
        preview_ids = ",".join(str(int(row["id"])) for row in stale_rows[:10])
        suffix = ",..." if recovered_count > 10 else ""
        append_worker_log(
            "capture worker recovered stale capture jobs "
            f"count={recovered_count} threshold_hours={older_than_hours:g} "
            f"job_ids={preview_ids}{suffix}"
        )
    return recovered_count


def build_job_log_banner(
    *,
    job_id: int,
    temp_image_path: Path,
    image_sha256: str | None,
    captured_at: str | None,
) -> list[str]:
    return [
        build_log_event(
            job_log_prefix(job_id, "start"),
            image=temp_image_path,
            sha=image_sha256,
            captured=captured_at,
        )
    ]


def append_job_log_banner(
    *,
    job_id: int,
    temp_image_path: Path,
    image_sha256: str | None,
    captured_at: str | None,
) -> None:
    for message in build_job_log_banner(
        job_id=job_id,
        temp_image_path=temp_image_path,
        image_sha256=image_sha256,
        captured_at=captured_at,
    ):
        append_worker_log(message)


def emit_progress_message(message: str, *, echo_stdout: bool = False) -> None:
    append_worker_log(message)
    if echo_stdout:
        print(enrich_job_progress_message(message))


def run_with_progress_logs(
    label: str,
    func,
    *,
    echo_stdout: bool = False,
    interval_seconds: float = DEFAULT_OCR_PROGRESS_INTERVAL,
):
    started_at = time.perf_counter()
    rss_mb, private_mb = current_process_memory_mb()
    emit_progress_message(
        build_log_event(
            label,
            status="start",
            rss=format_memory_value(rss_mb),
            private=format_memory_value(private_mb),
        ),
        echo_stdout=echo_stdout,
    )
    stop_event = threading.Event()

    def reporter() -> None:
        while not stop_event.wait(interval_seconds):
            elapsed_seconds = int(time.perf_counter() - started_at)
            rss_mb, private_mb = current_process_memory_mb()
            emit_progress_message(
                build_log_event(
                    label,
                    status="running",
                    t=f"{elapsed_seconds}s",
                    rss=format_memory_value(rss_mb),
                    private=format_memory_value(private_mb),
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
            build_log_event(
                label,
                status="done",
                t=f"{elapsed_ms}ms",
                rss=format_memory_value(rss_mb),
                private=format_memory_value(private_mb),
            ),
            echo_stdout=echo_stdout,
        )


def filter_easyocr_blocks(
    blocks: list[OcrBlock],
    enabled_languages: tuple[str, ...] = DEFAULT_CAPTURE_LANGUAGES,
) -> tuple[list[OcrBlock], dict[str, int]]:
    stats = {
        "detected": len(blocks),
        "kept": 0,
        "empty": 0,
        "off_language": 0,
        "vertical": 0,
        "low_confidence": 0,
        "missing_bbox": 0,
    }
    kept: list[OcrBlock] = []
    for block in blocks:
        text = normalize_token(block.text)
        if not text:
            stats["empty"] += 1
            continue
        if block.confidence is not None and block.confidence < DEFAULT_OCR_MIN_CONFIDENCE:
            stats["low_confidence"] += 1
            continue
        if not contains_target_language_text(text, enabled_languages=enabled_languages):
            stats["off_language"] += 1
            continue
        if block.bbox is None:
            stats["missing_bbox"] += 1
            continue
        if not is_horizontal_bbox(
            block.bbox,
            min_ratio=DEFAULT_OCR_HORIZONTAL_MIN_RATIO,
            max_abs_angle_degrees=DEFAULT_OCR_HORIZONTAL_MAX_ANGLE,
        ):
            stats["vertical"] += 1
            continue
        kept.append(
            OcrBlock(
                text=text,
                confidence=block.confidence,
                bbox=block.bbox,
            )
        )
    kept = sort_ocr_blocks_reading_order(kept)
    stats["kept"] = len(kept)
    return kept, stats


def rect_to_bbox(left: int, top: int, right: int, bottom: int) -> list[list[float]]:
    return [
        [float(left), float(top)],
        [float(right), float(top)],
        [float(right), float(bottom)],
        [float(left), float(bottom)],
    ]


def crop_image_region(
    image,
    *,
    left: int,
    top: int,
    right: int,
    bottom: int,
    padding: int = DEFAULT_OCR_REGION_PADDING,
):
    width, height = image.size
    return image.crop(
        (
            max(0, left - padding),
            max(0, top - padding),
            min(width, right + padding),
            min(height, bottom + padding),
        )
    )


def count_text_letters(value: str) -> int:
    """Letters of any script (kana, kanji, Latin, Hangul...): what makes a region worth reading."""
    return sum(languages.script_histogram(value).values())


# Backwards-compatible name from the Japanese-only worker.
count_japanese_characters = count_text_letters


def score_region(
    *,
    text: str,
    confidence: float | None,
    left: int,
    top: int,
    right: int,
    bottom: int,
    image_width: int,
    image_height: int,
    blocks_in_region: int,
) -> float:
    japanese_chars = count_text_letters(text)
    line_count = len([line for line in text.splitlines() if line.strip()])
    region_width = max(1, right - left)
    region_height = max(1, bottom - top)
    area_ratio = (region_width * region_height) / max(1.0, float(image_width * image_height))
    center_x = (left + right) / 2.0
    screen_center_x = image_width / 2.0
    center_bias = max(0.0, 1.0 - (abs(center_x - screen_center_x) / max(1.0, screen_center_x)))
    confidence_value = confidence if confidence is not None else 0.0
    top_penalty = 0.0
    if top < image_height * 0.18 and japanese_chars <= 4:
        top_penalty = 6.0
    edge_penalty = 0.0
    if center_bias < 0.25:
        edge_penalty = 4.0
    return (
        (japanese_chars * 4.0)
        + (line_count * 3.5)
        + (blocks_in_region * 2.0)
        + (confidence_value * 8.0)
        + (center_bias * 6.0)
        + (area_ratio * 120.0)
        - top_penalty
        - edge_penalty
    )


class EasyOcrAdapter:
    """Legacy single-engine EasyOCR flow (``LANG_JP_OCR_BACKEND=easyocr``).

    Reads the downscaled capture, groups boxes into regions, re-reads the best
    regions and keeps the top ones. Languages come from ``LANG_JP_EASYOCR_LANGS``.
    """

    downscales_input = True

    def __init__(
        self,
        *,
        enabled_languages: tuple[str, ...] = DEFAULT_CAPTURE_LANGUAGES,
    ) -> None:
        self.enabled_languages = tuple(enabled_languages) or (DEFAULT_LANGUAGE_CODE,)
        self._engine = EasyOcrBackend()
        append_worker_log(
            build_log_event(
                "worker",
                step="ocr_init",
                engine="easyocr",
                gpu=getattr(self._engine, "gpu_enabled", None),
                gpu_mode=getattr(self._engine, "gpu_mode", None),
                languages=",".join(getattr(self._engine, "languages", ())),
                capture_languages=",".join(self.enabled_languages),
                min_conf=DEFAULT_OCR_MIN_CONFIDENCE,
                min_ratio=DEFAULT_OCR_HORIZONTAL_MIN_RATIO,
                max_angle=DEFAULT_OCR_HORIZONTAL_MAX_ANGLE,
                max_regions=DEFAULT_OCR_MAX_REGIONS,
            )
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
                append_worker_log(prefix_log_context(progress_label, note))

        def do_read() -> list[OcrBlock]:
            return self._engine.read_blocks(image_path)

        label = progress_label or build_log_event("step=ocr", image=image_path)
        try:
            detected_blocks = run_with_progress_logs(label, do_read, echo_stdout=echo_stdout)
            kept_blocks, stats = filter_easyocr_blocks(detected_blocks, self.enabled_languages)
            grouped_regions = group_ocr_blocks_into_regions(kept_blocks)
            append_worker_log(
                build_log_event(
                    label,
                    status="filter",
                    boxes=stats["detected"],
                    kept=stats["kept"],
                    regions=len(grouped_regions),
                    drop_off_lang=stats["off_language"],
                    drop_vertical=stats["vertical"],
                    drop_low_conf=stats["low_confidence"],
                    drop_no_bbox=stats["missing_bbox"],
                    drop_empty=stats["empty"],
                )
            )
            if not grouped_regions:
                return kept_blocks

            try:
                from PIL import Image
            except ImportError as exc:
                raise SystemExit("Pillow is required to crop grouped OCR regions.") from exc

            final_blocks: list[OcrBlock] = []
            with Image.open(image_path) as image_file:
                prepared_image = image_file.convert("RGB")
                image_width, image_height = prepared_image.size
                scored_regions: list[tuple[float, int, OcrBlock]] = []
                for region_index, region in enumerate(grouped_regions, start=1):
                    crop = crop_image_region(
                        prepared_image,
                        left=region.left,
                        top=region.top,
                        right=region.right,
                        bottom=region.bottom,
                    )
                    rerun_blocks = self._engine.read_blocks(crop)
                    rerun_kept_blocks, rerun_stats = filter_easyocr_blocks(
                        rerun_blocks, self.enabled_languages
                    )
                    region_text = "\n".join(block.text for block in rerun_kept_blocks if block.text)
                    region_confidence: float | None = None
                    if rerun_kept_blocks:
                        confidences = [
                            block.confidence for block in rerun_kept_blocks if block.confidence is not None
                        ]
                        if confidences:
                            region_confidence = sum(confidences) / len(confidences)
                    if not region_text:
                        region_text = "\n".join(block.text for block in region.blocks if block.text)
                        confidences = [
                            block.confidence for block in region.blocks if block.confidence is not None
                        ]
                        if confidences:
                            region_confidence = sum(confidences) / len(confidences)
                    region_text = region_text.strip()
                    if not region_text:
                        continue
                    region_score = score_region(
                        text=region_text,
                        confidence=region_confidence,
                        left=region.left,
                        top=region.top,
                        right=region.right,
                        bottom=region.bottom,
                        image_width=image_width,
                        image_height=image_height,
                        blocks_in_region=len(region.blocks),
                    )
                    if DEBUG_ENABLED:
                        append_worker_log(
                            build_log_event(
                                label,
                                status="region",
                                region=region_index,
                                blocks=len(region.blocks),
                                rerun_detected=rerun_stats["detected"],
                                rerun_kept=rerun_stats["kept"],
                                bounds=f"{region.left},{region.top},{region.right},{region.bottom}",
                                score=region_score,
                            )
                        )
                    scored_regions.append(
                        (
                            region_score,
                            region_index,
                            OcrBlock(
                                text=region_text,
                                confidence=region_confidence,
                                bbox=rect_to_bbox(region.left, region.top, region.right, region.bottom),
                            ),
                        )
                    )
                scored_regions.sort(key=lambda item: item[0], reverse=True)
                if DEFAULT_OCR_MAX_REGIONS > 0:
                    scored_regions = scored_regions[:DEFAULT_OCR_MAX_REGIONS]
                final_blocks = sort_ocr_blocks_reading_order([item[2] for item in scored_regions])
            append_worker_log(
                build_log_event(
                    label,
                    status="select",
                    regions=len(final_blocks),
                    limit=DEFAULT_OCR_MAX_REGIONS,
                    scores=",".join(f"{item[0]:.2f}" for item in scored_regions),
                )
            )
            return final_blocks
        finally:
            if cleanup_path is not None:
                delete_if_exists(cleanup_path)


class MeikiOcrAdapter:
    """The Japanese pass: meikiocr on the full-resolution capture with the Japanese filters.

    meikiocr is Japanese-only, so this adapter is exactly the pre-polyglot flow
    and is reused unchanged inside :class:`MultiEngineOcrAdapter`.
    """

    # One pass on the full-resolution capture; meikiocr resizes internally for detection.
    downscales_input = False
    engine_name = "meikiocr"

    def __init__(self) -> None:
        self._engine = get_meikiocr_backend()
        append_worker_log(
            build_log_event(
                "worker",
                step="ocr_init",
                engine="meikiocr",
                provider=self._engine.provider,
                min_char_conf=self._engine.min_char_confidence,
            )
        )

    def extract_blocks(
        self,
        image_path: Path,
        *,
        prepared: bool = False,
        progress_label: str | None = None,
        echo_stdout: bool = False,
    ) -> list[OcrBlock]:
        # `prepared` is accepted for parity with EasyOcrAdapter; there is no input to prepare.
        def do_read():
            return self._engine.read_line_boxes(image_path)

        label = progress_label or build_log_event("step=ocr", image=image_path)
        line_boxes = run_with_progress_logs(label, do_read, echo_stdout=echo_stdout)
        kept_boxes, stats = filter_ocr_line_boxes(line_boxes, language_codes=("ja",))
        blocks = [
            OcrBlock(text=normalize_token(block.text), confidence=block.confidence, bbox=block.bbox)
            for block in ocr_line_boxes_to_blocks(kept_boxes)
        ]
        append_worker_log(
            build_log_event(
                label,
                status="filter",
                engine="meikiocr",
                boxes=stats["detected"],
                kept=stats["kept"],
                rows=len(blocks),
                drop_off_lang=stats["off_language"],
                drop_contained=stats["contained"],
                drop_furigana=stats["furigana"],
            )
        )
        return blocks


def downscale_for_easyocr(image, *, max_side: int = DEFAULT_OCR_MAX_SIDE):
    """``(image, factor)``: the image EasyOCR reads and how to map its boxes back."""
    from PIL import Image

    width, height = image.size
    longest = max(width, height)
    if max_side <= 0 or longest <= max_side:
        return image, 1.0
    scale = max_side / float(longest)
    resampling = getattr(Image, "Resampling", Image)
    resized = image.resize(
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        resampling.LANCZOS,
    )
    return resized, 1.0 / scale


class EasyOcrGroupAdapter:
    """One EasyOCR reader for one compatible language group, single full-image pass.

    Used for the non-Japanese languages next to meikiocr. Boxes come back in the
    capture's full-resolution coordinates so they can be merged with meikiocr's.
    """

    downscales_input = False

    def __init__(
        self,
        group: tuple[str, ...],
        *,
        enabled_languages: tuple[str, ...] = DEFAULT_CAPTURE_LANGUAGES,
    ) -> None:
        self.group = tuple(group)
        self.enabled_languages = tuple(enabled_languages) or (DEFAULT_LANGUAGE_CODE,)
        self.engine_name = "easyocr:" + "+".join(self.group)
        self._engine = get_easyocr_backend(self.group)
        append_worker_log(
            build_log_event(
                "worker",
                step="ocr_init",
                engine=self.engine_name,
                gpu=getattr(self._engine, "gpu_enabled", None),
                capture_languages=",".join(self.enabled_languages),
                min_conf=DEFAULT_OCR_MIN_CONFIDENCE,
                max_side=DEFAULT_OCR_MAX_SIDE,
            )
        )

    def extract_blocks(
        self,
        image_path: Path,
        *,
        prepared: bool = False,
        progress_label: str | None = None,
        echo_stdout: bool = False,
    ) -> list[OcrBlock]:
        try:
            from PIL import Image
        except ImportError as exc:
            raise SystemExit("Pillow is required to prepare OCR input images.") from exc

        label = progress_label or build_log_event("step=ocr", image=image_path)

        def do_read() -> tuple[list[OcrBlock], float]:
            with Image.open(image_path) as image_file:
                rgb = image_file.convert("RGB")
            target, factor = downscale_for_easyocr(rgb)
            return self._engine.read_blocks(target), factor

        detected_blocks, factor = run_with_progress_logs(
            f"{label} engine={self.engine_name}", do_read, echo_stdout=echo_stdout
        )
        kept_blocks, stats = filter_easyocr_blocks(detected_blocks, self.enabled_languages)
        blocks = scale_ocr_blocks(kept_blocks, factor)
        append_worker_log(
            build_log_event(
                label,
                status="filter",
                engine=self.engine_name,
                boxes=stats["detected"],
                kept=stats["kept"],
                drop_off_lang=stats["off_language"],
                drop_vertical=stats["vertical"],
                drop_low_conf=stats["low_confidence"],
                drop_no_bbox=stats["missing_bbox"],
                drop_empty=stats["empty"],
            )
        )
        return blocks


class MultiEngineOcrAdapter:
    """meikiocr for Japanese plus one EasyOCR reader per language group, merged.

    Every engine reads the whole capture; blocks are merged by bounding-box
    overlap and text similarity, earlier engines (meikiocr first) winning ties.
    """

    downscales_input = False

    def __init__(
        self,
        plan: OcrEnginePlan,
        *,
        enabled_languages: tuple[str, ...] | None = None,
    ) -> None:
        self.plan = plan
        self.enabled_languages = tuple(enabled_languages or plan.language_codes)
        self.engines: list[MeikiOcrAdapter | EasyOcrGroupAdapter] = []
        if plan.use_meikiocr:
            self.engines.append(MeikiOcrAdapter())
        for group in plan.easyocr_groups:
            self.engines.append(
                EasyOcrGroupAdapter(group, enabled_languages=self.enabled_languages)
            )
        self.engine_name = "+".join(engine.engine_name for engine in self.engines) or "none"
        append_worker_log(
            build_log_event(
                "worker",
                step="ocr_plan",
                backend=plan.backend,
                languages=",".join(plan.language_codes),
                engines=",".join(plan.engine_names) or "none",
                no_engine_for=",".join(plan.unsupported) or None,
            )
        )

    @property
    def easyocr_engines(self) -> list[EasyOcrGroupAdapter]:
        return [engine for engine in self.engines if isinstance(engine, EasyOcrGroupAdapter)]

    def _read_easyocr_groups(
        self,
        image_path: Path,
        *,
        label: str,
        echo_stdout: bool,
    ) -> list[OcrBlock]:
        """All EasyOCR readers over one shared detection pass (see ``recognize_with_shared_detection``)."""
        engines = self.easyocr_engines
        if not engines:
            return []
        try:
            from PIL import Image
        except ImportError as exc:
            raise SystemExit("Pillow is required to prepare OCR input images.") from exc

        def do_read() -> tuple[list[OcrBlock], float]:
            with Image.open(image_path) as image_file:
                rgb = image_file.convert("RGB")
            target, factor = downscale_for_easyocr(rgb)
            color, grey = EasyOcrBackend.prepare_arrays(target)

            def on_pass(backend, sent: int, improved: int) -> None:
                append_worker_log(
                    build_log_event(
                        label,
                        status="recognize",
                        engine="easyocr:" + "+".join(getattr(backend, "languages", ())),
                        boxes=sent,
                        improved=improved,
                    )
                )

            blocks = recognize_with_shared_detection(
                [engine._engine for engine in engines],
                color,
                grey,
                confident=DEFAULT_OCR_CONFIDENT_READ,
                on_pass=on_pass,
            )
            return blocks, factor

        engine_label = "easyocr:" + "|".join("+".join(engine.group) for engine in engines)
        detected_blocks, factor = run_with_progress_logs(
            f"{label} engine={engine_label}", do_read, echo_stdout=echo_stdout
        )
        kept_blocks, stats = filter_easyocr_blocks(detected_blocks, self.enabled_languages)
        append_worker_log(
            build_log_event(
                label,
                status="filter",
                engine=engine_label,
                boxes=stats["detected"],
                kept=stats["kept"],
                drop_off_lang=stats["off_language"],
                drop_vertical=stats["vertical"],
                drop_low_conf=stats["low_confidence"],
                drop_no_bbox=stats["missing_bbox"],
                drop_empty=stats["empty"],
            )
        )
        return scale_ocr_blocks(kept_blocks, factor)

    def extract_blocks(
        self,
        image_path: Path,
        *,
        prepared: bool = False,
        progress_label: str | None = None,
        echo_stdout: bool = False,
    ) -> list[OcrBlock]:
        label = progress_label or build_log_event("step=ocr", image=image_path)
        groups: list[list[OcrBlock]] = []
        names: list[str] = []
        for engine in self.engines:
            # EasyOCR groups share one detection pass below; every other engine
            # (meikiocr) reads the capture on its own.
            if not isinstance(engine, EasyOcrGroupAdapter):
                groups.append(
                    engine.extract_blocks(
                        image_path,
                        prepared=prepared,
                        progress_label=progress_label,
                        echo_stdout=echo_stdout,
                    )
                )
                names.append(getattr(engine, "engine_name", type(engine).__name__))
        if self.easyocr_engines:
            groups.append(self._read_easyocr_groups(image_path, label=label, echo_stdout=echo_stdout))
            names.append("easyocr")
        merged = merge_engine_blocks(groups)
        append_worker_log(
            build_log_event(
                label,
                status="merge",
                engines=",".join(names),
                per_engine=",".join(str(len(group)) for group in groups),
                merged=len(merged),
            )
        )
        return merged


CaptureOcrAdapter = EasyOcrAdapter | MeikiOcrAdapter | EasyOcrGroupAdapter | MultiEngineOcrAdapter


def build_capture_ocr_adapter(
    name: str | None = None,
    *,
    enabled_languages: tuple[str, ...] | None = None,
) -> CaptureOcrAdapter:
    """OCR engines for the enabled languages (plan item C4).

    ``auto`` (default): meikiocr alone for a Japanese-only set, otherwise
    meikiocr plus one EasyOCR reader per compatible language group, merged.
    ``meikiocr`` / ``easyocr`` force the old single-engine flows.
    """
    codes = tuple(enabled_languages or DEFAULT_CAPTURE_LANGUAGES)
    backend = resolve_capture_ocr_backend(name)
    if backend == "easyocr":
        return EasyOcrAdapter(enabled_languages=codes)
    plan = plan_ocr_engines(codes, backend=backend)
    if plan.use_meikiocr and not plan.easyocr_groups:
        return MeikiOcrAdapter()
    return MultiEngineOcrAdapter(plan, enabled_languages=codes)


class LanguageSegmentAnalyzer:
    """Turn OCR blocks into draft segments and candidates, one language at a time.

    Each block is split into script runs, every run is detected against the
    languages enabled for capture, and the matching analyzer (Sudachi for
    Japanese, the generic tokenizer for word-delimited languages, ...) produces
    the candidates. Blocks therefore can yield more than one segment when they
    mix languages.
    """

    def __init__(
        self,
        enabled_languages: tuple[str, ...] = DEFAULT_CAPTURE_LANGUAGES,
        *,
        forced_language: str | None = None,
    ) -> None:
        self.enabled_languages = tuple(enabled_languages) or (DEFAULT_LANGUAGE_CODE,)
        # A capture profile with a single language pins every block to it (no detection).
        self.forced_language = languages.normalize_language_code(forced_language) or None
        # Fail early when an enabled language needs a dependency that is missing.
        for code in self.enabled_languages:
            try:
                languages.get_analyzer(code)
            except languages.AnalyzerUnavailable as exc:
                raise SystemExit(str(exc)) from exc
        append_worker_log(
            build_log_event(
                "worker",
                step="analyzer_init",
                languages=",".join(self.enabled_languages),
                forced=self.forced_language,
            )
        )

    def analyze_blocks(
        self,
        connection,
        blocks: list[OcrBlock],
    ) -> list[list[tuple[dict[str, Any], list[dict[str, Any]]]]]:
        """Analyze a whole screen: its Latin text as a whole settles the short strings."""
        prior = None
        if self.forced_language is None:
            prior = languages.latin_language_prior(block.text for block in blocks)
        return [self.analyze_block(connection, block, prior=prior) for block in blocks]

    def analyze_block(
        self,
        connection,
        block: OcrBlock,
        *,
        prior: str | None = None,
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        normalized_text = normalize_token(block.text)
        if not normalized_text:
            return []
        runs = languages.analyze_text(
            normalized_text,
            language=self.forced_language or languages.AUTO_LANGUAGE_CODE,
            candidates=self.enabled_languages,
            prior=prior,
        )
        if not runs:
            # Nothing matched an enabled language; keep the raw segment so the
            # reviewer still sees the text, tagged with the language it looks
            # like (unrestricted detection) and without candidates.
            guess = languages.detect_language(
                normalized_text, default=self.enabled_languages[0], prior=prior
            )
            payload, candidates = build_segment_candidates(
                connection,
                text=normalized_text,
                language_code=guess.code,
                tokens=[],
                confidence=block.confidence,
                bbox_json=bbox_json(block.bbox),
            )
            return [(payload, candidates)]
        results: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for run in runs:
            run_text = normalize_token(run.text)
            if not run_text:
                continue
            tokens = run.tokens
            if (
                block.confidence is not None
                and block.confidence < DEFAULT_MIN_TERM_CONFIDENCE
                and languages.base_language_code(run.language_code) != DEFAULT_LANGUAGE_CODE
            ):
                tokens = []
            payload, candidates = build_segment_candidates(
                connection,
                text=run_text,
                language_code=run.language_code,
                tokens=tokens,
                confidence=block.confidence,
                bbox_json=bbox_json(block.bbox),
            )
            results.append((payload, candidates))
        return results

    def analyze_segment(self, connection, block: OcrBlock) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Single-segment view of :meth:`analyze_block` (first language run)."""
        results = self.analyze_block(connection, block)
        if not results:
            return (
                {
                    "raw_text": block.text,
                    "normalized_text": normalize_token(block.text),
                    "language_code": self.enabled_languages[0],
                    "reading": None,
                    "confidence": block.confidence,
                    "bbox_json": bbox_json(block.bbox),
                },
                [],
            )
        return results[0]


# Backwards-compatible name: the worker used to be Japanese-only.
SudachiAnalyzer = LanguageSegmentAnalyzer


class CapturePipeline:
    """OCR adapters and analyzers per language set, built once and reused.

    The capture set can change between jobs (tray menu, web UI, CLI, capture
    profiles), so each job asks for the adapter matching its languages. Readers
    are cached inside ``ocr_backends`` as well, so switching sets never reloads
    a model that is already in memory.
    """

    def __init__(self, *, backend: str | None = None) -> None:
        self.backend = backend
        self._adapters: dict[tuple[str, ...], CaptureOcrAdapter] = {}
        self._analyzers: dict[tuple[tuple[str, ...], str | None], LanguageSegmentAnalyzer] = {}

    def ocr_for(self, codes: tuple[str, ...]) -> CaptureOcrAdapter:
        key = tuple(codes) or (DEFAULT_LANGUAGE_CODE,)
        adapter = self._adapters.get(key)
        if adapter is None:
            adapter = build_capture_ocr_adapter(self.backend, enabled_languages=key)
            self._adapters[key] = adapter
        return adapter

    def analyzer_for(
        self,
        codes: tuple[str, ...],
        *,
        forced_language: str | None = None,
    ) -> LanguageSegmentAnalyzer:
        key = (tuple(codes) or (DEFAULT_LANGUAGE_CODE,), forced_language)
        analyzer = self._analyzers.get(key)
        if analyzer is None:
            analyzer = LanguageSegmentAnalyzer(key[0], forced_language=forced_language)
            self._analyzers[key] = analyzer
        return analyzer

    def warm_up(self, db_path: Path) -> tuple[str, ...]:
        """Load the engines for the current capture set so the first job is not slow."""
        connection = db_connect(db_path)
        try:
            codes = resolve_capture_languages(connection)
        finally:
            connection.close()
        self.ocr_for(codes)
        self.analyzer_for(codes)
        return codes


def dominant_block_language(
    analyzed_blocks: list[list[tuple[dict[str, Any], list[dict[str, Any]]]]],
    *,
    default: str = DEFAULT_LANGUAGE_CODE,
) -> tuple[str, float | None]:
    """Pick the language covering most text across all analyzed segments.

    Characters are weighted by how much text they stand for: a kanji or a
    Hangul syllable block carries a whole syllable or word, a Latin letter does
    not, so "Press START to continue" does not outweigh a ten-character
    Japanese sentence on the same screen.
    """
    weights: dict[str, float] = {}
    total = 0.0
    for block_results in analyzed_blocks:
        for payload, _ in block_results:
            code = str(payload.get("language_code") or default)
            weight = weighted_text_length(str(payload.get("raw_text") or ""))
            weights[code] = weights.get(code, 0.0) + weight
            total += weight
    if not weights or total == 0:
        return default, None
    code, weight = max(weights.items(), key=lambda item: (item[1], item[0] == default, item[0]))
    return code, round(weight / total, 3)


SCRIPT_TEXT_WEIGHTS: dict[str, float] = {
    languages.SCRIPT_JAPANESE: 2.5,
    languages.SCRIPT_HAN: 2.5,
    languages.SCRIPT_HANGUL: 2.0,
}


def weighted_text_length(text: str) -> float:
    return sum(
        count * SCRIPT_TEXT_WEIGHTS.get(script, 1.0)
        for script, count in languages.script_histogram(text).items()
    )


def job_field(job, name: str) -> str | None:
    """Column that may be missing on databases created before the column existed."""
    try:
        keys = job.keys()
    except AttributeError:
        return None
    if name not in keys:
        return None
    value = job[name]
    return str(value) if value not in (None, "") else None


def blocks_with_text(blocks: list[OcrBlock]) -> tuple[list[OcrBlock], str]:
    """Drop empty OCR blocks and join the rest into the source raw_text."""
    normalized_blocks = [block for block in blocks if normalize_token(block.text)]
    return normalized_blocks, "\n".join(block.text for block in normalized_blocks)


def store_source_analysis(
    connection,
    *,
    source_id: int,
    analyzed_blocks: list[list[tuple[dict[str, Any], list[dict[str, Any]]]]],
) -> tuple[int, int]:
    """Store the language-tagged segments and candidates of a source's analyzed blocks."""
    segment_count = 0
    candidate_count = 0
    for block_results in analyzed_blocks:
        for segment_payload, candidates in block_results:
            segment_id = create_source_analysis_segment(
                connection,
                source_id=source_id,
                segment_index=segment_count,
                raw_text=segment_payload["raw_text"],
                normalized_text=segment_payload["normalized_text"],
                reading=segment_payload["reading"],
                confidence=segment_payload["confidence"],
                bbox_json=segment_payload["bbox_json"],
                language_code=segment_payload["language_code"],
            )
            segment_count += 1
            candidate_count += store_segment_candidates(
                connection,
                segment_id=segment_id,
                candidates=candidates,
            )
    return segment_count, candidate_count


def auto_promote_source_terms(connection, source_id: int) -> tuple[int, int]:
    """Promote a new source's candidates without marking it reviewed (LANG_JP_AUTO_PROMOTE_SOURCE_TERMS)."""
    if not DEFAULT_AUTO_PROMOTE_SOURCE_TERMS:
        return 0, 0
    return approve_source_analysis_candidates(
        connection,
        source_id=source_id,
        mark_reviewed=False,
    )


def process_job_once(
    db_path: Path,
    *,
    ocr: CaptureOcrAdapter | None = None,
    analyzer: LanguageSegmentAnalyzer | None = None,
    translator: GoogleGtxSourceTranslator | None,
    pipeline: CapturePipeline | None = None,
) -> bool:
    """Process one capture job.

    With a ``pipeline`` the languages come from the job's capture profile or the
    database capture set and the matching OCR adapter and analyzer are picked per
    job. Explicit ``ocr``/``analyzer`` arguments pin them instead (tests, forced
    single-engine runs).
    """
    connection = db_connect(db_path)
    try:
        job = claim_next_capture_job(connection)
        window_title = job_field(job, "window_title") if job is not None else None
        process_name = job_field(job, "process_name") if job is not None else None
        enabled_languages: tuple[str, ...]
        capture_profile = None
        forced_language: str | None = None
        if job is not None:
            if analyzer is not None and ocr is not None:
                enabled_languages = getattr(analyzer, "enabled_languages", DEFAULT_CAPTURE_LANGUAGES)
                capture_profile = match_capture_profile(
                    connection, window_title=window_title, process_name=process_name
                )
            else:
                enabled_languages, capture_profile, forced = resolve_job_languages(
                    connection, window_title=window_title, process_name=process_name
                )
                forced_language = enabled_languages[0] if forced else None
    finally:
        connection.close()
    if job is None:
        return False

    if ocr is None or analyzer is None:
        active_pipeline = pipeline or CapturePipeline()
        if ocr is None:
            ocr = active_pipeline.ocr_for(enabled_languages)
        if analyzer is None:
            analyzer = active_pipeline.analyzer_for(
                enabled_languages, forced_language=forced_language
            )

    job_id = int(job["id"])
    probe = WorkerJobProbe(job_id)
    temp_image_path = resolve_repo_local_path(job["temp_image_path"])
    if temp_image_path is None:
        temp_image_path = Path(str(job["temp_image_path"]))
    image_sha256 = job["image_sha256"]
    captured_at = job["captured_at"]
    capture_profile_id = int(capture_profile["id"]) if capture_profile is not None else None
    capture_profile_name = str(capture_profile["name"]) if capture_profile is not None else None
    append_job_log_banner(
        job_id=job_id,
        temp_image_path=temp_image_path,
        image_sha256=image_sha256,
        captured_at=captured_at,
    )
    probe.mark(
        "languages",
        languages=",".join(enabled_languages),
        profile=capture_profile_name,
        forced=forced_language,
        window=window_title,
        process=process_name,
    )
    if not temp_image_path.exists():
        connection = db_connect(db_path)
        try:
            update_capture_job_status(
                connection,
                job_id=job_id,
                status="rejected",
                last_error=f"Temp image does not exist: {temp_image_path}",
            )
            connection.commit()
        finally:
            connection.close()
        probe.mark(
            "done",
            include_memory=True,
            status="rejected",
            reason="temp_image_missing",
            image=temp_image_path,
        )
        return True

    try:
        connection = db_connect(db_path)
        try:
            duplicate_source = find_source_by_image_sha256(connection, image_sha256)
            if duplicate_source is not None:
                update_capture_job_status(
                    connection,
                    job_id=job_id,
                    status="rejected",
                    last_error=f"Duplicate source #{duplicate_source['id']} for image hash.",
                )
                connection.commit()
                delete_if_exists(temp_image_path)
                probe.mark(
                    "done",
                    include_memory=True,
                    status="rejected",
                    reason="duplicate_source",
                    duplicate_source_id=duplicate_source["id"],
                )
                return True
        finally:
            connection.close()

        blocks = ocr.extract_blocks(
            temp_image_path,
            progress_label=job_log_prefix(job_id, "ocr"),
        )
        normalized_blocks, raw_text = blocks_with_text(blocks)
        ocr_text_hash = stable_text_hash(raw_text)
        probe.mark(
            "ocr_result",
            include_memory=True,
            blocks=len(blocks),
            kept=len(normalized_blocks),
            text_len=len(raw_text),
        )
        target_found = any(
            contains_target_language_text(block.text, enabled_languages=enabled_languages)
            for block in normalized_blocks
        )
        analyzed_blocks: list[list[tuple[dict[str, Any], list[dict[str, Any]]]]] = []
        if target_found:
            connection = db_connect(db_path)
            try:
                analyzed_blocks = analyzer.analyze_blocks(connection, normalized_blocks)
            finally:
                connection.close()
            # Text that passed the quick check can still turn out to be in a language
            # that is not captured (a Portuguese screen with Portuguese off) or too
            # noisy to give a single candidate.
            target_found = any(
                candidates for block_results in analyzed_blocks for _, candidates in block_results
            )
        if not target_found:
            connection = db_connect(db_path)
            try:
                update_capture_job_status(
                    connection,
                    job_id=job_id,
                    status="rejected",
                    last_error=(
                        "No text in the enabled capture languages "
                        f"({', '.join(enabled_languages)}) detected in OCR output."
                    ),
                    raw_text=raw_text,
                    ocr_text_hash=ocr_text_hash,
                )
                connection.commit()
            finally:
                connection.close()
            delete_if_exists(temp_image_path)
            probe.mark(
                "done",
                include_memory=True,
                status="rejected",
                reason="no_target_language_text",
                blocks=len(normalized_blocks),
                text_len=len(raw_text),
            )
            return True

        final_path = build_capture_path(captured_at, image_sha256)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            final_path = final_path.with_name(
                f"{final_path.stem}-{int(time.time())}{final_path.suffix}"
            )
        shutil.move(str(temp_image_path), final_path)
        probe.mark("image_saved", image=final_path)

        connection = db_connect(db_path)
        try:
            # The source is filed under the captured language that covers most of it,
            # not under the language of lines that were kept only as context.
            source_language, source_language_confidence = dominant_block_language(
                [
                    [(payload, candidates) for payload, candidates in block_results if candidates]
                    for block_results in analyzed_blocks
                ],
                default=enabled_languages[0],
            )
            source_id = create_source_record(
                connection,
                source_type="image",
                language_code=source_language,
                language_confidence=source_language_confidence,
                ingest_mode="auto_capture",
                review_status="pending",
                title=window_title,
                game=capture_profile_name,
                source_path=str(final_path),
                raw_text=raw_text,
                notes="capture_worker_auto_ingest",
                captured_at=captured_at,
                image_sha256=image_sha256,
                ocr_text_hash=ocr_text_hash,
                capture_job_id=job_id,
            )
            probe.mark(
                "source_saved",
                source_id=source_id,
                text_len=len(raw_text),
                blocks=len(normalized_blocks),
            )
            segment_count, candidate_count = store_source_analysis(
                connection,
                source_id=source_id,
                analyzed_blocks=analyzed_blocks,
            )
            segment_languages = {
                str(payload["language_code"])
                for block_results in analyzed_blocks
                for payload, _ in block_results
            }
            probe.mark(
                "analyze",
                segments=segment_count,
                candidates=candidate_count,
                language=source_language,
                languages=",".join(sorted(segment_languages)) or source_language,
                profile=capture_profile_name,
            )
            promoted_terms, promoted_candidates = auto_promote_source_terms(
                connection,
                source_id,
            )
            probe.mark(
                "promote",
                terms=promoted_terms,
                candidates=promoted_candidates,
            )
            update_capture_job_status(
                connection,
                job_id=job_id,
                status="accepted",
                final_image_path=str(final_path),
                raw_text=raw_text,
                ocr_text_hash=ocr_text_hash,
                capture_profile_id=capture_profile_id,
            )
            connection.commit()
        finally:
            connection.close()
        if translator is not None and raw_text:
            connection = db_connect(db_path)
            try:
                upsert_source_translation(
                    connection,
                    source_id=source_id,
                    source_text_hash=ocr_text_hash,
                    target_language=translator.target_language,
                    provider=translator.provider_name,
                    status="pending",
                    translated_text=None,
                    error_message=None,
                )
                connection.commit()
            finally:
                connection.close()
            probe.mark(
                "translate",
                status="queued",
                provider=translator.provider_name,
                lang=translator.target_language,
            )
        probe.mark(
            "done",
            include_memory=True,
            status="accepted",
            source_id=source_id,
            image=final_path,
            terms=promoted_terms,
            candidates=promoted_candidates,
        )
        probe.mark(
            "source_complete",
            status="saved",
            total=probe.elapsed_seconds_label(),
            source_id=source_id,
            image=final_path,
        )
    except Exception as exc:
        connection = db_connect(db_path)
        try:
            update_capture_job_status(
                connection,
                job_id=job_id,
                status="failed",
                last_error=str(exc),
            )
            connection.commit()
        finally:
            connection.close()
        probe.mark("done", include_memory=True, status="failed", error=exc)
        return True

    return True


def process_pending_translation_once(
    db_path: Path,
    *,
    translator: GoogleGtxSourceTranslator | None,
) -> bool:
    if translator is None:
        return False

    connection = db_connect(db_path)
    try:
        row = claim_next_source_translation(
            connection,
            target_language=translator.target_language,
            provider=translator.provider_name,
        )
        job = dict(row) if row is not None else None
    finally:
        connection.close()
    if job is None:
        return False

    source_id = int(job["source_id"])
    current_source_text_hash = job["current_source_text_hash"] or job["source_text_hash"]
    raw_text = str(job["raw_text"] or "")
    lines = split_source_text_lines([raw_text])
    if not lines:
        connection = db_connect(db_path)
        try:
            upsert_source_translation(
                connection,
                source_id=source_id,
                source_text_hash=current_source_text_hash,
                target_language=translator.target_language,
                provider=translator.provider_name,
                status="failed",
                translated_text=None,
                error_message="Source has no text to translate.",
            )
            connection.commit()
        finally:
            connection.close()
        append_worker_log(
            build_log_event(
                "source_translation",
                status="failed",
                source_id=source_id,
                provider=translator.provider_name,
                lang=translator.target_language,
                error="no_source_text",
            )
        )
        return True

    append_worker_log(
        build_log_event(
            "source_translation",
            status="start",
            source_id=source_id,
            provider=translator.provider_name,
            lang=translator.target_language,
            lines=len(lines),
            text_len=len(raw_text),
        )
    )
    try:
        source_language = (
            languages.base_language_code(job.get("source_language_code"))
            or DEFAULT_LANGUAGE_CODE
        )
        if source_language == languages.base_language_code(translator.target_language):
            # Already in the target language: nothing to translate, keep the text.
            translated_text = "\n".join(lines)
        else:
            translated_text = translator.translate_lines(lines, source_language=source_language)
        connection = db_connect(db_path)
        try:
            upsert_source_translation(
                connection,
                source_id=source_id,
                source_text_hash=current_source_text_hash,
                target_language=translator.target_language,
                provider=translator.provider_name,
                status="translated",
                translated_text=translated_text,
                error_message=None,
            )
            connection.commit()
        finally:
            connection.close()
        append_worker_log(
            build_log_event(
                "source_translation",
                status="done",
                source_id=source_id,
                provider=translator.provider_name,
                lang=translator.target_language,
                text_len=len(translated_text),
            )
        )
    except Exception as exc:
        connection = db_connect(db_path)
        try:
            upsert_source_translation(
                connection,
                source_id=source_id,
                source_text_hash=current_source_text_hash,
                target_language=translator.target_language,
                provider=translator.provider_name,
                status="failed",
                translated_text=None,
                error_message=str(exc),
            )
            connection.commit()
        finally:
            connection.close()
        append_worker_log(
            build_log_event(
                "source_translation",
                status="failed",
                source_id=source_id,
                provider=translator.provider_name,
                lang=translator.target_language,
                error=exc,
            )
        )
    return True


def diagnose_image(
    image_path: Path,
    *,
    with_analysis: bool,
    output_json: Path | None,
    db_path: Path = DEFAULT_DB_PATH,
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

    enabled_languages = DEFAULT_CAPTURE_LANGUAGES
    if db_path.exists():
        connection = db_connect(db_path)
        try:
            enabled_languages = resolve_capture_languages(connection)
        finally:
            connection.close()
    probe.mark("languages", note=f"languages={','.join(enabled_languages)}")

    probe.mark("before_ocr_init")
    ocr = build_capture_ocr_adapter(enabled_languages=enabled_languages)
    probe.mark("after_ocr_init")

    probe.mark("before_ocr_predict")
    if ocr.downscales_input:
        ocr_input_path, resize_note, cleanup_path = prepare_ocr_input_image(image_path)
    else:
        ocr_input_path, resize_note, cleanup_path = image_path, None, None
    if resize_note:
        probe.mark("ocr_input_prepared", note=resize_note)
    try:
        blocks = ocr.extract_blocks(
            ocr_input_path,
            prepared=True,
            progress_label=build_log_event("diagnose", step="ocr", image=ocr_input_path),
            echo_stdout=True,
        )
    finally:
        if cleanup_path is not None:
            delete_if_exists(cleanup_path)
    normalized_blocks, raw_text = blocks_with_text(blocks)
    target_detected = any(
        contains_target_language_text(block.text, enabled_languages=enabled_languages)
        for block in normalized_blocks
    )
    probe.mark(
        "after_ocr_predict",
        note=(
            f"blocks={len(blocks)} normalized_blocks={len(normalized_blocks)} "
            f"target_language_detected={target_detected} raw_text_len={len(raw_text)}"
        ),
    )
    for block in normalized_blocks:
        print(f"  [{block.confidence if block.confidence is None else round(block.confidence, 2)}] {block.text}")

    if with_analysis:
        probe.mark("before_analyzer_init")
        analyzer = LanguageSegmentAnalyzer(enabled_languages)
        probe.mark("after_analyzer_init")
        candidate_total = 0
        with db_connect(db_path) as connection:
            for block in normalized_blocks:
                for _, candidates in analyzer.analyze_block(connection, block):
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
    parser.add_argument(
        "--recover-stale-after-hours",
        type=float,
        default=DEFAULT_STALE_CAPTURE_JOB_HOURS,
        help="Recover processing jobs older than this threshold before starting normal work.",
    )
    parser.add_argument(
        "--no-recover-stale-jobs",
        action="store_true",
        help="Skip stale capture-job recovery during worker startup.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    once_parser = subparsers.add_parser("once", help="Process a single pending capture job.")
    once_parser.set_defaults(mode="once")

    loop_parser = subparsers.add_parser("loop", help="Process capture jobs continuously.")
    loop_parser.set_defaults(mode="loop")

    drain_parser = subparsers.add_parser(
        "drain",
        help="Process all pending capture jobs until the queue is empty, then exit.",
    )
    drain_parser.set_defaults(mode="drain")

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
        help="Also initialize the language analyzers and run analysis on OCR blocks.",
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
            db_path=args.db,
        )
        return

    if not args.no_recover_stale_jobs:
        recover_stale_capture_jobs_on_startup(
            args.db,
            older_than_hours=args.recover_stale_after_hours,
        )

    # Engines and analyzers are picked per job from the database capture set (and
    # capture profiles); warming up loads the current set before the first job.
    pipeline = CapturePipeline()
    warm_codes = pipeline.warm_up(args.db)
    append_worker_log(
        build_log_event(
            "worker",
            step="ready",
            languages=",".join(warm_codes),
            override=os.environ.get(CAPTURE_LANGUAGES_ENV) or None,
        )
    )
    translator = (
        GoogleGtxSourceTranslator(target_language=DEFAULT_SOURCE_TRANSLATION_TARGET)
        if DEFAULT_ENABLE_SOURCE_TRANSLATION
        else None
    )

    if args.mode == "once":
        processed = process_job_once(args.db, translator=translator, pipeline=pipeline)
        if not processed:
            print("No pending capture jobs.")
            if DEBUG_ENABLED:
                append_worker_log("capture worker idle: no pending capture jobs")
        return

    if args.mode == "drain":
        drained_jobs = 0
        drained_translations = 0
        while True:
            processed = process_job_once(
                args.db,
                translator=translator,
                pipeline=pipeline,
            )
            if processed:
                drained_jobs += 1
                continue
            translated = process_pending_translation_once(args.db, translator=translator)
            if translated:
                drained_translations += 1
                continue
            append_worker_log(
                "capture worker drained queues "
                f"processed_jobs={drained_jobs} processed_translations={drained_translations}"
            )
            return

    idle_polls = 0
    while True:
        processed = process_job_once(
            args.db,
            translator=translator,
            pipeline=pipeline,
        )
        if processed:
            idle_polls = 0
            continue

        translated = process_pending_translation_once(args.db, translator=translator)
        if translated:
            idle_polls = 0
            continue

        idle_polls += 1
        if DEBUG_ENABLED and idle_polls >= 15:
            append_worker_log(
                "capture worker idle: still waiting for pending capture jobs or translations"
            )
            idle_polls = 0
        time.sleep(max(0.25, args.poll_interval))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        append_worker_log("capture worker crashed with an unhandled exception")
        for line in traceback.format_exc().rstrip().splitlines():
            append_worker_log(f"traceback {line}")
        raise
