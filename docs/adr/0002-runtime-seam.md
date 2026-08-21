# ADR 0002 — Command↔runtime boundary: a mockable executor seam for all side effects

Status: proposed

## Context

Today, side effects are scattered across modules:

- `lib/converge.py` — `execute()` shells out via raw `subprocess.run` (sparkrun start/stop, docker compose up, systemctl), and `write_files()` writes rendered bytes into the install dir.
- `lib/cli.py` — `cmd_status`, `cmd_teardown`, and `cmd_upgrade` each run their own `subprocess.run` / `_run()` calls (sparkrun status/stop/update, docker compose ps/down/pull, tailscale, pip install -r requirements).
- `lib/render.py` — writes rendered files into `deploy/` as a side effect of rendering.

Consequences: (a) Phase 2's integration and shell↔Python parity tests cannot exercise `apply`/`teardown`/`upgrade` without a real node, docker, and sparkrun; (b) the safety invariants — idempotent converge, model restart gated behind `--apply` — live in tangled plan-build + execute code and are only weakly testable; (c) every new command (Phase 3 `logs`/`validate`, Phase 5 `recipes`/`check images`) would copy yet another `subprocess` pattern.

We need one boundary where **all** side-effecting operations pass, so that production code calls a seam, and tests substitute a fake that records calls and returns canned results. This seam is the contract Phase 2 tests hang off.

## Decision

Introduce a single **runtime** object — the only place in the codebase that executes external commands and performs filesystem writes. All command and converge logic is *plan-and-call*: it decides *what* and *in what order*, and the runtime performs it. The real runtime wraps subprocess execution and the local filesystem; tests inject a fake runtime. No module other than the runtime module (per ADR 0001, `sparklab/core/runtime.py`) is permitted to invoke `subprocess` or write files outside the repo's own state dir and `deploy/` dry-run scratch.

### What crosses the seam (conceptual operations)

The seam is organized in two families. **Mutations** are actions that change node state; **probes** are read-only queries. Probes must be safe to call any number of times.

*Model workloads (via `sparkrun`):*
- start/ensure model workload (recipe name, optional cluster)
- stop model workload (recipe name, optional cluster)
- query model/workload status
- set up inter-node SSH mesh (host list) — cluster only
- create saved cluster (name, host list) — cluster only
- update sparkrun and its recipe registries

*LiteLLM + monitoring stack (via `docker compose` against the rendered compose file):*
- bring stack up (detached, remove orphans)
- tear stack down (optionally including named volumes)
- query stack container status
- inspect rendered compose config (validation / image enumeration)
- pull stack images

*Images:*
- pull a container image
- probe image availability (manifest/registry lookup — backs Phase 4 `check images`)

*System services:*
- ensure a host service is enabled and running (tailscaled, cloudflared); query service state; query tailscale status

*Files:*
- write a managed file into the install dir (path, bytes)
- remove a managed file from the install dir (path)

*Self-maintenance:*
- update the CLI's own engine dependencies (pip)

Two invariants of the seam:

1. **The runtime executes, it does not decide.** Diffing rendered content against recorded state, deciding which files changed, and deciding whether a model restart is needed stays in the converge planner (`core/converge.py`). The runtime receives already-decided operations. This is what keeps *model-restart gated behind an explicit `--apply` flag* enforceable and testable: the gate is a property of plan construction, and tests can assert a fake runtime never receives a stop/start pair when the gate is closed.
2. **Every call is observable.** Each operation returns a uniform result (success/failure, exit status, captured output) and the runtime (or an observing wrapper around it) records what was called, in order. The real runtime is a thin wrapper; the fake records calls and returns canned results — that log is the assertion surface for parity tests (emitted-command diff) and for integration tests (ordering of actions).

### Dependency-injection point

The runtime is constructed in exactly one place: the CLI entry layer (`sparklab/cli.py`, per ADR 0001), where the production runtime is assembled from the loaded config (install dir, compose file path, sparkrun resolution) and **injected into each command handler** as part of its invocation context. Command handlers and core planner/executor functions never construct or look up the runtime themselves; they receive it.

The test DI point is therefore the same edge: a test builds its command context with a **fake runtime** (records calls, returns canned results, optionally asserts the call sequence) and drives the full command path — `apply` planning through execution, `teardown`, `upgrade`, `status` — with zero docker, sparkrun, or node access. The fake must satisfy the same conceptual contract as the real runtime, so it is defined against this ADR's operation list, not against implementation details.

This keeps the production path unchanged in behavior: `bin/spark-lab` (legacy shell, still operational) and the new console script both end at the same entry layer that injects the real runtime.

## Consequences

- **Positive**
  - Phase 2 integration tests and shell↔Python parity tests can run entirely in CI against the fake runtime — no staging node required for `apply`/`teardown`/`upgrade` logic; the staging node (assumption A6) is reserved for true E2E (Phase 6).
  - The safety invariants become assertions: idempotency (a second `apply` with unchanged state plans zero operations), restart gating (stop/start operations appear in the plan only when the gate is open), and removal convergence (dropped managed files produce remove calls) are all directly testable against recorded calls.
  - New commands (`logs`, `check images`, `recipes show`'s optional fetch) acquire their side effects by *adding operations to this seam* rather than inventing patterns; the seam is the extension point for Phase 4's image availability probe and Phase 5's discovery fetches.
  - Error handling becomes uniform: one place defines how a failed external command is reported (exit status + captured output) and how a partial apply surfaces.
- **Negative / costs**
  - Every existing scattered `subprocess`/file-write call must be re-expressed as a seam operation — a bounded rewrite of `converge.execute`, `converge.write_files`, `cli._run` call sites, and `render`'s output writes, gated by Phase 2/3 parity tests.
  - A small indirection tax on reading the code path (plan → runtime op → real command); mitigated by keeping operation names close to the argv they produce so parity diffs stay readable.
  - The fake runtime must be maintained in lockstep with this operation list; adding a real operation without a fake counterpart is a review gate, not a compile error.
- **Constraints honored**
  - Idempotent converge and the `--apply`-gated model restart are unchanged in meaning; the seam makes them testable without changing them.
  - No secrets cross the seam: operations carry no credentials (tokens stay in env / rendered files, per the repo's secrets invariant).

## Alternatives considered

- **Monkeypatch `subprocess.run` in tests.** No call log, no per-call canned results, no place to encode "which operations are probes vs mutations"; breaks the moment any module shells out in a new shape. Rejected.
- **Per-command executors (each command owns its subprocess handling).** Preserves today's scattering; each new command re-derives error handling; tests must patch multiple seams. Rejected.
- **Make the converge plan itself the executor (commands always go through `converge`).** Unifies apply/upgrade but not status/teardown/logs, and would push read-only probes through a planner designed for mutations. Rejected in favor of a flatter single seam serving all commands.
- **A microservice or shell wrapper as the execution layer.** An extra process boundary for a single-user CLI; complicates the parity harness (A5). Rejected.
