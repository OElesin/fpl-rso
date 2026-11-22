#!/usr/bin/env python3
"""
Controlled multi-model comparison for the RSI proposer.

Every model gets the SAME seed strategy, the SAME data and public/private split,
and N independent attempts (no compounding between attempts), so each attempt is
a clean sample of "how good is this model at proposing one improvement?".

Per attempt we record: produced a candidate?, valid?, private-score delta vs the
seed, wall-clock latency, token usage, and any error. One JSON line per attempt.

Usage:
    python scripts/compare_models.py --model gpt-6-astra --iterations 5 \
        --seed-file /tmp/seed.py --out /tmp/compare/gpt-6-astra.jsonl

Run one process per model in parallel; results are aggregated by
scripts/summarize_comparison.py.
"""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.loader import load_seasons
from eval.backtest import BacktestConfig, evaluate_strategy_code, load_strategy_from_file, evaluate_agent
from outer_loop import proposer
from outer_loop.proposer import generate_candidate, validate_candidate

THRESHOLD = 0.1  # same keep-threshold as the production loop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="short name or full Bedrock id")
    ap.add_argument("--iterations", type=int, default=5)
    ap.add_argument("--seed-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seasons", nargs="+", default=["2024-25", "2025-26", "2026-27"])
    ap.add_argument("--cand-dir", default="/tmp/compare")
    args = ap.parse_args()

    seed = Path(args.seed_file).read_text()
    Path(args.cand_dir).mkdir(parents=True, exist_ok=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    data = load_seasons(args.seasons)
    cfg = BacktestConfig(seasons=args.seasons)
    base = evaluate_strategy_code(cfg, data, seed)
    eval_dict = {
        "public_score": base.public_score,
        "private_score": base.private_score,
        "total_score": base.total_score,
        "season_results": base.season_results,
    }
    print(f"[{args.model}] seed baseline private={base.private_score:.2f}", flush=True)

    for i in range(1, args.iterations + 1):
        rec = {
            "model": args.model,
            "attempt": i,
            "seed_score": round(base.private_score, 4),
            "status": None,
            "delta": None,
            "private_score": None,
            "latency_s": None,
            "input_tokens": None,
            "output_tokens": None,
            "candidate_chars": None,
            "error": None,
        }
        t0 = time.time()
        proposer.LAST_USAGE.clear()  # failed calls don't update it; avoid stale counts
        try:
            cand = generate_candidate(
                current_code=seed,
                eval_result=eval_dict,
                iteration=i,
                max_iterations=args.iterations,
                failed_attempts=[],
                model=args.model,
                region="us-east-1",
            )
            rec["latency_s"] = round(time.time() - t0, 1)
            usage = dict(proposer.LAST_USAGE)
            rec["input_tokens"] = usage.get("inputTokens")
            rec["output_tokens"] = usage.get("outputTokens")

            if cand is None:
                rec["status"] = "no_candidate"
            else:
                rec["candidate_chars"] = len(cand)
                ok, reason = validate_candidate(cand)
                if not ok:
                    rec["status"] = "invalid"
                    rec["error"] = reason[:200]
                else:
                    path = Path(args.cand_dir) / f"cand_{args.model}_{i}.py"
                    path.write_text(cand)
                    try:
                        res = evaluate_strategy_code(cfg, data, cand)
                        rec["private_score"] = round(res.private_score, 4)
                        rec["delta"] = round(res.private_score - base.private_score, 4)
                        rec["status"] = "improved" if rec["delta"] >= THRESHOLD else "not_improved"
                    except Exception as e:  # candidate crashed in the backtest
                        rec["status"] = "eval_crash"
                        rec["error"] = str(e)[:200]
        except Exception as e:
            rec["latency_s"] = round(time.time() - t0, 1)
            rec["status"] = "error"
            rec["error"] = (str(e) or traceback.format_exc())[:300]

        with out.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[{args.model}] attempt {i}: {rec['status']} delta={rec['delta']} "
              f"{rec['latency_s']}s out_tok={rec['output_tokens']}", flush=True)


if __name__ == "__main__":
    main()
