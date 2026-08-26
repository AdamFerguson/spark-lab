# Phase 6 — Security, supply-chain, secrets & LLM-path audit

**Scope:** node-independent security review of the `feature/python-cli` tree
before merge. The live/staging E2E + rollback drills are **tabled** pending the
staging Spark and are covered in `docs/STAGING_E2E.md`; the merge decision is
gated on both this audit **and** the staging run passing (`docs/MERGE_GATE.md`).

**Date:** 2026-08-22 · **Verdict:** PASS (node-independent) · **PENDING:** staging E2E + rollback.

---

## 1. Secrets — PASS

- **Authoritative gate:** `scripts/secret-scan.sh` (grep fallback; gitleaks is
  auto-used when present). It scans the working tree, **excluding** the
  gitignored sinks (`.env`, `config.yaml`, `deploy/`, `.sparklab-state/`,
  `.venv/`) so it catches *real* secrets without false-positiving on the files
  that legitimately hold them.
- **Enforcement, not memory:** a pre-commit hook runs the scanner on every
  commit; CI re-runs the *same* script (single source of truth) with
  **gitleaks pinned to v8.30.1**.
- **Invariant:** the repo holds zero personal names, real hostnames, real IPs,
  usernames, or secrets/keys/tokens/passwords. Secrets are referenced only by
  **env-var name** in config; values live in gitignored `.env`.
- **Result:** clean on `feature/python-cli` (verified 2026-08-22).

## 2. Supply chain — PASS

The runtime dependency set is minimal and fully audited with `pip-audit`
against the exact pinned versions:

| Package    | Version | Role        | Transitive deps | Known CVEs (pip-audit) |
|------------|---------|-------------|-----------------|------------------------|
| PyYAML     | 6.0.3   | runtime     | —               | none                   |
| Jinja2     | 3.1.6   | runtime     | MarkupSafe      | none                   |
| MarkupSafe | 3.0.3   | transitive  | —               | none                   |

`pip-audit` → **"No known vulnerabilities found."**

- **Dev/test tooling:** `coverage` (runtime-free); CI installs `yamllint` +
  `gitleaks` (pinned 8.30.1).
- **No network calls in the engine's runtime path** (render + converge are
  pure). The only network path in the whole CLI is the **opt-in** `recipes
  convert` LLM refinement (audited in §3); it is off by default.
- **Follow-up (roadmap):** the `uv` migration would add a committed `uv.lock`,
  tightening supply-chain pinning to exact, reproducible resolutions.

## 3. LLM call path — PASS (opt-in, guarded)

Audited `sparklab/core/discovery/convert.py` + `sparklab/commands/recipes.py`:

- **Opt-in:** the LLM path runs only when `discovery.convert.llm: true` **and**
  an `endpoint` is set. Default config has `llm: false` → no LLM call at all.
- **Token via env only:** the API key is `config.secret(key_env)` (resolved from
  `.env`), never a literal in `config.yaml`. If unset, **no** auth header is sent.
- **Minimal content:** the prompt contains only the cookbook entry (model id,
  image, serve flags, description). No `.env` values, no machine hostnames, no
  user data, no keys.
- **No secrets in prompts:** the prompt body is built solely from the entry dict.
- **Graceful fallback:** any LLM failure (unreachable, malformed YAML) falls back
  to the deterministic transform; the candidate is **still validated** before it
  is written.
- **Never auto-applied:** conversion output is a candidate file for review.

## 4. Regression — PASS

- **81 tests** green locally; the golden regression suite (v1 renders
  byte-identically) is green; CI is green.

## 5. PENDING — needs the staging Spark

- Full E2E + rollback drills on the staging node → `docs/STAGING_E2E.md` +
  `scripts/staging.sh`.
- The merge decision → `docs/MERGE_GATE.md`, gated on the above passing.

## How to re-run this audit

```bash
# secrets (the authoritative gate)
bash scripts/secret-scan.sh

# supply chain (exact pinned versions)
python3 -m pip install pip-audit
printf 'PyYAML==6.0.3\nJinja2==3.1.6\nMarkupSafe==3.0.3\n' > /tmp/reqs.txt
python3 -m pip_audit -r /tmp/reqs.txt --no-deps

# regression
python3 -m unittest discover -s tests -t .
```
