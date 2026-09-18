"""PDF loading, chunking, and local embedding.

Embeddings run locally via fastembed (ONNX, CPU-only) so the app has no
embedding API cost and works offline.
"""

from fastembed import TextEmbedding
from llama_index.readers.file import PDFReader
from llama_index.core.node_parser import SentenceSplitter

# Downloads the model (~130MB) on first use, then caches it on disk.
embedder = TextEmbedding()

# Read the dimension off the model instead of hardcoding it: vector_db builds
# the Qdrant collection from this, and a mismatch fails every upsert.
EMBED_DIM = len(next(iter(embedder.embed(["probe"]))))

splitter = SentenceSplitter(chunk_size=1000, chunk_overlap=200)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts. fastembed batches internally, so large lists are fine."""
    return [v.tolist() for v in embedder.embed(texts)]


def load_and_chunk_pdf(path: str) -> list[str]:
    """Extract text from a PDF and split it into overlapping chunks."""
    docs = PDFReader().load_data(file=path)
    chunks = []
    texts = [d.text for d in docs if getattr(d, "text", None) is not None]
    for t in texts:
        chunks.extend(splitter.split_text(t))
    return chunks
