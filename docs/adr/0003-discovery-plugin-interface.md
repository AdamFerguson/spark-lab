# ADR 0003 — Recipe-discovery source plugin interface

Status: proposed

## Context

Today a "recipe" is a single sparkrun recipe YAML that spark-lab *renders locally* from the `model:` section of `config.yaml` (`templates/sparkrun_recipe.yaml.j2`). Phase 5 of the migration plan (`docs/MIGRATION_PLAN.md`) adds three things on top: standalone in-repo `recipes/`, **discovery** of runnable recipes from external sources, and opt-in LLM-assisted auto-conversion of a discovered recipe into a sparkrun candidate.

Discovery requires pulling model-recipe knowledge from at least two very different places:

1. **sparkrun registries** — remote registries of sparkrun recipes, addressable by name, browsable, fetched as ready-to-run recipe documents (the format this lab already executes via `sparkrun`).
2. **The SGLang cookbook** — a curated collection of SGLang model entries (model id, image, serve flags/tuning knobs, target hardware notes), which is *not* a sparkrun document and must be normalized/converted before it is runnable.

The discovery commands are `recipes search <query>`, `recipes list`, and `recipes show <reference>` (read-only, non-disruptive, opt-in per command). The interface must let these commands fan out over *N* sources without the commands knowing which sources exist, so that **adding a source is a config change, not a code change** for anything bundled or entry-point-installed.

## Decision

Define a **`RecipeSource`** abstraction — a pluggable discovery source — plus a **uniform discovered-recipe record** that every source must emit. The framework (registry + contract + record type) lives in `sparklab/core/discovery/` per ADR 0001; the two planned adapters (`sparkrun`, `cookbook`) are the first built-in implementations.

### The uniform record: a discovered recipe

Regardless of source, a discovered recipe is expressed as one record with:

- **Identity:** the source that produced it, and a source-native reference (registry name/id, cookbook entry slug) plus a stable composite reference of the form `source://reference` used by `recipes show`.
- **Description:** display name, short description, and origin (URL or registry/collection identity a user can cite).
- **Model & runtime facts:** model id (e.g. a Hugging Face id), serving framework (today always SGLang, but the field exists), container image reference, target hardware class (e.g. DGX Spark / GB10) where the source provides one.
- **Suitability metadata:** tags/keywords used by `search` (model family, quantization, speculative-decoding features, multi-node).
- **Body (lazy):** the full recipe document in the source's native form. `list`/`search` return records *without* the body; `show` fetches it on demand. For the cookbook adapter the native body is the cookbook entry; conversion of that body into a sparkrun *candidate* is a later, separate step (Phase 5 auto-conversion) that consumes this same record — the record type is the hand-off contract.
- **Freshness:** a timestamp/collection identity so the framework can cache and revalidate.

The record is intentionally source-agnostic: nothing in it is sparkrun-only or cookbook-only, so a third source (a local `recipes/` directory, a git repo, an internal wiki) fits without touching consumers.

### The `RecipeSource` operations

A source exposes three conceptual operations:

1. **`list`** — enumerate recipes in the source, with optional filtering (hardware class, model family, tags). Returns uniform records, bodies not included.
2. **`search`** — free-text / structured query (model name, tag, hardware) returning ranked-or-unordered matches as uniform records.
3. **`show` (fetch)** — given the source-native reference, return the record *with* its full body.

Contract properties:

- **Read-only.** Discovery sources never mutate node state. A source that needs to shell out (e.g. invoking `sparkrun` registry commands, or pulling a cookbook) does so through the runtime seam (ADR 0002) — probes, not mutations — so discovery is testable with the same fake runtime as the rest of the CLI.
- **Self-contained errors.** A source signals failure (unreachable, malformed entry, auth missing) per-source; the framework reports it and continues with the remaining sources. No source may take down `recipes search` as a whole.
- **Cacheable.** A source declares the freshness of its results; the framework stores results locally (cache dir under the state area) and revalidates by its declared freshness, so repeated searches stay fast and degrade gracefully offline.

### Declaring and registering sources: config-driven, no core code change

Sources are declared in `config.yaml` under a `discovery:` section:

- **which** sources are enabled, each by its built-in kind (`sparkrun-registry`, `sglang-cookbook`, …) and a user-chosen alias;
- **per-source options** — e.g. the registry endpoint/collection for sparkrun registries, the cookbook repository/branch or mirror URL, timeouts, cache TTL;
- **credentials only by env-var name** (e.g. a registry token env var), never as literal values, consistent with the repo-wide no-secrets-in-config invariant (`.env` holds values; config names the variable, exactly as `hf_token_env` and `master_key_env` do today).

Registration mechanics:

- **Built-in adapters** ship inside the package (`sparklab/core/discovery/`): the loader maps a declared kind to the bundled adapter and constructs it from the section's options. *Adding an enabled source, pointing it at a different registry/cookbook, or changing its options is a config change only.*
- **Third-party adapters** register themselves through a package entry point named `sparklab.recipe_sources`, which the loader resolves at startup alongside built-ins; an unknown-but-installable kind is therefore also a code-free change (install a package, then add a config entry). Entry-point failures are reported per-source and never break the rest of the CLI.
- The **framework registry** is the only object that knows *how* to build sources; commands know *only* the `RecipeSource` contract and the record type.

### How the commands consume sources

- **`recipes search <query>`** — the command loads the discovery registry from config, fans the query out to every *enabled* source (bounded, per-source errors isolated), merges the returned records, deduplicates by model id + image where possible, and presents them with their `source://reference` so a user can immediately `show` or (later) convert one.
- **`recipes list [source]`** — enumerates one named source (by alias) or all enabled sources; bodies not fetched.
- **`recipes show <reference>`** — resolves the `source://reference` composite to the source alias + native reference, calls that source's fetch operation, and prints the full record (metadata + native body). It is read-only; it never writes into `install_dir` and never touches the model. A follow-up conversion command (Phase 5) is the only path that turns a shown record into a sparkrun candidate, and it remains opt-in and validated per the migration plan.

## Consequences

- **Positive**
  - Adding a discovery source (bundled kind, new endpoint, new options) is a **config change, no code change**; adding an entirely new *kind* is an entry-point install + config entry — core command and framework code are untouched.
  - `recipes search/list/show` are written once against the uniform record and gain every future source for free; the same record type is the input contract for Phase 5 auto-conversion, so conversion and discovery evolve independently but interoperate.
  - Discovery is read-only and non-disruptive by construction: it is probes-only through the runtime seam (ADR 0002), per-source error isolation means a dead registry never blocks a healthy cookbook, and caching keeps it fast/offline-tolerant.
- **Negative / costs**
  - Two adapters to build and keep current with their upstreams (registry CLI shape; cookbook layout), plus the framework's caching/freshness bookkeeping — all Phase 5 scope.
  - The record type is a public contract: adding a field later must stay additive (unknown fields tolerated on read) so third-party entry-point adapters don't break.
  - Search quality is bounded by the weakest adapter's normalization; cross-source ranking/dedup is a first pass, not a search engine.
- **Constraints honored**
  - Non-disruptive: discovery commands never mutate node state; the model and stack are untouched.
  - No secrets in config: registry/cookbook credentials are referenced by env-var name only; values live in `.env`.
  - Legacy recipe path (rendered into `install_dir`) is untouched; discovery runs in parallel per the migration plan's coexistence rule.

## Alternatives considered

- **Hardcode the two sources in the commands.** Zero framework cost today, but every new source means command edits; violates the "adding a source needs no code change" requirement. Rejected.
- **One generic "HTTP GET a listing page" adapter instead of per-source adapters.** The two planned sources are not the same shape (registry CLI/JSON vs. a versioned docs repo of entries); a single adapter would encode the most specific source's assumptions. Per-source adapters over a shared record type is the right split. Rejected.
- **Discovery as shell scripts wrapped via the runtime.** Consistent with A5's legacy-path spirit, but gives no uniform record, no config-driven registration, and no stable hand-off to auto-conversion. Rejected.
- **Push discovery behind the LiteLLM gateway (a remote service).** Introduces network dependency and a second system to operate for an internal, low-user-count tool (assumption A4). Rejected.
