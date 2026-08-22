"""Auto-conversion: a discovered record -> a sparkrun *candidate* recipe (ADR 0003).

Two paths:
  * **deterministic** -- normalize a cookbook entry (model/image/flags) into a
    sparkrun recipe. This is the baseline and the manual fallback.
  * **LLM-assisted** -- optionally refine the candidate through the configured
    LLM endpoint. The LLM transport is injectable so this is testable without a
    node; when the LLM is unreachable or returns malformed YAML, we fall back to
    the deterministic result.

Output is always a **candidate** (written to a file the user reviews); it is
never applied to the running model.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, Optional

import yaml


def _to_flag(key: str) -> str:
    return "--" + str(key).replace("_", "-")


def deterministic_transform(entry: Dict) -> Dict:
    """Normalize a cookbook entry into a sparkrun recipe dict."""
    model = str(entry.get("model", ""))
    image = str(entry.get("image", ""))
    flags = dict(entry.get("flags") or {})
    defaults = {"host": "0.0.0.0", "port": int(entry.get("port", 30000))}
    for k, v in flags.items():
        if k not in ("host", "port"):
            defaults[k] = v

    cmd_flags = []
    for k, v in flags.items():
        flag = _to_flag(k)
        cmd_flags.append(flag if (isinstance(v, bool) and v) else f"{flag} {v}")
    command = ("sglang serve \\\n"
               "    --enable-metrics \\\n"
               "    --trust-remote-code \\\n"
               "    --model-path {model} \\\n"
               + " \\\n".join(f"    {f}" for f in cmd_flags)
               + " \\\n    --host {host} \\\n    --port {port}\n")
    slug = str(entry.get("slug") or model).lower().replace("/", "-").replace(" ", "-")
    return {
        "name": slug,
        "model": model,
        "runtime": "sglang",
        "min_nodes": 1,
        "container": image,
        "executor": "docker",
        "executor_config": {"ipc": "host", "privileged": True},
        "defaults": defaults,
        "metadata": {"description": str(entry.get("description", ""))},
        "command": command,
        "_candidate_source": "sglang-cookbook",
    }


def _prompt(record, body: Dict) -> str:
    entry = yaml.safe_dump(body, sort_keys=False)
    return (
        "Convert this SGLang cookbook entry into a sparkrun recipe YAML "
        "(fields: name, model, runtime, min_nodes, container, executor, "
        "executor_config, defaults, metadata, command). Return ONLY the YAML.\n"
        f"Entry:\n{entry}"
    )


def _parse_yaml_block(text: str) -> Optional[Dict]:
    if not text:
        return None
    m = re.search(r"```ya?ml\s*(.*?)```", text, re.DOTALL)
    candidate = m.group(1) if m else text
    try:
        data = yaml.safe_load(candidate)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def build_candidate(record, llm_transport: Optional[Callable[[str], str]] = None) -> Dict:
    """Return a sparkrun recipe candidate dict for a discovered record.

    - If the body is already a sparkrun recipe (has ``model`` + ``command``), use it.
    - Otherwise normalize it (cookbook entry) deterministically.
    - If ``llm_transport`` is given, try it first; fall back on any failure.
    """
    body = record.body or {}
    if llm_transport is not None:
        try:
            parsed = _parse_yaml_block(llm_transport(_prompt(record, body)))
            if parsed and parsed.get("model") and parsed.get("command"):
                parsed.setdefault("_candidate_source", "llm")
                return parsed
        except Exception:
            pass  # LLM unreachable / malformed -> deterministic fallback
    if body.get("model") and body.get("command"):
        cand = dict(body)
        cand.setdefault("_candidate_source", "sparkrun-registry")
        return cand
    return deterministic_transform(body)


def validate_candidate(cand: Dict) -> list:
    """Return a list of problems (empty == valid)."""
    problems = []
    if not cand.get("model"):
        problems.append("missing 'model'")
    if not (cand.get("container") or cand.get("image")):
        problems.append("missing 'container'/'image'")
    if not cand.get("command"):
        problems.append("missing 'command'")
    return problems


def make_llm_transport(endpoint: str, model: str, api_key: Optional[str], runtime) -> Callable:
    """A real LLM transport: POST to the endpoint via the runtime seam (curl)."""
    def transport(prompt: str) -> str:
        payload = json.dumps({"model": model,
                              "messages": [{"role": "user", "content": prompt}]})
        argv = ["curl", "-s", "-X", "POST", endpoint,
                "-H", "Content-Type: application/json", "-d", payload]
        if api_key:
            argv += ["-H", f"Authorization: Bearer {api_key}"]
        cp = runtime.run(argv)
        text = getattr(cp, "stdout", "") or ""
        return json.loads(text)["choices"][0]["message"]["content"]
    return transport
