# Single Image OCR Spec

## Goal

Provide a working OCR path for one image at a time, without depending on the passive capture daemon.

## Scope

- Input is a single local image file.
- Output is UTF-8 text plus structured JSON for the recognized regions.
- Supported backends are:
  - `manga-ocr` for manga-style / vertical-text regions
  - `easyocr` for simpler UI / social-media screenshots
- Region selection supports:
  - visible green overlay boxes already present in the image
  - manual ROI coordinates
  - full-image fallback

## Non-goals

- General full-desktop OCR as the default path.
- Automatic vocabulary insertion into the DB in v1 of this flow.
- Perfect reading order reconstruction.
- Generic text-region detection for arbitrary screenshots.

## Chosen approach

Inspired by:
- `MangaOCR`: strong recognizer for Japanese manga / vertical text regions.
- `YomiNinja`: region/template-driven OCR is more reliable than blind whole-screen OCR.

The single-image flow is region-first:

1. Load the image.
2. Select OCR regions:
   - `overlay_boxes`: detect visible green rectangles in the screenshot.
   - `manual`: use explicit `x1,y1,x2,y2` coordinates.
   - `full_image`: use the whole image as one region.
3. Crop each region with padding.
4. Run the chosen backend on each crop.
5. Optionally keep only lines containing Japanese characters for cleaner UI/social-media OCR output.
6. Save:
   - plain text
   - JSON with coordinates and text
   - optional debug outputs

## Why this direction

- Different screenshot types need different OCR backends.
- `manga-ocr` is a recognizer, not a full desktop OCR pipeline.
- It performs better on actual Japanese text regions than on a whole desktop screenshot.
- `easyocr` is a useful fallback for cleaner UI-like screenshots where `manga-ocr` returns nothing.
- Region-first processing is the cheapest reliable path to a usable result.

## Expected usage

```powershell
py -3.12 .\single_image_ocr.py `
  --image "C:\path\to\image.png" `
  --region-mode overlay_boxes `
  --output-text "C:\path\to\image.txt" `
  --output-json "C:\path\to\image.json"
```

## Follow-up integration

Once this path is stable:

1. Reuse the same OCR function from `capture_worker.py`.
2. Run OCR only on accepted screenshots, one image at a time.
3. Keep linguistic analysis as a separate step from OCR.
