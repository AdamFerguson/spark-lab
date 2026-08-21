# spark-lab — Agent Rules

These rules apply to every agent (and subagent) working in this repository.

## Non-negotiable: no secrets are ever committed

Before **any** `git commit` in this repo — including the very first commit,
amended commits, and rebase/merge results:

1. Run the full secret scan: `scripts/secret-scan.sh`
2. If it reports **any** finding, **stop**. Move the value out of tracked files
   into the gitignored `.env` / `config.yaml` (or an env var), re-stage, and
   re-run until it is clean.
3. **Do not commit** while the scan is dirty.

A `pre-commit` hook (`.githooks/pre-commit`) enforces the same scan
automatically on every commit and blocks it on findings. Install it once per
clone with:

```
git config core.hooksPath .githooks
```

**Never** use `git commit --no-verify` in this repo. If you believe the repo is
clean but the scan is noisy, you may delegate a review, but the scan must be
explicitly green before the commit lands.

You may delegate the scan itself to a subagent ("run `scripts/secret-scan.sh`
and report the result"), but the command and its exit code are the source of
truth — not a judgment about what *looks* like a secret.

## What counts as a secret / must never be tracked

- Real tokens/keys/passwords: HF tokens (`hf_…`, `hf_oauth_…`), LiteLLM
  master/salt keys (`sk-…`), model API keys, Grafana/DB passwords, GitHub
  tokens, AWS/Google/Slack credentials, private keys.
- A value that is **only referenced by env-var name** is fine (e.g.
  `LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}` in a rendered file, or
  `hf_token_env: HF_TOKEN` in config). A **real value** in a tracked file is not.
- Identifying/sensitive specifics: real hostnames, personal usernames, private
  IPs, email addresses. Use placeholders (`my-spark`, `<your-hf-token>`).

## Where secrets live

- Real values: **only** in the gitignored `.env` and `config.yaml`.
- `config.example.yaml` / `.env.example`: **names and patterns only**, never
  real values.
- Anything committed to `git` must stay committable by anyone, ever.

## Quick check

```
scripts/secret-scan.sh        # 0 clean / 1 findings — gate on this
git check-ignore -v .env config.yaml deploy x   # confirm sinks are ignored
```
