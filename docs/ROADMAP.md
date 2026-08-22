# Roadmap / design notes

Forward-looking design notes for where spark-lab is heading. These are **notes,
not decisions yet** — each would be promoted to an ADR (and would supersede the
ADR it touches) when we actually do the work. The migration phases
(`docs/MIGRATION_PLAN.md`) are the *current* track; this file is what's queued
behind it.

---

## 1. `requirements.txt` → `uv`

**Note.** Replace the pip-based dependency flow with **`uv`** as the package +
venv manager, and add a committed lockfile (`uv.lock`).

### What it changes
- **`bin/spark-lab`** already prefers `uv` when present and falls back to
  `python3 -m venv`. The wrapper would become uv-first with the pip path retired:
  `uv venv` + `uv sync` (against `uv.lock`) instead of `pip install -r requirements.txt`.
- **Packaging** (`pyproject.toml`): the runtime deps already live here (PEP 621);
  `uv` reads the same `[project] dependencies`, so `uv` and the wheel build share
  one source of truth. `requirements.txt` (and `requirements-dev.txt`) would be
  retired in favor of `uv`'s groups (`[dependency-groups] dev = [...]`).
- **CI** (`.github/workflows/validate.yml`): the test step would `pip install`
  → `uv sync` (or `uv pip install` into the test venv). Faster, reproducible, and
  the lockfile pins exact resolutions (stronger supply-chain posture — feeds the
  Objective 8 / security-audit goals).

### Why
- Reproducible, locked resolutions (no "works on my machine" drift).
- One tool for venv + install + lock; faster than pip.
- `uv.lock` is a reviewable, versioned artifact (the "no secrets / no drift" ethos
  extends to dependencies).

### Open questions
- Do we commit `uv.lock` to the repo (recommended: yes) and regenerate on dep bumps?
- Minimum `uv` version to require (CI + docs), and the no-`uv` fallback story for
  users who don't have it installed (keep the `python3 -m venv` fallback, or make
  `uv` required and provide a one-line install?).
- Does `uv` change the `pip`-based `spark-lab upgrade` dep-refresh (would become
  `uv sync`)?

---

## 2. `argparse` → `click`

**Note.** Replace the argparse CLI with **`click`** (a `click.Group` of
commands). This **supersedes ADR 0005** (which chose to keep argparse and defer
Click/Typer).

### What it changes
- The `sparklab/commands/` split maps cleanly onto Click: each
  `commands/<name>.py` becomes `@cli.command()` (or a sub-`@click.group()` for
  `check` → `check config` / `check images`). `sparklab/cli.py` becomes the
  `cli = click.Group()` + registration; `main()` wraps `cli()`.
- Options become decorators: `--config`/`-v`/`--json` → `@click.option` (a
  `ctx`/global or a shared option-decorator), `apply --dry-run/--diff/--apply`,
  `check images --probe`, `teardown --yes/--purge`, `migrate --dry-run`,
  `logs --lines/-f`.
- The runtime-seam injection (ADR 0002) is unchanged — `main` still builds the
  runtime and hands it to each command; with Click that's a `@click.pass_obj`
  context or a module-level default.

### Why
- Less boilerplate than argparse; options/flags are declarative.
- Better ergonomics: auto-grouped `--help`, consistent option naming, easy
  confirmation prompts (e.g. `teardown` could use `click.confirm`), and
  machine-readable `--json` via a callback.
- `click.Group` makes the `check`/`check images` nesting natural (argparse had
  hand-rolled nested subparsers).

### Tradeoffs / notes
- ADR 0005 deferred Click on "zero new deps" grounds. Click is a small, pure-Python
  dep — the new runtime dep is accepted in exchange for the CLI ergonomics.
- Must keep the **parity suite green** through the swap: the command set, exit
  codes (ADR 0006), and observable behavior stay identical; only the parsing
  layer changes. The `commands/*.py` logic is already decoupled from argparse,
  which makes this low-risk.
- `spark-lab logs --follow` streams; Click handles that fine (stream to stdout).

### Open questions
- Keep argparse as a documented fallback, or fully cut it?
- Should shared options (`--config`, `--json`) be a Click param decorator /
  `@click.group` with `context_settings`, or a small mixin?

---

## Also queued (deferred from Phases 3–4, not dropped)

- **`capture` port** — `scripts/capture.sh` still works; port it to
  `spark-lab capture` (docs-oriented, low priority).
- **`secret-scan` in Python** — kept the zero-dep shell scanner as the single
  authoritative gate to avoid a drift-prone duplicate; revisit if we want the
  gate in the tested Python codebase (a `spark-lab secret-scan` behind the seam).
- **Progress indicators** — low value for the current fast ops; revisit if
  multi-node/cluster apply gets slow.
- **In-repo `sparkrun` registry** — see `docs/REGISTRY.md` (design note from
  Phase 3; implementation is Phase 5 territory).
