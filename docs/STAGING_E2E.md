# Staging E2E + Rollback runbook (Phase 6, **tabled pending the staging Spark**)

This is the node-dependent half of Phase 6. It is **tabled** until a staging
DGX Spark is available; when it is, this becomes a verbatim run. The
node-independent half of Phase 6 (security/supply-chain/secrets/LLM-path audit +
regression + merge-gate definition) is already done — see `docs/AUDIT-P6.md`.

## What this proves on the staging node

- **Converge & idempotency:** `apply` builds the stack; a second `apply` is a
  no-op. Editing the config converges the node to the new state.
- **Multi-model + gated restart:** switching the active model + `apply` restarts
  the model (the `--apply`-gated path) without disturbing other services.
- **Discovery + conversion:** `recipes list/search/convert` work against the
  in-repo registry + cookbook; conversion produces a validated, never-applied
  candidate.
- **Upgrade:** `upgrade` refreshes deps + images and re-applies.
- **Rollback:** reverting a config change + `apply` converges the node back.

## Preconditions

1. A **staging** Spark (not the live node). A distinct `install_dir` + host.
2. The repo checked out on `feature/python-cli`; `spark-lab` on PATH
   (`pip install -e .` in a venv, or use `bin/spark-lab`).
3. A staging `.env` with test values for the `*_env` vars the config names
   (`LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `LITELLM_DB_PASSWORD`,
   `GRAFANA_ADMIN_PASSWORD`, `HF_TOKEN` if gated).
4. `config.staging.yaml` (copy of `config.staging.example.yaml`, tuned).

> **Do not** point the staging config at the live host or install_dir. Set
> `LIVE_INSTALL_DIR=<live install_dir>` so `scripts/staging.sh` refuses to run
> against the live dir.

## How to run

```bash
cd /path/to/spark-lab
cp config.staging.example.yaml config.staging.yaml   # then edit install_dir + .env

# full E2E + rollback drills, captured to staging-report/
LIVE_INSTALL_DIR=~/AI SPARKLAB=spark-lab \
  scripts/staging.sh all --config config.staging.yaml

# or just one half:
scripts/staging.sh e2e --config config.staging.yaml
scripts/staging.sh rollback --config config.staging.yaml
```

`scripts/staging.sh` runs each step through the real CLI, tees every step to
`staging-report/<step>.log`, and prints a `PASS: n FAIL: m` summary. It exits 0
only when every step passes.

## Step checklist (what each step verifies)

| Step | Command | Verify |
|---|---|---|
| preflight | `validate` | schema + render + required binaries ok |
| images | `check images` | every image the deploy will pull resolves |
| apply-dry | `apply --dry-run` | plan is sane; nothing written |
| apply | `apply --yes` | stack + model converged onto the staging node |
| status | `status` | workloads + stack + network reflect the config |
| idempotent | `apply --yes` | **no-op** (converged; nothing re-run) |
| switch-model | (flips config) | exactly one active + one inactive model |
| apply-switch | `apply --yes` | model restarted to the new active model |
| status-2 | `status` | now serving the new model |
| recipes-* | `recipes ...` | discovery + conversion work end to end |
| upgrade | `upgrade` | deps + images refreshed, re-applied cleanly |
| snapshot/change/apply-chg | (rollback) | a config change converges |
| revert/apply-rt | (rollback) | reverting + applying **converges back** |

## Pass/fail criteria

- **Green:** `PASS: N FAIL: 0` from `scripts/staging.sh`, and the logs in
  `staging-report/` show the expected converge/restart/rollback behavior.
- **Red:** any step FAIL — read its `staging-report/<step>.log`, fix, re-run.

## Cleanup

After the run, tear down the staging stack so the node is clean:

```bash
spark-lab teardown --config config.staging.yaml --yes
```

## Status

**TABLED.** To be executed on the staging Spark and the results recorded here
(log excerpt + pass/fail) before the merge gate (`docs/MERGE_GATE.md`) can be
closed.
