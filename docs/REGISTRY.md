# Design note — In-repo `sparkrun` recipe registry

**Status:** design note (decision captured; implementation lands in Phase 4/5).
**Supersedes part of:** ADR 0003 (discovery) and ADR 0004 (config schema) where they touch recipe sourcing.

## The idea

Maintain a **`sparkrun` recipe registry inside this repo**, so the model recipes we
ship are in *standard sparkrun registry form* rather than only as a Jinja template
we render on the fly. Two goals:

1. **Reusability.** Anyone can add this repo as a sparkrun registry and run our
   recipes directly — they stay "maximally reusable" because they are the format
   sparkrun itself consumes.
2. **Third-party recipes.** `config.yaml` can also point at recipes that live in
   *other* registries (remote or local), so the server can run a model we never
   authored.

`config.yaml` keeps its two jobs unchanged: (a) configure the general server
setup (monitoring, network, install paths, LiteLLM) and (b) **indicate which
recipe to run**. It no longer needs to *describe* the recipe — it just *selects* it.

## Registry layout (per the sparkrun spec)

```
.sparkrun/registry.yaml      # manifest (required for auto-discovery)
recipes/                     # default subdirectory of recipe YAML files
  qwen3-32b-sglang.yaml      # one file per model recipe
  phi-4-14b-vllm.yaml
  ...
# optional, later:
tuning/  benchmarking/  mods/
```

`.sparkrun/registry.yaml` (concretely for this repo):

```yaml
registries:
  - name: spark-lab          # used in @spark-lab/<recipe> syntax
    description: "Curated model recipes for a DGX Spark lab"
    recipes: recipes         # default; shown explicitly for clarity
    enabled: true
```

Manifest fields (reference): `name` (required), `description`, `recipes`,
`tuning`, `benchmarks`, `mods`, `enabled`, `visible`. Reserved prefixes
(`sparkrun`, `official`, `arena`) belong to approved orgs — `spark-lab` is fine.

## Reuse story (for other people)

```bash
sparkrun registry add https://github.com/AdamFerguson/spark-lab
sparkrun run @spark-lab/qwen3-32b-sglang -H <host>
```

Because the recipes are the registry format, they are discoverable and runnable by
the stock `sparkrun` CLI with no spark-lab involvement — that is the "maximally
reusable" property we want.

## How `config.yaml` selects a recipe

Recipe references follow sparkrun's own discovery order, so `config.yaml` can be
expressed the same way a human would:

| Reference | Resolves via |
|---|---|
| `@spark-lab/qwen3-32b-sglang` | scoped lookup in this repo's registry (internal) |
| `@some-team/phi-4-vllm` | scoped lookup in a *third-party* registry |
| `https://…/recipe.yaml` | direct URL |
| `./local/recipe.yaml` | file path |
| `qwen3-32b-sglang` | flat name search across configured registries |

Sketch of the config shape (Phase 4 detail, not settled):

```yaml
model:
  recipe: "@spark-lab/qwen3-32b-sglang"   # which recipe to run (internal or 3rd-party)
  resources: { mem: 32GB, ... }           # optional overrides for this node
```

`spark-lab apply` would then: ensure the referenced registry is available
(local → present in-repo; remote → `sparkrun registry add <url>` if not yet), and
`sparkrun run <ref> --ensure`.

## Reconciling with the current design

Today the recipe is a **single parameterized Jinja template**
(`sparklab/templates/sparkrun_recipe.yaml.j2`) rendered to
`sparkrun/recipes/<name>.yaml`, driven by `model.*` fields in `config.yaml`.

The registry approach is **discrete, pre-authored recipe files** chosen by name.
These are different models. Reconciliation options:

- **(A) Templates become generators for registry recipes.** Keep the Jinja
  template(s) as the *authoring* source, and a step renders them into
  `recipes/*.yaml` (committed or generated at apply). Registry files are the
  reusable artifact; the template is the factory. *Recommended* — preserves the
  "tune a few knobs per node" ergonomics while producing standard registry
  files.
- **(B) Drop the template; recipes are hand-authored files in `recipes/`.**
  Simplest and most "standard," but per-node tuning moves entirely into
  `config.yaml` overrides or per-node recipe files.
- **(C) Both.** A small set of canonical registry recipes for popular models, plus
  the template for the "one primary configured model" case.

Open questions to settle in Phase 4/5:
- Registry `name` (default `spark-lab`) and whether it's config-overridable.
- Whether registry recipes are **committed** (versioned, reviewable, shareable)
  vs **generated at apply** from templates.
- How remote/third-party registries are declared in `config.yaml`
  (a top-level `registries:` list of URLs?) and how trust is handled
  (`sparkrun registry trust`) given hooks execution.
- Interaction with ADR 0004's `conversion:` (converting a 3rd-party recipe to a
  sparkrun-optimized one) — the registry is a natural place to store conversions.
- Reserved/hidden (`visible: false`) recipes and versioning of individual recipes.

## Relationship to other ADRs

- **ADR 0003 (discovery plugin):** the registry is one concrete `RecipeSource`
  implementation. `RecipeSource` gains a "registry" adapter that reads
  `.sparkrun/registry.yaml` + `recipes/` (local) and remote registries
  (`sparkrun registry` / URL).
- **ADR 0004 (config schema):** `model.recipe` (single) vs `models:` (multi) both
  become "a recipe reference + overrides"; the reference is now a sparkrun
  registry/URL/path expression, not an inline recipe definition.

## Non-goals (for now)

- Being an upstream to sparkrun's own registry (we reserve the right to
  contribute recipes upstream, but this repo stands alone).
- Building a registry host / web UI; a git repo + `.sparkrun/registry.yaml` is
  sufficient.
