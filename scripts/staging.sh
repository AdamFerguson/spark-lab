#!/usr/bin/env bash
# spark-lab staging E2E + rollback runbook (Phase 6).
#
# Runs the real `spark-lab` CLI against a STAGING node only. It captures every
# step to ./staging-report/ and the run is green only if every step exits 0.
#
#   scripts/staging.sh e2e     --config config.staging.yaml   # full E2E
#   scripts/staging.sh rollback --config config.staging.yaml  # rollback drills
#   scripts/staging.sh all     --config config.staging.yaml   # e2e + rollback
#
# SAFETY: set LIVE_INSTALL_DIR to the live node's install_dir; if the staging
# config points at the same dir, this script REFUSES to run. Never point a
# staging config at the live host/install_dir.

set -uo pipefail   # not -e: run all steps, record failures, fail at the end

SL="${SPARKLAB:-spark-lab}"   # CLI on PATH (pip install -e . , or bin/spark-lab)
CONFIG="config.yaml"
MODE="all"
REPORT="staging-report"
LIVE_DIR="${LIVE_INSTALL_DIR:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    e2e|rollback|all) MODE="$1"; shift ;;
    --config) CONFIG="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ ! -f "$CONFIG" ]; then echo "config not found: $CONFIG" >&2; exit 2; fi
mkdir -p "$REPORT"; : > "$REPORT/summary.txt"

# ---- SAFETY GUARD: refuse to run against the live install_dir -------------
if [ -n "$LIVE_DIR" ]; then
  STAGE_DIR=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG')).get('install_dir',''))" 2>/dev/null)
  if [ -n "$STAGE_DIR" ] && [ "$STAGE_DIR" = "$LIVE_DIR" ]; then
    echo "REFUSING: staging install_dir == LIVE_INSTALL_DIR ($LIVE_DIR)." >&2
    echo "Point the staging config at a distinct install_dir + host first." >&2
    exit 3
  fi
fi

pass=0; fail=0
slc() { "$SL" "$@" --config "$CONFIG"; }
step() {  # step <name> <cmd...>
  local name="$1"; shift
  local log="$REPORT/$name.log"
  echo "=== [$name] $*" | tee -a "$REPORT/summary.txt"
  if "$@" 2>&1 | tee "$log"; then
    echo "    PASS" | tee -a "$REPORT/summary.txt"; pass=$((pass+1))
  else
    echo "    FAIL (see $log)" | tee -a "$REPORT/summary.txt"; fail=$((fail+1))
  fi
}

# ---- E2E ------------------------------------------------------------------
switch_active_model() {
  python3 - "$CONFIG" <<'PY'
import sys, yaml
p = sys.argv[1]; d = yaml.safe_load(open(p)); m = d.get("models", {})
actives = [k for k, v in m.items() if v.get("active")]
inact = [k for k, v in m.items() if not v.get("active")]
if len(actives) != 1 or not inact:
    print("staging config needs exactly one active + one inactive model"); sys.exit(1)
t = inact[0]
for k in m: m[k]["active"] = (k == t)
d.pop("active_models", None)
open(p, "w").write(yaml.safe_dump(d, sort_keys=False))
print(f"switched active model -> {t}")
PY
}

e2e() {
  step "preflight"    slc validate
  step "images"       slc check images
  step "apply-dry"    slc apply --dry-run
  step "apply"        slc apply --yes
  step "status"       slc status
  step "idempotent"   slc apply --yes      # 2nd apply -> converged no-op
  step "switch-model" switch_active_model
  step "apply-switch" slc apply --yes      # converges to the new active model
  step "status-2"     slc status
  step "recipes-list"   slc recipes list
  step "recipes-search" slc recipes search qwen
  step "recipes-convert" slc recipes convert cookbook://qwen3-8b --dry-run
  step "upgrade"      slc upgrade          # refresh deps + images, re-apply
}

# ---- ROLLBACK DRILLS ------------------------------------------------------
bump_port() {
  python3 - "$CONFIG" <<'PY'
import sys, yaml
p = sys.argv[1]; d = yaml.safe_load(open(p)); lit = d.setdefault("litellm", {})
lit["port"] = int(lit.get("port", 4000)) + 1
open(p, "w").write(yaml.safe_dump(d, sort_keys=False)); print("bumped litellm.port")
PY
}

rollback() {
  step "snapshot"   cp "$CONFIG" "$REPORT/config.baseline.yaml"
  step "change"     bump_port
  step "apply-chg"  slc apply --yes
  step "revert"     cp "$REPORT/config.baseline.yaml" "$CONFIG"
  step "apply-rt"   slc apply --yes   # converges back to baseline
  step "status-rt"  slc status
}

case "$MODE" in
  e2e) e2e ;;
  rollback) rollback ;;
  all) e2e; rollback ;;
esac

echo
echo "=== staging report: $REPORT/ (summary: $REPORT/summary.txt) ==="
echo "PASS: $pass   FAIL: $fail"
if [ "$fail" -ne 0 ]; then echo "One or more staging steps FAILED."; exit 1; fi
echo "All staging steps passed."
