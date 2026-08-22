# Migrating the existing Spark onto `spark-lab`

**Goal:** convert the already-running production Spark over to being managed by
`spark-lab` **without disturbing the live model service** — then have all future
changes flow through `spark-lab` (idempotent, convergent, reversible).

This is a **plan to execute after the new (staging) Spark passes its E2E**. It is
safe to re-read whenever, and every step is non-destructive until you explicitly
opt into a model restart.

> **Hard constraint honored throughout:** the running model is never stopped or
> restarted by adoption or by a routine `apply`. The model restart is fail-safe
> gated behind an explicit `apply --apply`.

---

## Why this is low-risk

- **`spark-lab adopt` is read-only against the live install.** It renders what
  spark-lab *would* write, compares each file against what is actually on disk, and
  records the on-disk reality into `.sparklab-state/state.json`. It writes **only**
  the state file (never into the install dir) and runs **no** `sparkrun` command.
- **The restart is fail-safe.** After `adopt`, a routine `spark-lab apply` will at
  most *report* "a model change is pending" — it will **not** restart the model.
  Only `spark-lab apply --apply` restarts it, and only you can type that.
- **The legacy path stays operational.** Until you deliberately converge, the
  running stack is exactly what you have today; nothing new is forced on it.
- **Byte-identical is the target.** Because the config is derived from the same
  captured baseline, the goal is that `apply --dry-run` shows the recipe as
  *unchanged*. If it is, adoption is seamless and there is literally nothing to
  restart.

---

## Prerequisites (before you start, on the existing Spark)

- [ ] You have a **capture** of the current live state: the running recipe
      (`<install_dir>/sparkrun/recipes/<model>.yaml`), the `docker-compose.yml`,
      the on-disk `config` values, and the running `.env`. (`scripts/capture.sh` /
      `spark-lab capture` — the same capture that produced `tests/capture/`.)
      **This capture is your rollback.** Save it somewhere safe *outside* the node.
- [ ] You know the **exact `install_dir`** the live stack uses and the **recipe
      name** of the running model.
- [ ] The `spark-lab` repo is available to clone on the node.

---

## Steps

### 0. Take + save the rollback snapshot
Capture the live files + running state now (step above). Confirm you have the
recipe, the compose, and the `.env`. If any of those is missing, grab it directly
from the node **before** touching anything:

```bash
# e.g. (adjust paths to your live install_dir)
cp ~/AI/sparkrun/recipes/<model>.yaml   /tmp/rollback/
cp ~/AI/litellm/docker-compose.yml      /tmp/rollback/
cp ~/AI/.env                            /tmp/rollback/
docker compose -f ~/AI/litellm/docker-compose.yml ps   # record the running services
sparkrun status                                  # record the running job
```

### 1. Clone + precheck
```bash
git clone https://github.com/AdamFerguson/spark-lab ~/spark-lab
cd ~/spark-lab
spark-lab doctor          # / check system: detects docker, sparkrun, uv, ...
```
Confirm the required tools are present (they are, since the stack already runs).

### 2. Create a config that mirrors the live setup
Create `config.yaml` + `.env` in `~/spark-lab` describing the **existing** install:

- `install.install_dir` = the live install_dir (e.g. `~/AI`).
- The model block (image, `hf_model`, port, `params`, `extra_flags`) = what the
  live recipe uses (copy from the captured recipe).
- `model.recipe_name` = the live recipe's basename.
- The `.env` values = the live secrets (LiteLLM keys, DB password, etc.).

These values contain your real hostnames/secrets and **never go into the repo** —
they live only in your local `config.yaml` + `.env`.

### 3. Verify the render matches the live files
```bash
spark-lab validate                 # config + .env present, tools available
spark-lab apply --dry-run --diff   # show the diff vs the live install, file by file
```
**Aim for "no changes" on the recipe** (byte-identical to the live one). That is
the success criterion for a zero-downtime cutover. Review any drift carefully —
these are the only files that *could* change.

### 4. Adopt the running install (no restart)
```bash
spark-lab adopt --dry-run          # preview what would be recorded
spark-lab adopt                    # record on-disk state + the running model -> state.json
spark-lab status                   # confirm it reads back the live stack
```
Adoption writes **only** `.sparklab-state/state.json`. The live stack is untouched.

### 5. Decide what to do with any drift
- **If step 3 showed the recipe unchanged:** you're done. `spark-lab` now fully
  manages the install; the model is *converged* and a routine `apply` will never
  restart it.
- **If there is recipe drift:** pick one:
  - **(a) Keep the live recipe as-is.** `adopt` already recorded the on-disk
    recipe as "current," so routine `apply` will *not* restart the model. You can
    keep using spark-lab for the stack (monitoring, gateway, etc.) while the model
    recipe stays exactly as it runs today. Drift stays *visible* (the `apply
    --dry-run` diff) so you can converge it later deliberately.
  - **(b) Converge to spark-lab's recipe (one-time restart).** Schedule a brief
    window and run `spark-lab apply --apply`. This writes spark-lab's recipe and
    restarts the model **once**. After that it's fully managed and stable.

### 6. From here on
All changes go through `spark-lab` (edit `config.yaml` → `apply --dry-run` →
`apply [--apply]`). The legacy hand-run path is still available as the rollback,
but the live stack is now convergently managed.

---

## Rollback (if anything goes wrong)

Because adoption never rewrites the live files or restarts the model, "rollback"
is almost never needed. If you *did* converge (step 5b) and the new model misbehaves:

1. Stop the new model: `sparkrun stop <recipe>` (or `spark-lab teardown` if you
   want the whole stack down — **use this deliberately**, it stops the model).
2. Restore the captured files from `/tmp/rollback/` into the install_dir.
3. Bring the stack back up the legacy way:
   `docker compose -f .../docker-compose.yml up -d` + `sparkrun run <model>`.
4. Delete `~/spark-lab/.sparklab-state/state.json` to reset spark-lab's view.

The captured `.env` + recipe are the source of truth for this path.

---

## Open questions to confirm on the node

- [ ] Exact `install_dir` + recipe name of the running model.
- [ ] Whether the live recipe is already byte-identical to a spark-lab render
      (step 3 will tell you — this decides 5a vs 5b).
- [ ] Whether the live stack's LiteLLM/monitoring already match the template, or
      have hand-edits that would show as drift.
