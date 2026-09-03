# ADR-0010: Zoo model swapping via llama-swap

**Status:** Accepted (2026-09-03). **Feature branch:** `feature/model-swapping`.

## Context

Trying a new model on the lab means stopping the resident engine and paying a
full cold start (minutes for 27B-class; the flash-next readiness bound is
1500s+). The two GB10 nodes hold 128GB of *unified* memory each, so big
models cannot co-reside -- swapping is the only option; the goal is to make
it automatic, declarative, and gateway-invisible.

## Decision

Run **llama-swap** (mostlygeek, Go, single binary) as a per-host **user
systemd daemon** on any host with `swap`-enabled models (the "zoo"). Clients
keep talking to the one LiteLLM gateway; zoo models get generated
`extra_models` entries whose `api_base` is llama-swap. llama-swap reads the
requested `model` field, and starts the right engine by invoking **sparkrun
on the node**:

- `cmd`:     `bash -lc 'exec sparkrun run <install>/sparkrun/recipes/<alias>.yaml --ensure --hosts <addr>'`
- `cmdStop`: the same tolerant `sparkrun stop` converge uses ("No running
  workload matches intent" == already stopped)

spark-lab renders `llama-swap/config.yaml`, the systemd user unit, and the
zoo models' node-side recipes from the same declarative `models:` blocks
(no second source of truth). TTL classes: small=30m, mid=2h, large/pinned=
never; per-model overrides. `spark-lab zoo prepare` installs/starts the
daemon; `spark-lab swap status|unload` observe/steer it. Zoo models are
`active: false` by validation: **`apply` never launches or stops them**.

Phase 1 (this ADR): single-node zoo models, llama-swap on the control-plane
host. Phase 2 (explicitly deferred): the `vllm-snapshot` accelerator
(`swap.fast_resume`) for seconds-scale suspend/restore of qualifying vLLM
models, and spanning models in the zoo pool.

## Why this shape (alternatives rejected)

- **Per-model engine endpoints registered in LiteLLM (spark-lab "swap" CLI)**:
  requires explicit switching + gateway churn per swap; rejected for
  request-driven automation ("don't want to think about it").
- **vllm-snapshot as the spine**: measured ~9s swaps on GB10 (26B NVFP4 MoE)
  but vLLM-only, and requires Triton attention (FlashInfer state is
  silently-wrong after restore). Too narrow to be mandatory; demoted to an
  optional per-model accelerator phase.
- **Containerized llama-swap (compose service + docker.sock)**: `cmd`s would
  need sparkrun + ssh + docker *inside* the container; a host user-daemon
  reuses the node's real toolchain. (Docker-socket delegation avoided.)
- **SGLang memory-saver / vLLM sleep level-1**: "CPU backup" offload is the
  *same physical pool* on unified memory -- frees nothing (independently
  measured by the vllm-snapshot author on GB10).
- **Co-resident small quants (llama.cpp)**: proven on GB10 and complementary
  (small models genuinely coexist); not a general answer for NVFP4 MoE
  engines, so out of scope for the zoo spine.
- **Multi-model LiteLLM instances**: LiteLLM proxies, it does not own engine
  lifecycles -- the "separate litellm per model" reading was rejected
  outright (one gateway stays).

## Consequences

- Zoo lifecycle joins converge: `llama-swap/config.yaml` changes restart the
  daemon (best-effort; missing unit == hint to run `zoo prepare`), and every
  apply asserts the service is running on swap hosts.
- Validated invariants: zoo models inactive, single-node, exactly one host,
  that host runs the control plane, engine ports unique against co-resident
  models. `model up/down` reject zoo models.
- The first request to a cold model blocks through engine load (bounded by
  `healthCheckTimeout` + generated deployment `timeout`); llama-swap's
  `sendLoadingState` surfaces progress to clients.
- Supply chain stays deliberate: spark-lab never downloads the llama-swap
  binary; OPERATIONS documents the pinned one-time install (v252,
  linux_arm64 for GB10) and `zoo prepare` verifies it.
- zoo hosts expose llama-swap on the LAN/tailnet port (no auth): same trust
  class as engine ports today; the gateway remains the only authenticated
  surface. Do not expose the swap port beyond the private network.
