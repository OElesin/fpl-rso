#!/usr/bin/env python3
"""
Aggregate scripts/compare_models.py output into one markdown table.

Token counts are only taken from attempts where the agent finished normally
(status other than no_candidate/error); failed calls don't report usage.
Input tokens are not compared across providers because prompt caching is
reported differently per provider.

Usage: python scripts/summarize_comparison.py /tmp/compare/*.jsonl
"""

import json
import statistics
import sys
from collections import Counter

FINISHED = {"improved", "not_improved", "invalid", "eval_crash"}


def main(paths):
    rows = {}
    for p in paths:
        for line in open(p):
            r = json.loads(line)
            rows.setdefault(r["model"], []).append(r)

    print("| Model | Attempts | Candidates scored | Improved (>= +0.1) | Best delta | Median delta | Mean latency (s) | Mean output tokens | Failures |")
    print("|---|---|---|---|---|---|---|---|---|")
    for model, rs in rows.items():
        deltas = [r["delta"] for r in rs if r["delta"] is not None]
        improved = sum(1 for r in rs if r["status"] == "improved")
        lat = [r["latency_s"] for r in rs if r["latency_s"] is not None]
        out_tok = [r["output_tokens"] for r in rs if r["status"] in FINISHED and r["output_tokens"]]
        fails = Counter(r["status"] for r in rs if r["status"] not in FINISHED)
        print(
            f"| {model} | {len(rs)} | {len(deltas)} | {improved} | "
            f"{max(deltas):+.2f} | {statistics.median(deltas):+.2f} | "
            if deltas else f"| {model} | {len(rs)} | 0 | 0 | n/a | n/a | ",
            end="",
        )
        print(
            f"{statistics.mean(lat):.0f} | "
            f"{statistics.mean(out_tok):,.0f} | " if out_tok else f"{statistics.mean(lat):.0f} | n/a | ",
            end="",
        )
        print(", ".join(f"{k} x{v}" for k, v in fails.items()) or "none", "|")


if __name__ == "__main__":
    main(sys.argv[1:])
