"""Generate the committed golden fixtures in ``tests/golden/``.

Run once -- and to refresh after an *intentional* behavior change:

    python3 tests/gen_golden.py

It renders the fixed ``REFERENCE_CONFIG`` with pinned (dummy) secrets and
records the regression baselines the parity test compares against:

  * the sha256 of every rendered target file (pins templates + verbatim assets),
  * the full rendered recipe text,
  * the apply command sequence for a fresh single-node apply.

``install_dir`` is a fixed absolute path in the reference config and ``SPARKRUN``
is pinned to ``sparkrun``, so the goldens are identical on every machine/CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sparklab.core import config as config_mod, converge, render  # noqa: E402
from tests.helpers import (  # noqa: E402
    GOLDEN_DIR,
    REFERENCE_CONFIG,
    REFERENCE_ENV,
    SECRET_DUMMY,
    V3_CLUSTER_CONFIG,
)


def main() -> None:
    with mock.patch.dict(os.environ, {**SECRET_DUMMY, "SPARKRUN": "sparkrun"}):
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(REFERENCE_CONFIG)
        (d / ".env").write_text(REFERENCE_ENV)
        cfg = config_mod.load(str(d / "config.yaml"))
        rendered = render.render(cfg, d / "deploy")
        sha = {rel: hashlib.sha256(data).hexdigest() for rel, data in rendered.items()}
        plan = converge.build_plan(cfg, rendered, {}, None, allow_restart=True)
        cmds = [[desc, [str(x) for x in argv]] for desc, argv in plan.commands]

        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        (GOLDEN_DIR / "reference_config.yaml").write_text(REFERENCE_CONFIG)
        (GOLDEN_DIR / "expected_sha256.json").write_text(json.dumps(sha, indent=2, sort_keys=True) + "\n")
        (GOLDEN_DIR / "expected_recipe.yaml").write_text(
            rendered[f"sparkrun/recipes/{cfg.recipe_name}.yaml"].decode("utf-8")
        )
        (GOLDEN_DIR / "expected_commands.json").write_text(json.dumps(cmds, indent=2) + "\n")

        # v3 cluster golden: per-host view renders for the fixed two-host config
        (d / "v3.yaml").write_text(V3_CLUSTER_CONFIG)
        v3 = config_mod.load(str(d / "v3.yaml"))
        v3_sha: dict = {}
        for host in (s.name for s in v3.host_specs):
            view = v3.view_for(host)
            r = render.render(view, d / f"v3-{host}")
            v3_sha[host] = {rel: hashlib.sha256(data).hexdigest() for rel, data in r.items()}
        (GOLDEN_DIR / "expected_v3_sha256.json").write_text(json.dumps(v3_sha, indent=2, sort_keys=True) + "\n")
        print(
            f"wrote {GOLDEN_DIR}: {len(sha)} files, {len(cmds)} commands, "
            f"recipe={cfg.recipe_name} (+ v3 views: {', '.join(v3_sha)})"
        )


if __name__ == "__main__":
    main()
