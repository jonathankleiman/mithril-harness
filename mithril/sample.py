"""Stratified, reproducible task sampling for evaluation.

We sample uniformly at random *within* practice areas (so every area is
represented) with a fixed seed. No filtering on criteria-count or document
count — that would bias the all-pass estimate. We do record each task's size
so the sample's representativeness (and cost) is transparent.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
from pathlib import Path

from mithril import config


def all_tasks() -> list[str]:
    ids = []
    for p in glob.glob(str(config.TASKS_DIR / "**" / "task.json"), recursive=True):
        rel = Path(p).parent.relative_to(config.TASKS_DIR)
        ids.append(str(rel))
    return sorted(ids)


def task_size(task_id: str) -> tuple[int, int]:
    """(n_criteria, n_documents) — for reporting only."""
    td = config.TASKS_DIR / Path(*task_id.split("/"))
    cfg = json.loads((td / "task.json").read_text())
    docs = td / "documents"
    ndoc = sum(len(f) for _, _, f in os.walk(docs)) if docs.is_dir() else 0
    return len(cfg.get("criteria", [])), ndoc


def stratified_sample(n: int, seed: int = 42) -> list[str]:
    rng = random.Random(seed)
    by_area: dict[str, list[str]] = {}
    for t in all_tasks():
        by_area.setdefault(t.split("/")[0], []).append(t)
    areas = sorted(by_area)
    for a in areas:
        rng.shuffle(by_area[a])
    # Round-robin across areas so the sample spreads evenly.
    picked, i = [], 0
    while len(picked) < n and any(by_area[a] for a in areas):
        a = areas[i % len(areas)]
        if by_area[a]:
            picked.append(by_area[a].pop())
        i += 1
    return picked


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=15)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    sample = stratified_sample(args.n, args.seed)
    sizes = [task_size(t) for t in sample]
    print(json.dumps(sample, indent=2))
    crit = [c for c, _ in sizes]
    print(f"\n# {len(sample)} tasks | criteria: min={min(crit)} median={sorted(crit)[len(crit)//2]} max={max(crit)} total={sum(crit)}")
