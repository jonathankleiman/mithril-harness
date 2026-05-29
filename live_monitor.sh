#!/usr/bin/env bash
# READ-ONLY live dashboard for a running sweep. Safe: only reads files.
# Does NOT kill, restart, or touch any process. Ctrl-C stops the DASHBOARD only
# (the sweep keeps running in its own background process).
#
# Usage:   bash live_monitor.sh opus1        (or: iter1, ab6, ...)
TAG="${1:-opus1}"
DIR="results/sweep-$TAG"
while true; do
  clear
  echo "=== sweep-$TAG  $(date '+%H:%M:%S') ===  (read-only; Ctrl-C stops only this view)"
  graded=$(find "$DIR" -name scores.json 2>/dev/null | wc -l | tr -d ' ')
  done=$(find "$DIR" -name metrics.json 2>/dev/null | wc -l | tr -d ' ')
  total=$(find "$DIR" -name config.json 2>/dev/null | wc -l | tr -d ' ')
  echo "agent-done: $done/$total   graded: $graded/$total"
  echo "------------------------------------------------------------------"
  NOW=$(date +%s)
  for d in $(find "$DIR" -type d \( -path '*/mithril' -o -path '*/baseline' \) 2>/dev/null | sort); do
    [ -d "$d/output" ] || continue
    t=$(basename "$(dirname "$d")")
    turns=$(python3 -c "import json;print(max([json.loads(l).get('turn',0) for l in open('$d/transcript.jsonl')]+[0]))" 2>/dev/null)
    big=$(ls -S "$d"/output/*.md 2>/dev/null | head -1)
    sz=$([ -n "$big" ] && wc -c <"$big" | tr -d ' ' || echo 0)
    age=$(( NOW - $(stat -f '%m' "$d/transcript.jsonl" 2>/dev/null || echo "$NOW") ))
    if [ -f "$d/scores.json" ]; then
      res=$(python3 -c "import json;s=json.load(open('$d/scores.json'));print(('ALL-PASS' if s['all_pass'] else str(s['n_passed'])+'/'+str(s['n_criteria'])))" 2>/dev/null)
      st="GRADED $res"
    elif [ -f "$d/metrics.json" ]; then st="grading…"; else st="t$turns ${sz}B (${age}s)"; fi
    printf "  %-44s %s\n" "${t:0:44}" "$st"
  done
  echo "------------------------------------------------------------------"
  echo "(refreshes every 5s)"
  sleep 5
done
