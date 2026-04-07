from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image


OCR_BACKEND_CHOICES = ("manga_ocr", "easyocr")
DEFAULT_EASYOCR_LANGUAGES = ("ja", "en")
DEFAULT_EASYOCR_GPU_MODE = "auto"


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


class MangaOcrBackend:
    name = "manga_ocr"

    def __init__(self) -> None:
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
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
        self._reader = easyocr.Reader(list(self.languages), gpu=self.gpu_enabled)

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


def build_ocr_backend(name: str) -> OcrBackend:
    if name == "manga_ocr":
        return MangaOcrBackend()
    if name == "easyocr":
        return EasyOcrBackend()
    raise SystemExit(f"Unsupported backend: {name}")
