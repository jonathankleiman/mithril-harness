"""Re-grade all completed runs in a sweep with a chosen judge model.

Grading is decoupled from the agent run: deliverables already sit on disk, so
we can re-grade with the pro judge without re-running any (throughput-limited)
agent loops. Overwrites each run's scores.json and rewrites summary.json.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from mithril import config

config.load_env_file()

from mithril.eval import evaluate_run
from mithril.sweep import summarize
from mithril.deliverables import finalize_deliverables
from mithril.tasks import load_task_spec


def _regrade_one(run_dir: Path, judge_model: str, judge_parallel: int) -> dict | None:
    cfg = json.loads((run_dir / "config.json").read_text())
    task_id, arm = cfg["task"], cfg["harness"]
    out = run_dir / "output"
    if not out.exists() or not any(out.rglob("*")):
        return None  # nothing produced — skip
    # Salvage runs whose agent loop crashed before finalize: render .md → .docx.
    spec = load_task_spec(task_id)
    finalize_deliverables(out, spec.deliverables)
    if not any(out.rglob("*.docx")) and not any(out.rglob("*.xlsx")):
        return None  # still no gradeable deliverable
    scores = evaluate_run(run_dir, task_id, judge_model=judge_model, parallel=judge_parallel)
    m = json.loads((run_dir / "metrics.json").read_text()) if (run_dir / "metrics.json").exists() else {}
    return {
        "task": task_id, "arm": arm, "all_pass": scores["all_pass"],
        "n_criteria": scores["n_criteria"], "n_passed": scores["n_passed"],
        "agent_cost": m.get("deepseek_usage", {}).get("est_cost_usd", 0),
        "judge_cost": scores["judge_usage"]["est_cost_usd"],
        "turns": m.get("turn_count"), "compactions": m.get("compactions", 0),
        "verify_passes": m.get("verify_passes", 0),
        "contamination_flags": m.get("contamination_flags", []),
        "run_dir": str(run_dir),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="ab10")
    ap.add_argument("--judge-model", default="deepseek-v4-pro")
    ap.add_argument("--judge-parallel", type=int, default=6)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    base = config.RESULTS_DIR / f"sweep-{args.tag}"
    run_dirs = sorted({c.parent for c in base.rglob("config.json")})
    print(f"Re-grading {len(run_dirs)} runs under sweep-{args.tag} with judge={args.judge_model}")
    bal0 = config.deepseek_balance(); print(f"balance start: ${bal0}")

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(_regrade_one, rd, args.judge_model, args.judge_parallel): rd for rd in run_dirs}
        for fut in as_completed(futs):
            rec = fut.result()
            if rec is None:
                continue
            status = "ALL-PASS" if rec["all_pass"] else f"{rec['n_passed']}/{rec['n_criteria']}"
            print(f"  [{rec['arm']:8}] {rec['task'].split('/')[-1][:48]:48} → {status}")
            results.append(rec)

    arms = sorted({r["arm"] for r in results})
    summary = summarize(results, arms)
    bal1 = config.deepseek_balance()
    summary["judge_model"] = args.judge_model
    summary["regrade_balance_delta_usd"] = round(bal0 - bal1, 4) if (bal0 and bal1) else None
    (base / "results_pro.json").write_text(json.dumps(results, indent=2))
    (base / "summary_pro.json").write_text(json.dumps(summary, indent=2))
    from mithril.sweep import format_summary
    print("\n" + format_summary(summary))
    print(f"regrade spend: ${summary['regrade_balance_delta_usd']}")
    print(f"written: {base}/summary_pro.json")


if __name__ == "__main__":
    main()
