#!/usr/bin/env bash
# One-time node-side preparation for the qwen38-flash-next recipe
# (RadixArk/Qwen3.8-Flash-Next-NVFP4 on SGLang, GB10 / DGX Spark).
#
# Why this exists: the recipe's SGLang image needs two source files patched
# (PLE mmap + sm_120/121 QSA gate; see recipes/flash-next/patches/). Upstream
# (shantanugoel/qwen38-flash-next-sglang-dgx-spark, MIT) extracts those files
# from the image, patches them on the host, and bind-mounts them back in read-
# only. This script reproduces that extraction+patch on this node, into a fixed
# location the recipe's executor_config.volumes point at:
#
#   $BASE_DIR/build/qwen4_exp.py                     (patched, mounted ro)
#   $BASE_DIR/build/qwen_sparse_attn_backend.py      (patched, mounted ro)
#   $BASE_DIR/ple/                                   (48 GB PLE mmap backing dir)
#   $BASE_DIR/sglang-cache/                          (SGlang/flashinfer cache)
#   $BASE_DIR/build/in_image_paths.txt               (resolved mount targets)
#
# It also downloads the pinned checkpoint revision into the standard HF cache
# (sparkrun and the container both read it; resumable).
#
# Re-running is safe: patches are idempotent (each prints "ALREADY PATCHED"),
# the image pull and the download are no-ops when already present.
#
# Usage (on the node, from any checkout of this repo):
#   HF_TOKEN=hf_... bash scripts/flash-next-prepare.sh
#   # or, with the token in your spark-lab .env:
#   HF_TOKEN="$(sed -n 's/^HF_TOKEN=//p' ~/spark-lab/.env | tr -d '"')" \
#     bash scripts/flash-next-prepare.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PATCHES="${REPO_ROOT}/recipes/flash-next/patches"

IMAGE="${IMAGE:-lmsysorg/sglang:qwen38flashnext}"
MODEL="${MODEL:-RadixArk/Qwen3.8-Flash-Next-NVFP4}"
REVISION="${REVISION:-7b719225242aacd3dbd3f9407468c2ee9a9d2594}"
BASE_DIR="${BASE_DIR:-$HOME/AI/flash-next}"
HF_CACHE="${HF_CACHE:-${HF_HOME:-$HOME/.cache/huggingface}}"

echo "== flash-next prepare: ${MODEL} @ ${REVISION}"
echo "   image   : ${IMAGE}"
echo "   base dir: ${BASE_DIR}"

arch="$(uname -m)"
compute="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1)"
if [[ "${arch}" != "aarch64" || "${compute}" != "12.1" ]]; then
  echo "Expected aarch64 / SM 12.1 (GB10); got ${arch} / ${compute}." >&2
  exit 1
fi

mkdir -p "${BASE_DIR}/build" "${BASE_DIR}/ple" "${BASE_DIR}/sglang-cache"

echo "-- image"
docker image inspect "${IMAGE}" >/dev/null 2>&1 || docker pull "${IMAGE}"

echo "-- resolving in-image paths"
qwen4_path="$(docker run --rm --entrypoint python3 "${IMAGE}" -c \
  'import sglang.srt.models.qwen4_exp as m; print(m.__file__)' | tail -1)"
qsa_path="$(docker run --rm --entrypoint python3 "${IMAGE}" -c \
  'import sglang.srt.layers.attention.qwen_sparse_attn_backend as m; print(m.__file__)' | tail -1)"
printf '%s\n%s\n' "${qwen4_path}" "${qsa_path}" > "${BASE_DIR}/build/in_image_paths.txt"
echo "   ${qwen4_path}"
echo "   ${qsa_path}"

echo "-- extracting + patching"
extract() {
  local cid
  cid="$(docker create "${IMAGE}")"
  docker cp "${cid}:$1" "$2"
  docker rm -f "${cid}" >/dev/null
}
extract "${qwen4_path}" "${BASE_DIR}/build/qwen4_exp.py"
extract "${qsa_path}"   "${BASE_DIR}/build/qwen_sparse_attn_backend.py"

python3 "${PATCHES}/ple_mmap.py"               "${BASE_DIR}/build/qwen4_exp.py"
python3 "${PATCHES}/ple_reuse.py"              "${BASE_DIR}/build/qwen4_exp.py"
python3 "${PATCHES}/qsa_drop_sm121_sdpa.py"    "${BASE_DIR}/build/qwen_sparse_attn_backend.py"
python3 "${PATCHES}/qsa_trtllm_sm120.py"       "${BASE_DIR}/build/qwen_sparse_attn_backend.py"
python3 -m py_compile "${BASE_DIR}/build/qwen4_exp.py" "${BASE_DIR}/build/qwen_sparse_attn_backend.py"

python3 - "${BASE_DIR}/build/qwen4_exp.py" "${BASE_DIR}/build/qwen_sparse_attn_backend.py" <<'PY'
from pathlib import Path
import sys
qwen4 = Path(sys.argv[1]).read_text()
qsa = Path(sys.argv[2]).read_text()
assert "_alloc_ple_table" in qwen4, "PLE mmap helper missing"
assert "_alloc_ple_table(source_weight.shape" in qwen4
assert "_ple_shard_matches" in qwen4, "PLE mmap reuse fast path missing"
assert "if is_sm121():" not in qsa, "SM121 SDPA intercept still present"
assert "is_sm100_supported() or is_sm120_supported()" in qsa, "sm_120 gate missing"
print("   patches ok")
PY

echo "-- recipe mount-target check"
python3 - "${REPO_ROOT}/recipes/qwen38-flash-next.yaml" "${BASE_DIR}/build/in_image_paths.txt" <<'PY'
import sys
from pathlib import Path
try:
    import yaml
except ImportError:
    print("   (no pyyaml on host -- skipping recipe cross-check)")
    sys.exit(0)
recipe = yaml.safe_load(Path(sys.argv[1]).read_text())
vols = (recipe.get("executor_config") or {}).get("volumes") or []
mounts = [v.split(":")[-2] for v in vols if v.endswith(":ro")]
expected = [l.strip() for l in Path(sys.argv[2]).read_text().splitlines() if l.strip()]
if sorted(mounts) != sorted(expected):
    print("MISMATCH between recipe mount targets and the image's actual paths:")
    print("  recipe :", mounts)
    print("  image  :", expected)
    print("The image tag changed its layout -- update the recipe's executor_config.volumes.")
    sys.exit(1)
print("   recipe mount targets match the image")
PY

echo "-- checkpoint ${REVISION}"
if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN is empty. Export it (gated checkpoint) and re-run." >&2
  exit 1
fi
snapshot="${HF_CACHE}/hub/models--${MODEL//\//--}/snapshots/${REVISION}"
if [[ -f "${snapshot}/config.json" ]] && [[ -f "${snapshot}/model.safetensors.index.json" ]] \
   && ! find "${HF_CACHE}/hub/models--${MODEL//\//--}" -name '*.incomplete' -print -quit | grep -q .; then
  echo "   already in ${HF_CACHE}"
else
  echo "   downloading into ${HF_CACHE} as uid $(id -u) (resumable)..."
  HF_HOME="${HF_CACHE}" HF_TOKEN="${HF_TOKEN}" hf download "${MODEL}" \
    --revision "${REVISION}" --max-workers 8
fi

echo
echo "prepare complete. Next:"
echo "  1. spark-lab apply        # converges the recipe; first boot fills the"
echo "                            #    48 GB PLE table (~45-60 min quiet), later"
echo "                            #    boots ~10 min (shard-reuse fast path)"
echo "  2. spark-lab check        # gateway on the control-plane host picks the"
echo "                            #    model up via implicit central serving"
