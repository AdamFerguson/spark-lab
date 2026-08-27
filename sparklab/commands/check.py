"""`spark-lab check` — the consolidated pre-flight (config + images + system).

Usage:

* ``spark-lab check`` (or ``check config``) — the config pre-flight: schema,
  secrets, host set, per-host render + binaries (read-only).
* ``spark-lab check --images`` (or ``check images``) — resolve the stack images
  (``--probe`` additionally runs ``docker manifest inspect`` per host).
* ``spark-lab check --system`` (or ``check system``) — per-host system
  precheck; ``--install`` (``--all``) installs missing tools.

Any combination works: ``spark-lab check --images --system --hosts luna,sol``.
``validate`` and ``doctor`` remain available as hidden aliases
(``validate`` == ``check``, ``doctor`` == ``check --system``).
"""
from __future__ import annotations

from . import images, system, validate


def run(args) -> int:
    args._label = "check"   # the banner says the command the user actually ran
    what = getattr(args, "what", None)
    do_config = what == "config" or (what is None and not (args.images or args.system))
    do_images = what == "images" or args.images
    do_system = what == "system" or args.system

    rcs = []
    if do_config:
        rcs.append(validate.run(args))
    if do_images:
        rcs.append(images.run(args))
    if do_system:
        rcs.append(system.check(args))
    if not rcs:
        do_config_fallback = validate.run(args)
        return do_config_fallback
    return 0 if all(rc == 0 for rc in rcs) else 1
