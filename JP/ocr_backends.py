from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image


OCR_BACKEND_CHOICES = ("manga_ocr", "easyocr", "meikiocr")
CAPTURE_OCR_BACKEND_CHOICES = ("meikiocr", "easyocr")
DEFAULT_CAPTURE_OCR_BACKEND = "meikiocr"
DEFAULT_EASYOCR_LANGUAGES = ("ja", "en")
DEFAULT_EASYOCR_GPU_MODE = "auto"
# Post-filter thresholds tuned on JP/ocr_bench (see ocr-precision-research.md).
DEFAULT_MEIKIOCR_MIN_CHAR_CONFIDENCE = 0.3  # stray punctuation in word gaps scores ~0.1-0.2
MIN_LINE_JAPANESE_CHARS = 2
MIN_LINE_JAPANESE_RATIO = 0.3
CONTAINED_BOX_MIN_COVERAGE = 0.7
FURIGANA_MAX_HEIGHT_RATIO = 0.85
FURIGANA_MIN_HORIZONTAL_OVERLAP = 0.4
ROOT = Path(__file__).resolve().parent
MODEL_CACHE_ROOT = ROOT / "data" / "model_cache"


@dataclass(slots=True)
class OcrBlock:
    text: str
    confidence: float | None
    bbox: list[list[float]] | None


@dataclass(slots=True)
class OcrRegionCandidate:
    left: int
    top: int
    right: int
    bottom: int
    blocks: list[OcrBlock]


class OcrBackend(Protocol):
    name: str

    def recognize(self, crop: Image.Image) -> str: ...


def parse_csv_env(
    value: str | None,
    *,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    if value is None:
        return fallback
    parsed = tuple(part.strip() for part in str(value).split(",") if part.strip())
    return parsed or fallback


def resolve_easyocr_languages(value: str | None = None) -> tuple[str, ...]:
    raw_value = value
    if raw_value is None:
        raw_value = os.environ.get("LANG_JP_EASYOCR_LANGS")
    return parse_csv_env(raw_value, fallback=DEFAULT_EASYOCR_LANGUAGES)


def resolve_capture_ocr_backend(value: str | None = None) -> str:
    raw_value = value
    if raw_value is None:
        raw_value = os.environ.get("LANG_JP_OCR_BACKEND", DEFAULT_CAPTURE_OCR_BACKEND)
    normalized = str(raw_value).strip().lower() or DEFAULT_CAPTURE_OCR_BACKEND
    if normalized not in CAPTURE_OCR_BACKEND_CHOICES:
        raise ValueError(
            f"Unsupported LANG_JP_OCR_BACKEND={raw_value!r}; "
            f"expected one of {', '.join(CAPTURE_OCR_BACKEND_CHOICES)}."
        )
    return normalized


def torch_cuda_is_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_easyocr_use_gpu(value: str | None = None) -> bool:
    raw_value = value
    if raw_value is None:
        raw_value = os.environ.get("LANG_JP_EASYOCR_GPU", DEFAULT_EASYOCR_GPU_MODE)
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "gpu", "cuda"}:
        return True
    if normalized in {"0", "false", "no", "off", "cpu"}:
        return False
    return torch_cuda_is_available()


def ensure_repo_cache_dir(*parts: str) -> Path:
    path = MODEL_CACHE_ROOT.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_output_text(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text


def contains_japanese_chars(value: str) -> bool:
    for char in value:
        codepoint = ord(char)
        if 0x3040 <= codepoint <= 0x309F:
            return True
        if 0x30A0 <= codepoint <= 0x30FF:
            return True
        if 0x4E00 <= codepoint <= 0x9FFF:
            return True
    return False


def filter_japanese_lines(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    kept = [line for line in lines if contains_japanese_chars(line)]
    return normalize_output_text("\n".join(kept))


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bbox_width(bbox: list[list[float]] | None) -> float:
    if not bbox or len(bbox) < 4:
        return 0.0
    top = math.dist(bbox[0], bbox[1])
    bottom = math.dist(bbox[3], bbox[2])
    return (top + bottom) / 2.0


def bbox_height(bbox: list[list[float]] | None) -> float:
    if not bbox or len(bbox) < 4:
        return 0.0
    left = math.dist(bbox[0], bbox[3])
    right = math.dist(bbox[1], bbox[2])
    return (left + right) / 2.0


def bbox_angle_degrees(bbox: list[list[float]] | None) -> float:
    if not bbox or len(bbox) < 2:
        return 90.0
    dx = bbox[1][0] - bbox[0][0]
    dy = bbox[1][1] - bbox[0][1]
    if dx == 0 and dy == 0:
        return 90.0
    return math.degrees(math.atan2(dy, dx))


def is_horizontal_bbox(
    bbox: list[list[float]] | None,
    *,
    min_ratio: float = 1.2,
    max_abs_angle_degrees: float = 18.0,
) -> bool:
    width = bbox_width(bbox)
    height = bbox_height(bbox)
    if width <= 0 or height <= 0:
        return False
    if width < height * min_ratio:
        return False
    angle = abs(bbox_angle_degrees(bbox))
    return angle <= max_abs_angle_degrees


def bbox_top_left(bbox: list[list[float]] | None) -> tuple[float, float]:
    if not bbox:
        return (0.0, 0.0)
    xs = [point[0] for point in bbox]
    ys = [point[1] for point in bbox]
    return (min(xs), min(ys))


@dataclass(slots=True)
class _BlockLayout:
    original_index: int
    block: OcrBlock
    left: float
    top: float
    right: float
    bottom: float

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)


@dataclass(slots=True)
class _ReadingRow:
    top: float
    bottom: float
    center_y_total: float
    count: int
    max_height: float
    blocks: list[_BlockLayout]

    @property
    def center_y(self) -> float:
        return self.center_y_total / max(1, self.count)

    def add(self, layout: _BlockLayout) -> None:
        self.top = min(self.top, layout.top)
        self.bottom = max(self.bottom, layout.bottom)
        self.center_y_total += layout.center_y
        self.count += 1
        self.max_height = max(self.max_height, layout.height)
        self.blocks.append(layout)


def _block_layout(block: OcrBlock, original_index: int) -> _BlockLayout | None:
    bounds = bbox_bounds(block.bbox)
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    return _BlockLayout(
        original_index=original_index,
        block=block,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
    )


def _row_vertical_gap(row: _ReadingRow, layout: _BlockLayout) -> float:
    return max(0.0, layout.top - row.bottom, row.top - layout.bottom)


def _row_matches_layout(row: _ReadingRow, layout: _BlockLayout) -> bool:
    gap_tolerance = max(6.0, min(row.max_height, layout.height) * 0.35)
    if _row_vertical_gap(row, layout) > gap_tolerance:
        return False
    center_tolerance = max(row.max_height, layout.height) * 0.75
    return abs(layout.center_y - row.center_y) <= center_tolerance


def sort_ocr_blocks_reading_order(blocks: list[OcrBlock]) -> list[OcrBlock]:
    layouts: list[_BlockLayout] = []
    fallback_blocks: list[tuple[int, OcrBlock]] = []
    for original_index, block in enumerate(blocks):
        layout = _block_layout(block, original_index)
        if layout is None:
            fallback_blocks.append((original_index, block))
            continue
        layouts.append(layout)

    layouts.sort(key=lambda layout: (layout.top, layout.left, layout.original_index))
    rows: list[_ReadingRow] = []
    for layout in layouts:
        best_row: _ReadingRow | None = None
        best_key: tuple[float, float] | None = None
        for row in rows:
            if not _row_matches_layout(row, layout):
                continue
            candidate_key = (_row_vertical_gap(row, layout), abs(layout.center_y - row.center_y))
            if best_key is None or candidate_key < best_key:
                best_key = candidate_key
                best_row = row
        if best_row is None:
            rows.append(
                _ReadingRow(
                    top=layout.top,
                    bottom=layout.bottom,
                    center_y_total=layout.center_y,
                    count=1,
                    max_height=layout.height,
                    blocks=[layout],
                )
            )
            continue
        best_row.add(layout)

    rows.sort(
        key=lambda row: (
            row.top,
            min(layout.left for layout in row.blocks),
        )
    )

    ordered_blocks: list[OcrBlock] = []
    for row in rows:
        row.blocks.sort(key=lambda layout: (layout.left, layout.top, layout.original_index))
        ordered_blocks.extend(layout.block for layout in row.blocks)

    ordered_blocks.extend(block for _, block in sorted(fallback_blocks, key=lambda item: item[0]))
    return ordered_blocks


def bbox_bounds(bbox: list[list[float]] | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    xs = [point[0] for point in bbox]
    ys = [point[1] for point in bbox]
    return (min(xs), min(ys), max(xs), max(ys))


def _expand_bounds(
    bounds: tuple[float, float, float, float],
    *,
    x_padding: float,
    y_padding: float,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = bounds
    return (
        left - x_padding,
        top - y_padding,
        right + x_padding,
        bottom + y_padding,
    )


def _bounds_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] < second[0]
        or second[2] < first[0]
        or first[3] < second[1]
        or second[3] < first[1]
    )


def _merge_bounds(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def group_ocr_blocks_into_regions(
    blocks: list[OcrBlock],
    *,
    min_x_padding: float = 18.0,
    min_y_padding: float = 10.0,
    x_padding_ratio: float = 1.4,
    y_padding_ratio: float = 0.7,
) -> list[OcrRegionCandidate]:
    clusters: list[dict[str, object]] = []
    for block in sort_ocr_blocks_reading_order(blocks):
        bounds = bbox_bounds(block.bbox)
        if bounds is None:
            continue
        height = max(1.0, bbox_height(block.bbox))
        expanded = _expand_bounds(
            bounds,
            x_padding=max(min_x_padding, height * x_padding_ratio),
            y_padding=max(min_y_padding, height * y_padding_ratio),
        )

        matching_indexes = [
            index
            for index, cluster in enumerate(clusters)
            if _bounds_overlap(expanded, cluster["expanded"])  # type: ignore[index]
        ]
        if not matching_indexes:
            clusters.append(
                {
                    "original": bounds,
                    "expanded": expanded,
                    "blocks": [block],
                }
            )
            continue

        target_index = matching_indexes[0]
        target = clusters[target_index]
        target["original"] = _merge_bounds(target["original"], bounds)  # type: ignore[index]
        target["expanded"] = _merge_bounds(target["expanded"], expanded)  # type: ignore[index]
        target["blocks"].append(block)  # type: ignore[index]

        for merge_index in reversed(matching_indexes[1:]):
            extra = clusters.pop(merge_index)
            target["original"] = _merge_bounds(target["original"], extra["original"])  # type: ignore[index]
            target["expanded"] = _merge_bounds(target["expanded"], extra["expanded"])  # type: ignore[index]
            target["blocks"].extend(extra["blocks"])  # type: ignore[index]

    regions = [
        OcrRegionCandidate(
            left=int(cluster["original"][0]),  # type: ignore[index]
            top=int(cluster["original"][1]),  # type: ignore[index]
            right=int(cluster["original"][2]),  # type: ignore[index]
            bottom=int(cluster["original"][3]),  # type: ignore[index]
            blocks=sort_ocr_blocks_reading_order(cluster["blocks"]),  # type: ignore[index]
        )
        for cluster in clusters
    ]
    return sorted(regions, key=lambda region: (region.top, region.left))


def is_japanese_text_char(char: str) -> bool:
    """Kana, kanji, ー and 々: the characters the OCR benchmark scores."""
    codepoint = ord(char)
    return (
        0x3041 <= codepoint <= 0x3096
        or 0x30A1 <= codepoint <= 0x30FA
        or codepoint in (0x30FC, 0x3005)
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0x3400 <= codepoint <= 0x4DBF
    )


def is_kanji_text_char(char: str) -> bool:
    codepoint = ord(char)
    return 0x4E00 <= codepoint <= 0x9FFF or 0x3400 <= codepoint <= 0x4DBF


@dataclass(slots=True)
class OcrLineBox:
    """One detected text line with an axis-aligned box, before reading-order grouping."""

    text: str
    confidence: float | None
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return max(1.0, self.right - self.left)

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)


def meikiocr_line_to_box(
    line: dict,
    *,
    min_char_confidence: float = DEFAULT_MEIKIOCR_MIN_CHAR_CONFIDENCE,
) -> OcrLineBox | None:
    """Drop low-confidence characters and rebuild the line box from the ones left."""
    chars = [
        char
        for char in line.get("chars") or []
        if float(char["conf"]) >= min_char_confidence
    ]
    if not chars:
        return None
    return OcrLineBox(
        text="".join(char["char"] for char in chars),
        confidence=min(float(char["conf"]) for char in chars),
        left=float(min(char["bbox"][0] for char in chars)),
        top=float(min(char["bbox"][1] for char in chars)),
        right=float(max(char["bbox"][2] for char in chars)),
        bottom=float(max(char["bbox"][3] for char in chars)),
    )


def is_plausible_japanese_line(text: str) -> bool:
    """Reject icon and Latin garbage: need 2+ Japanese chars making up 30%+ of the line."""
    visible = [char for char in text if not char.isspace()]
    japanese = sum(1 for char in visible if is_japanese_text_char(char))
    return japanese >= MIN_LINE_JAPANESE_CHARS and japanese >= MIN_LINE_JAPANESE_RATIO * len(visible)


def drop_contained_boxes(boxes: list[OcrLineBox]) -> list[OcrLineBox]:
    """Drop a box mostly covered by a larger one (duplicate detections of the same text)."""
    kept: list[OcrLineBox] = []
    for box in boxes:
        area = box.width * box.height
        covered = False
        for other in boxes:
            if other is box or other.width * other.height <= area:
                continue
            overlap_width = min(box.right, other.right) - max(box.left, other.left)
            overlap_height = min(box.bottom, other.bottom) - max(box.top, other.top)
            if (
                overlap_width > 0
                and overlap_height > 0
                and overlap_width * overlap_height >= CONTAINED_BOX_MIN_COVERAGE * area
            ):
                covered = True
                break
        if not covered:
            kept.append(box)
    return kept


def is_furigana_box(box: OcrLineBox, boxes: list[OcrLineBox]) -> bool:
    """owocr-style ruby rule: a kana-only box just above a taller kanji box it overlaps."""
    core = [char for char in box.text if is_japanese_text_char(char)]
    if not core or any(is_kanji_text_char(char) for char in core):
        return False
    for other in boxes:
        if other is box or not any(is_kanji_text_char(char) for char in other.text):
            continue
        if box.height >= FURIGANA_MAX_HEIGHT_RATIO * other.height:
            continue
        center_gap = (other.top + other.bottom) / 2 - (box.top + box.bottom) / 2
        if not (abs(other.height - box.height) / 2 <= center_gap <= other.height + box.height / 2):
            continue
        overlap_width = min(box.right, other.right) - max(box.left, other.left)
        if overlap_width > FURIGANA_MIN_HORIZONTAL_OVERLAP * box.width:
            return True
    return False


def drop_furigana_boxes(boxes: list[OcrLineBox]) -> list[OcrLineBox]:
    return [box for box in boxes if not is_furigana_box(box, boxes)]


def filter_ocr_line_boxes(boxes: list[OcrLineBox]) -> tuple[list[OcrLineBox], dict[str, int]]:
    """Apply the line, duplicate and furigana filters in that order."""
    plausible = [box for box in boxes if is_plausible_japanese_line(box.text)]
    deduplicated = drop_contained_boxes(plausible)
    kept = drop_furigana_boxes(deduplicated)
    stats = {
        "detected": len(boxes),
        "kept": len(kept),
        "non_japanese": len(boxes) - len(plausible),
        "contained": len(plausible) - len(deduplicated),
        "furigana": len(deduplicated) - len(kept),
    }
    return kept, stats


def group_ocr_line_boxes_into_rows(boxes: list[OcrLineBox]) -> list[list[OcrLineBox]]:
    """Group boxes of similar height that share a horizontal band, in reading order."""
    rows: list[list[OcrLineBox]] = []
    for box in sorted(boxes, key=lambda item: (item.top, item.left)):
        center_y = (box.top + box.bottom) / 2
        for row in rows:
            row_top = min(item.top for item in row)
            row_bottom = max(item.bottom for item in row)
            row_height = max(item.height for item in row)
            same_band = row_top - 0.25 * row_height <= center_y <= row_bottom + 0.25 * row_height
            if same_band and abs(box.height - row_height) < 0.5 * max(box.height, row_height):
                row.append(box)
                break
        else:
            rows.append([box])
    for row in rows:
        row.sort(key=lambda item: item.left)
    rows.sort(key=lambda row: (min(item.top for item in row), min(item.left for item in row)))
    return rows


def ocr_row_to_block(row: list[OcrLineBox]) -> OcrBlock:
    left = min(box.left for box in row)
    top = min(box.top for box in row)
    right = max(box.right for box in row)
    bottom = max(box.bottom for box in row)
    confidences = [box.confidence for box in row if box.confidence is not None]
    return OcrBlock(
        text=" ".join(box.text for box in row),
        confidence=min(confidences) if confidences else None,
        bbox=[[left, top], [right, top], [right, bottom], [left, bottom]],
    )


def ocr_line_boxes_to_blocks(boxes: list[OcrLineBox]) -> list[OcrBlock]:
    return [ocr_row_to_block(row) for row in group_ocr_line_boxes_into_rows(boxes)]


class MangaOcrBackend:
    name = "manga_ocr"

    def __init__(self) -> None:
        huggingface_cache = ensure_repo_cache_dir("huggingface")
        modelscope_cache = ensure_repo_cache_dir("modelscope")
        torch_cache = ensure_repo_cache_dir("torch")
        os.environ.setdefault("HF_HOME", str(huggingface_cache))
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(huggingface_cache / "hub"))
        os.environ.setdefault("TRANSFORMERS_CACHE", str(huggingface_cache / "transformers"))
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("MODELSCOPE_CACHE", str(modelscope_cache))
        os.environ.setdefault("TORCH_HOME", str(torch_cache))
        try:
            from manga_ocr import MangaOcr
        except ImportError as exc:
            raise SystemExit(
                "Missing dependency `manga-ocr`. Install it in the Python 3.12 environment "
                "before using the OCR tooling."
            ) from exc

        self._engine = MangaOcr()

    def recognize(self, crop: Image.Image) -> str:
        text = self._engine(crop)
        return normalize_output_text(text)


class EasyOcrBackend:
    name = "easyocr"

    def __init__(self) -> None:
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        easyocr_cache = ensure_repo_cache_dir("easyocr")
        user_network_dir = ensure_repo_cache_dir("easyocr", "user_network")
        os.environ.setdefault("EASYOCR_MODULE_PATH", str(easyocr_cache))
        try:
            import easyocr
        except ImportError as exc:
            raise SystemExit(
                "Missing dependency `easyocr`. Install it in the Python 3.12 environment "
                "before using the OCR tooling."
            ) from exc

        self.languages = resolve_easyocr_languages()
        self.gpu_mode = os.environ.get("LANG_JP_EASYOCR_GPU", DEFAULT_EASYOCR_GPU_MODE)
        self.gpu_enabled = resolve_easyocr_use_gpu(self.gpu_mode)
        self._reader = easyocr.Reader(
            list(self.languages),
            gpu=self.gpu_enabled,
            model_storage_directory=str(easyocr_cache),
            user_network_directory=str(user_network_dir),
            verbose=False,
        )

    def recognize(self, crop: Image.Image) -> str:
        blocks = self.read_blocks(crop)
        return normalize_output_text("\n".join(block.text for block in blocks if block.text))

    def read_blocks(self, source: Image.Image | str | Path) -> list[OcrBlock]:
        if isinstance(source, Image.Image):
            target = np.array(source.convert("RGB"))
        else:
            target = str(source)
        result = self._reader.readtext(target, detail=1, paragraph=False)
        blocks: list[OcrBlock] = []
        for item in result:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            bbox_raw, text_raw, confidence_raw = item[0], item[1], item[2]
            text = normalize_output_text(str(text_raw))
            if not text:
                continue
            bbox: list[list[float]] | None = None
            if isinstance(bbox_raw, (list, tuple)):
                bbox = []
                for point in bbox_raw:
                    if (
                        isinstance(point, (list, tuple))
                        and len(point) >= 2
                    ):
                        bbox.append([float(point[0]), float(point[1])])
                if not bbox:
                    bbox = None
            blocks.append(
                OcrBlock(
                    text=text,
                    confidence=optional_float(confidence_raw),
                    bbox=bbox,
                )
            )
        return blocks


class MeikiOcrBackend:
    """meikiocr (detector + recognizer trained on game text) plus the benchmark post-filters.

    Runs on the full-resolution image: meikiocr resizes internally for detection and
    recognizes from full-resolution crops, so the input should not be downscaled.
    """

    name = "meikiocr"

    def __init__(
        self,
        *,
        min_char_confidence: float = DEFAULT_MEIKIOCR_MIN_CHAR_CONFIDENCE,
    ) -> None:
        huggingface_cache = ensure_repo_cache_dir("huggingface")
        os.environ.setdefault("HF_HOME", str(huggingface_cache))
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(huggingface_cache / "hub"))
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        try:
            from meikiocr import MeikiOCR
        except ImportError as exc:
            raise SystemExit(
                "Missing dependency `meikiocr`. Install it in the Python 3.12 environment "
                "before using the OCR tooling."
            ) from exc

        self.min_char_confidence = min_char_confidence
        self._engine = MeikiOCR()
        self.provider = getattr(self._engine, "active_provider", None)

    def read_line_boxes(self, source: Image.Image | str | Path) -> list[OcrLineBox]:
        """One box per detected line, after dropping low-confidence characters."""
        if isinstance(source, Image.Image):
            rgb = np.array(source.convert("RGB"))
        else:
            with Image.open(source) as image:
                rgb = np.array(image.convert("RGB"))
        boxes: list[OcrLineBox] = []
        for line in self._engine.run_ocr(rgb[:, :, ::-1].copy()):  # meikiocr expects BGR
            box = meikiocr_line_to_box(line, min_char_confidence=self.min_char_confidence)
            if box is not None:
                boxes.append(box)
        return boxes

    def read_blocks(self, source: Image.Image | str | Path) -> list[OcrBlock]:
        kept, _ = filter_ocr_line_boxes(self.read_line_boxes(source))
        return ocr_line_boxes_to_blocks(kept)

    def recognize(self, crop: Image.Image) -> str:
        return normalize_output_text("\n".join(block.text for block in self.read_blocks(crop)))


def build_ocr_backend(name: str) -> OcrBackend:
    if name == "manga_ocr":
        return MangaOcrBackend()
    if name == "easyocr":
        return EasyOcrBackend()
    if name == "meikiocr":
        return MeikiOcrBackend()
    raise SystemExit(f"Unsupported backend: {name}")
