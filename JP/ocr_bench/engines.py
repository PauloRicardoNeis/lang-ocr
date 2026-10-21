"""OCR engine variants for the precision benchmark.

Every engine maps a screenshot path to text lines in reading order. Model
imports are lazy, so the harness runs in any environment that has the packages
for the engines you select (meikiocr can live in its own venv).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

BENCH_DIR = Path(__file__).resolve().parent
JP_DIR = BENCH_DIR.parent
MODEL_CACHE = JP_DIR / "data" / "model_cache"
sys.path.insert(0, str(JP_DIR))
os.environ.setdefault("HF_HOME", str(MODEL_CACHE / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(MODEL_CACHE / "paddlex"))
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


def is_jp_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3041 <= cp <= 0x3096  # hiragana
        or 0x30A1 <= cp <= 0x30FA  # katakana
        or cp in (0x30FC, 0x3005)  # ー 々
        or 0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
    )


def is_kanji(ch: str) -> bool:
    return 0x4E00 <= ord(ch) <= 0x9FFF or 0x3400 <= ord(ch) <= 0x4DBF


@dataclass(slots=True)
class Box:
    text: str
    conf: float | None
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


# --------------------------------------------------------------------------- post-processing


def plausible_jp_line(text: str) -> bool:
    """Reject icon/Latin garbage: need 2+ Japanese chars making up 30%+ of the line."""
    visible = [ch for ch in text if not ch.isspace()]
    jp = sum(1 for ch in visible if is_jp_char(ch))
    return jp >= 2 and jp >= 0.3 * len(visible)


def drop_contained(boxes: list[Box]) -> list[Box]:
    """Drop a box mostly covered by a larger one (duplicate detections of the same text)."""
    kept = []
    for a in boxes:
        area_a = a.width * a.height
        covered = False
        for b in boxes:
            if b is a or b.width * b.height <= area_a:
                continue
            iw = min(a.right, b.right) - max(a.left, b.left)
            ih = min(a.bottom, b.bottom) - max(a.top, b.top)
            if iw > 0 and ih > 0 and iw * ih >= 0.7 * area_a:
                covered = True
                break
        if not covered:
            kept.append(a)
    return kept


def drop_furigana(boxes: list[Box]) -> list[Box]:
    """owocr-style ruby filter: a kana-only box just above a taller kanji box it overlaps."""
    kept = []
    for a in boxes:
        core = [ch for ch in a.text if is_jp_char(ch)]
        is_ruby = False
        if core and not any(is_kanji(ch) for ch in core):
            for b in boxes:
                if b is a or not any(is_kanji(ch) for ch in b.text) or a.height >= 0.85 * b.height:
                    continue
                dy = (b.top + b.bottom) / 2 - (a.top + a.bottom) / 2
                if not (abs(b.height - a.height) / 2 <= dy <= b.height + a.height / 2):
                    continue
                if min(a.right, b.right) - max(a.left, b.left) > 0.4 * a.width:
                    is_ruby = True
                    break
        if not is_ruby:
            kept.append(a)
    return kept


def postprocess(boxes: list[Box]) -> list[Box]:
    boxes = [box for box in boxes if plausible_jp_line(box.text)]
    return drop_furigana(drop_contained(boxes))


def basic_filter(boxes: list[Box], min_conf: float) -> list[Box]:
    out = []
    for box in boxes:
        if not any(is_jp_char(ch) for ch in box.text):
            continue
        if box.conf is not None and box.conf < min_conf:
            continue
        if box.width < box.height and len(box.text) > 1:  # vertical text is out of scope
            continue
        out.append(box)
    return out


def rows_in_reading_order(boxes: list[Box]) -> list[list[Box]]:
    rows: list[list[Box]] = []
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


def boxes_to_lines(boxes: list[Box]) -> list[str]:
    return [" ".join(item.text for item in row) for row in rows_in_reading_order(boxes)]


def load_rgb(path: Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGB"))


# --------------------------------------------------------------------------- EasyOCR (current production path)

_easy_adapter = None


def easy_adapter():
    global _easy_adapter
    if _easy_adapter is None:
        import capture_worker

        capture_worker.append_worker_log = lambda message: None  # keep benchmark out of the daemon log
        _easy_adapter = capture_worker.EasyOcrAdapter()
    return _easy_adapter


def run_baseline(path: Path) -> list[str]:
    """capture_worker as shipped: 1600px downscale, region grouping, second OCR pass per region."""
    import capture_worker

    capture_worker.DEFAULT_OCR_MAX_SIDE = 1600
    blocks = easy_adapter().extract_blocks(path)
    return [line for block in blocks for line in block.text.splitlines()]


def easy_boxes(path: Path) -> list[Box]:
    reader = easy_adapter()._engine._reader
    out = []
    for bbox, text, conf in reader.readtext(load_rgb(path), detail=1, paragraph=False):
        xs = [point[0] for point in bbox]
        ys = [point[1] for point in bbox]
        out.append(Box(str(text).strip(), float(conf), min(xs), min(ys), max(xs), max(ys)))
    return out


def run_easyocr_post(path: Path) -> list[str]:
    """Single EasyOCR pass at full resolution + the shared post-processing."""
    return boxes_to_lines(postprocess(basic_filter(easy_boxes(path), min_conf=0.30)))


# --------------------------------------------------------------------------- PaddleOCR PP-OCRv5

_paddle = None


def paddle_boxes(path: Path) -> list[Box]:
    global _paddle
    if _paddle is None:
        from paddleocr import PaddleOCR

        _paddle = PaddleOCR(
            text_detection_model_name="PP-OCRv5_server_det",
            text_recognition_model_name="PP-OCRv5_server_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,  # paddlepaddle 3.3 CPU crashes in oneDNN otherwise
            text_det_limit_type="max",
            text_det_limit_side_len=1920,
        )
    out = []
    # Passing a path segfaults on 4K PNGs here; an in-memory BGR array works.
    for result in _paddle.predict(load_rgb(path)[:, :, ::-1].copy()):
        for text, conf, box in zip(result["rec_texts"], result["rec_scores"], result["rec_boxes"]):
            left, top, right, bottom = (float(value) for value in box)
            out.append(Box(str(text).strip(), float(conf), left, top, right, bottom))
    return out


def run_paddle_post(path: Path) -> list[str]:
    return boxes_to_lines(postprocess(basic_filter(paddle_boxes(path), min_conf=0.5)))


# --------------------------------------------------------------------------- EasyOCR detection + manga-ocr per line

_manga = None


def run_easydet_manga(path: Path) -> list[str]:
    global _manga
    if _manga is None:
        from manga_ocr import MangaOcr

        _manga = MangaOcr()
    image = Image.open(path).convert("RGB")
    width, height = image.size
    lines = []
    for row in rows_in_reading_order(postprocess(basic_filter(easy_boxes(path), min_conf=0.30))):
        parts = []
        for box in row:
            pad = box.height * 0.15
            crop = image.crop(
                (
                    max(0, box.left - pad),
                    max(0, box.top - pad),
                    min(width, box.right + pad),
                    min(height, box.bottom + pad),
                )
            )
            parts.append(_manga(crop))
        lines.append(" ".join(parts))
    return lines


# --------------------------------------------------------------------------- meikiocr (game-text model)

_meiki = None
MEIKI_MIN_CHAR_CONF = 0.3  # stray punctuation in word gaps scores ~0.1-0.2


def meiki_boxes(path: Path, min_char_conf: float) -> list[Box]:
    global _meiki
    if _meiki is None:
        from meikiocr import MeikiOCR

        _meiki = MeikiOCR()
    out = []
    for line in _meiki.run_ocr(load_rgb(path)[:, :, ::-1].copy()):
        chars = [char for char in line.get("chars") or [] if char["conf"] >= min_char_conf]
        if not chars:
            continue
        out.append(
            Box(
                "".join(char["char"] for char in chars),
                min(char["conf"] for char in chars),
                min(char["bbox"][0] for char in chars),
                min(char["bbox"][1] for char in chars),
                max(char["bbox"][2] for char in chars),
                max(char["bbox"][3] for char in chars),
            )
        )
    return out


def run_meiki_raw(path: Path) -> list[str]:
    return boxes_to_lines(basic_filter(meiki_boxes(path, 0.0), min_conf=0.0))


def run_meiki_post(path: Path) -> list[str]:
    return boxes_to_lines(postprocess(meiki_boxes(path, MEIKI_MIN_CHAR_CONF)))


ENGINES = {
    "baseline": run_baseline,
    "easyocr_post": run_easyocr_post,
    "paddle_v5_post": run_paddle_post,
    "easydet_manga": run_easydet_manga,
    "meiki_raw": run_meiki_raw,
    "meiki_post": run_meiki_post,
}
