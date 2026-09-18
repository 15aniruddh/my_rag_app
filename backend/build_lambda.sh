#!/usr/bin/env bash
# Build a Lambda deployment zip for the REST API.
#
# Must run on Linux/x86_64 wheels, not macOS ones: onnxruntime ships native
# binaries. --platform pulls the right wheels regardless of the build machine.
set -euo pipefail

OUT="${1:-lambda.zip}"
BUILD=".lambda_build"

rm -rf "$BUILD" "$OUT"
mkdir -p "$BUILD"

python3 -m pip install \
  --target "$BUILD" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  -r requirements.txt

cp api.py rag.py limits.py data_loader.py vector_db.py custom_types.py lambda_handler.py "$BUILD/"

# Bake the 720KB quantized embedding model in, so cold starts do not reach out
# to HuggingFace. FASTEMBED_CACHE_PATH must match this on the Lambda config.
mkdir -p "$BUILD/model_cache"
FASTEMBED_CACHE_PATH="$BUILD/model_cache" python3 - <<'PY'
from fastembed import TextEmbedding
list(TextEmbedding().embed(["warm"]))
print("model baked into package")
PY

# Trim what Lambda never executes.
find "$BUILD" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type f -name "*.pyc" -delete 2>/dev/null || true

(cd "$BUILD" && zip -qr "../$OUT" .)
echo "built $OUT ($(du -h "$OUT" | cut -f1)); unzipped $(du -sh "$BUILD" | cut -f1) / 250MB limit"
