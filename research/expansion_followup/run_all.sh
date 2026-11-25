#!/bin/bash
# Sequential supervisor: one variant per process (memory isolation), in dependency order.
# exp4b must run after all exp3 variants (it reads their trades.csv for the
# pre-registered best-timing selection).
set -u
BASE=/home/z/my-project/work/github_tf_test/research/expansion_followup
RUNNER="$BASE/run_experiments.py"
mkdir -p "$BASE/runs"
ORDER="baseline_repro exp1_expansion_only exp2_pullback_only exp3a_sunday_only exp3b_h23_only exp3c_no_sunday exp3d_no_h23 exp3e_sunday_h23 exp4a_exp_pullback exp4b_exp_pb_timing"

echo "chain START $(date -u +%H:%M:%S)" >> "$BASE/runs/chain.log"
for v in $ORDER; do
  echo "=== $v START $(date -u +%H:%M:%S)" >> "$BASE/runs/chain.log"
  python3 "$RUNNER" --variant "$v" > "$BASE/runs/$v.console.log" 2>&1
  rc=$?
  echo "=== $v END rc=$rc $(date -u +%H:%M:%S)" >> "$BASE/runs/chain.log"
  if [ $rc -ne 0 ]; then
    echo "!!! $v FAILED (rc=$rc) — see runs/$v.console.log" >> "$BASE/runs/chain.log"
  fi
done
echo "chain DONE $(date -u +%H:%M:%S)" >> "$BASE/runs/chain.log"
