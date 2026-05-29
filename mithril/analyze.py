"""Failure analysis over a sweep: where did all-pass slip, and why.

Aggregates every scores.json under a sweep tag and surfaces:
  - per-arm all-pass / criterion-pass with the baseline→mithril delta
  - "near-miss" tasks (missed 1–3 criteria) — the actionable frontier for
    pushing toward all-pass
  - every failed criterion with the judge's reasoning, so failure *modes* can
    be read off directly (missing finding, wrong figure, shallow rationale…)
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from mithril import config


def load_sweep(tag: str) -> list[dict]:
    base = config.RESULTS_DIR / f"sweep-{tag}"
    rows = []
    for sp in base.rglob("scores.json"):
        s = json.loads(sp.read_text())
        # arm is the parent dir of the run (…/<task>/<arm>/scores.json)
        arm = sp.parent.name
        s["_arm"] = arm
        s["_run_dir"] = str(sp.parent)
        rows.append(s)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="ab15")
    ap.add_argument("--arm", default="mithril")
    ap.add_argument("--show-fails", action="store_true")
    args = ap.parse_args()

    rows = load_sweep(args.tag)
    arms = sorted({r["_arm"] for r in rows})
    print(f"=== sweep {args.tag}: {len(rows)} graded runs, arms={arms} ===\n")

    for arm in arms:
        ar = [r for r in rows if r["_arm"] == arm]
        ap_ct = sum(1 for r in ar if r["all_pass"])
        cp = sum(r["n_passed"] for r in ar)
        ct = sum(r["n_criteria"] for r in ar)
        print(f"[{arm}] all-pass {ap_ct}/{len(ar)} = {ap_ct/len(ar)*100:.1f}% | "
              f"criterion-pass {cp}/{ct} = {cp/ct*100:.1f}%")
        # near-miss distribution
        miss = sorted(r["n_criteria"] - r["n_passed"] for r in ar)
        from collections import Counter
        print(f"     misses-per-task: {dict(Counter(miss))}")
    print()

    # near-miss tasks for the focus arm
    focus = [r for r in rows if r["_arm"] == args.arm]
    near = sorted([r for r in focus if 0 < (r["n_criteria"] - r["n_passed"]) <= 3],
                  key=lambda r: r["n_criteria"] - r["n_passed"])
    print(f"--- {args.arm}: near-miss tasks (≤3 missed) — the all-pass frontier ---")
    for r in near:
        m = r["n_criteria"] - r["n_passed"]
        print(f"  miss {m}: {r['task']}  ({r['n_passed']}/{r['n_criteria']})")
        for c in r["criteria_results"]:
            if c["verdict"] != "pass":
                print(f"        ✗ {c['id']} {c['title'][:70]}")
                print(f"          → {c['reasoning'][:200]}")
    print()

    if args.show_fails:
        print(f"--- ALL failed criteria ({args.arm}) ---")
        for r in focus:
            fails = [c for c in r["criteria_results"] if c["verdict"] != "pass"]
            if fails:
                print(f"\n{r['task']} ({len(fails)} fails):")
                for c in fails:
                    print(f"  ✗ {c['id']} {c['title'][:75]}\n     {c['reasoning'][:220]}")


if __name__ == "__main__":
    main()
