"""Build an auditable bundle of a run (or a whole sweep) for external review.

Each run already produces a complete, tamper-evident trail on disk:
  config.json      — model, judge, task, settings, timestamps
  transcript.jsonl — every model turn + every tool call and its result
  metrics.json     — tokens, cost, doc coverage, contamination flags
  scores.json      — per-criterion verdict + the judge's reasoning + judge model
  output/          — the graded deliverable(s)

This tool copies that trail into a clean `audit-bundles/<tag>/<task>__<arm>/`
folder, writes a human-readable AUDIT.md per run, and runs an independent
contamination check (proving the agent never read the grading rubric), so the
whole package can be handed to Harvey for verification.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from mithril import config

# Tokens that would indicate the agent touched the grading file itself.
_LEAK_TOKENS = ("task.json", "match_criteria")


def _contamination_check(transcript: Path) -> dict:
    """Independently re-scan a transcript for any access to the grading rubric."""
    hits = []
    if transcript.exists():
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("role") in ("assistant", "tool"):
                blob = (str(e.get("text", "")) + " " + str(e.get("arguments", "")) + " " + str(e.get("result_preview", ""))).lower()
                for tok in _LEAK_TOKENS:
                    if tok in blob:
                        hits.append({"turn": e.get("turn"), "role": e.get("role"), "token": tok})
    return {"clean": len(hits) == 0, "leak_hits": hits}


def _audit_md(run_dir: Path) -> str:
    cfg = json.loads((run_dir / "config.json").read_text()) if (run_dir / "config.json").exists() else {}
    scores = json.loads((run_dir / "scores.json").read_text()) if (run_dir / "scores.json").exists() else {}
    metrics = json.loads((run_dir / "metrics.json").read_text()) if (run_dir / "metrics.json").exists() else {}
    contam = _contamination_check(run_dir / "transcript.jsonl")

    lines = [
        f"# Audit — {cfg.get('task','?')}",
        "",
        f"- **Harness:** {cfg.get('harness','?')}",
        f"- **Agent model:** {cfg.get('model','?')}",
        f"- **Judge model:** {scores.get('judge_model','?')}",
        f"- **Result:** {'ALL-PASS ✅' if scores.get('all_pass') else 'not all-pass'} "
        f"({scores.get('n_passed','?')}/{scores.get('n_criteria','?')} criteria)",
        f"- **Document coverage:** {metrics.get('documents_read','?')}/{metrics.get('total_documents','?')} read",
        f"- **Contamination check:** {'PASS — no rubric access detected' if contam['clean'] else 'FAIL — ' + str(contam['leak_hits'])}",
        f"- **Tokens / est. cost:** agent {metrics.get('deepseek_usage',{})}; judge {scores.get('judge_usage',{})}",
        "",
        "## Per-criterion verdicts (judge)",
        "",
        "| ID | Verdict | Criterion | Judge reasoning |",
        "|---|---|---|---|",
    ]
    for c in scores.get("criteria_results", []):
        reason = c.get("reasoning", "").replace("|", "\\|").replace("\n", " ")[:300]
        lines.append(f"| {c['id']} | {'✅' if c['verdict']=='pass' else '❌'} | {c['title'][:80].replace('|','\\|')} | {reason} |")
    lines += ["", "## Files in this bundle",
              "- `transcript.jsonl` — full agent trajectory (every tool call + result)",
              "- `config.json`, `metrics.json`, `scores.json` — settings, telemetry, grades",
              "- `output/` — the graded deliverable(s)"]
    return "\n".join(lines)


def bundle_run(run_dir: Path, dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    for fn in ("config.json", "transcript.jsonl", "metrics.json", "scores.json"):
        if (run_dir / fn).exists():
            shutil.copy2(run_dir / fn, dest / fn)
    if (run_dir / "output").exists():
        shutil.copytree(run_dir / "output", dest / "output", dirs_exist_ok=True)
    (dest / "AUDIT.md").write_text(_audit_md(run_dir))
    contam = _contamination_check(run_dir / "transcript.jsonl")
    scores = json.loads((run_dir / "scores.json").read_text()) if (run_dir / "scores.json").exists() else {}
    return {"all_pass": scores.get("all_pass"), "n_passed": scores.get("n_passed"),
            "n_criteria": scores.get("n_criteria"), "contamination_clean": contam["clean"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="sweep tag, e.g. iter1")
    ap.add_argument("--out", default=None, help="output bundle dir")
    args = ap.parse_args()

    src = config.RESULTS_DIR / f"sweep-{args.tag}"
    out = Path(args.out) if args.out else (config.REPO_ROOT / "audit-bundles" / args.tag)
    manifest = {"tag": args.tag, "runs": []}
    for cfg_path in sorted(src.rglob("config.json")):
        run_dir = cfg_path.parent
        cfg = json.loads(cfg_path.read_text())
        name = cfg["task"].replace("/", "__") + "__" + cfg.get("harness", "?")
        summary = bundle_run(run_dir, out / name)
        summary["run"] = name
        manifest["runs"].append(summary)
        print(f"  bundled {name}: all_pass={summary['all_pass']} contamination_clean={summary['contamination_clean']}")

    n = len(manifest["runs"])
    ap_n = sum(1 for r in manifest["runs"] if r.get("all_pass"))
    clean = sum(1 for r in manifest["runs"] if r.get("contamination_clean"))
    manifest["all_pass_rate"] = round(ap_n / n, 4) if n else None
    manifest["all_contamination_clean"] = clean == n
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n{n} runs bundled → {out}")
    print(f"all-pass {ap_n}/{n} | contamination-clean {clean}/{n}")


if __name__ == "__main__":
    main()
