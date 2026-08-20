"""spark-lab engine.

Config-driven, idempotent deployment of an OpenAI-compatible LLM stack on an
NVIDIA DGX Spark (or a cluster managed by `sparkrun`):
    SGLang (via sparkrun) -> LiteLLM gateway -> Prometheus/Grafana
    + Tailscale (private) + optional Cloudflare Tunnel (public).
"""

__version__ = "0.1.0"
