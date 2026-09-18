"""Core RAG operations, independent of any transport.

Both entry points call into here:
  api.py   - REST, used by the React frontend and AWS Lambda
  main.py  - Inngest workflows, used for local durable runs

Keeping the logic here means the two never drift apart.
"""

import os
import uuid

import requests
from dotenv import load_dotenv

from data_loader import embed_texts, load_and_chunk_pdf
from vector_db import QdrantStorage

load_dotenv()

# Chunks of context retrieved per question and sent to the model.
TOP_K = 5

# Built once per process. On Lambda this survives across warm invocations.
_store: QdrantStorage | None = None


def store() -> QdrantStorage:
    global _store
    if _store is None:
        _store = QdrantStorage()
    return _store


def ingest_pdf(pdf_path: str, source_id: str) -> int:
    """Chunk, embed and upsert a PDF. Returns the number of chunks stored."""
    chunks = load_and_chunk_pdf(pdf_path)
    if not chunks:
        return 0
    vecs = embed_texts(chunks)
    # uuid5 keeps ids deterministic, so re-ingesting the same PDF updates the
    # existing points instead of duplicating them.
    ids = [str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}_{i}")) for i in range(len(chunks))]
    payloads = [{"text": c, "source": source_id} for c in chunks]
    store().upsert(ids, vecs, payloads)
    return len(chunks)


def search(question: str, top_k: int = TOP_K) -> dict:
    """Retrieve context chunks for a question."""
    return store().search(embed_texts([question])[0], top_k)


def answer(question: str, contexts: list[str]) -> str:
    """Ask Gemini the question using the retrieved context.

    Gemini speaks the OpenAI chat format, so this is a plain chat/completions
    call. Swap providers by changing GEMINI_ENDPOINT and GEMINI_MODEL in .env.
    """
    context_block = "\n\n".join(f"- {c}" for c in contexts)
    user_content = (
        "Use the following context to answer the question. \n\n"
        f"Context:\n{context_block}\n\n"
        f"Question: {question}\n\n"
        "Answer the question based on the context provided."
    )
    resp = requests.post(
        os.environ["GEMINI_ENDPOINT"].rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['GEMINI_API_KEY']}"},
        json={
            "model": os.environ["GEMINI_MODEL"],
            "max_tokens": 1024,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "You answer questions based on the context provided."},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def query(question: str, top_k: int = TOP_K) -> dict:
    """Full RAG round trip: retrieve, then answer."""
    found = search(question, top_k)
    if not found["contexts"]:
        return {
            "answer": "I could not find anything relevant in your documents.",
            "sources": [],
            "num_contexts": 0,
        }
    return {
        "answer": answer(question, found["contexts"]),
        "sources": found["sources"],
        "num_contexts": len(found["contexts"]),
    }


def library() -> dict:
    """Counts and distinct sources, for the frontend sidebar."""
    client = store().client
    if not client.collection_exists(store().collection):
        return {"chunks": 0, "documents": []}
    sources, offset = set(), None
    while True:  # one scroll page is not guaranteed to cover everything
        points, offset = client.scroll(
            store().collection, limit=256, offset=offset,
            with_payload=["source"], with_vectors=False,
        )
        sources.update(p.payload.get("source", "") for p in points if p.payload)
        if offset is None:
            break
    return {
        "chunks": client.count(store().collection, exact=True).count,
        "documents": sorted(s for s in sources if s),
    }


def delete_document(source: str) -> bool:
    """Remove one document's chunks. False if that source was not indexed."""
    if source not in library()["documents"]:
        return False
    store().delete_source(source)
    return True


def clear_library() -> None:
    """Remove every document."""
    store().clear()
