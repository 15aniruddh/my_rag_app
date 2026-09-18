"""PDF loading, chunking, and local embedding.

Embeddings run locally via fastembed (ONNX, CPU-only), so the app has no
embedding API cost.

PDF parsing uses pypdf directly rather than llama-index: llama-index pulls in
pandas, sqlalchemy, nltk and aiohttp (~170MB of Linux wheels) for what amounts
to text extraction and a text splitter, and the Lambda zip limit is 250MB.
"""

import os
import re
import tarfile
import tempfile

from fastembed import TextEmbedding
from pypdf import PdfReader

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200


def _ensure_model_cache() -> None:
    """Populate the fastembed cache from S3 when running on Lambda.

    A Lambda zip cannot carry the 65MB model and stay under the 250MB limit, so
    it is stored in S3 and pulled into /tmp on the first call in a container.
    Warm invocations skip this; locally (no MODEL_S3_BUCKET) fastembed just
    downloads from HuggingFace and caches it itself.
    """
    bucket = os.getenv("MODEL_S3_BUCKET")
    key = os.getenv("MODEL_S3_KEY")
    cache = os.getenv("FASTEMBED_CACHE_PATH")
    if not (bucket and key and cache):
        return
    if os.path.isdir(cache) and os.listdir(cache):
        return  # already warm

    import boto3  # provided by the Lambda runtime; not a packaged dependency

    os.makedirs(cache, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", dir=os.path.dirname(cache) or None)
    os.close(fd)
    try:
        boto3.client("s3").download_file(bucket, key, tmp_path)
        with tarfile.open(tmp_path) as tar:
            tar.extractall(cache)
    finally:
        os.unlink(tmp_path)


_ensure_model_cache()

embedder = TextEmbedding()

# Read the dimension off the model instead of hardcoding it: vector_db builds
# the Qdrant collection from this, and a mismatch fails every upsert.
EMBED_DIM = len(next(iter(embedder.embed(["probe"]))))


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts. fastembed batches internally, so large lists are fine."""
    return [v.tolist() for v in embedder.embed(texts)]


def _split(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries.

    Falls back to sentence boundaries, then to a hard cut, so a single
    enormous paragraph still gets divided rather than returned whole.
    """
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []

    # Paragraphs first; anything still too long is broken on sentence ends.
    pieces: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            while len(sentence) > size:
                pieces.append(sentence[:size])
                sentence = sentence[size:]
            if sentence:
                pieces.append(sentence)

    # Pack pieces up to the size limit, carrying `overlap` characters of the
    # previous chunk so context is not lost across a boundary.
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{piece}" if tail else piece
        else:
            current = piece
    if current:
        chunks.append(current)
    return chunks


def load_and_chunk_pdf(path: str) -> list[str]:
    """Extract text from a PDF and split it into overlapping chunks."""
    reader = PdfReader(path)
    text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    return _split(text)
