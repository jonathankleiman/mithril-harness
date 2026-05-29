# Mithril: a criteria-blind harness for Harvey's Legal Agent Benchmark

**Goal.** Run an open model (DeepSeek v4) on Harvey's Legal Agent Benchmark (LAB)
under an improved *harness* and lift the **all-pass rate** well above the ~11%
frontier baseline — targeting 30% — with **zero data contamination**.

**Approach in one line.** Don't change the model; change the scaffolding. Every
gain here comes from generic legal-work discipline encoded in the harness — never
from knowledge of the grading rubric.

> Results are filled in from the live A/B sweep (`results/sweep-ab15/summary.json`)
> in [§6](#6-results). Everything else is the design and is final.

---

## 1. What LAB measures, and why it is hard

LAB hands an agent a partner-style instruction plus a closed set of matter
documents and grades the resulting work product against a rubric of binary,
equally-weighted criteria. The headline metric is **all-pass**: a task scores 1.0
only if *every* criterion passes, else 0.0.

The criteria are hyper-specific needles. A representative one:

> *PASS if the report identifies that the Escrow Agreement states the
> Indemnification Escrow Amount as $35,560,000, which is incorrect — the SPA
> requires 8% of Equity Value ($443,500,000) = $35,480,000, an $80,000
> discrepancy. FAIL otherwise.*

Measured over the 1,251 tasks in this repo:

| | criteria/task |
|---|---|
| min | 23 |
| median | **56** |
| mean | 59 |
| p90 | 81 |
| max | 194 |

All-pass on a median task means hitting **56 independent needles at once**. If a
model passed each criterion independently with probability *p*, all-pass would be
*p⁵⁶*. To reach **30% all-pass you need p ≈ 0.30^(1/56) ≈ 98%** per criterion;
even *p* = 95% yields only ~6%. This single fact dictates the entire design: the
binding objective is **completeness** — never miss the one thing — far more than
raw drafting quality. A diligence memo that catches 95% of issues isn't 95%
useful; it's wrong.

---

## 2. The harness (`mithril/`)

The mithril harness is a layer over LAB that keeps LAB's task data and grading
pipeline **verbatim** and changes only the agent loop, the executor, and the
model adapter. Five interventions, all criteria-blind:

### 2.1 Structured legal operating procedure (system prompt)
The agent is instructed to work like a meticulous senior associate: **plan**
(inventory documents; derive, from the assignment alone, the checklist a
supervising partner would expect) → **read every document in full**, recording
load-bearing facts (figures, dates, defined terms, section numbers, obligations)
into a running case file → **cross-reference** (legal issues live in the gaps
*between* documents — a requirement here vs. reality there; a figure that doesn't
reconcile; a consent not obtained), verifying **every number with arithmetic** →
**draft** so each finding is stated explicitly with its supporting fact and
citation → **verify**. None of this references the rubric; it is how the work is
actually done.

### 2.2 Full-document-coverage gate
Missing one document means missing every issue inside it — and every criterion
that depends on it. The loop tracks which documents have been read; if the agent
tries to finish with matter documents unread, it is told exactly which ones and
sent back. (Frontier models that approach ~90% document coverage outperform; we
enforce it.)

### 2.3 Natural-language compaction (BRIDGE memo)
The headline technique from Harvey/Baseten's post-training write-up. On
long-horizon tasks the context degrades or overflows. When it grows past a
threshold, the agent writes a **complete case-file memo** — per-document facts,
cross-document findings, plan state, deliverable state — and history is rebuilt
around that memo. Long-horizon coherence is preserved without carrying raw
history.

### 2.4 Forced senior-partner verification pass
The single highest-leverage move against all-pass. When the agent first tries to
finish, it is forced into one focused review: re-read the deliverable; re-verify
**every figure, date, and citation** against the sources (recomputing numbers);
walk the plan and documents for any **substantive** issue missed; fix gaps. This
directly attacks the "one missed needle" failure mode. (An edit-spree guard stops
it from devolving into cosmetic polishing.)

### 2.5 Robust deliverable finalization
The agent always writes **Markdown** (what models do best; what the grader reads
back as text anyway). The harness converts each requested deliverable to a real
`.docx`/`.xlsx` via pandoc/openpyxl — the exact inverse of the grader's extraction
path (`pandoc -t markdown` for docx, `pandas.read_excel` for xlsx) — and is robust
to mis-naming or mis-formatting. This removes file-format plumbing as a confound
so the comparison measures *reasoning*, not OOXML correctness.

### Bonus: a real grep fix
LAB's `grep` searched the raw zip bytes of `.docx`/`.pdf` files and silently
matched nothing — a false-negative that can make an agent wrongly conclude a term
is *absent*. Mithril's executor greps the **parsed** text (cached), so
cross-document needle-finding actually works. Both A/B arms share this, so it
isn't what the comparison attributes to "the harness interventions."

### Engineering notes
- **DeepSeek adapter** — LAB's OpenAI adapter uses the Responses API, which
  DeepSeek doesn't serve. Mithril adds a Chat-Completions adapter with tool
  calling. DeepSeek's v4 models run in *thinking mode* and require
  `reasoning_content` to be echoed back each turn (like Anthropic thinking
  blocks); the adapter preserves and replays it. Bounded socket timeouts + retries
  make it resilient to connectivity loss; sweeps are resumable.
- **Local executor** — duck-typed to LAB's Podman `Sandbox` (same `/workspace`
  path discipline, same parsers), running host-side for speed. The Podman sandbox
  remains available for untrusted inputs.

---

## 3. No data contamination — an auditable boundary

This is a hard constraint, enforced in code:

- **The agent never sees the rubric.** `load_task_spec` returns only the
  assignment spec — `title`, `instructions`, requested deliverable filenames,
  `work_type` — and the read-only `documents/` directory. It never reads or
  retains `criteria` / `match_criteria`. (`mithril/tasks.py`.)
- **The agent's tools cannot reach the rubric.** `task.json` lives *outside* the
  `documents/` mount the tools are rooted at.
- **No criteria-aware logic anywhere in the run path.** Every intervention in §2
  is generic legal-work discipline. The harness consults the deliverable
  *filenames* (which already appear in the instructions) only to name and convert
  output files — never the grading text.
- **No training on test tasks.** No fine-tuning at all.
- **Defense-in-depth audit.** Every run's transcript is scanned for any reference
  to `task.json` / `criteria` / `match_criteria` / `rubric`; the count is recorded
  in `metrics.json` (`contamination_flags`). Target: zero.

---

## 4. Grading methodology

We reuse LAB's grading pipeline **unchanged**: the same `score_rubric`, the same
per-criterion deliverable scoping, the same `rubric_criterion` prompt, the same
all-pass logic. The *only* change is the judge **model**: the official benchmark
judges with claude-sonnet-4-6 / GPT-5.4; we judge with DeepSeek because that is
the API key available.

**Caveat (stated plainly).** Agent and judge are the same model family, so
verdicts carry some self-preference bias. We mitigate with temperature 0 and the
unchanged strict rubric prompt, and we recommend re-grading with a frontier judge
for any official leaderboard number. In our runs the DeepSeek judge was
observably strict (e.g., it failed an agent for identifying a bad trustee
appointment but not explaining the *reason* for it), which is reassuring.

---

## 5. Experimental design

- **Sample.** 15 tasks, stratified across practice areas, fixed seed, **no**
  filtering on rubric size or document count (which would bias the estimate).
  Sampled sizes: median **61** criteria/task (vs. benchmark median 56 — i.e. a
  *slightly harder* sample), median 5 documents.
- **A/B.** The same DeepSeek-v4-pro model runs every task under (a) a **baseline**
  harness (stock LAB loop, identical I/O plumbing, no interventions) and (b) the
  **mithril** harness. This isolates the harness interventions — the exact
  comparison Harvey/Baseten use to attribute gains to harness optimization.
- **Cost** is recorded as the authoritative **balance delta** from DeepSeek's
  billing API, not just token estimates.

---

## 6. Results

<!-- FILL FROM results/sweep-ab15/summary.json -->
_pending sweep completion_

---

## 7. Failure analysis

<!-- FILL FROM mithril/analyze.py -->
_pending_

---

## 8. Path to the official leaderboard / 30%

<!-- FILL: discussion grounded in observed all-pass-vs-rubric-size, judge swap, scaling sample -->
_pending_

---

## 9. Reproducibility

```bash
# one task, mithril harness
python -m mithril.run --task <area>/<task> --model deepseek-v4-pro --harness mithril
# grade it (DeepSeek judge)
python -m mithril.eval --run-dir results/<run-id> --task <area>/<task>
# stratified A/B sweep (resumable)
python -m mithril.sample -n 15 > sample15.json
python -m mithril.sweep --tasks-file sample15.json --arms baseline mithril \
    --agent-model deepseek-v4-pro --judge-model deepseek-v4-pro --run-tag ab15
# failure analysis
python -m mithril.analyze --tag ab15 --arm mithril
```
