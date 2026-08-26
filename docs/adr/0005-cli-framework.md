# ADR 0005: CLI framework — keep argparse; defer a dedicated framework

Status: proposed

## Context

`spark-lab` today is a flat, argparse-based CLI with five subcommands
(`init`, `apply`, `status`, `teardown`, `upgrade`). The relevant facts that
constrain this decision:

- **Entry surface.** `bin/spark-lab` is a bash wrapper that manages a managed
  venv (installing only `pyyaml` + `jinja2` when missing) and then runs the
  CLI as a module (`python -m lib.cli`). The venv is the project's whole
  runtime; there is no packaging metadata yet.
- **Current structure.** `lib/cli.py` builds one `argparse` parser, a shared
  "common" parent parser carrying `--config` / `-v,--verbose` / `--json`,
  `add_subparsers(..., required=True)` for the five commands, and dispatches via
  `set_defaults(func=...)`. Each `cmd_*` returns a process exit `int`;
  `main(argv)` parses and calls it.
- **The Phase-1 runtime seam** is a mockable boundary around the
  `sparkrun` / `docker` / `systemctl` / `ssh` subprocess calls (see
  `converge.find_sparkrun`, `converge.execute`, and `cli._run`). Command logic
  is already decoupled from the parser: `cmd_*` receive a parsed-args object
  and return an int.
- **Minimal-surface posture.** The project deliberately ships *only* `jinja2`
  and `pyyaml` in the managed venv, with a secret-scan + supply-chain gate in
  CI. Any new dependency is a real, recurring cost (install, version, audit,
  supply-chain review, and a line that must be justified against the posture).
- **Project invariants.** (a) Legacy shell scripts stay operational — the new
  Python CLI is additive, nothing is deleted pre-merge. (b) No secrets in config
  or repo. (c) `converge` is idempotent and the model-restart is gated behind an
  explicit `--apply`/`--yes` flag.
- **Forward scope (Phase 3).** Make the CLI `pip`/`pipx`-installable via a
  `console_scripts` entry point, add additive UX (`logs`, `validate`/`check`,
  broader `--dry-run`, progress indicators), and keep shellcheck-clean.

The open question is whether the CLI framework should stay argparse or adopt a
dedicated framework (Click or Typer) now, on the feature branch, before the
code phase.

## Decision

**Keep `argparse`. Do not adopt a dedicated CLI framework in this cycle.**

1. **The five existing subcommands stay argparse as-is** (zero rewrite). New
   commands (`logs`, `validate`/`check`, `recipes *`, `migrate`) are added to
   the same flat `add_subparsers` tree with the same "common" parent parser
   (`--config` / `-v` / `--json`).
2. **Packaging** is satisfied without a framework: add packaging metadata that
   declares a `console_scripts` entry point named `spark-lab` bound to the
   existing top-level entry callable (the current `main`), which accepts an argv
   list and returns a process exit code. This makes `pip install`/`pipx` work
   while `bin/spark-lab` continues to operate on the node (invariant: legacy
   path stays operational). The shell wrapper and the installed entry point are
   two faces of the same `main`.
3. **UX gaps are closed additively inside argparse, not by swapping the
   framework:**
   - *Subcommand grouping in help* is achievable with a custom
     `HelpFormatter` (section headers) — no new dependency.
   - *Consistent error formatting* and *stable exit codes* come from ADR 0006,
     not from the framework.
   - *Shell completion* is an opt-in, separately-justified dependency
     (e.g. `argcomplete`) adopted **only if** it becomes a stated requirement.
     It is orthogonal to this decision: it costs a dependency whichever
     framework is used.
4. **Revisit trigger.** If the CLI grows genuinely *nested* command groups that
   argparse's flat model cannot express cleanly, **or** rich help / shell
   completion becomes a hard (not "nice-to-have") requirement, open a new ADR to
   re-evaluate. Today neither is in the Phase-3 scope.

## Rationale

- **Supply-chain is the dominant constraint.** The project's whole posture is a
  two-dependency managed venv. A framework is a *transverse* concern: none of the
  Phase-3 deliverables (installable entry point, `logs`, `validate`, `--dry-run`
  breadth, progress) *require* a new framework to be delivered. argparse already
  supports the flat command set, required subcommands, shared flags, and
  int-returning handlers. Choosing a framework buys features we do not currently
  need at a permanent dependency/audit cost we do not need to pay.
- **Testability is unaffected.** The runtime seam (subprocess mocking) is
  independent of the parser framework. Command-level tests call `main(argv)`
  (or a `cmd_*` with a parsed-args object) and inject a fake runtime; CLI-UX
  tests capture help/usage text and a deliberate parse-error. This works identically
  for argparse today; adopting Click/Typer would not make the *command* logic
  more testable — it would only change how help text is asserted.
- **Migration cost is the deciding asymmetry.** argparse = zero rewrite of the
  five subcommands and zero parity re-verification. Click/Typer = rewrite every
  flag, re-map non-obvious semantics (the `--apply`/`--yes` aliasing, `dest`
  naming, the shared "common" parent), and re-run the Phase-2 shell↔Python parity
  suite to prove no regression. That is real effort and real risk for UX
  features that are not required this cycle.
- **Packaging works without a framework.** A `console_scripts` entry point just
  needs a callable that takes argv and returns an exit code — which `main`
  already is.
- **The invariants are framework-neutral** and are actually enforced by ADR 0006,
  not by whichever parser is chosen.

## Consequences

- **Positive**
  - No new runtime dependencies; the venv stays `pyyaml` + `jinja2`.
  - Zero migration/regression risk on the existing five subcommands; the
    Phase-2 parity suite keeps passing without re-baselining.
  - A single, stable, stdlib-based parsing layer that is easy to audit.
  - Packaging (`pip`/`pipx`/`console_scripts`) achieved with metadata only.
- **Negative / accepted trade-offs**
  - Help formatting and subcommand grouping are "hand-rolled" (custom
    formatter) rather than declarative; acceptable at this command count.
  - Shell completion is not available out of the box and would be a separate,
    dependency-bearing add.
  - As the command tree grows, argparse help output and grouping maintenance
    get more manual; the revisit trigger above bounds that.
- **Neutral**
  - The console entry point and the `bin/spark-lab` wrapper coexist; both drive
    the same `main`, so node behavior is unchanged (invariant holds).

## Alternatives considered

- **Click (single new dependency).**
  - *Pros:* subcommand groups, clean custom error handling, built-in shell
    completion, small single-dep footprint.
  - *Why not:* the features it uniquely offers (grouping, built-in completion)
    are not required in Phase 3, and it still means rewriting the five
    subcommands, re-mapping flag semantics, and re-running parity to prove no
    regression — for a capability we can defer. A 1-dep cost, but the cost is the
    rewrite + re-verification, not the dependency itself. Revisit if grouping /
    completion become hard requirements.

- **Typer (heaviest footprint).**
  - *Pros:* type-hinted commands, polished help, built-in shell completion,
    the most "batteries-included" DX.
  - *Why not:* the worst fit for a minimal-surface project. It transitively pulls
    in `rich` (and its own tree: `markdown-it-py`, `pygments`, `typing-extensions`
    on older Pythons) — a large, fast-moving dependency chain that materially
    widens the audit/supply-chain surface the project has deliberately kept tiny.
    It also introduces type-hint→argument mapping quirks that would complicate the
    non-obvious flag semantics already in use (`--apply`/`--yes` aliasing,
    `dest` naming). The weight is not justified for a flat subcommand set.

- **Do nothing (leave the bare `python -m lib.cli` invocation, no packaging).**
  - *Why not:* fails the Phase-3 requirement that the CLI be
    `pip`/`pipx`-installable. This decision adds the `console_scripts` entry
    point *without* adding a framework, which is the minimal path to that goal.

## Relationship to other ADRs

- **ADR 0006** defines the CLI contract (exit codes, structured errors,
  logging, dry-run semantics, `--json` shape, config handling, progress, and how
  the runtime seam is injected). Those contracts hold *regardless* of which
  framework is chosen — which is exactly why the framework is a low-stakes
  decision here.
