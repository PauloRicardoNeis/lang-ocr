from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "data"
DEFAULT_REGION_MODE = "overlay_boxes"


@dataclass(slots=True)
class OcrRegion:
    index: int
    left: int
    top: int
    right: int
    bottom: int
    source: str

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(slots=True)
class RecognizedRegion:
    index: int
    source: str
    left: int
    top: int
    right: int
    bottom: int
    text: str


@dataclass(slots=True)
class SingleImageOcrResult:
    backend: str
    image_path: str
    region_mode: str
    regions: list[RecognizedRegion]

    @property
    def raw_text(self) -> str:
        return "\n\n".join(region.text for region in self.regions if region.text)


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
                "before using single_image_ocr.py."
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
                "before using single_image_ocr.py with `--backend easyocr`."
            ) from exc

        self._reader = easyocr.Reader(["ja", "en"], gpu=False)

    def recognize(self, crop: Image.Image) -> str:
        lines = self._reader.readtext(np.array(crop), detail=0, paragraph=False)
        return normalize_output_text("\n".join(str(line) for line in lines if str(line).strip()))


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


def build_backend(name: str) -> MangaOcrBackend | EasyOcrBackend:
    if name == "manga_ocr":
        return MangaOcrBackend()
    if name == "easyocr":
        return EasyOcrBackend()
    raise SystemExit(f"Unsupported backend: {name}")


def parse_roi(value: str, index: int) -> OcrRegion:
    try:
        left, top, right, bottom = [int(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid ROI `{value}`. Expected format: x1,y1,x2,y2"
        ) from exc
    if left >= right or top >= bottom:
        raise argparse.ArgumentTypeError(
            f"Invalid ROI `{value}`. Expected x1 < x2 and y1 < y2."
        )
    return OcrRegion(index=index, left=left, top=top, right=right, bottom=bottom, source="manual")


def detect_overlay_regions(image_path: Path) -> list[OcrRegion]:
    image = cv2.imread(str(image_path))
    if image is None:
        raise SystemExit(f"Failed to load image for overlay-box detection: {image_path}")

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower = np.array([35, 20, 120], dtype=np.uint8)
    upper = np.array([95, 255, 255], dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if area < 1500 or w < 20 or h < 20:
            continue
        candidates.append((x, y, x + w, y + h))

    candidates.sort(key=lambda item: (item[1], item[0]))
    return [
        OcrRegion(index=index, left=left, top=top, right=right, bottom=bottom, source="overlay_box")
        for index, (left, top, right, bottom) in enumerate(candidates, start=1)
    ]


def full_image_region(image: Image.Image) -> list[OcrRegion]:
    return [
        OcrRegion(
            index=1,
            left=0,
            top=0,
            right=image.width,
            bottom=image.height,
            source="full_image",
        )
    ]


def load_regions(args: argparse.Namespace, image: Image.Image) -> list[OcrRegion]:
    if args.region_mode == "manual":
        if not args.roi:
            raise SystemExit("`--region-mode manual` requires at least one `--roi x1,y1,x2,y2`.")
        return [parse_roi(value, index) for index, value in enumerate(args.roi, start=1)]
    if args.region_mode == "full_image":
        return full_image_region(image)

    regions = detect_overlay_regions(args.image)
    if not regions:
        raise SystemExit(
            "No overlay boxes detected. Use `--region-mode manual` with explicit `--roi` values "
            "or `--region-mode full_image`."
        )
    return regions


def crop_region(image: Image.Image, region: OcrRegion, padding: int) -> Image.Image:
    left = max(0, region.left - padding)
    top = max(0, region.top - padding)
    right = min(image.width, region.right + padding)
    bottom = min(image.height, region.bottom + padding)
    return image.crop((left, top, right, bottom))


def annotate_debug_image(image: Image.Image, regions: list[RecognizedRegion]) -> Image.Image:
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)
    for region in regions:
        draw.rectangle((region.left, region.top, region.right, region.bottom), outline=(0, 255, 0), width=3)
        draw.text((region.left + 4, max(0, region.top - 20)), str(region.index), fill=(255, 0, 0))
    return annotated


def run_single_image_ocr(args: argparse.Namespace) -> SingleImageOcrResult:
    backend = build_backend(args.backend)
    image = Image.open(args.image).convert("RGB")
    regions = load_regions(args, image)
    print(f"Using backend={backend.name} region_mode={args.region_mode} regions={len(regions)}")

    debug_dir = args.debug_dir
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)

    recognized_regions: list[RecognizedRegion] = []
    for region in regions:
        crop = crop_region(image, region, args.padding)
        if debug_dir is not None:
            crop.save(debug_dir / f"region_{region.index:02d}.png")
        text = backend.recognize(crop)
        if args.japanese_only:
            text = filter_japanese_lines(text)
        print(f"[region {region.index}] {text!r}")
        recognized_regions.append(
            RecognizedRegion(
                index=region.index,
                source=region.source,
                left=region.left,
                top=region.top,
                right=region.right,
                bottom=region.bottom,
                text=text,
            )
        )

    return SingleImageOcrResult(
        backend=backend.name,
        image_path=str(args.image),
        region_mode=args.region_mode,
        regions=recognized_regions,
    )


def write_outputs(
    result: SingleImageOcrResult,
    *,
    output_text: Path | None,
    output_json: Path | None,
    debug_image: Path | None,
) -> None:
    if output_text is not None:
        output_text.parent.mkdir(parents=True, exist_ok=True)
        output_text.write_text(result.raw_text, encoding="utf-8")
        print(f"Wrote text output to {output_text}")

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "backend": result.backend,
            "image_path": result.image_path,
            "region_mode": result.region_mode,
            "raw_text": result.raw_text,
            "regions": [asdict(region) for region in result.regions],
        }
        output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote JSON output to {output_json}")

    if debug_image is not None:
        debug_image.parent.mkdir(parents=True, exist_ok=True)
        image = Image.open(result.image_path).convert("RGB")
        annotate_debug_image(image, result.regions).save(debug_image)
        print(f"Wrote debug image to {debug_image}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Single-image OCR path for Japanese screenshots using a region-first flow.",
    )
    parser.add_argument("--image", type=Path, required=True, help="Absolute path to the image to OCR.")
    parser.add_argument(
        "--backend",
        choices=["manga_ocr", "easyocr"],
        default="manga_ocr",
        help="OCR backend to use.",
    )
    parser.add_argument(
        "--region-mode",
        choices=["overlay_boxes", "manual", "full_image"],
        default=DEFAULT_REGION_MODE,
        help="How to choose OCR regions from the image.",
    )
    parser.add_argument(
        "--roi",
        action="append",
        help="Manual ROI in x1,y1,x2,y2 format. Repeat for multiple regions.",
    )
    parser.add_argument(
        "--padding",
        type=int,
        default=8,
        help="Padding to add around each region before OCR.",
    )
    parser.add_argument(
        "--japanese-only",
        action="store_true",
        help="Keep only OCR lines that contain Japanese characters.",
    )
    parser.add_argument(
        "--output-text",
        type=Path,
        help="Optional UTF-8 text output path.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional JSON output path.",
    )
    parser.add_argument(
        "--debug-image",
        type=Path,
        help="Optional annotated debug image path.",
    )
    parser.add_argument(
        "--debug-dir",
        type=Path,
        help="Optional directory where cropped region images will be written.",
    )
    return parser


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    result = run_single_image_ocr(args)
    write_outputs(
        result,
        output_text=args.output_text,
        output_json=args.output_json,
        debug_image=args.debug_image,
    )
    if result.raw_text:
        print("\n=== OCR TEXT ===")
        print(result.raw_text)
    else:
        print("\n=== OCR TEXT ===\n<empty>")


if __name__ == "__main__":
    main()
