"""Render the Results / Failure-analysis markdown for REPORT.md from a sweep."""

from __future__ import annotations

import argparse
import json
from collections import Counter

from mithril import config
from mithril.analyze import load_sweep


def pct(x):
    return f"{x*100:.1f}%" if x is not None else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="ab15")
    args = ap.parse_args()
    base = config.RESULTS_DIR / f"sweep-{args.tag}"
    # Prefer the pro-judge re-grade summary when present.
    spath = base / "summary_pro.json"
    if not spath.exists():
        spath = base / "summary.json"
    summary = json.loads(spath.read_text())
    print(f"<!-- source: {spath.name}, judge={summary.get('judge_model','?')} -->\n")
    rows = load_sweep(args.tag)

    print("## 6. Results\n")
    print(f"Sample: {len(summary.get('tasks', []))} tasks, stratified across practice areas. "
          f"Agent + judge: DeepSeek-v4-pro. Actual spend (balance delta): "
          f"**${summary.get('balance_delta_usd')}**.\n")
    print("| Harness | All-pass | All-pass rate | 95% CI | Criterion-pass rate | Contamination |")
    print("|---|---|---|---|---|---|")
    for arm in ("baseline", "mithril"):
        s = summary["by_arm"].get(arm)
        if not s:
            continue
        ci = s["all_pass_ci95"]
        print(f"| {arm} | {s['all_pass_count']}/{s['n_tasks']} | **{pct(s['all_pass_rate'])}** | "
              f"[{pct(ci[0])}, {pct(ci[1])}] | {pct(s['criterion_pass_rate'])} "
              f"({s['criteria_passed']}/{s['criteria_total']}) | {s['contamination_flags']} |")

    b = summary["by_arm"].get("baseline")
    m = summary["by_arm"].get("mithril")
    if b and m and b["all_pass_rate"] is not None and m["all_pass_rate"] is not None:
        mult = (m["all_pass_rate"] / b["all_pass_rate"]) if b["all_pass_rate"] else float("inf")
        print(f"\n**Harness lift:** all-pass {pct(b['all_pass_rate'])} → {pct(m['all_pass_rate'])} "
              f"({'∞' if mult==float('inf') else f'{mult:.1f}×'}); "
              f"criterion-pass {pct(b['criterion_pass_rate'])} → {pct(m['criterion_pass_rate'])}.")

    # per-task table
    print("\n### Per-task (mithril)\n")
    print("| Task | Criteria | Passed | Result | Misses |")
    print("|---|---|---|---|---|")
    for r in sorted([r for r in rows if r["_arm"] == "mithril"], key=lambda r: r["n_criteria"]):
        miss = r["n_criteria"] - r["n_passed"]
        res = "✅ ALL-PASS" if r["all_pass"] else "fail"
        print(f"| {r['task'].split('/')[-1][:48]} | {r['n_criteria']} | {r['n_passed']} | {res} | {miss} |")

    # all-pass vs rubric size
    print("\n### All-pass vs. rubric size (mithril)\n")
    buckets = {"≤40": [], "41–60": [], "61–90": [], ">90": []}
    for r in rows:
        if r["_arm"] != "mithril":
            continue
        n = r["n_criteria"]
        k = "≤40" if n <= 40 else "41–60" if n <= 60 else "61–90" if n <= 90 else ">90"
        buckets[k].append(1 if r["all_pass"] else 0)
    print("| Rubric size | Tasks | All-pass |")
    print("|---|---|---|")
    for k, v in buckets.items():
        if v:
            print(f"| {k} criteria | {len(v)} | {sum(v)}/{len(v)} = {pct(sum(v)/len(v))} |")

    print("\n## 7. Failure analysis\n")
    focus = [r for r in rows if r["_arm"] == "mithril"]
    miss_dist = Counter(r["n_criteria"] - r["n_passed"] for r in focus)
    print(f"Misses-per-task distribution (mithril): {dict(sorted(miss_dist.items()))}\n")
    near = sorted([r for r in focus if 0 < (r["n_criteria"] - r["n_passed"]) <= 3],
                  key=lambda r: r["n_criteria"] - r["n_passed"])
    print(f"**Near-miss tasks** (≤3 criteria from all-pass — the actionable frontier): {len(near)}\n")
    for r in near:
        print(f"- **{r['task']}** ({r['n_passed']}/{r['n_criteria']}):")
        for c in r["criteria_results"]:
            if c["verdict"] != "pass":
                print(f"  - ✗ {c['title'][:80]} — {c['reasoning'][:160]}")


if __name__ == "__main__":
    main()
