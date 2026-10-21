# Spec: Passive OCR Improvements For Horizontal Dialogue

## Summary

This spec defines a focused improvement pass for the passive OCR worker so it performs better on horizontal in-game dialogue boxes with:

- large main text
- small furigana above kanji
- outlined fonts
- textured backgrounds inside the dialogue panel

The goal is to improve OCR quality while preserving the passive value of the tool: screenshots should still flow automatically into the catalog and generate vocabulary candidates without requiring manual ROI selection per image.

This spec does **not** assume that the correct answer is to reduce automation. The intent is to improve extraction quality.

## Problem Statement

The current worker in `capture_worker.py` is OCR-first:

1. Run `EasyOCR` on the screenshot.
2. Filter horizontal Japanese boxes.
3. Group boxes into regions.
4. Re-run `EasyOCR` on those grouped regions.
5. Build `raw_text` from the surviving blocks.

This works for some clean horizontal text, but it fails on common JRPG dialogue UI where:

- furigana is detected as independent text blocks
- the main line is fragmented into multiple boxes
- rare kanji are confused with common lookalikes
- outline + background texture reduces confidence on the main phrase

Recent failure example:

- target text: `\u7adc\u738b\u304c\u68f2\u3080\u57ce\u3060\u3002`
- `EasyOCR` produced fragments close to:
  - `\u308a\u3086\u3046\u304a\u3046`
  - `\u3059`
  - `\u3057\u308d`
  - `\u7adc\u738b\u52a0`
  - `\u6a2a\u3080\u57ce\u3060\u3002`

The main issue is OCR quality at extraction time, not downstream vocabulary promotion.

## Goals

- Improve OCR quality for horizontal dialogue boxes in game screenshots.
- Reduce cases where furigana disrupts the main line reading.
- Keep the flow passive and automatic.
- Preserve current database ingestion and source creation behavior.
- Prefer solutions that improve the main path before adding backend complexity.

## Non-Goals

- Full support for arbitrary manga-style vertical text in passive capture.
- Manual ROI per screenshot.
- Game-specific templates in v1.
- Heavy language correction that rewrites OCR into "best guess" text with no audit trail.
- Replacing the passive workflow with manual review as the primary mode.

## Main Design Direction

The next version should become **region-first for dialogue**, not OCR-first for the whole screenshot.

That means:

1. Detect likely dialogue panel regions first.
2. Crop those regions.
3. OCR the cropped dialogue region.
4. Build `raw_text` from the best cropped result.

This is a better fit for the target problem because it removes large amounts of irrelevant screen content before recognition.

## Proposed Improvements

### 1. Dialogue Region Detection

Add a lightweight dialogue-region detector before OCR.

Target properties of a candidate dialogue region:

- large dark or semi-transparent panel
- centered or lower-center horizontal placement
- contains several high-contrast text boxes
- wider than tall
- stable rectangular or near-rectangular footprint

Possible implementation order:

1. Heuristic region proposals from OCR box density:
   - cluster nearby Japanese horizontal boxes
   - score regions that are wide, dense, and centered low on screen
2. Visual panel heuristics:
   - detect large dark translucent rectangles
   - detect high local contrast inside those panels
3. Combine both signals into one region score

The key change is that the worker should stop treating the whole screenshot as the main OCR target.

### 2. Title And Body Split Inside Dialogue Region

After a dialogue region is found, split it into likely text subregions:

- speaker/title area
- dialogue body area

This matters because the title often OCRs well while the body is where furigana causes damage.

Initial heuristics:

- top line with larger text and shorter length -> likely speaker/title
- larger lower text span -> likely body
- small kana-only boxes above larger kanji-containing boxes should remain available as signals, but they should not define the main body segmentation by themselves

This is **not** the same as globally ignoring furigana-like text. The split is layout-driven, not content-driven.

### 3. Better Main OCR Path For Horizontal Dialogue

Keep `EasyOCR` as the primary backend for passive capture, but run it on the cropped dialogue subregion instead of on the whole screenshot whenever a valid dialogue region is found.

Expected gains:

- less background noise
- fewer unrelated detections
- better box grouping
- lower chance that furigana fragments dominate the result

This should be the default path and the first implementation milestone.

### 4. Optional Local Fallback On Suspicious Body Crop

If region-first `EasyOCR` still performs poorly on the dialogue body crop, allow an optional **local fallback** for that crop only.

Important constraints:

- no fallback on full-screen image
- no fallback on title crop if primary already looks good
- no fallback if region-first `EasyOCR` is already good enough
- fallback is optional and should be added only if the benchmark justifies it

Candidate fallback backend:

- `manga-ocr` on the dialogue body crop only

Why this is scoped locally:

- `manga-ocr` helped on a tight crop in testing
- `manga-ocr` did not help on the full screenshot
- local fallback contains complexity and runtime cost

## How To Decide Whether Fallback Is Better

The system cannot know ground truth automatically. It can only estimate which output is more plausible.

So the worker should use a **quality score**, not a binary "backend B is always better" rule.

### Fallback Trigger

Only run fallback if the primary body OCR is suspicious.

Suggested suspicious signals:

- low mean OCR confidence on body blocks
- body output broken into many tiny fragments
- many kana-only micro-lines above larger text
- low-confidence rejection removed a large share of body text
- resulting body text looks structurally weak

### Candidate Quality Score

Score the primary output and fallback output separately.

Suggested score components:

- `+` count of Japanese characters
- `+` count of kanji-containing tokens
- `+` Sudachi tokenization quality
  - more normal tokens
  - fewer broken one-character fragments
- `+` count of matches to existing catalog terms and known kanji
- `+` normal sentence-shape signals
  - ends with `\u3002`, `!`, `?`, `\u3060`, `\u3067\u3059`, `\u304b`, etc.
- `-` orphan one-character lines
- `-` multiple tiny kana-only lines stacked above the main line
- `-` symbol/noise ratio
- `-` obvious OCR garbage fragments

Notes:

- `EasyOCR` provides confidence and should contribute to the score.
- `manga-ocr` may not provide equivalent confidence, so its score should rely more on text plausibility and token quality.
- Keep the scoring interpretable and logged for debugging.

### Winner Selection Rule

Use a conservative rule:

- keep primary unless fallback score exceeds primary by a clear margin
- if the scores are close, keep primary
- log both candidate texts and their scores

Suggested initial rule:

- accept fallback only if `fallback_score >= primary_score + margin`
- initial margin can be something like `8.0`

This avoids flip-flopping on ambiguous cases.

## Why Not Globally Ignore Furigana-Like Text

A global furigana-ignore filter is risky because small kana text can also be legitimate content:

- short dialogue fragments
- particles
- menu text
- real words like `shiro`, `suru`, `hai`

So the safe direction is:

- improve regioning and body extraction first
- use small-kana layout only as a quality signal
- avoid aggressive global deletion rules

## Benchmark Requirement

No OCR change should be judged from one screenshot.

Create a benchmark set of real screenshots:

- 20-50 horizontal dialogue screenshots
- include outlined fonts
- include furigana cases
- include clean cases without furigana
- include title + body layouts

For each screenshot, record:

- expected text
- current worker output
- candidate output after each change

Metrics:

- exact line match rate
- character error rate
- kanji correctness on body text
- rate of body fragmentation
- rate of false kana-only fragments in final `raw_text`

## Rollout Plan

### Phase 1. Benchmark Harness

- curate benchmark screenshots
- store expected text
- add a repeatable evaluation script

Acceptance:

- we can compare baseline vs modified OCR on the same set

### Phase 2. Dialogue Region Detection

- add candidate dialogue region proposals
- score and choose best region
- log region bounds and score

Acceptance:

- the worker finds the dialogue panel reliably on the benchmark set

### Phase 3. Title/Body Split

- split selected dialogue region into title and body
- OCR them separately with `EasyOCR`

Acceptance:

- body OCR improves on furigana-heavy dialogue cases

### Phase 4. Optional Local Fallback

- add fallback only on suspicious body crops
- compare outputs with quality score
- keep primary unless fallback clearly wins

Acceptance:

- fallback improves benchmark accuracy enough to justify extra cost
- if not, remove or disable the fallback path

### Phase 5. Production Tuning

- tune thresholds from benchmark results
- add logs for:
  - chosen dialogue region
  - title/body bounds
  - primary score
  - fallback score
  - chosen backend

Acceptance:

- the worker remains stable in passive sessions
- OCR quality improves without unacceptable runtime cost

## Logging Requirements

For each processed capture, log:

- whether dialogue region detection was used
- selected dialogue region bounds and score
- title/body crop bounds
- primary OCR text for each crop
- fallback OCR text when used
- quality score per candidate
- final chosen text
- OCR runtime per stage

This is required so OCR regressions can be audited later.

## Acceptance Criteria

The task is successful if:

- horizontal dialogue screenshots produce more accurate `raw_text`
- furigana-heavy cases stop breaking the main body as often
- the passive pipeline remains automatic
- runtime remains acceptable for tray usage
- fallback is only kept if benchmarked gains justify it

## Open Questions

- what is the cheapest reliable dialogue-region detector for this UI style?
- should region detection rely only on OCR box geometry first, or also on panel-color heuristics?
- what exact benchmark size is enough before rollout?
- does `manga-ocr` improve enough on body crops to justify the extra complexity?

## Recommended First Implementation

Start with:

1. benchmark set
2. dialogue region detection
3. title/body split
4. region-first `EasyOCR`

Do **not** implement fallback until those are measured.

If region-first `EasyOCR` already fixes most failures, stop there.
