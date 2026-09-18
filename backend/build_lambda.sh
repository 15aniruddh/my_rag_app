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

echo "==> building the model tarball"
# Done BEFORE trimming, against the pristine install: the import test needs the
# package exactly as pip produced it.
#
# Which interpreter can import fastembed depends on where this runs:
#   CI (linux/x86_64): the wheels just installed into $BUILD import directly.
#   developer Mac:     those are Linux binaries and will not import, so fall
#                      back to the project venv.
# Either way the downloaded files are plain ONNX and JSON, not platform-specific.
BAKE_PY=""
BAKE_PATH=""
if PYTHONPATH="$BUILD" python3 -c "import fastembed" >/dev/null 2>&1; then
  BAKE_PY="python3"; BAKE_PATH="$BUILD"
  echo "   using the freshly built package"
elif [ -x ".venv/bin/python" ] && .venv/bin/python -c "import fastembed" >/dev/null 2>&1; then
  BAKE_PY=".venv/bin/python"
  echo "   using .venv"
elif python3 -c "import fastembed" >/dev/null 2>&1; then
  BAKE_PY="python3"
  echo "   using system python3"
fi

if [ -z "$BAKE_PY" ]; then
  echo "!! no interpreter can import fastembed. Diagnostics:" >&2
  PYTHONPATH="$BUILD" python3 -c "import fastembed" 2>&1 | tail -5 >&2 || true
  exit 1
fi

FASTEMBED_CACHE_PATH="$MODEL_DIR" PYTHONPATH="$BAKE_PATH" "$BAKE_PY" -c '
from fastembed import TextEmbedding
list(TextEmbedding().embed(["warm"]))
print("   model downloaded")
'
[ -n "$(ls -A "$MODEL_DIR")" ] || { echo "!! model cache empty" >&2; exit 1; }

echo "==> trimming"
# Conservative on purpose. Earlier versions also deleted *.dist-info and
# hf_xet; dist-info removal breaks importlib.metadata.version(), which
# fastembed calls on import. With ~60MB of headroom under the 250MB limit,
# shaving 16MB is not worth a runtime ImportError.
find "$BUILD" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -type f -name "*.pyc" -delete 2>/dev/null || true

(cd "$BUILD" && zip -qr "$ZIP" .)
UNZIPPED_MB=$(du -sm "$BUILD" | cut -f1)
echo "==> lambda.zip: $(du -h "$ZIP" | cut -f1) zipped, ${UNZIPPED_MB}MB unzipped (limit 250MB)"

if [ "$UNZIPPED_MB" -gt 250 ]; then
  echo "!! ${UNZIPPED_MB}MB exceeds Lambda's 250MB unzipped limit" >&2
  exit 1
fi

tar -czf "$MODEL_TGZ" -C "$MODEL_DIR" .
echo "==> model.tar.gz: $(du -h "$MODEL_TGZ" | cut -f1)"
