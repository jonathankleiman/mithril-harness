"""Run one model against one LAB task under a chosen harness.

    harness="mithril"  → improved loop (coverage + compaction + verify gates)
    harness="baseline" → stock LAB loop, same I/O plumbing, no interventions

Both arms share the same model adapter, the same local executor, and the same
deliverable finalizer, so an A/B between them isolates the *harness
interventions* — exactly the comparison Harvey/Baseten use to attribute gains
to harness optimization.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from mithril import config

config.load_env_file()

from mithril.deepseek_adapter import DeepSeekAdapter
from mithril.local_exec import make_local_executor
from mithril.agent_loop import run_agent_improved
from mithril.deliverables import finalize_deliverables
from mithril.tasks import load_task_spec

MITHRIL_SYSTEM = (Path(__file__).parent / "system_prompt.md").read_text()
BASELINE_SYSTEM = (Path(__file__).parent / "baseline_system_prompt.md").read_text()

# Only flag tokens that indicate access to the grading file itself. ("criteria"
# alone is common legal English — "eligibility criteria", "default criteria" —
# and produced false positives; the real leak indicators are the filename and
# the rubric's field name.)
_CONTAM_TOKENS = ("task.json", "match_criteria")


def _contamination_scan(transcript_path: Path) -> list[str]:
    """Defense-in-depth: flag any agent reference to grading internals."""
    if not transcript_path.exists():
        return []
    hits = []
    for line in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("role") in ("assistant", "tool"):
            blob = (e.get("text", "") + " " + str(e.get("arguments", ""))).lower()
            for tok in _CONTAM_TOKENS:
                if tok in blob:
                    hits.append(f"turn {e.get('turn')}: {e.get('role')} mentioned '{tok}'")
    return hits


def run_task(
    task_id: str,
    model: str,
    harness: str = "mithril",
    max_turns: int = 80,
    shell_timeout: int = 60,
    run_id: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 16384,  # larger so big single-`write` deliverables don't truncate the tool-call JSON
) -> dict:
    spec = load_task_spec(task_id)

    if run_id is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_id = f"{task_id}/{harness}-{model}/{ts}"
    run_dir = config.RESULTS_DIR / run_id
    output_dir = run_dir / "output"
    workspace_dir = run_dir / "workspace"
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    adapter = DeepSeekAdapter(model=model, temperature=temperature, max_tokens=max_tokens)
    sandbox, executor = make_local_executor(
        documents_dir=spec.documents_dir, output_dir=output_dir,
        workspace_dir=workspace_dir, shell_timeout=shell_timeout,
    )

    transcript_path = run_dir / "transcript.jsonl"
    (run_dir / "config.json").write_text(json.dumps({
        "task": task_id, "model": model, "harness": harness, "run_id": run_id,
        "max_turns": max_turns, "temperature": temperature,
        "deliverables_requested": spec.deliverables, "work_type": spec.work_type,
        "started_at": datetime.now().isoformat(),
    }, indent=2))

    try:
        if harness == "mithril":
            result = run_agent_improved(
                adapter=adapter, system_prompt=MITHRIL_SYSTEM, user_prompt=spec.instructions,
                tool_executor=executor, documents_dir=spec.documents_dir, output_dir=output_dir,
                expected_deliverables=spec.deliverables, max_turns=max_turns,
                transcript_path=str(transcript_path),
            )
        elif harness == "baseline":
            from harness.agent_loop import run_agent
            result = run_agent(
                adapter=adapter, system_prompt=BASELINE_SYSTEM, user_prompt=spec.instructions,
                tool_executor=executor, max_turns=max_turns, transcript_path=str(transcript_path),
            )
        else:
            raise ValueError(f"unknown harness: {harness}")
    finally:
        sandbox.stop()

    deliv_report = finalize_deliverables(output_dir, spec.deliverables)
    contam = _contamination_scan(transcript_path)

    metrics = {
        "task": task_id, "model": model, "harness": harness, "run_id": run_id,
        "turn_count": result.get("turn_count"),
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
        "wall_clock_seconds": result.get("wall_clock_seconds"),
        "finished_cleanly": result.get("finished_cleanly"),
        "context_overflow": result.get("context_overflow", False),
        "compactions": result.get("compactions", 0),
        "coverage_nudges": result.get("coverage_nudges", 0),
        "verify_passes": result.get("verify_passes", 0),
        "deliverables_report": deliv_report,
        "contamination_flags": contam,
        "deepseek_usage": adapter.usage_dict(),
        **result.get("tool_metrics", {}),
        "completed_at": datetime.now().isoformat(),
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return {"run_id": run_id, "run_dir": str(run_dir), "metrics": metrics}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--model", default=config.AGENT_MODEL)
    ap.add_argument("--harness", default="mithril", choices=["mithril", "baseline"])
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()
    out = run_task(args.task, args.model, args.harness, max_turns=args.max_turns, run_id=args.run_id)
    print(json.dumps(out["metrics"], indent=2))
    print(f"\nrun_dir: {out['run_dir']}")
