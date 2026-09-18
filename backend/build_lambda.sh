#!/usr/bin/env bash
# Build the Lambda deployment zip and the model tarball.
#
# Two artefacts, because they cannot travel together:
#   lambda.zip   the code and its dependencies  (~196MB unzipped, limit 250MB)
#   model.tar.gz the 65MB embedding model, pulled from S3 on cold start
#
# Lambda runs linux/x86_64 on Amazon Linux 2023 (glibc 2.34), so wheels are
# pinned to manylinux_2_28 - onnxruntime no longer publishes manylinux2014.
set -euo pipefail
cd "$(dirname "$0")"

BUILD="$PWD/.lambda_build"
MODEL_DIR="$PWD/.model_cache"
ZIP="$PWD/lambda.zip"
MODEL_TGZ="$PWD/model.tar.gz"

rm -rf "$BUILD" "$MODEL_DIR" "$ZIP" "$MODEL_TGZ"
mkdir -p "$BUILD" "$MODEL_DIR"

echo "==> installing linux/x86_64 wheels for python 3.13"
# uv resolves cross-platform targets correctly regardless of the interpreter
# running this script; plain pip evaluates markers against the RUNNING python
# and mis-resolves on a 3.14 machine.
if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --target "$BUILD" \
    --python-platform x86_64-manylinux_2_28 --python-version 3.13 \
    -r requirements.txt
else
  python3 -m pip install --quiet --target "$BUILD" \
    --platform manylinux_2_28_x86_64 --implementation cp \
    --python-version 3.13 --only-binary=:all: -r requirements.txt
fi

cp api.py rag.py limits.py data_loader.py vector_db.py custom_types.py lambda_handler.py "$BUILD/"

echo "==> trimming"
# hf_xet is only used for xet-backed downloads; the model comes from S3, and
# nothing imports it (verified). onnxruntime's training/quantisation helpers
# are not used for inference.
rm -rf "$BUILD/hf_xet" "$BUILD"/hf_xet-*
rm -rf "$BUILD/onnxruntime/transformers" "$BUILD/onnxruntime/tools" "$BUILD/onnxruntime/quantization"
find "$BUILD" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type d -name "*.dist-info" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type f -name "*.pyc" -delete 2>/dev/null || true

(cd "$BUILD" && zip -qr "$ZIP" .)
UNZIPPED_MB=$(du -sm "$BUILD" | cut -f1)
echo "==> lambda.zip: $(du -h "$ZIP" | cut -f1) zipped, ${UNZIPPED_MB}MB unzipped (limit 250MB)"

if [ "$UNZIPPED_MB" -gt 250 ]; then
  echo "!! ${UNZIPPED_MB}MB exceeds Lambda's 250MB unzipped limit" >&2
  exit 1
fi

echo "==> building the model tarball"
# Any interpreter with fastembed produces the same cache: the files are plain
# ONNX and JSON, not platform-specific.
BAKE_PY=""
for c in ".venv/bin/python" "python3"; do
  if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
    if "$c" -c "import fastembed" >/dev/null 2>&1; then BAKE_PY="$c"; break; fi
  fi
done
if [ -z "$BAKE_PY" ]; then
  echo "!! no interpreter with fastembed; run 'uv sync' first" >&2
  exit 1
fi

FASTEMBED_CACHE_PATH="$MODEL_DIR" "$BAKE_PY" -c '
from fastembed import TextEmbedding
list(TextEmbedding().embed(["warm"]))
print("   model downloaded")
'
[ -n "$(ls -A "$MODEL_DIR")" ] || { echo "!! model cache empty" >&2; exit 1; }

tar -czf "$MODEL_TGZ" -C "$MODEL_DIR" .
echo "==> model.tar.gz: $(du -h "$MODEL_TGZ" | cut -f1)"
