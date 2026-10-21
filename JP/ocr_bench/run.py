"""OCR precision benchmark: run engines over hand-transcribed captures and score them.

    python JP/ocr_bench/run.py baseline easyocr_post     # run (cached per image) + score
    python JP/ocr_bench/run.py --score                   # score cached results only

Ground truth lives in gt.json (game text only, furigana excluded). Scores compare
Japanese characters only (kana, kanji, ー, 々), after NFKC normalization.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

from engines import ENGINES, JP_DIR, is_jp_char

BENCH_DIR = Path(__file__).resolve().parent
DATA_DIR = JP_DIR / "data" / "ocr_bench"
PNG_DIR = DATA_DIR / "png"
RESULTS_DIR = DATA_DIR / "results"


def load_gt() -> dict:
    return json.loads((BENCH_DIR / "gt.json").read_text(encoding="utf-8"))


def ensure_pngs(source_ids) -> dict[str, Path]:
    """Decode each benchmark source image once into a lossless 4K PNG."""
    from PIL import Image

    PNG_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(JP_DIR / "data" / "lang_jp.sqlite3")
    paths = {}
    for source_id in source_ids:
        target = PNG_DIR / f"{source_id}.png"
        if not target.exists():
            (relative,) = connection.execute(
                "SELECT source_path FROM sources WHERE id = ?", (int(source_id),)
            ).fetchone()
            Image.open(JP_DIR / relative).convert("RGB").save(target)
        paths[source_id] = target
    return paths


def norm(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if is_jp_char(ch))


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def score_image(pred_lines: list[str], entry: dict) -> dict:
    gt_lines = [line for line in (norm(item) for item in entry["lines"]) if line]
    gt_text = "".join(gt_lines)
    pred_text = "".join(norm(line) for line in pred_lines)
    gt_bag, pred_bag = Counter(gt_text), Counter(pred_text)
    # extra_ok: real Japanese visible outside the game (other windows); not penalized, not required
    allowed_extra = Counter(norm("".join(entry.get("extra_ok", []))))
    matched = sum((gt_bag & pred_bag).values())
    tolerated = sum(((pred_bag - gt_bag) & allowed_extra).values())
    return {
        "gt_chars": len(gt_text),
        "matched": matched,
        "false_pos": len(pred_text) - matched - tolerated,
        "edit": levenshtein(pred_text, gt_text),
        "gt_lines": len(gt_lines),
        "line_hits": sum(1 for line in gt_lines if line in pred_text),
    }


def run_engine(name: str, images: dict[str, Path]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{name}.json"
    results = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    for source_id, path in images.items():
        if source_id in results:
            continue
        start = time.perf_counter()
        lines = ENGINES[name](path)
        results[source_id] = {"lines": lines, "seconds": round(time.perf_counter() - start, 3)}
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{name} {source_id} {results[source_id]['seconds']:.2f}s {' | '.join(lines)[:110]}", flush=True)


def print_scores() -> None:
    gt = load_gt()
    keys = ["precision", "recall", "f1", "cer", "line_acc", "false_pos", "sec_per_img"]
    for label, holdout in (("tuning set", False), ("held-out set", True)):
        subset = {k: v for k, v in gt.items() if bool(v.get("holdout")) == holdout}
        print(f"\n== {label} ({len(subset)} images)")
        print(f"{'engine':16} " + " ".join(f"{key:>11}" for key in keys))
        table = []
        for out_path in sorted(RESULTS_DIR.glob("*.json")):
            results = json.loads(out_path.read_text(encoding="utf-8"))
            ids = [source_id for source_id in subset if source_id in results]
            if len(ids) < len(subset):
                continue
            total = Counter()
            for source_id in ids:
                total.update(score_image(results[source_id]["lines"], subset[source_id]))
            precision = total["matched"] / max(1, total["matched"] + total["false_pos"])
            recall = total["matched"] / max(1, total["gt_chars"])
            # first image of a run pays model warm-up; report steady-state timing
            ordered = [results[source_id]["seconds"] for source_id in gt if source_id in results][1:]
            table.append(
                (
                    out_path.stem,
                    {
                        "precision": round(precision, 3),
                        "recall": round(recall, 3),
                        "f1": round(2 * precision * recall / max(1e-9, precision + recall), 3),
                        "cer": round(total["edit"] / max(1, total["gt_chars"]), 3),
                        "line_acc": round(total["line_hits"] / max(1, total["gt_lines"]), 3),
                        "false_pos": total["false_pos"],
                        "sec_per_img": round(sum(ordered) / max(1, len(ordered)), 2),
                    },
                )
            )
        for name, row in sorted(table, key=lambda item: -item[1]["f1"]):
            print(f"{name:16} " + " ".join(f"{row[key]:>11}" for key in keys))


if __name__ == "__main__":
    engines = [arg for arg in sys.argv[1:] if arg != "--score"]
    if engines:
        images = ensure_pngs(load_gt().keys())
        for engine in engines:
            run_engine(engine, images)
    print_scores()
