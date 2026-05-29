"""Shared configuration for the mithril harness.

The mithril harness is a *criteria-blind* improvement layer over Harvey's
Legal Agent Benchmark (LAB). It reuses LAB's task data and grading pipeline
verbatim — only the agent loop, the executor, and the model differ.

Paths
-----
REPO_ROOT/                     this project
  harvey-labs/                 the cloned benchmark (task data + evaluation/)
  mithril/                     this package
  results/                     run outputs + scores (LAB layout)

Contamination boundary
-----------------------
The agent NEVER sees task.json's ``criteria`` / ``match_criteria``. The run
harness loads only the *assignment spec* from task.json — ``title``,
``instructions``, ``deliverables``, ``work_type`` — and exposes only the
documents/ directory to the agent's tools. See ``load_task_spec``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HARVEY_LABS = REPO_ROOT / "harvey-labs"
TASKS_DIR = HARVEY_LABS / "tasks"
RESULTS_DIR = REPO_ROOT / "results"

# Make the benchmark's evaluation/scoring pipeline importable so we can grade
# faithfully with our own (DeepSeek) judge.
if str(HARVEY_LABS) not in sys.path:
    sys.path.insert(0, str(HARVEY_LABS))

# ── DeepSeek (OpenAI-compatible chat completions) ─────────────────────────

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def deepseek_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Export it before running the harness."
        )
    return key


# Model roles. The agent should be the strongest available model (per-criterion
# pass rate is what drives all-pass). The judge model is configurable so we can
# trade cost vs. fidelity.
AGENT_MODEL = os.environ.get("MITHRIL_AGENT_MODEL", "deepseek-v4-pro")
JUDGE_MODEL = os.environ.get("MITHRIL_JUDGE_MODEL", "deepseek-v4-pro")

# Pricing (USD per 1M tokens) for cost estimation only. DeepSeek bills
# cache-hit input far cheaper than cache-miss; we record both from the API.
# These are best-effort defaults; the harness records *actual* token counts so
# cost can be recomputed if the rate card differs.
# Calibrated against an observed balance delta (a v4-pro run with 6.6M prompt
# tokens — 99% cache hits — + 40k completion cost ≈ $0.08). Cache-hit input is
# nearly free. These are best-effort; the sweep also records the authoritative
# balance delta from the billing API.
PRICE_PER_M = {
    "deepseek-v4-pro": {"in_miss": 0.27, "in_hit": 0.007, "out": 0.40},
    "deepseek-v4-flash": {"in_miss": 0.14, "in_hit": 0.0035, "out": 0.20},
}


def deepseek_balance() -> float | None:
    """Query the DeepSeek billing API for the current USD balance (ground-truth cost)."""
    import httpx
    try:
        r = httpx.get(f"{DEEPSEEK_BASE_URL}/user/balance",
                      headers={"Authorization": f"Bearer {deepseek_api_key()}"}, timeout=15)
        return float(r.json()["balance_infos"][0]["total_balance"])
    except Exception:  # noqa: BLE001
        return None


def load_env_file() -> None:
    """Load REPO_ROOT/.env into os.environ (without overriding existing keys)."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value:
                os.environ.setdefault(key, value)
