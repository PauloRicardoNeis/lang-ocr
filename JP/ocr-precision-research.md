# OCR Precision Research

Date: 2026-10-18

## Recommendation

Replace EasyOCR in the passive worker with **meikiocr** (a detector + recognizer trained on Japanese video-game text), run it once on the full-resolution capture, and add four cheap post-filters. On 27 hand-transcribed captures:

| Pipeline | Set | Precision | Recall | Lines read exactly | False-positive JP chars | Time/frame |
|---|---|---|---|---|---|---|
| Current worker (`baseline`) | tuning (20) | 0.794 | 0.781 | 51.5% | 154 | 2.6 s (GPU) |
| meikiocr + post-filters (`meiki_post`) | tuning (20) | **0.978** | **0.984** | **94.2%** | **17** | **0.45 s (CPU)** |
| Current worker (`baseline`) | held-out (7) | 0.893 | 0.664 | 55.6% | 21 | 2.6 s (GPU) |
| meikiocr + post-filters (`meiki_post`) | held-out (7) | **0.989** | **0.992** | **94.4%** | **3** | **0.45 s (CPU)** |

The held-out images were transcribed after the filter thresholds were fixed, so the gain is not an artifact of tuning.

Concrete examples (current worker → meikiocr + filters):

- `老隊長 | 竜王加 | しろ` → `老隊長 | 竜王が棲む城だ。`
- `かけだし昌険者 | 気をぬくな。 伝承 によれば | てんしょう | 勇者の墓は | ゆうしゃ | はか | どうくつ | おく | 本番は | ほんばん | これからだぞ。` → `かけだし冒険者 | 気をぬくな。伝承によれば | 勇者の墓はこの洞窟の奥にある。 | 本番はこれからだぞ。`
- `ラルス] 日世陸下に会ってみてくれ | せいへいか` → `ぜひラルス16世陛下に会ってみてくれ。`
- `初回勝利報醐 | しょかいしょうりぼうしゅう | おうよう` → `初回勝利報酬 | 応用のウィッチデッキのデッキコードを手に入れた!`

## Why The Current Pipeline Loses Precision

Measured by changing one thing at a time (tuning set, F1):

| Change | F1 | What it shows |
|---|---|---|
| Current worker (1600 px downscale, region grouping, second OCR pass) | 0.787 | baseline |
| Same, without the 1600 px downscale | 0.788 | Downscaling is **not** the main loss |
| Single EasyOCR pass at full resolution, no region rerun | 0.814 | The region rerun re-reads furigana into the body |
| + furigana / garbage / duplicate filters (`easyocr_post`) | 0.884 | Layout filtering fixes most of the structural noise |
| Swap recognizer to meikiocr, same filters (`meiki_post`) | 0.981 | The remaining gap is EasyOCR's Japanese recognizer |

Root causes, in order of impact:

1. **EasyOCR's Japanese model misreads kanji and small kana.** Observed in the benchmark: 冒→骨/昌, 窟→農, 棲→横, 陛→陸, 頑→通, 侶→悟, 酬→醐, 寮→察, 旅→旋, ょ→よ, っ→つ. No EasyOCR setting fixes this.
2. **Furigana becomes separate lines interleaved with the text** (`勇者の墓は | ゆうしゃ | はか`), and the second-pass region crop mixes it back into body lines.
3. **Whole-monitor capture.** The daemon uses DXGI duplication of the primary monitor, so Codex/ChatGPT/Steam windows next to the game produce glyph-shaped garbage (`川`, `口`, `ロ`, `日`) and mixed Latin lines.

## Engines Compared

All engines use the same post-filters where noted. Tuning set, 20 images.

| Engine | Precision | Recall | F1 | Lines exact | Time/frame | Notes |
|---|---|---|---|---|---|---|
| `meiki_post` | 0.978 | 0.984 | 0.981 | 94.2% | 0.45 s CPU | Recommended |
| `easydet_manga` (EasyOCR boxes + manga-ocr per line) | 0.961 | 0.869 | 0.912 | 76.7% | 2.2 s GPU | Kanji mostly right; misses whatever EasyOCR's detector misses |
| `easyocr_post` | 0.927 | 0.845 | 0.884 | 63.1% | 1.7 s GPU | Best you can get without changing recognizer |
| `meiki_raw` (no filters) | 0.796 | 0.987 | 0.881 | 95.1% | 0.47 s CPU | Reads furigana and desktop text too |
| `baseline` | 0.794 | 0.781 | 0.787 | 51.5% | 2.6 s GPU | Current worker |
| `paddle_v5_post` (PP-OCRv5 server) | 0.941* | 0.970* | 0.955* | 86.2%* | ~50 s CPU | *17 of 20 images; see below |

PaddleOCR PP-OCRv5 is the closest competitor on accuracy but is not practical here. The installed `paddlepaddle` 3.3.1 is CPU-only: ~50 s per 4K frame with oneDNN disabled. With oneDNN on it crashes immediately, and it still segfaulted intermittently, so only 17 of the 20 tuning images finished. On those same 17 images `meiki_post` scored 0.974 precision / 0.992 recall, against PaddleOCR's 0.941 / 0.970. GPU Paddle on the RTX 5070 Ti would need a Blackwell-capable `paddlepaddle-gpu` build.

Researched but not run here:

- **OneOCR** (Windows 11 Snipping Tool engine, via `oneocr`/owocr): community reports put it among the best local engines, but it needs proprietary DLLs copied out of Snipping Tool, which is not installed on this machine, and nothing licenses that use.
- **Windows.Media.Ocr** (`winocr`): needs the `ja-JP` OCR language capability, which is not installed. owocr does not recommend it over OneOCR.
- **Chrome Screen AI / Google Lens** (via owocr): strong reputation, but Screen AI is an extracted Chrome component and Lens is an unofficial cloud API; both are in a ToS grey area.
- **Local VLMs** (Qwen3-VL, DeepSeek-OCR, PaddleOCR-VL): heavier, can drop or invent text, and unnecessary given the meikiocr result. PaddleOCR-VL's Blackwell support is Linux/Docker only.

## Integration Plan

1. **Add a `MeikiOcrBackend` to `ocr_backends.py`.** meikiocr's dependencies are unpinned and the current environment already has numpy, opencv-python-headless and huggingface-hub, so `pip install meikiocr` should only add `onnxruntime` and its small dependencies (not tried in the main environment; the benchmark used a separate venv). Models are ~45 MB from Hugging Face, cached under `JP/data/model_cache/huggingface`. CPU onnxruntime is already faster than the current GPU path, so OCR no longer needs torch/CUDA.
2. **Simplify `EasyOcrAdapter.extract_blocks` into a single pass** on the full-resolution capture (meikiocr resizes internally for detection and recognizes from full-resolution crops), then apply, in order:
   - drop characters with confidence < 0.3 (stray `、。」` in word gaps score 0.1–0.2);
   - drop lines with fewer than 2 Japanese characters or < 30% Japanese characters;
   - drop boxes ≥ 70% covered by a larger box (duplicate detections);
   - drop furigana: a kana-only box under 0.85× the height of a kanji box just below it that it overlaps horizontally (owocr's rule).

   The reference implementation of all four is in `JP/ocr_bench/engines.py` (`meiki_boxes` + `postprocess`). Keep EasyOCR selectable with an env flag for comparison.
3. **Capture the game window, not the whole monitor.** The daemon currently uses `windows-capture`'s DXGI duplication of the primary monitor; the same crate also has a Graphics Capture path that targets a single window. Alternatively, crop each frame to the foreground window's rect. This removes the largest remaining false-positive source.
4. **Gate OCR changes on the benchmark** (`python JP/ocr_bench/run.py <engine>`), and add captures to `JP/ocr_bench/gt.json` when a new game shows up.

This supersedes most of `horizontal_dialog_ocr_spec.md`: region detection, the title/body split and the manga-ocr fallback were all aimed at furigana fragmentation and kanji confusion, which meikiocr plus the furigana filter already fix.

## Benchmark

- Location: `JP/ocr_bench/` (`gt.json`, `engines.py`, `run.py`). Images are decoded to lossless PNG under `JP/data/ocr_bench/png/`, results cached under `JP/data/ocr_bench/results/` (both git-ignored).
- Set: 27 captures from `sources` — Dragon Quest I&II HD-2D dialogue and menus, Shadowverse (Switch emulator) dialogue and menus, Netflix + Language Reactor subtitle panels, and one desktop with no Japanese. 7 are a held-out set.
- Ground truth: game/subtitle text only, furigana excluded. Japanese text visible in other windows is listed as `extra_ok`, so reading it is not penalized.
- Metrics count Japanese characters only (kana, kanji, ー, 々 after NFKC). Precision = correct output characters / all output characters; recall = correct output characters / ground-truth characters; "lines exact" = ground-truth lines that appear verbatim in the output.
- Running meikiocr: it is not installed in the main environment. Either `pip install meikiocr` there, or use a separate venv and run `run.py meiki_post` with that interpreter. EasyOCR engines need the main environment.

## Caveats

- 27 images, transcribed by one reader; ground truth may contain small mistakes.
- Coverage is mostly DQ HD-2D, Shadowverse and subtitles. Vertical text is out of scope (meikiocr's vertical model is beta).
- meikiocr handles at most 64 lines per frame and 48 characters per line; dense browser pages can exceed that.
- Remaining meikiocr errors: び→ぴ, ょ→よ, and one subtitle line missed.
- The furigana filter throws away reading hints. They could be kept as reading candidates for the analyzer later.
- meikiocr code is Apache-2.0 and both model repos are tagged LGPL-3.0 on Hugging Face: fine for personal use, check before redistributing.

## Sources

- meikiocr: <https://github.com/rtr46/meikiocr>, models <https://huggingface.co/rtr46/meiki.text.detect.v0>, <https://huggingface.co/rtr46/meiki.txt.recognition.v0>
- owocr (engine wrappers, furigana filter): <https://github.com/AuroraWright/owocr>
- OneOCR wrapper: <https://github.com/AuroraWright/oneocr>
- GameSentenceMiner OCR docs: <https://docs.gamesentenceminer.com/docs/features/ocr/>
- PP-OCRv5: <https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5.html>
- manga-ocr: <https://github.com/kha-white/manga-ocr>
- EasyOCR options (`canvas_size`, decoders): <https://www.jaided.ai/easyocr/documentation/>
