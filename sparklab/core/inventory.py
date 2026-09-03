"""Live inventory: what is ACTUALLY running on a node, whatever started it.

Models you launch by hand (``docker run``/kit scripts) are not owned by the
spark-lab config -- but you still want to see them and expose them. This
module probes, ON THE NODE (read-only, via the runtime seam):

  * every container port answering ``GET /v1/models`` (an OpenAI-compatible
    engine, managed or not) -> ``{"container", "port", "models": [...]}``
  * on control-plane hosts: the gateway's live served model list.

Feeds ``status`` (human + ``--json``), ``sync`` and ``expose``. No writes, no
restarts -- pure observation.
"""

from __future__ import annotations

from typing import Any, Dict, List

# One sh -c pipeline. Per container: full Cmd/Entrypoint come from
# ``docker inspect`` (``docker ps``'s .Command TRUNCATES long commands,
# which is exactly the engines we care about); candidate ports = published
# HostPorts PLUS any --port flag in the command (host-network engines publish
# nothing); probe each; responders print ENGINE|name|port|ids. %s = gateway
# port (excluded here; reported separately as the served model list).
_ENGINES_FMT = (
    "for id in $(docker ps -q); do "
    "n=$(docker inspect -f '{{.Name}}' $id | cut -c2-); "
    "pp=$(docker inspect -f "
    "'{{range .NetworkSettings.Ports}}{{range .}}{{.HostPort}} {{end}}{{end}}' $id); "
    'cmd=$(docker inspect -f \'{{join .Config.Cmd " "}} {{join .Config.Entrypoint " "}}\' $id); '
    "ports=\"$pp $(echo \"$cmd\" | grep -oE '[-][-]?port[= ][0-9]+' | grep -oE '[0-9]+')\"; "
    "for port in $(echo \"$ports\" | tr ' ' '\\n' | sort -u | grep -E '^[0-9]+$'); do "
    '[ "$port" = "%s" ] && continue; '
    'm=$(curl -sf -m 3 "http://127.0.0.1:$port/v1/models" 2>/dev/null '
    '| grep -oE \'"id"[ ]*:[ ]*"[^"]*"\' | cut -d\'"\' -f4 | paste -sd, -); '
    '[ -n "$m" ] && echo "ENGINE|$n|$port|$m"; '
    "done; done"
)


def engines_argv(gateway_port: int) -> List[str]:
    return ["sh", "-c", _ENGINES_FMT % gateway_port]


def _models_argv(port: int, key: str) -> List[str]:
    """GET the gateway's /v1/models. The auth key is inlined into the
    node-side curl header (generated secrets are [A-Za-z0-9.-]; the argv is
    never echoed -- run_capture hides output and no banner prints this)."""
    script = (
        f'curl -sf -m 5 -H "Authorization: Bearer {key}" '
        f"http://127.0.0.1:{port}/v1/models "
        '| grep -oE \'"id"[ ]*:[ ]*"[^"]*"\' | cut -d\'"\' -f4'
    )
    return ["sh", "-c", script]


def discover(runtime, cfg) -> Dict[str, Any]:
    """The live view for the runtime's node: engines + (gateway served list on
    control-plane hosts) + (zoo resident list on swap hosts). Best-effort: a
    missing docker/curl yields empty lists, never an exception."""
    out: Dict[str, Any] = {"engines": [], "gateway": None, "swap": None}
    if runtime is None or not runtime.available("docker"):
        return out
    gw_port = int((cfg.litellm or {}).get("port", 4000))
    r = runtime.run_capture(engines_argv(gw_port))
    for line in (r.stdout or "").splitlines():
        parts = line.split("|")
        if len(parts) == 4 and parts[0] == "ENGINE":
            out["engines"].append(
                {"container": parts[1], "port": int(parts[2]), "models": [m for m in parts[3].split(",") if m]}
            )
    if getattr(cfg, "control_plane_enabled", lambda: True)():
        key = cfg.secret((cfg.litellm or {}).get("master_key_env")) or ""
        g = runtime.run_capture(_models_argv(gw_port, key))
        served: List[str] = [s.strip() for s in (g.stdout or "").splitlines() if s.strip()]
        out["gateway"] = {"port": gw_port, "served": served, "reachable": g.returncode == 0 and bool(served)}
    if getattr(cfg, "swap_for_host", lambda: False)():
        # Who the zoo currently holds resident (llama-swap /running).
        r = runtime.run_capture(["sh", "-c", f"curl -sf -m 3 http://127.0.0.1:{cfg.swap_port()}/running || true"])
        out["swap"] = {
            "port": cfg.swap_port(),
            "resident": swap_resident(r.stdout or ""),
            "reachable": r.returncode == 0 and bool((r.stdout or "").strip()),
        }
    return out


def swap_resident(text: str) -> List[str]:
    """Model ids from llama-swap's /running (tolerant of shape drift)."""
    import json as _json

    try:
        data = _json.loads(text or "")
    except _json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else (data.get("models") or data.get("running") or [])
    out: List[str] = []
    for it in items:
        if isinstance(it, dict):
            out.append(str(it.get("id") or it.get("model") or it))
        else:
            out.append(str(it))
    return out
