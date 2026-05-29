"""The mithril agent loop — a criteria-blind improvement over the stock loop.

The stock LAB loop is a bare shuttle: call model, run tools, repeat until the
model stops. That under-performs on all-pass grading because the model tends
to (a) skip documents, (b) lose detail over long horizons, and (c) declare
victory while one needle is still missing.

This loop adds four interventions, none of which look at the rubric:

  1. **Deliverable-existence gate** — never let the agent "finish" without
     having written the requested deliverable.
  2. **Document-coverage enforcement** — if the agent tries to finish with
     matter documents still unread, it is told exactly which ones and sent
     back. Missing a document means missing every issue inside it.
  3. **Natural-language compaction (BRIDGE memo)** — when the context grows
     past a threshold, the model writes a complete CASE FILE memo and history
     is rebuilt around it, preserving long-horizon coherence. (The headline
     technique from Harvey/Baseten's post-training write-up.)
  4. **Senior-partner verification pass** — when the agent first tries to
     finish, it is forced to re-read its deliverable, re-verify every figure
     and citation against the sources, walk its plan for anything missing,
     and fix gaps. This directly attacks the "one missed item" all-pass
     killer.

All four are generic legal-work discipline; none encode answers.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from harness.adapters.base import ModelAdapter

# Extensions the coverage gate considers "readable matter documents".
_READABLE = {".docx", ".pdf", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".json", ".html"}


def _list_documents(documents_dir: Path) -> list[str]:
    return sorted(
        str(p.relative_to(documents_dir))
        for p in documents_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _READABLE
    )


def _output_files(output_dir: Path) -> list[str]:
    if not output_dir.exists():
        return []
    return [str(p.relative_to(output_dir)) for p in output_dir.rglob("*") if p.is_file()]


def _output_md_bytes(output_dir: Path) -> int:
    """Total bytes of the agent's markdown deliverables — used to tell productive
    drafting (deliverable growing) from cosmetic churn (size flat)."""
    if not output_dir.exists():
        return 0
    return sum(p.stat().st_size for p in output_dir.rglob("*.md") if p.is_file())


VERIFY_PROMPT = (
    "Partner review before this goes out — one focused pass, then finish. Do not stop yet.\n\n"
    "Act as the reviewing partner who will sign this work:\n"
    "1. Re-read your deliverable in the output directory in full (use `read`).\n"
    "2. Verify every figure, dollar amount, percentage, date, defined term, and section/clause citation against the "
    "source documents; recompute key numbers with `python3`. Correct anything that is wrong or unsupported.\n"
    "3. Walk your plan and the source documents once: is there any SUBSTANTIVE issue, deficiency, missing required "
    "item, inconsistency, or discrepancy you failed to capture or stated only vaguely? Legal work fails on the one "
    "material thing that was missed — add anything genuinely missing.\n"
    "4. Confirm every part of the assignment is addressed and each finding is stated explicitly with its support.\n"
    "5. Structural check: if the assignment maps to, compares against, or analyzes a structured source instrument "
    "(one with its own enumerated domains, schedules, articles, sections, or commitment categories), confirm the "
    "deliverable reproduces that instrument's top-level categories as actual navigable HEADINGS, covering all or "
    "nearly all of them. If a required category appears only inside tables/prose but not as a heading, restructure so "
    "it is a heading — this is a SUBSTANTIVE completeness fix, not cosmetic, and IS permitted.\n\n"
    "Make only SUBSTANTIVE corrections — fixes to accuracy, completeness, missing findings, or the structural "
    "conformance above. Do NOT make cosmetic, stylistic, or wording-only edits, and do not re-edit the same passage "
    "repeatedly. When the substantive review is done and any real gaps are fixed, write 'REVIEW COMPLETE' and stop."
)

# Gate D — a dedicated depth/completeness critic, run once AFTER the accuracy verify
# pass. Targets the dominant all-pass failure mode: the agent NAMES an issue but
# stops short of its specific mechanism, consequence, every instance, or the
# contrary argument. Every checklist item is a GENERIC procedural test applied to
# the agent's OWN findings — it encodes no answers (contamination-safe). Additive
# and bounded (one pass) to avoid deliverable-bloat / over-edit regression.
DEPTH_CRITIC_PROMPT = (
    "Depth-and-completeness audit before sign-off. Do NOT restyle, shorten, or delete anything. Go finding by "
    "finding through your deliverable and, for EACH one, run this checklist; where a box is unticked AND the answer "
    "is material, add one or two sentences (no more) to close it:\n"
    "1. APPLICATION: if you named a governing law, rule, standard, seat, or eligibility/regulatory status anywhere, "
    "did you APPLY it to the specific instrument/right/order at hand and state the concrete consequence "
    "(enforceability, validity, availability, impossibility) — not just name it?\n"
    "2. ALL AVENUES: for every remedy, right, or procedure, did you enumerate every available route (institutional "
    "AND court/regulatory, primary AND parallel/supplemental) — or affirmatively state none exists?\n"
    "3. GENERAL-BEFORE-SPECIFIC: for every rule family or defined term you cited by a sub-part, did you also state "
    "the governing umbrella/chapeau/general rule, or note its absence?\n"
    "4. TWO TIME AXES: for every clause/condition/trigger you marked not-triggered, not-applicable, or satisfied, did "
    "you also test whether it is an ongoing/forward-looking constraint a foreseeable later event would trip — naming "
    "that event and its precise consequence?\n"
    "5. ALL OPERATIVE DIMENSIONS: for every clause flagged as deviating on ONE axis (amount, duration), did you "
    "separately check its other dimensions (scope of covered assets/persons, things acquired/arising AFTER the "
    "trigger, geography, gating/eligibility conditions, carve-outs) against the applicable baseline?\n"
    "6. DEADLINE TRIAD: for every deadline, did you state the precise trigger (including any later-of/earlier-of "
    "disjunction), the exact duration, AND the actor who grants any extension or exercises discretion?\n"
    "7. CONTRARY RISK: everywhere you concluded a right is preserved, a deadline is soft, or an omission is harmless "
    "(\"without waiver\", \"may be deferred\", \"no risk\", \"acceptable\"), did you also state the strongest opposing "
    "argument the other side could run and the protective step that defeats it?\n"
    "8. WHO BENEFITS: for every discretionary or approval-gated provision (board approval, consent, designation "
    "rights), did you map the discretion-holder to the parties and flag whether it disproportionately advantages the "
    "drafting/controlling party?\n"
    "9. BASELINE CHARACTERIZATION: for every 'should-add'/missing-provision recommendation, did you state where it "
    "sits on the market/standard-form spectrum with a DEFINITE characterization, not hedged frequency language?\n\n"
    "Apply each lens to EVERY instance it touches (every party, clause, section, item) — do not analyze one and skip "
    "the rest. When the audit is complete and any material gaps are closed, write 'AUDIT COMPLETE' and stop."
)

COMPACT_INSTRUCTION = (
    "[CONTEXT LIMIT APPROACHING — produce a CASE FILE memo now]\n\n"
    "You are about to lose your working memory of this matter. Write a COMPLETE case file memo so you can finish "
    "the assignment without re-reading the documents. Do not call any tools — just write the memo as your reply. "
    "Include, exhaustively and specifically:\n"
    "- Per document: every load-bearing fact — exact figures, dollar amounts, dates, defined terms, section/clause "
    "numbers, parties, obligations, conditions.\n"
    "- Every cross-document finding, issue, discrepancy, deficiency, or missing item you have identified so far, "
    "with its supporting facts and citations.\n"
    "- Your plan checklist and which items remain to be done.\n"
    "- The current state of your deliverable (what sections exist, what is still missing).\n\n"
    "Anything not written into this memo is permanently lost. Be complete."
)


def run_agent_improved(
    adapter: ModelAdapter,
    system_prompt: str,
    user_prompt: str,
    tool_executor,
    documents_dir: Path,
    output_dir: Path,
    expected_deliverables: list[str] | None = None,
    tools: list[dict] | None = None,
    max_turns: int = 55,
    transcript_path: str | None = None,
    compact_threshold_tokens: int = 110_000,
    max_compactions: int = 4,
    max_coverage_nudges: int = 2,
    max_verify_passes: int = 1,
    max_depth_passes: int = 1,
) -> dict:
    from harness.tools import get_all_tool_definitions

    if tools is None:
        tools = get_all_tool_definitions()
    documents_dir = Path(documents_dir)
    output_dir = Path(output_dir)
    all_docs = _list_documents(documents_dir)
    expected_deliverables = expected_deliverables or []

    messages = [
        adapter.make_system_message(system_prompt),
        adapter.make_user_message(user_prompt),
    ]

    total_input_tokens = total_output_tokens = 0
    turn_count = 0
    coverage_nudges = verify_passes = compactions = deliverable_nudges = depth_passes = 0
    consecutive_edit_turns = 0
    edit_spree_nudged = False
    prev_md_bytes = 0
    last_input_tokens = 0
    context_overflow = False
    start = time.time()

    tf = None
    if transcript_path:
        Path(transcript_path).parent.mkdir(parents=True, exist_ok=True)
        tf = open(transcript_path, "w")

    def log(entry: dict):
        if tf:
            tf.write(json.dumps(entry) + "\n")
            tf.flush()

    response = None
    try:
        turn = 0
        while turn < max_turns:
            turn += 1
            turn_count = turn
            try:
                response = adapter.chat(messages, tools)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                if "prompt is too long" in err or "context_length_exceeded" in err or "maximum context length" in err:
                    context_overflow = True
                    # Emergency compaction if we still have budget, else bail.
                    if compactions < max_compactions:
                        compactions += 1
                        messages = _emergency_truncate(messages)
                        context_overflow = False
                        continue
                    break
                # Any other mid-run API error (e.g. a transient 400/overload):
                # stop the loop gracefully so the deliverable produced so far is
                # still finalized, graded, and auditable — never crash the run.
                log({"turn": turn, "role": "error", "error": f"{type(e).__name__}: {err[:300]}"})
                break

            messages.append(response.message)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            last_input_tokens = response.input_tokens
            log({"turn": turn, "role": "assistant", "text": (response.text or "")[:800],
                 "tool_calls": [{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls],
                 "input_tokens": response.input_tokens, "output_tokens": response.output_tokens})

            if not response.tool_calls:
                # ── the model wants to finish: run the gates in order ──
                # Gate A: deliverable must exist.
                outs = _output_files(output_dir)
                if expected_deliverables and not outs and deliverable_nudges < 2:
                    deliverable_nudges += 1
                    want = ", ".join(expected_deliverables)
                    messages.append(adapter.make_user_message(
                        f"You have not written any deliverable to the output directory. The assignment requires: "
                        f"{want}. Produce the deliverable now as a Markdown file (named with a .md extension), "
                        f"following your plan and covering every issue you found."))
                    log({"turn": turn, "role": "gate", "gate": "deliverable", "missing": expected_deliverables})
                    continue

                # Gate B: full document coverage.
                read = set(getattr(tool_executor, "files_read", []))
                unread = [d for d in all_docs if d not in read]
                if unread and coverage_nudges < max_coverage_nudges:
                    coverage_nudges += 1
                    listing = "\n".join(f"  - {d}" for d in unread)
                    messages.append(adapter.make_user_message(
                        f"Before finishing: you have not read {len(unread)} of the {len(all_docs)} matter documents. "
                        f"Every document may contain issues that belong in your deliverable. Read each of these in full, "
                        f"incorporate anything relevant, then continue:\n{listing}"))
                    log({"turn": turn, "role": "gate", "gate": "coverage", "unread": unread})
                    continue

                # Gate C: forced verification pass (accuracy-focused).
                if verify_passes < max_verify_passes:
                    verify_passes += 1
                    messages.append(adapter.make_user_message(VERIFY_PROMPT))
                    log({"turn": turn, "role": "gate", "gate": "verify", "pass": verify_passes})
                    continue

                # Gate D: depth/completeness critic (one bounded pass) — pushes every
                # finding to its full depth (mechanism, consequence, all instances,
                # contrary argument). Attacks the dominant all-pass failure mode.
                if depth_passes < max_depth_passes:
                    depth_passes += 1
                    messages.append(adapter.make_user_message(DEPTH_CRITIC_PROMPT))
                    log({"turn": turn, "role": "gate", "gate": "depth_critic", "pass": depth_passes})
                    continue

                break  # all gates satisfied — genuinely done

            # ── execute tool calls ──
            results = []
            for tc in response.tool_calls:
                out = tool_executor.execute(tc.name, tc.arguments)
                # Full arguments are logged (proves no rubric access); the result
                # is previewed but its full length + hash are recorded so the
                # audit trail is verifiable without storing megabytes of doc text.
                log({"turn": turn, "role": "tool", "tool_name": tc.name,
                     "arguments": tc.arguments if isinstance(tc.arguments, str) else json.dumps(tc.arguments),
                     "result_len": len(out),
                     "result_sha256": hashlib.sha256(out.encode("utf-8", "replace")).hexdigest()[:16],
                     "result_preview": out[:4000]})
                results.append((tc.id, out))
            messages.extend(adapter.make_tool_result_messages(results))

            # ── edit-spree guard (growth-aware) ──
            # Only treat edit-only turns that DON'T grow the deliverable as a
            # "spree" (true cosmetic churn). Section-by-section drafting via edits
            # grows the deliverable, so it never trips this — the bug that was
            # blocking the depth-critic on long deliverables. The nudge is appended
            # AFTER the tool results (correct message ordering) and never tells the
            # agent to bypass the verify/depth gates — those still fire on stop.
            names = [tc.name for tc in response.tool_calls]
            cur_md = _output_md_bytes(output_dir)
            grew = cur_md - prev_md_bytes
            prev_md_bytes = cur_md
            if names and all(n == "edit" for n in names) and grew < 300:
                consecutive_edit_turns += 1
            else:
                consecutive_edit_turns = 0  # productive (added content) or used other tools
            if consecutive_edit_turns >= 10 and not edit_spree_nudged:
                edit_spree_nudged = True
                consecutive_edit_turns = 0
                messages.append(adapter.make_user_message(
                    "You've made several edits in a row without adding new content. Stop reworking wording. "
                    "Only edit further to add a genuinely missing material finding; if the analysis is substantively "
                    "complete, simply stop — the final review steps will run automatically."))
                log({"turn": turn, "role": "gate", "gate": "edit_spree"})

            # ── compaction check ──
            if last_input_tokens > compact_threshold_tokens and compactions < max_compactions:
                compactions += 1
                messages = _compact(adapter, messages, system_prompt, user_prompt, tools, log, turn)

    finally:
        if tf:
            tf.close()

    elapsed = time.time() - start
    metrics = tool_executor.get_metrics()
    return {
        "messages": messages,
        "turn_count": turn_count,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "wall_clock_seconds": round(elapsed, 2),
        "finished_cleanly": (not context_overflow and response is not None and not response.tool_calls),
        "context_overflow": context_overflow,
        "compactions": compactions,
        "coverage_nudges": coverage_nudges,
        "verify_passes": verify_passes,
        "depth_passes": depth_passes,
        "tool_metrics": metrics,
    }


def _compact(adapter, messages, system_prompt, user_prompt, tools, log, turn) -> list[dict]:
    """Natural-language compaction: model writes a case-file memo; rebuild history around it."""
    probe = messages + [adapter.make_user_message(COMPACT_INSTRUCTION)]
    try:
        memo_resp = adapter.chat(probe, tools=[])  # no tools → forces a text memo
        memo = memo_resp.text or "(compaction produced no memo)"
    except Exception as e:  # noqa: BLE001
        memo = f"(compaction failed: {e})"
    log({"turn": turn, "role": "gate", "gate": "compaction", "memo_chars": len(memo)})
    return [
        adapter.make_system_message(system_prompt),
        adapter.make_user_message(user_prompt),
        adapter.make_user_message(
            "[CONTEXT COMPACTED] Here is your case file so far — treat it as your complete memory of the matter; "
            "you do not need to re-read documents already covered:\n\n" + memo +
            "\n\nYour deliverable draft (if any) is saved in the output directory — `read` it to continue. "
            "Finish the assignment: complete and verify the deliverable."),
    ]


def _emergency_truncate(messages: list[dict]) -> list[dict]:
    """Hard fallback when even compaction overflowed: keep system + first user + tail."""
    if len(messages) <= 4:
        return messages
    head = messages[:2]
    tail = messages[-2:]
    # Drop any leading orphan tool message in the tail (must follow an assistant tool_calls msg).
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    return head + [{"role": "user", "content": "[history truncated to fit context]"}] + tail
