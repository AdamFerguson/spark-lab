"""`spark-lab check system` / `doctor` -- detect + (optionally) install the tools
spark-lab needs on the node.

Read-only unless ``--install``. The tool table is **data, not code**: adding or
adjusting a required/optional tool (and its install command) is a one-line change
here. Detection + install are routed through the runtime seam (ADR 0002) so this
is testable with the fake runtime and never hard-codes ``subprocess``.
"""
from __future__ import annotations

import sys

from ..core import cluster

# (binary, required, why, install_command_or_None, needs_sudo)
TOOLS = [
    ("uv",        True,  "Manages the Python env + installs sparkrun (`uv tool install`).",
     "curl -LsSf https://astral.sh/uv/install.sh | sh", False),
    ("python3",   True,  "spark-lab is a Python CLI; uv builds the venv with this interpreter.",
     None, False),
    ("docker",    True,  "Runs the LiteLLM gateway + monitoring stack (docker compose).",
     "curl -fsSL https://get.docker.com | sh", True),
    ("git",       True,  "Repo/registry ops + the pre-commit secret gate.",
     "apt-get install -y git", True),
    ("curl",      True,  "Used by the installers + node diagnostics.",
     "apt-get install -y curl", True),
    ("sparkrun",  True,  "The model orchestrator -- the critical dependency; runs the recipe.",
     "uv tool install sparkrun", False),
    ("hf",        False, "Optional: Hugging Face CLI -- gated models, manual download/cache, token setup (`hf auth login`). sparkrun pulls models via its own lib, so this is not required.",
     "uv tool install huggingface_hub", False),
    ("tailscale", False, "Optional: private access to the gateway (network.tailscale).",
     "curl -fsSL https://tailscale.com/install.sh | sh", True),
    ("cloudflared", False, "Optional: public tunnel for the gateway (network.cloudflare).",
     None, False),
    ("gitleaks",  False, "Optional: stronger secret scanning (the hook falls back to grep).",
     "curl -sSL https://github.com/gitleaks/gitleaks/releases/latest/download/gitleaks_Linux_x64.tar.gz"
     " | tar -xz -C /tmp && install /tmp/gitleaks_Linux_x64/gitleaks ~/.local/bin/gitleaks", False),
]


def detect(runtime) -> list:
    """Return one dict per tool with a live ``present`` flag (via the runtime)."""
    out = []
    for name, required, why, install, sudo in TOOLS:
        out.append({"name": name, "required": required, "why": why, "install": install,
                    "sudo": sudo, "present": bool(runtime and runtime.available(name))})
    return out


def check_capabilities(runtime) -> list:
    """Capability checks that go beyond 'is the binary present'.

    The important one is **docker access**: the binary can be installed yet the
    current user may not be in the ``docker`` group, so every real command fails
    with 'permission denied ... docker.sock'. That is the classic first-run trap
    on a fresh node.

    Each entry: ``{name, required, ok, why, fix, relogin}``.
    """
    out = []
    if runtime and runtime.available("docker"):
        try:
            cp = runtime.run(["docker", "info"])
            ok = getattr(cp, "returncode", 1) == 0
        except Exception:  # noqa: BLE001 - treat a probe error as 'not ok'
            ok = False
        out.append({
            "name": "docker access",
            "required": True,
            "ok": ok,
            "why": "The current user must reach the docker daemon to run the stack + model container.",
            "fix": "sudo usermod -aG docker $(whoami)   # then re-login (new shell/session)",
            "relogin": not ok,
        })
    return out


def print_capabilities(caps, file=None) -> None:
    file = file or sys.stdout
    if not caps:
        return
    print("capabilities", file=file)
    for c in caps:
        status = "ok" if c["ok"] else "NEEDS FIX"
        req = "required" if c["required"] else "optional"
        print(f"  {c['name']:<14}{req:<10}{status:<10}{c['why']}", file=file)
        if not c["ok"]:
            print(f"    fix: {c['fix']}", file=file)


def caps_needing_fix(caps) -> list:
    return [c for c in caps if not c["ok"]]


def print_table(results, file=None) -> None:
    file = file or sys.stdout
    print("spark-lab system check", file=file)
    print(f"  {'tool':<12}{'required':<10}{'status':<10}why", file=file)
    for r in results:
        status = "ok" if r["present"] else "MISSING"
        req = "yes" if r["required"] else "optional"
        print(f"  {r['name']:<12}{req:<10}{status:<10}{r['why']}", file=file)


def missing(results, required_only: bool = True) -> list:
    return [r for r in results
            if not r["present"] and (r["required"] if required_only else True)]


def install(results, runtime, include_optional: bool = False) -> tuple:
    """Install the missing tools (required, + optional if requested) via the seam.

    Returns ``(installed, failed)``. A successful install that still isn't on
    PATH in this process (e.g. uv adds ~/.local/bin to the login shell) is
    reported, not silently treated as a failure of the run.
    """
    targets = missing(results, required_only=not include_optional)
    if not targets:
        print("Nothing to install (all needed tools present).")
        return [], []
    installed, failed = [], []
    for r in targets:
        cmd = r["install"]
        if not cmd:
            print(f"  [!] {r['name']}: no one-liner install available -- install it manually.")
            print(f"      why: {r['why']}")
            if r["required"]:
                failed.append(r["name"])
            continue
        print(f"  installing {r['name']} -> $ {('sudo ' if r['sudo'] else '')}{cmd}")
        if r["sudo"] and getattr(runtime, "is_remote", False):
            print(f"      (root is needed on {getattr(runtime, 'label', r['name'])} -- "
                  f"you may be asked for that node's sudo password)")
        argv = ["sh", "-lc", cmd]
        try:
            cp = runtime.run_sudo(argv) if r["sudo"] else runtime.run(argv)
            rc = getattr(cp, "returncode", 0)
        except Exception as e:  # noqa: BLE001 - surface, don't crash the run
            print(f"      install errored: {e}")
            failed.append(r["name"])
            continue
        if runtime.available(r["name"]):
            print(f"      {r['name']} is now available.")
            installed.append(r["name"])
        else:
            print(f"      {r['name']} install returned rc={rc} but is not yet on PATH -- "
                  f"reopen the shell (or re-run) to pick it up.")
            if rc == 0:
                installed.append(r["name"])   # installed; just not visible to this process yet
            else:
                failed.append(r["name"])
    return installed, failed


def _check_one(runtime, do_install: bool, include_optional: bool) -> int:
    """The per-host system check body (one runtime)."""
    results = detect(runtime)
    print_table(results)
    caps = check_capabilities(runtime)
    print_capabilities(caps)
    cap_fix = caps_needing_fix(caps)
    req_missing = missing(results, required_only=True)
    opt_missing = missing(results, required_only=False)
    if req_missing or cap_fix:
        parts = []
        if req_missing:
            parts.append("missing tools: " + ", ".join(r["name"] for r in req_missing))
        if cap_fix:
            parts.append("needs fix: " + ", ".join(c["name"] for c in cap_fix))
        print("\nNeeds attention: " + "; ".join(parts))
    else:
        note = "All required tools are present."
        if opt_missing:
            note += f" ({len(opt_missing)} optional missing: " \
                    + ", ".join(r["name"] for r in opt_missing) + " -- safe to ignore.)"
        print("\n" + note)
    if do_install:
        print("\nInstalling missing tools...")
        installed, _failed = install(results, runtime, include_optional=include_optional)
        after = detect(runtime)
        print_table(after)
        still = [r["name"] for r in missing(after, required_only=True)
                 if r["name"] not in installed]
        if still:
            print("\nStill missing required tool(s): " + ", ".join(still))
            return 1
        print("\nAll required tools installed (re-run in a fresh shell to pick them up).")
        return 0
    return 0 if (not missing(results, required_only=True) and not caps_needing_fix(caps)) else 1


def check(args) -> int:
    """`spark-lab check system` / `doctor`: report, and install with --install.

    Runs once per selected host (``--hosts``); a host's failure doesn't stop
    the others."""
    from ..core import config as config_mod
    try:
        cfg = config_mod.load(args.config)
    except ValueError as e:
        print(f"[INVALID] config: {e}", file=sys.stderr)
        return 1
    names = cluster.parse_hosts_arg(getattr(args, "hosts", None))
    ts = cluster.targets(cfg, names, runtime=getattr(args, "runtime", None))
    if not ts:
        return 1
    install = bool(getattr(args, "install", False))
    include_optional = bool(getattr(args, "all", False))
    return cluster.run_on_each(ts, lambda t: _check_one(t.runtime, install, include_optional))
