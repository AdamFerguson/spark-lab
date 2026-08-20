#!/usr/bin/env bash
# capture.sh — capture READ-ONLY terminal output for docs / a blog post.
#
# This never mutates the node: it only runs status/inspection commands and
# writes their output to text files. Sanitize the output before publishing
# (rename hostnames, never include keys/tokens).
#
# Usage:
#   scripts/capture.sh [output-dir]
#   COMPOSE_FILE=/path/to/docker-compose.yml scripts/capture.sh captures
set -uo pipefail

OUT="${1:-captures}"
COMPOSE_FILE="${COMPOSE_FILE:-$HOME/AI/litellm/docker-compose.yml}"
SPARKRUN="${SPARKRUN:-$(command -v sparkrun || echo "$HOME/.local/bin/sparkrun")}"
mkdir -p "$OUT"

capture() {  # capture <basename> <command...>
  local out="$OUT/$1"; shift
  echo "\$ $*" | tee "$out"
  "$@" >>"$out" 2>&1 || echo "(command exited $?)" >>"$out"
  echo "  -> $out"
}

echo "Capturing read-only output into $OUT/ ..."
capture sparkrun-status.txt  "$SPARKRUN" status
capture nvidia-smi.txt       nvidia-smi
capture tailscale-status.txt tailscale status
if [ -f "$COMPOSE_FILE" ]; then
  capture docker-compose-ps.txt docker compose -f "$COMPOSE_FILE" ps
else
  echo "(skipping docker-compose-ps: $COMPOSE_FILE not found)"
fi

echo
echo "Done. Review the files under $OUT/ and sanitize hostnames before publishing."
