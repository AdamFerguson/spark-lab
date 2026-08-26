# ADR 0001 — Target module / package layout for the pip-installable spark-lab CLI

Status: proposed

## Context

The engine today lives in `lib/` (`config.py`, `render.py`, `converge.py`, `state.py`, `cli.py`, ~780 LOC) and is driven through `bin/spark-lab`, a shell wrapper that builds a local `.venv`, installs `pyyaml`/`jinja2`, and execs `python -m lib.cli`. This works for in-repo use but is not distributable: `lib` is not a real distribution name, there is no packaging metadata, no console-script entry point, and no packaging of `templates/` alongside the code.

Phase 3 of the migration plan (`docs/MIGRATION_PLAN.md`) requires the CLI to be `pip`/`pipx`-installable, and Phase 5 will add new command groups (`recipes search/list/show`, auto-conversion) plus the discovery plugin framework (ADR 0003) and the runtime seam (ADR 0002). We must lock the module layout now, in Phase 1, so that:

- existing tests (`tests/test_converge.py`, 9 tests) survive the move with import-line changes only;
- every planned seam (runtime, discovery plugins, config schema v2) has an unambiguous home;
- the legacy shell entrypoint `bin/spark-lab` stays operational throughout (project invariant) even after the package is renamed/relaid.

## Decision

Adopt a single top-level distribution package named **`sparklab`** (PEP 621 metadata in a new `pyproject.toml` at the repo root), with a console-script entry point that exposes the command as `spark-lab`. The layout:

```
sparklab/                  # the installable Python package
  __init__.py              # distribution name + version
  cli.py                   # argparse wiring only: parse args, build context,
                           # dispatch to a command handler. No business logic.
  commands/                # one module per command / command group
    init.py, apply.py, status.py, teardown.py, upgrade.py   # existing
    logs.py, validate.py, check.py                          # Phase 3 UX
    recipes.py            # search / list / show (Phase 5, consumes ADR 0003)
    convert.py            # opt-in auto-conversion (Phase 5)
  core/                    # pure-logic engine: no argument parsing, no I/O
                           # except through the runtime seam (ADR 0002)
    config.py             # config load + resolution (today's lib/config.py)
    schema.py             # declarative config schema: v1 fields, v2 `models:`,
                           # image map, discovery sources, conversion flag;
                           # validation errors and defaults live here
    render.py             # Jinja2 rendering + target mapping (lib/render.py)
    converge.py           # plan building only — build_plan() and friends
                           # (lib/converge.py minus execution)
    state.py              # .sparklab-state persistence (lib/state.py)
    secrets.py            # .env parsing + secret resolution, split out of config
    runtime.py            # THE runtime seam (ADR 0002): the only module that
                           # touches subprocess, filesystem writes, images
    discovery/            # plugin framework (ADR 0003)
      registry.py         # plugin registry + config-driven loading
      base.py             # RecipeSource contract + uniform record type
      sparkrun.py         # adapter: sparkrun registries
      cookbook.py         # adapter: SGLang cookbook
templates/                 # Jinja2 templates + verbatim assets — packed as
                           # package data, resolved relative to the installed
                           # package (replaces today's "repo_root/templates")
bin/spark-lab              # legacy shell wrapper — STAYS, unchanged in purpose
scripts/                   # legacy shell helpers — STAY operational
config.example.yaml, .env.example, recipes/ (Phase 5)
tests/                     # test modules follow the new import paths
```

Key mapping rules from the current `lib/`:

| Today | Target | Notes |
|---|---|---|
| `lib/config.py` | `sparklab/core/config.py` + `sparklab/core/secrets.py` + `sparklab/core/schema.py` | `.env` parsing moves to `secrets`; the flat section accessors move to `schema` as declared fields; `Config` stays the in-memory object both `render` and `converge` consume |
| `lib/render.py` | `sparklab/core/render.py` | template root resolution switches from "repo_root/templates" to "package data directory" so it works when installed via pip |
| `lib/converge.py` | `sparklab/core/converge.py` (planning) + `sparklab/core/runtime.py` (execution) | `build_plan`, `find_sparkrun`-style lookup, and `compute_*_after_apply` stay in `converge`; `execute`, `write_files`, and the raw `subprocess.run` calls move behind the runtime seam (ADR 0002) |
| `lib/state.py` | `sparklab/core/state.py` | unchanged in behavior |
| `lib/cli.py` | `sparklab/cli.py` + `sparklab/commands/*.py` | `cmd_*` bodies move to command modules; `cli.py` keeps only parser construction + dispatch; the runtime is constructed here and injected into handlers (ADR 0002 DI point) |

### Rationale

- **`lib` → `sparklab`:** `lib` is a generic, collision-prone top-level name; `sparklab` is the product name, unambiguous in `sys.path`, and matches the console script (`spark-lab`).
- **commands/ vs core/ split:** command modules are thin adapters (parse result → call core → format output); everything testable without a CLI lives in `core/`. This keeps the 9 existing tests and the planned Phase 2 suite aimed at `core/`, and keeps commands cheap to add (Phase 3/5 add commands without touching engine code).
- **One `core/runtime.py` for all side effects:** a single seam module (not scattered `subprocess` calls) is what makes ADR 0002 enforceable — see that ADR.
- **discovery/ as a subpackage:** the plugin registry, base contract, and the two bundled adapters are one cohesive unit (ADR 0003); co-locating them means the framework and its first implementations evolve together.
- **schema.py separated from config.py:** Phase 4 multi-model support rewrites the config model; keeping "declared shape + validation + defaults" separate from "load + resolve + accessors" means the compat loader (legacy `model:` → `models:`) is a schema concern, and byte-identical legacy rendering (risk R3) is verifiable against the schema.
- **Legacy `bin/spark-lab` stays operational:** after the move, the wrapper's exec target becomes the installed `spark-lab` console script (or `python -m sparklab.cli` from its venv). The wrapper file, `scripts/capture.sh`, `scripts/secret-scan.sh`, and `.githooks/` are untouched by this layout decision; per the migration plan they are deprecated after merge, not deleted before it.
- **Templates as package data:** rendering must work when the CLI is installed via `pipx` into an isolated venv with no repo checkout; resolving templates relative to `__file__` inside the installed package preserves current behavior for in-repo use and adds install-dir correctness.

## Consequences

- **Positive**
  - `pip install .` / `pipx install` yields a `spark-lab` executable; no venv bootstrap shell script required for normal use.
  - Import rename is mechanical; existing tests migrate by changing import lines (`lib.converge` → `sparklab.core.converge`), which lands with Phase 2's test-harness work.
  - Every future seam has exactly one home: runtime → `core/runtime.py`, discovery → `core/discovery/`, schema v2 → `core/schema.py`, new commands → `commands/`.
- **Negative / costs**
  - A one-time move + import rewire, including `tests/` and any tooling that imports `lib` (CI steps, the shell wrapper's exec target).
  - New packaging surface to keep correct: `pyproject.toml` metadata, `console_scripts` entry point, package-data inclusion of `templates/` (missing data is a classic pip packaging bug — covered by a Phase 2 smoke test that imports and renders from an installed wheel).
  - `core/config.py` is split across three modules, so a reader must know the seam between schema, resolution, and accessors.
- **Constraints honored**
  - No behavior change is required by the layout itself (Phase 1 is contracts-only); the move is sequenced into Phase 3 where parity tests gate it.
  - Legacy shell scripts remain operational; the repo invariant that nothing in-repo is deleted pre-merge is respected — this ADR only adds a parallel package home.
  - No secrets are introduced: `config.example.yaml`/`.env.example` remain examples; the new `discovery.sources` config keys (ADR 0003) reference credentials only by env-var name.

## Alternatives considered

- **Keep `lib/` and just add packaging metadata.** Cheapest diff, but locks a collision-prone top-level name into the distribution, and `lib` reads as "unshipped" to contributors; the rename cost is small while the codebase is still ~800 LOC plus new modules.
- **Nested application layout (`sparklab/application/`, `sparklab/domain/`, `sparklab/infrastructure/`).** Over-engineered for this size; the commands/core split already gives the needed seam boundaries without a third tier of indirection.
- **Per-module packaging (separate `sparklab-render`, `sparklab-converge` distributions).** No benefit: nothing outside the repo consumes these modules individually; a single distribution keeps versioning trivial.
- **Symlink/alias package (`lib/` re-exporting `sparklab`) as a permanent dual home.** Rejected as permanent (two sources of truth risk); a *temporary* compat shim during the Phase 3 cutover window is acceptable and will be removed with the deprecation notice.
