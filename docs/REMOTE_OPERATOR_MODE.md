# Remote operator mode (Fabric) — design + implementation plan

**Status:** APPROVED (2026-08-26) — implementation complete (steps 1–10, 132 tests
green incl. 25 new remote tests, goldens byte-identical); step 11 (wiring on
pop-os) pending.

**Deviations from the original plan (all deliberate):**
* `RemoteState` lives in `core/remote.py` (not `state.py`) — `state.py` stays
  local-only; `node.py` selects the backend.
* `RemoteInstallFS.hash_files` reads via SFTP (`conn.open`) and hashes locally,
  instead of one `sha256sum` shell round-trip — simpler, binary-safe, trivially
  testable; 11 small files over one connection is negligible.
* Every remote command carries a PATH prefix
  (`export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"`) inside the login
  shell — belt-and-suspenders against the uv `~/.local/bin` gotcha.
* `build_plan` resolves install-relative paths through a `_install_rel_path`
  helper (Config `node_path`, with a fallback for test fakes that only provide
  `install_dir`) rather than calling `cfg.node_path` directly.
* `RemoteRuntime.home_path()` / `expand()` do plain `~` / `~/...` substitution
  (no `os.path.expanduser` — it has no home parameter).
**Goal:** operate a remote Spark install from any host (e.g. `pop-os`) without ssh'ing
in: `spark-lab --config labs/luna/config.yaml apply` converges the *remote* node over
SSH using **Fabric**. The control plane (LiteLLM + Postgres + monitoring) stays on the
Spark (ADR-0007); this is the "remote-deploy primitive" that ADR anticipated, in
generalized form.

## Key properties

- **Additive**: no `install.remote` → behavior is byte-identical to today
  (all tests + goldens stay as-is).
- **State stays on the managed node**: at `<repo_root>/.sparklab-state` inside the
  node's `~/spark-lab` checkout (default; `remote.repo_dir` overrides). The controller
  reads/writes it over SSH. Luna/sol already have correct state there → no migration,
  no phantom re-converge.
- **Secrets**: the controller's `.env` (next to the config file — `config.load` reads
  `<config_path.parent>/.env`) is the source of truth; rendered files (incl.
  `litellm/.env`) are pushed to the node.
- **One config targets one node**; two nodes = two configs. Suggested laptop layout:
  `labs/<node>/config.yaml` + `labs/<node>/.env` (per-node secrets: luna and sol have
  different `LITELLM_MASTER_KEY` etc.).
- ADR-0007's single-control-plane/worker architecture is a *later* step, unchanged.

## Config surface (new block, v1 + v2)

```yaml
install:
  name: heliosphere
  install_dir: ~/AI                 # path ON THE TARGET node (expanduser'd there)
  hosts: [sol.local, luna.local]    # unchanged (sparkrun cluster-mesh semantics)
  remote:                           # NEW — omit/empty = local (today's behavior)
    host: luna.tail9d5411.ts.net    # SSH target (magic-DNS name or tailnet IP)
    user: adam                      # optional; default = local user
    port: 22                        # optional
    identity_file: ~/.ssh/id_ed25519  # optional
    repo_dir: ~/spark-lab           # optional; node's spark-lab checkout (state + upgrade)
```

## Implementation steps (checkboxes = progress)

- [x] **1. `pyproject.toml`** — add `fabric>=3.2,<4` to dependencies (pulls
      invoke/paramiko/cryptography; requires-python is >=3.10, fine).
- [x] **2. `sparklab/core/remote.py` (new)** — the Fabric layer:
  - `RemoteTarget` — built from `cfg` (host/user/port/identity/install_dir/repo_dir).
  - `RemoteRuntime` — same interface as `Runtime` (`available`, `run`, `spawn`), over
    one `fabric.Connection` (built lazily, reused for the command's lifetime):
    - All commands execute as `bash -lc <shlex-quoted argv>` — a **login shell** so
      `~/.local/bin` (sparkrun, uv) is on PATH (non-interactive ssh otherwise misses it
      — the "sparkrun MISSING" gotcha hit on sol).
    - `run(argv)` → `conn.run(...)`; returns a `CompletedProcess`-shaped result;
      output streamed to the local terminal.
    - `spawn(argv)` → `bash -lc 'setsid nohup <cmd> </dev/null >/dev/null 2>&1 &'`
      — true detach over SSH (model launch; the bounded readiness probe confirms).
    - `available(binary)` → `bash -lc 'command -v <bin>'` return code.
  - `RemoteInstall` — file seam: `write(rel, data)` (`mkdir -p` + `conn.put` from a
    temp file), `read(rel) -> bytes|None` (`conn.open`), `exists(rel)`; adopt uses a
    SINGLE remote round-trip (`for f in ...; do [ -f ] && sha256sum || echo MISSING;
    done`) to collect existence+hashes for the rendered targets.
  - `RemoteState` — same `State` semantics over the node's `state.json`
    (`cat`/`put`).
- [x] **3. `sparklab/core/runtime.py`** — add `runtime_for(cfg)` → local `Runtime`
      when no `install.remote.host`, else `RemoteRuntime`.
- [x] **4. `sparklab/core/converge.py`** —
  - `write_files(cfg, rendered, install_fs)` — take the file seam instead of
    `Path(cfg.install_dir)` (local default keeps byte-identical behavior).
  - `find_sparkrun(runtime=None)` — when remote, resolve via
    `bash -lc 'command -v sparkrun'` (fallback `~/.local/bin/sparkrun`, then bare
    name, as today).
- [x] **5. `sparklab/core/state.py` / `sparklab/core/config.py`** — `State` gains an
      optional remote backing (thin wrapper; local path unchanged). `config` exposes
      `remote` (dict, default `{}`) + `remote_repo_dir` (default `~/spark-lab`,
      expanded REMOTELY — do not expanduser locally).
- [x] **6. `sparklab/core/node.py` (new)** — `node_env(cfg, runtime)` returning
      `(install_fs, state_obj)`: local → `Path`-based install + `State(cfg.state_dir)`;
      remote → `RemoteInstall` + `RemoteState` (state file at
      `<remote_repo_dir>/.sparklab-state/state.json`).
- [x] **7. Commands** —
  - `apply` — write_files + `_print_diffs` (remote reads) + state via seam; banner
    gains `(remote: user@host)`.
  - `adopt` — remote hash pass + remote state (update docstring: state file now lives
    on the managed node).
  - `teardown`, `status`, `logs`, `check images`, `check system` — already routed
    through `runtime` → remote automatically (verify `find_sparkrun` call sites).
  - `upgrade` — deps-refresh targets `remote.repo_dir`'s venv on the node
    (`uv lock/sync --directory <remote_repo_dir>` / `pip install -U -e <remote_repo_dir>`);
    `sparkrun update` + `docker compose pull` + re-apply remote. Note today's
    `_refresh_engine_deps` uses `Path(__file__).parents[2]` (the LOCAL checkout) — in
    remote mode use `cfg.remote_repo_dir` instead.
  - `validate` — binary pre-flight must use `runtime.available` (currently
    `shutil.which` — local-only) so it checks the TARGET node.
  - `init`, `recipes`, `migrate` — stay local-only (operator-side).
- [x] **8. `sparklab/cli.py`** — build the runtime from the loaded config
      (`runtime_for(cfg)`); fall back to `default_runtime()` when no config/remote
      target (init/recipes/convert/migrate need no node).
- [x] **9. Tests (hermetic, no SSH)** — `tests/helpers.py`: `StubConnection`
      (records runs/puts, canned `open()` bytes, canned `command -v` answers) +
      monkeypatch `remote.build_connection`. New `tests/test_remote.py`:
  - argv→shell-string quoting (incl. the nested-quoted readiness probe).
  - spawn wrapping (`setsid nohup ... &`), `bash -lc` login-shell form, `available()`.
  - **plan parity**: identical config local vs remote produces the identical converge
    plan/command sequence (same as goldens).
  - apply end-to-end with stubs: files "put" to remote paths, state written to the
    remote `state.json`, correct command sequence.
  - adopt remote hash pass; `find_sparkrun` remote; state load/save round-trip.
  - Existing 107 tests + goldens unchanged (local path untouched).
- [x] **10. Docs** — README: "Remote operator mode" section (config snippet, per-node
      `labs/<node>/` layout, "state lives on the node", ssh-key prerequisite).
      OPERATIONS.md: remote notes (login-shell PATH, long probes over SSH).
      `config.example.yaml` / `config.example.v2.yaml`: documented `remote:` block.
      ADR-0007: status note that the remote primitive shipped in this generalized form
      (control-plane/worker split still pending).
- [ ] **11. Wire it up on pop-os (the payoff)** —
  - `labs/luna/{config.yaml,.env}`: luna's current config values (incl. the tuned
    params/flags added 2026-08-26: mamba tuning, DSPARK, cache-report flags) +
    `install.remote.host: luna.tail9d5411.ts.net`; `.env` copied from luna's
    `~/spark-lab/.env` (node-side copy, never printed). Luna specifics: model_name
    `Qwen3.8-27B-NVFP4`, instance_label `luna`, images pin
    `docker.litellm.ai/berriai/litellm:main-stable`, model_info 32768/8192.
  - `labs/sol/{config.yaml,.env}`: same for sol (model_name
    `adam-spark-qwen3-8-27b`, instance_label `adam-spark`, litellm pin
    `ghcr.io/berriai/litellm:v1.99.0-dev.2`, model_info 262144/131072).
  - Run `spark-lab --config labs/luna/config.yaml adopt` + `apply --dry-run` from
    pop-os → confirm the pending luna recipe change shows with LiteLLM untouched.
  - User applies it from the laptop:
    `spark-lab --config labs/luna/config.yaml apply --apply`
  - Sync luna's node-local `config.yaml` with the laptop's (minus `remote:`) so both
    paths agree.

## Code anchors (read-only notes for resume)

- `sparklab/core/runtime.py` — `Runtime.available/run/spawn` + `default_runtime()`
  (ADR-0002 seam; the ONLY module that shells out locally).
- `sparklab/util.py` — `run_command(argv, ok=, runtime=)`; skips when
  `runtime.available(argv[0])` is false; returns `runtime.run(argv).returncode`.
- `sparklab/core/converge.py` — `find_sparkrun()` (line ~27, local which +
  `~/.local/bin` fallback); `build_plan`; `write_files(cfg, rendered, dry_run)`
  (~line 218, `Path(cfg.install_dir)` writes); `execute(plan, dry_run, verbose,
  runtime)` (~line 257: prints `==> desc`, runs `runtime.spawn` for `plan.background`
  else `runtime.run`; `best_effort` warns + continues); `_model_readiness_probe`
  (~line 68: `["sh","-c", "for i in $(seq 1 120); do curl -fsS -m 5
  http://<host>:30000/health ..."]`).
- `sparklab/core/state.py` — `State(state_dir)` with `load/save/files/model/
  set_state/clear`; file = `<state_dir>/state.json`.
- `sparklab/core/config.py` — `Config(repo_root=config_path.parent)`; `state_dir`
  property = `repo_root/.sparklab-state` (line ~261); `secret(name)` =
  `env.get(name) or os.environ`; `config.load` reads `.env` from
  `config_path.parent` (line ~305); `remote` does NOT exist yet (add it).
- `sparklab/commands/apply.py` — render → `build_plan` → `converge.write_files` →
  `converge.execute` → `st.set_state`; `_print_diffs` reads on-disk install files
  (needs remote read in remote mode).
- `sparklab/commands/adopt.py` — scans recipes dir (`install_dir/sparkrun/recipes`),
  hashes on-disk vs rendered, records on-disk reality; writes only state.
- `sparklab/commands/teardown.py` — `find_sparkrun()`, `sparkrun stop <recipe>
  [--hosts]`, `docker compose down [-v]`, `state.clear()`.
- `sparklab/commands/upgrade.py` — `_refresh_engine_deps(repo_root)` with
  `repo_root = Path(__file__).parents[2]` (LOCAL — switch to remote repo dir in
  remote mode); `sparkrun update`; `docker compose pull`; re-apply.
- `sparklab/commands/validate.py` — uses `shutil.which` directly (line ~44) —
  must go through `runtime.available` for remote pre-flight.
- `sparklab/cli.py` — `args.runtime = runtime_mod.default_runtime()` right before
  dispatch; subcommands: init/apply/status/teardown/upgrade/validate/check
  (config|images|system)/doctor/migrate/adopt/recipes/search|list|show|convert/logs.
- `tests/helpers.py` — `FakeRuntime(available=, fail=)` records `calls` + `spawned`;
  `REFERENCE_CONFIG`/`REFERENCE_ENV` (v1) + `SECRET_DUMMY`; golden parity tests in
  `tests/test_parity.py` (regenerate with `python3 tests/gen_golden.py`).
- `pyproject.toml` — `requires-python >=3.10`; deps currently PyYAML + Jinja2 only.

## Risks / notes

- New dependency tree (paramiko/cryptography) on every install — local mode imports
  fabric lazily only when `remote` is set.
- Long remote commands (readiness probe up to ~10 min) run in one SSH session;
  Fabric's keepalive handles idle.
- Dual-source-of-truth: node-local checkouts on luna/sol still exist; after wiring,
  config edits for a node happen on the laptop; node-local copies get synced so a
  stray node-side `apply` converges to the same values (warn in docs).
- A node without a `~/spark-lab` checkout would start from fresh state (one-time full
  re-converge) — both current nodes have one.
- `install_dir`/`repo_dir` with `~` must be expanded ON THE NODE (remote
  `bash -lc 'echo $HOME'` or just pass `~/...` inside the quoted remote command —
  the login shell expands it), never with local `expanduser`.
