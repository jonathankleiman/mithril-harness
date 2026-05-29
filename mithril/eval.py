"""Grade a run with LAB's score_rubric, using a DeepSeek judge.

This is the *grading* step — it is allowed to read the rubric. It is fully
separate from the agent run (which never sees criteria).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from mithril import config

config.load_env_file()

from evaluation.scoring import score_rubric
from mithril.judge import make_judge


def evaluate_run(run_dir: str | Path, task_id: str, judge_model: str | None = None,
                 parallel: int = 4) -> dict:
    run_dir = Path(run_dir)
    task_dir = config.TASKS_DIR / Path(*task_id.split("/"))
    cfg = json.loads((task_dir / "task.json").read_text())
    criteria = cfg["criteria"]          # grading step — rubric read here only
    task_desc = cfg.get("title", task_id)

    judge = make_judge(judge_model)
    result = score_rubric(criteria=criteria, run_dir=run_dir, judge=judge,
                          task_desc=task_desc, parallel=parallel)

    n_criteria = len(result.criteria_results)
    n_passed = sum(1 for c in result.criteria_results if c["verdict"] == "pass")
    all_pass = n_criteria > 0 and n_passed == n_criteria

    scores = {
        "run_id": str(run_dir.relative_to(config.RESULTS_DIR)) if str(run_dir).startswith(str(config.RESULTS_DIR)) else str(run_dir),
        "task": task_id,
        "score": result.score,
        "all_pass": all_pass,
        "n_criteria": n_criteria,
        "n_passed": n_passed,
        "summary": f"{n_passed}/{n_criteria} criteria passed." + (" ALL-PASS." if all_pass else f" Missed {n_criteria-n_passed} — FAIL."),
        "criteria_results": result.criteria_results,
        "judge_model": judge.model,
        "judge_usage": judge.usage_dict(),
        "scored_at": datetime.now().isoformat(),
    }
    (run_dir / "scores.json").write_text(json.dumps(scores, indent=2))
    return scores


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--parallel", type=int, default=4)
    args = ap.parse_args()
    s = evaluate_run(args.run_dir, args.task, args.judge_model, args.parallel)
    print(json.dumps({k: v for k, v in s.items() if k != "criteria_results"}, indent=2))
    for c in s["criteria_results"]:
        if c["verdict"] != "pass":
            print(f"  FAIL {c['id']} {c['title']}: {c['reasoning'][:160]}")
