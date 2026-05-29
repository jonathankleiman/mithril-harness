# Mithril Harness

**A criteria-blind agent harness that lifts open-model performance on Harvey's Legal Agent Benchmark (LAB).**

> Created by **Jonathan Kleiman** (2026). Licensed **AGPL-3.0 + attribution** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Mithril is a thin, model-agnostic layer over [Harvey's Legal Agent Benchmark](https://github.com/harveyai/harvey-labs). It keeps LAB's task data and grading pipeline **exactly as-is** and changes only the *scaffolding* around the model — the agent loop, the executor, the system prompt, and the model adapter — to make an ordinary LLM behave like a meticulous senior associate. The goal: raise the **all-pass rate** (the share of matters where *every* rubric criterion passes) without any data contamination.

## Why this is hard

LAB grades each deliverable against a rubric of binary, equally-weighted criteria, and a task only counts as a pass if **every** criterion passes. The median task has **~56 criteria**, so all-pass requires roughly **98% per-criterion accuracy**. The enemy isn't intelligence — it's *completeness*. A memo that catches 55 of 56 issues still fails.

## What the harness does (all criteria-blind)

1. **Document-coverage gate** — the agent cannot finish while any matter document is unread (a missed document = every issue inside it missed).
2. **Operating procedure** — plan → read everything → **draft a complete deliverable early** → cross-reference documents against each other → verify.
3. **Forced verification pass** — before finishing, the agent re-reads its deliverable, re-verifies every figure/date/citation against the sources (recomputing numbers), and fixes gaps. Additive-only (never shortens). This directly attacks the "one missed needle" failure.
4. **Natural-language compaction** — on long matters, the agent distills its working memory into a structured case-file memo so context doesn't rot.
5. **Robust deliverable finalization** — the agent writes Markdown; the harness renders real `.docx`/`.xlsx` via pandoc (the exact inverse of the grader's reader), removing file-format failures as a confound.

It also **fixes a real defect** in the stock harness: `grep` over a `.docx` searched the raw zip bytes and silently matched nothing; Mithril searches the *parsed* document text, so cross-document needle-finding works. And it adds a **DeepSeek adapter** (chat-completions, thinking-mode `reasoning_content` replay, streaming).

## No data contamination — enforced and auditable

- The agent **never** sees the rubric. `load_task_spec` returns only the assignment spec (title, instructions, requested deliverable filenames) and the read-only `documents/` directory — it never reads or retains `criteria` / `match_criteria`.
- `task.json` (which holds the rubric) lives outside the `documents/` mount the tools are rooted at, so it is physically unreachable by the agent.
- No fine-tuning / no training on test tasks.
- Every run is scanned for any reference to the grading file; `mithril/audit.py` re-verifies this independently and bundles the full trail for external review.

## Results

Grading uses LAB's `score_rubric` **unchanged**; only the judge *model* is configurable. The headline comparison is the official setup: **120 LAB tasks, judged by GPT-5.4**, agent = DeepSeek v4-pro. (Numbers and the full technical report are produced by the run pipeline below and live in `REPORT.md`.)

Early signal (6-task A/B, same model both arms): the harness raised all-pass from **0% → 17%** and criterion-pass from **83.7% → 86.1%**, including a clean **58/58** where the stock harness caught 47/58 — the coverage+verify discipline doing its job.

## Setup

```bash
git clone https://github.com/jonathankleiman/mithril-harness.git
cd mithril-harness
git clone https://github.com/harveyai/harvey-labs.git   # the benchmark (MIT, fetched separately)
cd harvey-labs && uv sync && cd ..
cp .env.example .env   # add DEEPSEEK_API_KEY and (for GPT-5.4 judging) OPENAI_API_KEY
```

## Usage

```bash
PY=harvey-labs/.venv/bin/python
# one task
$PY -m mithril.run  --task <area>/<task> --model deepseek-v4-pro --harness mithril
$PY -m mithril.eval --run-dir results/<run-id> --task <area>/<task> --judge-model gpt-5.4
# A/B sweep (baseline vs mithril), then audit bundle
$PY -m mithril.sample -n 120 > sample.json
$PY -m mithril.sweep  --tasks-file sample.json --arms baseline mithril \
     --agent-model deepseek-v4-pro --judge-model gpt-5.4 --run-tag run1
$PY -m mithril.audit  --tag run1      # → audit-bundles/run1/ for external review
```

## Auditability

Every run writes a complete, reviewable trail: `config.json`, `transcript.jsonl` (every tool call + result), `metrics.json`, and `scores.json` (each criterion's verdict + the judge's reasoning + the judge model). `mithril/audit.py` packages these into a per-run bundle with a human-readable `AUDIT.md` and an independent contamination check — ready to hand to Harvey.

## License

**GNU AGPL-3.0** with a Section 7(b) attribution requirement to Jonathan Kleiman. Any copy, derivative, or network deployment must preserve the attribution in [NOTICE](NOTICE). Harvey's LAB is separate MIT-licensed work and is not relicensed here.
