"""REST API for the React frontend, and the AWS Lambda entry point.

Endpoints:
  GET  /api/health   - liveness
  GET  /api/library  - chunk count and indexed document names
  POST /api/ingest   - multipart PDF upload; hands off to Inngest when the
                       durable path is configured, otherwise ingests inline
  POST /api/query    - question in, answer + sources out
"""

import os
import tempfile
from pathlib import Path

import inngest
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import limits
import rag
import uploads
from main import inngest_client

app = FastAPI(title="PDF Q&A API")

# Vite dev server runs on a different origin, and in production the S3/CloudFront
# site is a different origin again. Set ALLOWED_ORIGINS to that URL on deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = 6 * 1024 * 1024  # Lambda Function URLs cap payloads near 6MB


class QueryIn(BaseModel):
    # max_length caps prompt size: an unbounded question is both a cost and a
    # context-window problem.
    question: str = Field(min_length=1, max_length=limits.MAX_QUESTION_CHARS)
    top_k: int = Field(default=rag.TOP_K, ge=1, le=20)


class QueryOut(BaseModel):
    answer: str
    sources: list[str]
    num_contexts: int


@app.get("/api/health")
def health() -> dict:
    """Unauthenticated and unlimited: it must stay usable as a liveness probe."""
    return {"status": "ok", "budget": limits.budget_status()}


@app.get("/api/library", dependencies=[Depends(limits.require_access_key), Depends(limits.limit_read)])
def get_library() -> dict:
    try:
        return rag.library()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Vector store unreachable: {exc}") from exc


@app.post("/api/ingest", dependencies=[Depends(limits.require_access_key), Depends(limits.limit_ingest)])
async def ingest(file: UploadFile = File(...)) -> dict:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    # Refuse before reading the body if the library is already full: this
    # bounds how much a stranger can push into the Qdrant free cluster.
    try:
        stats = rag.library()
    except Exception:
        stats = {"documents": [], "chunks": 0}
    if len(stats["documents"]) >= limits.MAX_DOCUMENTS:
        raise HTTPException(
            status_code=507,
            detail=f"Library is full ({limits.MAX_DOCUMENTS} documents). Delete one first.",
        )
    if stats["chunks"] >= limits.MAX_CHUNKS:
        raise HTTPException(
            status_code=507,
            detail=f"Library is full ({limits.MAX_CHUNKS} chunks). Delete a document first.",
        )

    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PDF is {len(payload) // 1024}KB; limit is {MAX_UPLOAD_BYTES // 1024}KB.",
        )

    name = Path(file.filename).name

    # Durable path: stage the bytes in S3 and hand off to Inngest. Needs both
    # the bucket and an event key; with either missing we ingest inline, which
    # is what local development does.
    if uploads.enabled() and os.getenv("INNGEST_EVENT_KEY"):
        try:
            key = uploads.put(payload, name)
            await inngest_client.send(
                inngest.Event(name="rag/ingest_pdf", data={"s3_key": key, "source_id": name})
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not queue ingestion: {exc}") from exc
        # No chunk count: the work has not run yet. The client polls
        # /api/library until the name appears.
        return {"queued": True, "source": name}

    # Lambda's only writable location is /tmp, which gettempdir() resolves to.
    tmp_dir = Path(tempfile.gettempdir()) / "rag_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / name
    tmp_path.write_bytes(payload)

    try:
        ingested = rag.ingest_pdf(str(tmp_path), name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc
    finally:
        # The chunks live in Qdrant now; the PDF is dead weight either way.
        tmp_path.unlink(missing_ok=True)

    if ingested == 0:
        raise HTTPException(status_code=422, detail="No extractable text found in that PDF.")
    return {"ingested": ingested, "source": name}


@app.post(
    "/api/query",
    response_model=QueryOut,
    dependencies=[Depends(limits.require_access_key), Depends(limits.limit_query)],
)
def post_query(body: QueryIn) -> dict:
    # Only now, with a validated body and an LLM call imminent, spend quota.
    limits.consume_budget()
    try:
        return rag.query(body.question.strip(), body.top_k)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Query failed: {exc}") from exc


@app.delete(
    "/api/documents/{source}",
    dependencies=[Depends(limits.require_access_key), Depends(limits.limit_delete)],
)
def delete_document(source: str) -> dict:
    """Remove a single indexed document and all of its chunks."""
    try:
        removed = rag.delete_document(source)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}") from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"{source} is not indexed.")
    return {"deleted": source}


@app.delete(
    "/api/documents",
    dependencies=[Depends(limits.require_access_key), Depends(limits.limit_delete)],
)
def clear_documents() -> dict:
    """Remove every indexed document."""
    try:
        rag.clear_library()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Clear failed: {exc}") from exc
    return {"cleared": True}
