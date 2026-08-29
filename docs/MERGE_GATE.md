# Merge gate — `feature/python-cli` → `main`

The definition of "done enough to merge." Everything here is either already true
(node-independent) or explicitly **PENDING** the staging Spark. The legacy shell
path stays the operational system until merge **and** a deprecation window.

## The gate

| # | Criterion | Evidence | Status |
|---|-----------|----------|--------|
| 1 | **Parity:** the engine's rendered output + command sequence are stable (v1 renders byte-identically; v1→v2 upgrade renders identically) | golden regression suite (`tests/test_parity.py`, `tests/test_config_v2.py`) | ✅ PASS |
| 2 | **Full suite green in CI:** the entire test suite passes on the branch in CI | `.github/workflows/validate.yml` (unit tests + coverage ≥ 85%) | ✅ PASS (103 tests) |
| 3 | **Coverage** on `sparklab/` ≥ 85% (CI-gated) | `coverage report` | ✅ PASS (92%) |
| 4 | **Secrets:** the authoritative scanner is clean; pre-commit + CI both enforce it | `docs/AUDIT-P6.md` §1, `scripts/secret-scan.sh` | ✅ PASS |
| 5 | **Supply chain:** minimal dep set, pinned tooling, no known CVEs on the pinned versions | `docs/AUDIT-P6.md` §2 (pip-audit clean) | ✅ PASS |
| 6 | **LLM call path:** opt-in, token via env only, minimal content, no secrets in prompts, safe fallback | `docs/AUDIT-P6.md` §3 | ✅ PASS |
| 7 | **Staging E2E:** full converge/restart/discovery/upgrade flow passes on a staging node | `docs/STAGING_E2E.md`, live run on `luna` | ✅ PASS (see results below) |
| 8 | **Rollback drills:** reverting/tearing down converges the node back | `scripts/staging.sh`, `teardown` on `luna` | ✅ PASS (see results below) |
| 9 | **No live-node disturbance:** all E2E ran on a staging node + distinct install_dir; live model untouched | dedicated node `luna` + `install_dir ~/AI` | ✅ PASS (see results below) |

**Merge rule:** merge only when **all of 1–6 remain green** *and* **7–9 pass on
the staging node**. Until then, `feature/python-cli` is the work-in-progress
branch and `main` remains the operational system.

## When the staging Spark is available (the PENDING half)

1. `cp config.example.yaml config.staging.yaml`, set a distinct
   `install_dir` + host, and fill the staging `.env`.
2. Run the runbook, capturing results:
   ```bash
   LIVE_INSTALL_DIR=<live install_dir> scripts/staging.sh all --config config.staging.yaml
   ```
3. Confirm `PASS: N FAIL: 0`; record a short excerpt of `staging-report/summary.txt`
   + any restart/rollback log lines in `docs/STAGING_E2E.md` (Status).
4. `spark-lab teardown --config config.staging.yaml --yes` to clean up the node.
5. Mark rows 7–9 ✅ in this table; close the gate.

## Staging results (run live on `luna`, a dedicated DGX Spark)

Executed on `luna` (Ubuntu 24.04, GB10, 121 GiB) with `Qwen/Qwen3-0.6B` +
`lmsysorg/sglang:latest`, `install_dir ~/AI`.

- **Fresh setup (row 7):** `doctor` (all required tools + the new **docker-access**
  capability ok) → `validate` (11 files) → `apply` (pulled the SGLang image + model,
  model serving on `:30000`) → **LiteLLM gateway returned a real completion** for
  `luna-model` → Prometheus **healthy + scraping 82 `sglang_*` metrics** → Grafana
  healthy → **state recorded / converged**. The best-effort tailscale
  `systemctl` line warned + continued (needs root) instead of aborting.
- **Rollback (row 8):** `teardown --yes` stopped the model + removed all stack
  containers + **cleared state**; a post-teardown `apply --dry-run` is a fresh full
  plan (re-converge-ready). Node left clean; install files kept (no `--purge`).
- **No live disturbance (row 9):** everything ran on `luna` with its own `~/AI`;
  the live spark was never touched. The live-migration path (`adopt`) is read-only
  by design.

**Real bugs found + fixed on the live node** (each committed + CI-green):
1. single-node `run`/`stop` required `--hosts` (bare commands errored "No hosts
   specified");
2. `sparkrun run` must take the **recipe file path**, not a registry name;
3. `sparkrun stop` must also take the **path** (registry-name lookup failed).

**Operational hardening added during the run:** the precheck's **capability** layer
(docker access → exact `usermod` fix), **best-effort** root-gated infra-ensure steps,
and a doc note that the cold-start `apply` is slow/blocking (use `tmux`).

> Note: this was done ad-hoc (interactive), not via `scripts/staging.sh` — the
> runbook remains the scripted path, and `staging-report/` captures can still be
> generated from it. The gate above reflects the verified behavior.

## After merge

- A **deprecation window** during which the legacy shell path
  (`bin/spark-lab` + `lib/`) remains available and is then retired.
- Optional follow-ups live in `docs/ROADMAP.md` (uv migration, argparse→click,
  registry manifest alignment, capture/secret-scan Python ports).
