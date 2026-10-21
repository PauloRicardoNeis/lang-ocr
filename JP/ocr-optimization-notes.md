# OCR Optimization Notes

Date: 2026-04-05

## Current State

- Machine GPU: NVIDIA GeForce RTX 5070 Ti
- Driver/runtime check: `nvidia-smi` reports Driver `591.59` and CUDA runtime `13.1`
- Python in use: `3.12.10`
- Current PyTorch install: `torch 2.11.0+cu130`
- Current CUDA visibility in Python: `torch.cuda.is_available() == True`

This machine now has a working CUDA-enabled PyTorch install in the Python 3.12 environment used by the repo.

The backend has since been updated so EasyOCR defaults to GPU automatically when CUDA is available. The current knobs are:

- `LANG_JP_EASYOCR_GPU=auto|1|0`
- `LANG_JP_EASYOCR_LANGS=ja,en`

## Relevant Code Paths

- `JP/desktop_capture_daemon/src/main.rs`
  - `auto_start_worker` defaults to `false`
  - the daemon launches one-shot and drain worker modes through `spawn_worker(...)`
- `JP/capture_worker.py`
  - `EasyOcrAdapter.extract_blocks(...)` does a full-image OCR pass, groups regions, then re-runs OCR on grouped regions
  - `process_job_once(...)` runs OCR and translation in the same hot path
  - `main()` constructs `EasyOcrAdapter()` and `SudachiAnalyzer()` once per worker process
- `JP/ocr_backends.py`
  - `easyocr.Reader(["ja", "en"], gpu=False)` is hardcoded today
- `JP/horizontal_dialog_ocr_spec.md`
  - already points in the right direction: region-first dialogue OCR, title/body split, and optional fallback OCR only for suspicious crops

## Highest-Value Optimization Ideas

### 1. Keep the OCR worker hot

Problem:
- The daemon often spawns one-shot workers. That reloads EasyOCR and Sudachi repeatedly.

Why it matters:
- Model startup and analyzer initialization are likely the largest avoidable latency in passive capture.

Expected benefit:
- Better throughput and lower per-capture latency with minimal model-risk.

Notes:
- This is probably the best speed win before touching OCR models.

### 2. Go region-first before OCR

Problem:
- The current flow is OCR-first on the whole screenshot, then region grouping, then another OCR pass.

Why it matters:
- Whole-frame OCR wastes work on UI noise and gives weaker crops for dialogue-heavy screenshots.

Expected benefit:
- Better OCR quality and lower total OCR cost on JRPG dialogue panels.

Notes:
- Follow the direction in `JP/horizontal_dialog_ocr_spec.md`.

### 3. Re-run OCR only on the best candidate regions

Problem:
- The second OCR pass appears broader than necessary.

Why it matters:
- Re-reading several grouped regions compounds runtime quickly.

Expected benefit:
- Faster processing with little quality loss if candidate ranking is decent.

Proposed change:
- First-pass boxes should score candidate regions.
- Only re-run OCR on the top 1-2 regions unless debug mode is enabled.

### 4. Make EasyOCR runtime-configurable

Problem:
- The backend is currently hardcoded as `Reader(["ja", "en"], gpu=False)`.

Why it matters:
- There is no easy way to test GPU acceleration, `ja`-only recognition, or future backend tuning.

Expected benefit:
- Faster experiments and possibly meaningful speed gains on this machine.

Proposed change:
- Add config/env switches for:
  - `gpu=true|false`
  - languages: `["ja"]` vs `["ja", "en"]`
  - OCR max side / resize limits

### 5. Move translation off the OCR hot path

Problem:
- Translation currently happens inline during job processing.

Why it matters:
- Translation latency does not improve OCR quality and blocks job completion.

Expected benefit:
- Lower capture latency and less coupling between OCR quality and downstream enrichment.

Proposed change:
- Defer translation to:
  - review time, or
  - a separate enrichment pass/job.

## Quality-Focused Ideas

### 6. Split dialogue title and body

Problem:
- Title text and body text have different OCR behavior, especially when furigana is present.

Expected benefit:
- Better extraction on the dialogue body without losing the easier speaker/title line.

### 7. Use `manga-ocr` only as a local fallback

Problem:
- `manga-ocr` is not a good default for full-frame passive OCR.

Expected benefit:
- Better recovery on suspicious body crops without paying the full complexity cost on every image.

Trigger ideas:
- Low confidence
- Too many garbage fragments
- Strong mismatch between text density and extracted tokens

### 8. Tighten post-OCR cleanup and promotion

Problem:
- Even good OCR improvements will be diluted if noisy fragments still get promoted into terms/flashcards.

Expected benefit:
- Better downstream study quality, not just cleaner raw text.

Examples:
- Penalize punctuation-only and kana-only function-word spam in passive promotion.
- Raise the threshold for low-confidence auto-promotions.

## Speed-Focused Ideas

### 9. Reuse nearby frame geometry

Problem:
- Consecutive screenshots from the same game scene often reuse the same dialogue panel layout.

Expected benefit:
- Faster region selection by reusing the last good ROI as a prior.

### 10. Reduce search area before reducing image quality

Problem:
- Shrinking or simplifying the whole image can hurt OCR quality while still spending time on irrelevant pixels.

Expected benefit:
- Better speed/quality balance than global downscaling.

## Suggested Order Of Work

1. Make EasyOCR configurable and add benchmark logging.
2. Keep the worker hot during active capture sessions.
3. Implement region-first candidate selection.
4. Limit second-pass OCR to top-K regions.
5. Split dialogue title/body.
6. Add optional suspicious-crop fallback with `manga-ocr`.
7. Move translation off the hot path.
8. Tighten promotion/filtering rules.

## Benchmark Guidance

Do not judge OCR changes from one screenshot.

Build a small benchmark set:
- 20-50 horizontal dialogue screenshots
- include clean text, furigana-heavy text, textured panels, and noisy UI overlays

Track per run:
- raw extracted text
- confidence summary
- whether region detection triggered
- selected region bounds
- fallback usage
- OCR runtime by stage
- end-to-end job runtime

## CUDA Enablement For This Repo

### What is already present

- NVIDIA GPU: yes
- NVIDIA driver: yes
- CUDA runtime visible through `nvidia-smi`: yes

### What is missing

- A CUDA-enabled PyTorch wheel in the active Python environment

### Practical path

For EasyOCR on this machine, you usually do **not** need the full NVIDIA CUDA Toolkit unless you plan to compile CUDA software yourself.

The minimal path is:

1. Replace the CPU-only `torch` install with a CUDA-enabled PyTorch build for Windows.
2. Verify `torch.cuda.is_available()` returns `True`.
3. Make `JP/ocr_backends.py` stop forcing `gpu=False`.

### Verification snippet

```python
import torch
print(torch.__version__)
print(torch.cuda.is_available())
print(torch.cuda.device_count())
if torch.cuda.is_available():
    print(torch.cuda.get_device_name(0))
```

### Official references

- PyTorch local install guide: <https://pytorch.org/get-started/locally/>
- PyTorch previous versions and wheel indexes: <https://pytorch.org/get-started/previous-versions>
- NVIDIA CUDA installation guide for Windows: <https://docs.nvidia.com/cuda/cuda-installation-guide-microsoft-windows/>

### Practical caution

- Use the official PyTorch selector for Windows + Pip + Python and choose a CUDA build supported by the current PyTorch release.
- The Python package state on this machine is the real blocker today, not the GPU.
- After PyTorch sees CUDA, benchmark before changing the OCR architecture. GPU help is likely real, but it should be measured.

## Current Repo Behavior

- `JP/ocr_backends.py` now defaults to `LANG_JP_EASYOCR_GPU=auto`, which uses CUDA when `torch.cuda.is_available()` is true.
- `JP/capture_worker.py` now logs EasyOCR startup settings, including GPU mode and language set.
- `JP/capture_worker.py` now queues source translation as background backlog instead of blocking the OCR accept path.
- `JP/desktop_capture_daemon` now defaults to a persistent loop worker (`auto_start_worker = true`) so capture jobs and queued translations can be drained continuously.
- CPU fallback is still easy to force for comparison runs with:

```powershell
$env:LANG_JP_EASYOCR_GPU = "0"
python JP\capture_worker.py --once
```

## Queue-Based Pipeline

The current fast path is now:

1. capture screenshot
2. enqueue `capture_jobs`
3. loop worker performs OCR, source creation, analysis, and promotion
4. source is accepted immediately
5. translation is persisted as `source_translations.status = 'pending'`
6. the loop worker drains queued translations in the background when no capture job is ready

This keeps OCR/source latency low while preserving a durable backlog for translation work.
