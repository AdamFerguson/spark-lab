# ADR 0006: CLI best-practices checklist for the code phase

Status: proposed

## Context

ADR 0005 fixes the CLI *framework* (argparse). This ADR fixes the *behavioral
contract* every subcommand must honor when it is implemented/refactored in
Phase 3. It is framework-agnostic on purpose: the same checklist applies whether
the entry point stays argparse or is later migrated. It exists so the code phase
and its review have a shared, checkable definition of "a good `spark-lab`
command." It is a contract-level checklist, not implementation code.

It must respect the project invariants:
- Legacy shell scripts stay operational — new commands are additive; `status`
  surfaces migration hints; nothing is deleted pre-merge.
- No secrets in config or repo; `.env`/config are the only sinks for real
  values; `*.example` carry names/patterns only.
- `converge` is idempotent and the model-restart is gated behind an explicit
  `--apply`/`--yes` flag.

## Decision

The code phase is considered compliant when every subcommand satisfies the
checklist below. Reviewers block on any unmet item marked **(gate)**; others are
strong defaults.

### 1. Exit-code conventions
- **0** — success / converged / completed. A routine `apply` that succeeds exits
  0 even if work was done.
- **1** — expected operational failure: a planned command returned non-zero,
  config missing/invalid, a required secret absent, validation failed, or a
  destructive action was refused without its confirming flag.
- **2** — CLI misuse / bad arguments (the argparse parse-error code). Keep this
  reserved for usage errors, not for runtime failures.
- **Never** swallow a subprocess failure: propagate a non-zero code, do not
  collapse it to 0.
- **Pending-restart is not an error:** a routine `apply` that leaves a
  model-restart pending exits **0** but must emit the structured
  `restart-pending` warning (see §2) and a hint to re-run with `--apply`.
- **Dry-run success exits 0** even when the plan has changes — it is a preview,
  not a failure.
- If any new code is introduced beyond {0,1,2}, it must be documented here and
  in `--help`; no undocumented exit codes.

### 2. Structured errors
- **(gate)** Errors go to **stderr**; they never pollute stdout (which is where
  machine-readable / human summary output belongs).
- Every expected error is a **one-line, actionable message + `hint`** naming the
  likely fix (e.g. "config not found — run `spark-lab init`", "add `--apply` to
  restart the model").
- **(gate)** Stable, machine-readable **error codes** for the known failure
  classes: `config-missing`, `config-invalid`, `secrets-missing`,
  `command-failed`, `restart-pending`, `validation-failed`. Scripts and CI key
  off codes, not prose.
- No stack trace is shown to the user for *expected* errors; tracebacks are
  reserved for genuinely unexpected exceptions, where a short message + "run
  with `-v` for details" is shown and the full trace goes to the log (§3).

### 3. Logging strategy
- **(gate)** Use the stdlib `logging` module, not ad-hoc `print`, for
  diagnostics. Keep a clear split between **user-facing output** (stdout, plain,
  what the parity tests diff) and **diagnostic logging**.
- Levels: **DEBUG** (argv, file hashes, full plan, resolved paths) · **INFO**
  (each action taken, "Converged.") · **WARNING** (restart-pending, skipped
  command, missing binary) · **ERROR** (failures).
- **Where logs go:** human summary → stdout. Detailed log → a file under the
  state dir (e.g. `.sparklab-state/` logs) so the node keeps an audit trail of
  applies. `-v` raises *console* verbosity to DEBUG; it does not redirect the
  file log.
- **(gate)** Never log secret *values* (the `.env` contents). Log env-var
  **names**, never resolved values — in both the log file and `-v`/`--json`
  output (this upholds the no-secrets invariant).
- Logging must be **additive**: it must not alter the human-facing stdout that
  the shell↔Python parity tests compare.

### 4. Dry-run semantics
- **(gate)** `--dry-run` renders to a **throwaway** directory, builds the full
  plan, prints a clearly-prefixed "would run" line for every command, **writes
  nothing**, runs **no** subprocess, and exits 0.
- Deterministic: identical config → identical plan output (idempotent preview).
- **(gate)** Dry-run must never mutate state (`.sparklab-state`), the install
  dir, or the model; it never touches the runtime seam.
- A dry-run that detects a required restart reports it as
  "would require `--apply`", not as an error or a code-1 failure.
- Phase-3 "dry-run breadth": `validate`/`check` performs pre-execution
  validation (schema + image availability) and per-command previews under the
  same dry-run contract.

### 5. JSON / machine-readable output
- `--json` (already on the "common" parser) emits **one** JSON document on
  stdout; all human chatter moves to stderr.
- **(gate)** Stable, versioned schema: top-level object carrying `ok`,
  `command`, `dry_run`, a `plan` (file changes + would-run commands), a `state`
  block, an `errors[]` (each with `code`/`message`/`hint`), and a
  `schema_version` so consumers can rely on the shape.
- JSON and prose never mix on stdout; `--json` is opt-in and suppresses progress
  chatter (§7).
- `status --json` must be parseable by CI/monitoring (workloads, stack, network).
- Secrets are masked/omitted in any JSON output (§3).

### 6. Config handling
- **(gate)** `--config` defaults to `./config.yaml`, `expanduser`, resolved to
  absolute. Missing config → `config-missing` error + hint to run `init` (exit
  1), never a traceback.
- **(gate)** Validate the schema **before** acting (fail fast); the
  `validate`/`check` command exposes this as a first-class pre-execution probe.
- Secrets are resolved from `.env`/environment **by name only**; real values live
  only in the gitignored `.env`/`config.yaml`; `config.example.yaml`/
  `.env.example` carry names/patterns only (invariant).
- The Phase-4 backwards-compat loader upgrades a legacy single `model:` to
  `models:` with **zero render diff** and emits an explicit warning when it
  auto-upgrades.

### 7. Progress indicators
- For multi-step operations, emit a stable one-line-per-step "==> description"
  (already present in `converge.execute`) plus an overall step counter for
  multi-step plans.
- Progress lines go to stdout but stay **stable** (one line per step) so they do
  not break parity/JSON; suppressed entirely under `--json`.
- **No TTY assumptions**: detect a TTY and degrade to plain, sequential lines
  when piped/CI so output is always diffable.
- Long-running streaming commands (`logs`/`tail`) follow and stream; a Ctrl+C /
  SIGINT is handled gracefully (clean exit, non-zero reflecting interruption,
  no traceback).

### 8. Runtime-seam injection for tests
- **(gate)** Introduce a single, injectable **runtime object** — the
  command↔runtime boundary — that is the *only* place wrapping external
  subprocess / `shutil.which` / sparkrun-lookup calls (`sparkrun`, `docker`,
  `systemctl`, `ssh`).
- The seam returns the **same shape** the real one does (e.g. a result with
  `returncode`/`stdout`), so command logic is unchanged whether it talks to the
  real or a fake runtime.
- **Commands receive the runtime explicitly** (dependency injection through the
  entry point / constructor), rather than calling module-level `subprocess`
  directly. `main` constructs the default (real) runtime and passes it down.
- **Tests inject a fake/recording runtime** at the entry point — replacing the
  current practice of monkeypatching module attributes (e.g. overriding
  `converge.find_sparkrun`) with a single, central injection point.
- **Parity tests** drive the legacy shell path and the new Python path (backed by
  the recording runtime) with identical inputs and diff the outputs — rendered
  files **and** emitted commands.
- The seam is deliberately **narrow**: a boundary, not a service-registry
  abstraction. No over-engineering.

## Consequences

- **Positive:** a shared, reviewable definition of "correct" for every command;
  stable exit codes + error codes enable scripting/CI; the logging split +
  log-file gives a node audit trail without touching parity output; the runtime
  seam makes command logic unit-testable and centralizes the shell↔Python parity
  harness; secrets stay out of every output surface.
- **Negative / accepted:** a `schema_version` and error-code taxonomy must be
  kept stable over time (a small maintenance obligation); the custom log file
  adds one more on-disk artifact under `.sparklab-state` (gitignored).
- **Neutral:** nothing in this checklist changes the idempotent converge model or
  the `--apply`-gated model-restart; it only specifies how those behaviors are
  *reported*.

## Alternatives considered

- **Freeform per-command error/exit handling (no shared contract).**
  - *Why not:* produces inconsistent exit codes, tracebacks to users, and
    unparseable output — exactly what the review/CI gates (and the merge gate's
    "no open P0/P1") are meant to prevent. A shared contract is the point.
- **A dedicated logging/telemetry framework (e.g. structured-log library).**
  - *Why not:* a new dependency (contradicts the minimal-surface posture of
    ADR 0005) for what the stdlib `logging` + a small JSON shape already covers.
    Revisit only if log-shipping/remote-logging becomes a real requirement.
