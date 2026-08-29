# `spark-lab` — Feature-Branch Migration & Execution Plan

> **Status:** APPROVED · 2026-08-21
> 20,000-ft orchestration plan: phase breakdown, subagent routing, migration path, risk register, merge criteria.
> No implementation code. Architectural decisions and handoff points only.

## Current State (grounding)
The Python engine **already exists** and works: `lib/` (~780 LOC: `config`, `render`, `converge`, `state`, `cli`), argparse CLI (`init`/`apply`/`status`/`teardown`/`upgrade`), idempotent converge with a gated model-restart, `tests/test_converge.py` (9 tests), and GitHub Actions CI (render + compose + shellcheck + tests + secret-scan). **Shell surface to migrate is small (~162 LOC):** `bin/spark-lab` (venv wrapper), `scripts/capture.sh`, `scripts/secret-scan.sh`, `.githooks/pre-commit`. **Config** is single-model today (`model:`), with images *already* in `config.yaml` (Obj 8 is mostly done — needs completion, not invention). Legacy shell `sparkrun`/`litellm` ops run on the node and are wrapped by Python via subprocess.

**Implication:** Objectives 1 & 8 are *complete/harden*; Objectives 2, 3, 4, 5, 6, 7 are the net-new work. The plan is sequenced so the safety net (tests/CI) lands **before** the behavior changes (multi-model, recipe system).

## Assumptions (documented, not blocking)
- **A1 — Base exists:** the prior Python engine is the foundation; this branch hardens + extends it.
- **A2 — CI is GitHub Actions** (already in place); we extend, not replace.
- **A3 — Conversion LLM is config-driven** (Obj 6): default = the local LiteLLM on the Spark; remote optional. Opt-in flag; failure → manual fallback.
- **A4 — Low user count / internal:** migration can be per-node, no multi-tenant lock-in.
- **A5 — "Shell parity"** = the in-repo shell helpers *and* the node-side `sparkrun`/`litellm` behaviors Python wraps.
- **A6 — Staging node** available for E2E; the live production Spark is never touched in the branch.

---

## 1. Phase Summary (6 phases)

**Phase 1 — Architecture & Contracts (interfaces first, no behavior change).** Lock the target design on the feature branch: CLI framework decision (argparse vs Typer/Click) with rationale, module layout, **config schema v2** (multi-model `models:`, complete image map, `recipes/` location, discovery sources, conversion flag), a **command↔runtime boundary** (a mockable seam around the `sparkrun`/`docker`/`ssh` subprocess calls), and a **discovery source plugin interface**. Produces ADRs + interface specs only.
*Rollback trigger:* none — contracts are docs; revise on-branch before code starts.

**Phase 2 — Test Harness + Regression Parity (the safety net).** Build the test foundation *before* behavior changes: unit tests (config validation, render, converge — expand the existing 9), integration tests against a **mocked runtime seam** (no real docker/sparkrun), and **regression/parity tests** that drive the legacy shell path and the new Python path with identical inputs and diff outputs (rendered files + emitted commands). Extend GH Actions to run the suite + coverage on every PR.
*Rollback trigger:* a flaky parity test is quarantined, not deleted; tests-only phase has zero production surface.

**Phase 3 — CLI Hardening + Shell→Python Parity (code + review).** Complete the migration: console entry point (packaged, `pip`/`pipx`-installable) replacing the shell wrapper; `capture` → `spark-lab logs`/`capture`; `secret-scan` → Python; UX added **additively** — `logs <service>` (tail), `validate`/`check` (pre-execution validation), breadth of `--dry-run`, progress indicators. Legacy scripts stay in the repo and operational (deprecation, not deletion).
*Rollback trigger:* each command is additive and parity-gated; a bad command is removed individually.

**Phase 4 — Multi-Model Config + Image Abstraction (config + devops).** Implement schema v2: `models:` with per-model resource allocation + runtime config + which model(s) are active under sparkrun, plus a **backwards-compat loader** that auto-upgrades the legacy single `model:` to `models:` (no user action). Finish image abstraction: every image resolved from config with a dev/test-vs-prod override mechanism and a `check images` availability probe (pull/manifest) before accepting a config.
*Rollback trigger:* compat loader keeps single-model rendering byte-identical; if multi-model breaks, loader pins to single-model mode.

**Phase 5 — Recipe System: Standalone Storage + Discovery + Auto-Conversion.** (a) Version-controlled in-repo `recipes/*.yaml`, each runnable directly via `sparkrun`, coexisting with the legacy rendered-into-`install_dir` path; (b) **discovery** via the Phase 1 plugin interface — two initial adapters (sparkrun registries, SGLang cookbook) with `recipes search/list/show`, non-disruptive; (c) **auto-conversion** (opt-in) ingesting an SGLang cookbook entry → a *candidate* sparkrun recipe via the configured LLM endpoint, always validated, never auto-applied, with LLM-unreachable/malformed → manual fallback.
*Rollback trigger:* all three are additive/flag-gated — disable conversion → manual; discovery is opt-in per command; legacy recipe path untouched.

**Phase 6 — Integration Validation & Merge Gate (E2E + security + rollback).** Full E2E on the **staging** node (multi-model + recipes + monitoring), executed rollback drills, final security/supply-chain + secrets audit, and a full regression sweep (parity + entire suite green in CI). Define and document the main-branch merge gate. Legacy shell path remains the operational system until merge + a deprecation window.
*Rollback trigger:* if staging validation fails, the branch stays **unmerged** and the legacy shell system keeps serving — zero downtime by construction.

---

## 2. Subagent Assignment Matrix
Owner = domain role; work on the feature branch. "Depends On" = phases/interfaces that must land first.

| Phase | Work Item | Owner (role) | Pri | Depends On | Merge Gate |
|---|---|---|---|---|---|
| 1 | Target architecture + ADRs (module layout, CLI framework decision w/ rationale) | INTEGRATION_ARCHITECT | P0 | — | ADRs reviewed & merged to branch |
| 1 | Config schema v2 spec (multi-model, images, recipes, discovery, conversion flag) | INTEGRATION_ARCHITECT + DEVOPS | P0 | — | Schema spec reviewed |
| 1 | Command↔runtime boundary contract (mockable subprocess seam) | INTEGRATION_ARCHITECT + TEST_ENGINEER | P0 | — | Seam documented |
| 1 | CLI best-practices review (argparse vs Typer/Click; error/log/venv patterns) | CODE_REVIEWER | P0 | P1 arch | Decision + rationale in ADR |
| 1 | Discovery source plugin interface spec | RECIPE_SPECIALIST | P1 | P1 arch | Interface documented |
| 2 | Test architecture + mocking strategy (seam mocks, fixture configs) | TEST_ENGINEER | P0 | P1 seam | Mock harness in place |
| 2 | Unit tests: config validation, render, converge | TEST_ENGINEER | P0 | P2 arch | Coverage target met on `lib/` |
| 2 | Regression/parity tests (shell vs Python, same inputs → diff) | TEST_ENGINEER | P0 | P1 seam, P3 cmds | Parity suite green |
| 2 | CI pipeline config (extend GH Actions: test step + coverage) | TEST_ENGINEER | P0 | P2 arch | CI runs tests on every PR |
| 3 | CLI structure refactor + console entry point (installable) | CODE_REVIEWER | P1 | P1, P2 | Refactor passes parity + review |
| 3 | Shell→Python parity: `capture`→logs, `secret-scan`→py, wrapper→console | CODE_REVIEWER + INTEGRATION_ARCHITECT | P1 | P2 parity | Each command parity-verified |
| 3 | UX: `logs`/tail, `validate`/`check`, `--dry-run` breadth, progress | CODE_REVIEWER | P1 | P3 refactor | Additive; no regression |
| 4 | Multi-model config loader + backwards-compat upgrade | INTEGRATION_ARCHITECT | P0 | P1 schema | Legacy config renders identically |
| 4 | Per-model resource allocation + active-model runtime config | INTEGRATION_ARCHITECT + DEVOPS | P0 | P4 loader | Multi-model converges on staging |
| 4 | Container image abstraction (all-from-config + dev/prod override) | DEVOPS_ENGINEER | P0 | P1 schema | No hardcoded image remains |
| 4 | Image availability validation (`check images`) | DEVOPS_ENGINEER | P1 | P4 images | Probe validates images |
| 5 | Standalone recipe storage (in-repo `recipes/`, sparkrun-runnable, coexist) | RECIPE_SPECIALIST | P0 | P1 arch, P4 schema | Recipes runnable standalone |
| 5 | Recipe discovery (pluggable; registries + SGLang cookbook adapters) | RECIPE_SPECIALIST | P1 | P1 plugin interface | Search across ≥2 sources |
| 5 | Recipe auto-conversion (opt-in, LLM-assisted, manual fallback) | RECIPE_SPECIALIST | P2 | P5 discovery+storage | Candidate validates; fallback works |
| 5 | Security review of LLM conversion call path | SECURITY_AUDITOR | P1 | P5 conversion | Security review passed |
| 6 | E2E validation on staging (multi-model + recipes + monitoring) | INTEGRATION_ARCHITECT | P0 | all phases | Staging E2E green |
| 6 | Rollback drills + operational runbook | DEVOPS_ENGINEER | P0 | all phases | Rollback executed successfully |
| 6 | Final security / supply-chain + secrets audit | SECURITY_AUDITOR | P0 | all phases | Audit clean |
| 6 | Main-branch merge gate + deprecation/migration notice + migration script | INTEGRATION_ARCHITECT | P0 | all gates | Merge criteria met |

---

## 3. Migration Path (old → new, no downtime)
1. **Branch isolation:** all work on the feature branch; legacy shell scripts remain in-repo and operational throughout.
2. **Config migration (non-breaking):** the schema v2 **compat loader** auto-upgrades a legacy `config.yaml` (single `model:`) to `models:` with zero diff in rendered output — existing users take no action. A `spark-lab migrate` command/script performs the on-disk rewrite and can emit a standalone recipe for the current model.
3. **Recipe coexistence:** legacy rendered-into-`install_dir` path stays; new in-repo `recipes/` runs in parallel (each recipe runnable directly via `sparkrun`).
4. **Additive CLI:** new Python commands (`logs`, `validate`, multi-model apply) are added *alongside* the legacy scripts; `spark-lab status` surfaces a migration hint. Nothing is deleted before merge.
5. **Per-node cutover (staging first):** run `spark-lab apply` on a staging node with the new path; because converge is idempotent and model-restart is gated, switching config paths never drops a live model. On success, production cuts over by running the same command.
6. **Deprecation window:** after merge, legacy scripts are marked deprecated (still working) and removed only after a grace period + confirmed migration.

---

## 4. Risk Register (top 3)

**R1 — Live production disruption during migration** (the branch's whole purpose is to avoid this).
*Mitigation:* all validation on a staging node (A6); model restart always gated behind explicit `--apply`/confirmation; idempotent converge (re-run never restarts an unchanged model); legacy shell path stays operational until merge + deprecation.
*Rollback:* never delete legacy scripts pre-merge; if the new path misbehaves, keep operating the shell scripts and leave the branch unmerged.

**R2 — LLM-assisted conversion (Obj 6) emits bad/hallucinated recipes.**
*Mitigation:* opt-in flag; output is a *candidate* that must pass schema validation + a `--dry-run` render (+ optional staging smoke test) before ever being used; never auto-applied. Failure modes (unreachable/timeout/malformed) caught → clear manual fallback (emit source + starter template).
*Rollback:* disable the conversion flag → fully manual process; the LLM path is opt-in and isolated.

**R3 — Backwards-incompatible config/schema break (Obj 4) regresses existing users.**
*Mitigation:* additive schema v2 with a version key; compat loader upgrades legacy config with explicit warnings; regression/parity tests assert legacy single-model config renders **byte-identically** to pre-branch.
*Rollback:* compat loader keeps single-model mode; if multi-model rendering breaks, the loader pins to single-model mode and the change stays off the merge gate.

*(Secondary: supply-chain/secret exposure via the LLM call path — controlled by the obj-5 security review: token via env only, minimal content sent, no secrets in prompts; caught by the final Phase 6 audit.)*

---

## 5. Success Criteria
**Per-phase (measurable):**
- **P1:** ADRs + interface specs merged to the branch; schema v2, mockable seam, and plugin interfaces reviewed; zero main-branch impact.
- **P2:** suite covers config/render/converge (target ≥90% of `lib/` line coverage); mocked-seam integration tests pass; shell↔Python parity tests for `apply` pass; CI runs the suite on every PR and is green.
- **P3:** every legacy shell capability has a Python equivalent (parity-verified); CLI is `pip`/`pipx`-installable; `logs`/`validate`/`--dry-run`/progress present; shellcheck-clean; CI green.
- **P4:** multi-model config loads and converges; legacy single-model config auto-upgrades **with zero render diff**; every image resolvable from config (none hardcoded); `check images` validates availability; rollback demonstrated.
- **P5:** in-repo `recipes/` runnable standalone via `sparkrun`; `recipes search` works across ≥2 sources; opt-in auto-conversion produces a **valid** candidate from a cookbook entry and falls back to manual on LLM failure; LLM-path security review passed.
- **P6:** full E2E on staging passes; a rollback drill executed successfully; secrets/supply-chain audit clean; full regression sweep green.

**Main-branch merge gate (all must hold):**
1. CI 100% green on the branch (tests, parity, shellcheck, secret-scan).
2. Parity tests prove legacy behavior is preserved (no functional regression).
3. Migration + rollback documentation committed and reviewed.
4. Deprecation notice + `spark-lab migrate` available for legacy users.
5. No open P0/P1 findings from SECURITY_AUDITOR; supply-chain/secret audit clean.
6. Staging E2E + rollback-drill evidence attached to the PR.

---

## Execution Log
- **2026-08-21** — Plan approved. Feature branch `feature/python-cli` opened from `main`.
- **Phase 1 (complete):** 6 ADRs authored + committed to `feature/python-cli` (`01c9129`) — 0001 module layout, 0002 runtime seam, 0003 discovery plugin interface, 0004 config schema v2, 0005 CLI framework (decision: keep argparse), 0006 CLI best-practices. Contract gate passed: interface-level only, no implementation code, legacy shell path intact. (Note: subagents ran on the local `qwen3-8-27b` model — slow; one ADR was authored by the orchestrator after its subagent stalled.)
- **Phase 2 (complete):** 40 tests across config / render / converge / apply-integration / CLI, plus golden regression pinning rendered output + the apply command sequence. The runtime seam (ADR 0002) is wired through every command; `FakeRuntime` mocks all node side effects (no docker / sparkrun / node). **92% coverage on `lib/`**, CI-gated at 85%. `requirements.txt` is now the single source of runtime deps (`bin/spark-lab` + `upgrade` install from it). Committed `6e2f81e`; CI green.
- **Phase 3 (core complete):** CLI hardening + ADR 0001 structure. `lib/` → `sparklab/` package with `core/` (pure engine) + `commands/` (one thin module per subcommand); `cli.py` is argparse + dispatch only. Templates now ship **inside the package** (`sparklab/templates/`) so the CLI works from a checkout **and** when installed. **Packaging:** `pyproject.toml` + `spark-lab` console entry point — verified `pip install -e .` **and** a built wheel both put `spark-lab` on PATH and render correctly from site-packages. **New UX:** `validate`/`check` (read-only pre-flight: schema + render check + binary report) and `logs <service>` (tail stack logs, `--lines`/`--follow`). **Broader dry-run:** `apply --dry-run --diff` shows a unified diff of what would change on disk. **45 tests, 91% coverage** (CI-gated at 85%), CI green. Also jotted a **sparkrun registry design note** (`docs/REGISTRY.md`): maintain an in-repo `.sparkrun/registry.yaml` + `recipes/` so our model recipes are standard, reusable, and third-party recipes can be referenced from `config.yaml` (Phase 4/5).
  - *Deferred (documented, not dropped):* `capture` port (the shell `scripts/capture.sh` remains operational — it's a docs helper), `secret-scan` Python port (kept the zero-dep shell scanner as the single authoritative gate to avoid a drift-prone duplicate scanner; gitleaks stays the CI backstop), progress indicators (low value for the current fast ops).
- **Phase 4 (complete):** Config schema v2 (ADR 0004). `core/config.py` is now version-aware: active-model selection (`models:` + `active:` + `active_models:` override), `image()` resolution (env `SPARKLAB_IMAGE_<KEY>` > `profile:` > `images:` map > v1 field > default), `image_model()`, and `effective_params()` (`resources.mem_fraction_static` wins over `params`). `core/schema.py` adds `is_v2` + `upgrade_to_v2` (pure, **render-invariant**). Render routes every image through the resolver and the litellm + infra images become template vars with unchanged defaults, so **v1 renders byte-identically** (golden regression holds). New commands: `check images [--probe]` (resolve + report / manifest probe), `check config` (== `validate`), `migrate` (idempotent v1→v2 on-disk). `apply` is now fail-safe: it refuses to converge when the active model has no resolvable image. Added `config.example.v2.yaml` + `ARCHITECTURE.md` (v2 schema + command table). **61 tests; golden regression still green.** Jotted design notes in `docs/ROADMAP.md` (uv migration, argparse→click, deferred items). Committed `988685d`/`eb63fe2`; CI green.
- **Phase 5 (complete):** Recipe discovery + in-repo registry + auto-conversion (ADR 0003). `sparklab/core/discovery/` ships the source-agnostic `DiscoveredRecipe` record, the `RecipeSource` contract, and the `DiscoveryRegistry` (config-driven source construction via the `discovery:` section, per-source error isolation, optional on-disk body cache) plus two built-in adapters: `sparkrun-registry` + `sglang-cookbook` (third-party kinds via the `sparklab.recipe_sources` entry point). In-repo registry: `.sparkrun/registry.yaml` + `recipes/{qwen38-27b,llama31-8b}.yaml` (real, runnable sparkrun recipes); sample cookbook `cookbook/sglang.sample.json`. New commands: `recipes search/list/show/convert` -- `convert` produces a **validated, never-applied** sparkrun candidate (deterministic normalization; opt-in LLM-assisted refinement that falls back to deterministic on any failure). Enabling/redirecting a source is a config-only change. `docs/REGISTRY.md` updated with the Phase 5 status. **81 tests; golden regression still green; CI green.**
- **Phase 6a (complete -- node-independent):** the security/supply-chain/secrets/LLM-path audit (`docs/AUDIT-P6.md`: secrets clean via the hook+CI gitleaks gate; supply chain `pip-audit`-clean on the pinned PyYAML/Jinja2/MarkupSafe; LLM path opt-in + token-via-env-only + minimal content + no secrets in prompts + safe fallback; regression 81 tests + golden green). Regression sweep green. A **staging E2E + rollback runbook** is authored + smoke-tested: `scripts/staging.sh` (guarded, refuses `LIVE_INSTALL_DIR`, captures every step) + `docs/STAGING_E2E.md` (staging config = a copy of the single `config.example.yaml`; the dedicated staging example was dropped in favor of one example). The **merge gate** is defined in `docs/MERGE_GATE.md`: rows 1–6 (parity, CI, coverage, secrets, supply chain, LLM path) are ✅; rows 7–9 (staging E2E, rollback, no-live-disturbance) are ⏳ PENDING the staging Spark. Legacy shell path stays operational until merge + a deprecation window.
- **Phase 6a.5 (complete -- three user-requested additions on top of 6a):** (1) **uv migration** -- `pyproject.toml` is the single source of truth, `uv.lock` committed (reproducible/pinned/hashed), `bin/spark-lab` is uv-first (`uv sync --no-default-groups`, `pip install -e .` fallback), `spark-lab upgrade` re-resolves the lock, CI installs uv + runs `uv sync`; `requirements*.txt` retired. (2) **system precheck** -- `spark-lab doctor` (=`check system`) detects required/optional tools (uv, python3, docker, git, curl, sparkrun + optional tailscale/cloudflared/gitleaks), explains each, and `--install` kicks off the sudo-aware one-liner installers; the tool table is data, not code; `init` runs the check first and offers install interactively. (3) **de-SGLang** -- sparkrun stays the critical dependency; the inference engine is now a property of the recipe (`model.runtime` default `sglang` + optional `model.serve_command` override the generated serve block), and docs (SPEC/ARCHITECTURE/SETUP/OPERATIONS + config examples) are reframed so SGLang is the default/example, not a requirement. 90 tests; R3 + golden green; CI green throughout.
- **Post-staging plans (complete, ready to execute after the new Spark passes E2E):** (a) **`spark-lab adopt`** -- takes over an already-running install read-only (writes only state.json, runs no `sparkrun`), records on-disk reality + the running model, and flags drift; a routine `apply` after adopt never restarts the model (fail-safe), so migrating the live Spark is zero-downtime by default (6 tests). (b) **`docs/EXISTING_SPARK_MIGRATION.md`** -- capture -> reproduce config -> `apply --dry-run` (aim byte-identical) -> `adopt` -> keep-the-live-recipe vs one-time converge; rollback = the capture, legacy path stays operational. (c) **`docs/CLUSTERING.md`** -- two-Spark cluster grounded in the multi-node plumbing already in `converge` (`sparkrun setup ssh`/`cluster create`/`run --cluster`); `install.hosts` >1 + `model.min_nodes: 2` + `cluster_name` drive it; the 5 hardware-open-questions (exact sparkrun multi-node CLI, controller selection, image distribution, node placement, inter-node transport) are flagged for on-hardware confirmation. 96 tests; 92% coverage; CI green.
- **Phase 6b (staging E2E + rollback, live on `luna`):** full fresh setup on a dedicated DGX Spark (Qwen3-0.6B + SGLang, `~/AI`): `doctor` (new **docker-access** capability) -> `validate` -> `apply` (model serving) -> **LiteLLM gateway completion** -> Prometheus (82 `sglang_*` metrics) -> Grafana -> converged; then `teardown --yes` (stop + stack down + **state cleared** + re-apply-ready). **3 real bugs found + fixed on the node:** single-node `run`/`stop` need `--hosts`; `sparkrun run`/`stop` must use the **recipe file path** (not a registry name). **Operational hardening:** precheck **capability** layer (docker access -> exact `usermod` fix), **best-effort** root-gated infra-ensure steps (tailscale/cloudflare `systemctl` warn + continue), `hf` CLI as an optional precheck tool, + a cold-start-`apply` doc note. **Merge-gate rows 7-9 now PASS** (documented in `docs/MERGE_GATE.md`). 103 tests; 92% coverage; CI green.
- **Phase 6b (TABLED -- pending the staging Spark):** execute the E2E + rollback drills on the staging node (`scripts/staging.sh all`), record results in `docs/STAGING_E2E.md`, mark merge-gate rows 7–9 ✅, then merge `feature/python-cli` → `main`. Everything else is done; this is the remaining node-dependent work.
