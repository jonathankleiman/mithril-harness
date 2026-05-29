"""Run + grade many tasks across harness arms; aggregate with bootstrap CIs.

Usage:
    python -m mithril.sweep --tasks-file sample.json --arms baseline mithril \
        --agent-model deepseek-v4-pro --judge-model deepseek-v4-pro --concurrency 2
"""

from __future__ import annotations

import argparse
import json
import random
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from mithril import config

config.load_env_file()

from mithril.run import run_task
from mithril.eval import evaluate_run
from mithril.tasks import count_criteria


def _bootstrap_ci(values: list[int], iters: int = 5000, seed: int = 7) -> tuple[float, float]:
    """95% percentile bootstrap CI for the mean of 0/1 values."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iters):
        s = sum(values[rng.randrange(n)] for _ in range(n))
        means.append(s / n)
    means.sort()
    return (means[int(0.025 * iters)], means[int(0.975 * iters)])


def _cached(run_dir: Path, task_id: str, arm: str) -> dict | None:
    """If this run already completed (resume after interruption), reload it."""
    sp, mp = run_dir / "scores.json", run_dir / "metrics.json"
    if not (sp.exists() and mp.exists()):
        return None
    try:
        scores = json.loads(sp.read_text())
        m = json.loads(mp.read_text())
        return {"task": task_id, "arm": arm, "all_pass": scores["all_pass"],
                "n_criteria": scores["n_criteria"], "n_passed": scores["n_passed"],
                "agent_cost": m.get("deepseek_usage", {}).get("est_cost_usd", 0),
                "judge_cost": scores.get("judge_usage", {}).get("est_cost_usd", 0),
                "turns": m.get("turn_count"), "docs_read": m.get("documents_read"),
                "total_docs": m.get("total_documents"), "compactions": m.get("compactions", 0),
                "verify_passes": m.get("verify_passes", 0),
                "contamination_flags": m.get("contamination_flags", []),
                "run_dir": str(run_dir), "cached": True}
    except Exception:  # noqa: BLE001
        return None


def run_one(task_id: str, arm: str, agent_model: str, judge_model: str,
            run_tag: str, max_turns: int, judge_parallel: int,
            reasoning_effort: str | None = None) -> dict:
    rec = {"task": task_id, "arm": arm}
    run_id = f"sweep-{run_tag}/{task_id}/{arm}"
    run_dir = config.RESULTS_DIR / run_id
    hit = _cached(run_dir, task_id, arm)
    if hit is not None:
        return hit
    try:
        out = run_task(task_id, model=agent_model, harness=arm, max_turns=max_turns,
                       run_id=run_id, reasoning_effort=reasoning_effort)
        scores = evaluate_run(out["run_dir"], task_id, judge_model=judge_model, parallel=judge_parallel)
        rec.update({
            "all_pass": scores["all_pass"],
            "n_criteria": scores["n_criteria"],
            "n_passed": scores["n_passed"],
            "agent_cost": out["metrics"]["deepseek_usage"]["est_cost_usd"],
            "judge_cost": scores["judge_usage"]["est_cost_usd"],
            "turns": out["metrics"]["turn_count"],
            "docs_read": out["metrics"].get("documents_read"),
            "total_docs": out["metrics"].get("total_documents"),
            "compactions": out["metrics"].get("compactions", 0),
            "verify_passes": out["metrics"].get("verify_passes", 0),
            "contamination_flags": out["metrics"].get("contamination_flags", []),
            "run_dir": out["run_dir"],
        })
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["traceback"] = traceback.format_exc()[-1500:]
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-file", help="JSON list of task ids")
    ap.add_argument("--tasks", nargs="*", help="explicit task ids")
    ap.add_argument("--arms", nargs="+", default=["mithril"])
    ap.add_argument("--agent-model", default=config.AGENT_MODEL)
    ap.add_argument("--judge-model", default=config.JUDGE_MODEL)
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--judge-parallel", type=int, default=4)
    ap.add_argument("--reasoning-effort", default=None, help="agent thinking effort (e.g. max) — Claude/Anthropic agent")
    ap.add_argument("--run-tag", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args()

    if args.tasks_file:
        tasks = json.loads(Path(args.tasks_file).read_text())
    elif args.tasks:
        tasks = args.tasks
    else:
        raise SystemExit("provide --tasks-file or --tasks")

    jobs = [(t, arm) for arm in args.arms for t in tasks]
    print(f"Sweep {args.run_tag}: {len(tasks)} tasks × {len(args.arms)} arms = {len(jobs)} runs")
    bal_start = config.deepseek_balance()
    print(f"Balance at start: ${bal_start}")

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(run_one, t, arm, args.agent_model, args.judge_model,
                            args.run_tag, args.max_turns, args.judge_parallel,
                            args.reasoning_effort): (t, arm)
                for t, arm in jobs}
        for fut in as_completed(futs):
            t, arm = futs[fut]
            rec = fut.result()
            status = "ERROR" if "error" in rec else ("ALL-PASS" if rec.get("all_pass") else f"{rec.get('n_passed')}/{rec.get('n_criteria')}")
            print(f"  [{arm:8}] {t[:60]:60} → {status}")
            results.append(rec)

    bal_end = config.deepseek_balance()
    summary = summarize(results, args.arms)
    summary["balance_start"] = bal_start
    summary["balance_end"] = bal_end
    summary["balance_delta_usd"] = round(bal_start - bal_end, 4) if (bal_start and bal_end) else None
    summary["tasks"] = tasks
    out_dir = config.RESULTS_DIR / f"sweep-{args.run_tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\n" + format_summary(summary))
    print(f"Actual spend (balance delta): ${summary['balance_delta_usd']}")
    print(f"\nWritten: {out_dir}/summary.json")


def summarize(results: list[dict], arms: list[str]) -> dict:
    summary = {"by_arm": {}, "total_cost": 0.0}
    for arm in arms:
        recs = [r for r in results if r["arm"] == arm and "error" not in r]
        errs = [r for r in results if r["arm"] == arm and "error" in r]
        ap = [1 if r["all_pass"] else 0 for r in recs]
        crit_pass = sum(r["n_passed"] for r in recs)
        crit_total = sum(r["n_criteria"] for r in recs)
        cost = sum(r.get("agent_cost", 0) + r.get("judge_cost", 0) for r in recs)
        lo, hi = _bootstrap_ci(ap)
        summary["by_arm"][arm] = {
            "n_tasks": len(recs),
            "n_errors": len(errs),
            "all_pass_rate": round(sum(ap) / len(ap), 4) if ap else None,
            "all_pass_count": sum(ap),
            "all_pass_ci95": [round(lo, 4), round(hi, 4)],
            "criterion_pass_rate": round(crit_pass / crit_total, 4) if crit_total else None,
            "criteria_passed": crit_pass,
            "criteria_total": crit_total,
            "cost_usd": round(cost, 4),
            "contamination_flags": sum(len(r.get("contamination_flags", [])) for r in recs),
        }
        summary["total_cost"] += cost
    summary["total_cost"] = round(summary["total_cost"], 4)
    return summary


def format_summary(summary: dict) -> str:
    lines = ["=" * 72, "SWEEP SUMMARY", "=" * 72]
    for arm, s in summary["by_arm"].items():
        apr = s["all_pass_rate"]
        lines.append(
            f"[{arm}] all-pass {s['all_pass_count']}/{s['n_tasks']} = "
            f"{(apr*100 if apr is not None else 0):.1f}%  CI95 [{s['all_pass_ci95'][0]*100:.1f}, {s['all_pass_ci95'][1]*100:.1f}]%  | "
            f"criterion-pass {(s['criterion_pass_rate'] or 0)*100:.1f}% ({s['criteria_passed']}/{s['criteria_total']})  | "
            f"${s['cost_usd']}  | errors {s['n_errors']}  | contamination {s['contamination_flags']}")
    lines.append(f"TOTAL COST: ${summary['total_cost']}")
    lines.append("=" * 72)
    return "\n".join(lines)


if __name__ == "__main__":
    main()
