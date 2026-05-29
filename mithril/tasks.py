"""Task loading with a hard contamination boundary.

``load_task_spec`` returns ONLY the assignment spec a real associate would
receive — title, instructions, requested deliverable filenames, work type —
and the path to the read-only documents directory. It deliberately never
returns (or even retains) the ``criteria`` / ``match_criteria`` grading
rubric. The agent and the run harness see exactly this; the rubric is loaded
only by the separate grading step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mithril import config


@dataclass
class TaskSpec:
    task_id: str
    title: str
    instructions: str
    deliverables: list[str]          # requested output filenames (assignment spec)
    work_type: str
    task_dir: Path
    documents_dir: Path


def load_task_spec(task_id: str) -> TaskSpec:
    task_dir = config.TASKS_DIR / Path(*task_id.split("/"))
    cfg_path = task_dir / "task.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"task.json not found: {cfg_path}")
    cfg = json.loads(cfg_path.read_text())

    instructions = cfg.get("instructions")
    if not instructions:
        instr_path = task_dir / "instructions.md"
        instructions = instr_path.read_text(encoding="utf-8") if instr_path.exists() else ""

    deliverables_map = cfg.get("deliverables") or {}
    # Use the canonical filenames (values); these also appear in `instructions`.
    deliverables = list(dict.fromkeys(deliverables_map.values())) if deliverables_map else []

    docs = task_dir / "documents"
    # NOTE: `criteria` is intentionally NOT read or returned.
    return TaskSpec(
        task_id=task_id,
        title=cfg.get("title", task_id),
        instructions=instructions,
        deliverables=deliverables,
        work_type=cfg.get("work_type", ""),
        task_dir=task_dir,
        documents_dir=docs,
    )


def count_criteria(task_id: str) -> int:
    """For reporting/sampling only — never exposed to the agent."""
    cfg = json.loads((config.TASKS_DIR / Path(*task_id.split("/")) / "task.json").read_text())
    return len(cfg.get("criteria", []))
