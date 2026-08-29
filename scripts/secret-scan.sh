#!/usr/bin/env bash
#
# spark-lab secret scan.
#
# Scans ONLY the files that are tracked or staged — i.e. what is in, or about
# to go into, the repo. It deliberately does NOT scan the gitignored secret
# sinks (.env, config.yaml, deploy/, .sparklab-state/): that is exactly where
# real secret values live by design, and scanning them would be a false alarm.
#
# Usage:  scripts/secret-scan.sh
# Exits:  0 clean, 1 findings, 2 environment/usage error.
#
# This is a HARD gate. If it reports anything, do not commit — move the value
# into the gitignored .env / config.yaml (or an env var) and re-run.

set -uo pipefail

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "secret-scan: not inside a git repository" >&2
  exit 2
fi
cd "$(git rev-parse --show-toplevel)" || exit 2

# ---------------------------------------------------------------------------
# Layer 1 (path gate, deterministic): secret sinks must NEVER be committed,
# regardless of what value-detection says. A file named .env* or *.bak* is a
# local secret sink by definition; the gitignored deploy/state trees are
# generated output. Value scanners are shape-based and can miss credentials
# (unusual key formats, bare hex passwords, renamed sinks) -- this gate
# cannot. It inspects only the files staged for THIS commit, so already
# tracked examples (.env.example, config.example.yaml, templates) are
# unaffected.
# ---------------------------------------------------------------------------
staged="$(git diff --cached --name-only --diff-filter=ACMR)"
if [ -n "$staged" ]; then
  bad="$(<<<"$staged" grep -E '^(\.env|\.env\..+)$|^config\.(ya?ml)$|\.bak|^deploy/|^\.sparklab-state/|^labs/|^staging-report/|^recipes/candidates/')"
  bad="$(<<<"$bad" grep -v -E '^\.env\.example$' || true)"
  if [ -n "$bad" ]; then
    echo "secret-scan: BLOCKED -- secret-sink path(s) staged for commit:" >&2
    while IFS= read -r _b; do echo "    $_b" >&2; done <<<"$bad"
    echo "  These paths are local-only sinks (gitignored). Unstage with: git reset -- <path>" >&2
    echo "  Real values belong in the gitignored .env / config.yaml, never a .bak copy." >&2
    exit 1
  fi
fi

# Files that are tracked or staged. Union, de-duped, with gitignored sinks
# removed (the path gate above already blocks staging the sinks themselves;
# this keeps the value scanner quiet about the on-disk local copies).
files="$( { git ls-files; git diff --cached --name-only --diff-filter=ACMR; } \
  | LC_ALL=C sort -u \
  | grep -v -E '^(\.env|\.env\.|config\.yaml|config\.yml|config\.yaml\.|config\.yml\.|deploy/|\.sparklab-state/|\.venv/)' \
  | grep -v -E '\.bak$|\.bak\..*' )"

if [ -z "$files" ]; then
  echo "secret-scan: nothing tracked to scan — clean."
  exit 0
fi

# Preferred scanner, if installed. `gitleaks detect --no-git` scans the working
# tree and the committed .gitleaks.toml allowlists the gitignored secret sinks.
if command -v gitleaks >/dev/null 2>&1; then
  echo "secret-scan: using gitleaks $(gitleaks version 2>/dev/null | head -1 || true)"
  if gitleaks detect --no-git -c .gitleaks.toml; then
    echo "secret-scan: clean (gitleaks)."
    exit 0
  fi
  echo "secret-scan: gitleaks reported findings — DO NOT COMMIT." >&2
  exit 1
fi

# Fallback: high-signal, deterministic patterns. Chosen to catch the secret
# classes this stack uses while staying quiet on documented examples and
# env-var references (e.g. LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}).
BIGRE='hf_oauth_[A-Za-z0-9-]+|hf_[A-Za-z0-9-]{16,}|sk-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9-]{40,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|ghs_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z\-_]{25,}|xox[bpars]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

found=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  if matches="$(grep -aI -nE "$BIGRE" -- "$f" 2>/dev/null)" && [ -n "$matches" ]; then
    echo "secret-scan: potential secret in $f:"
    while IFS= read -r _m; do printf '    %s\n' "$_m"; done <<< "$matches"
    found=1
  fi
done <<< "$files"

if [ "$found" -ne 0 ]; then
  echo "" >&2
  echo "secret-scan: potential secret(s) found — review each hit above." >&2
  echo "  - Real secret? Move it to the gitignored .env / config.yaml, then re-run." >&2
  echo "  - Documented example / env-var reference? Note it, but prefer removing it." >&2
  exit 1
fi

echo "secret-scan: clean (grep fallback; install gitleaks for broader coverage)."
exit 0
