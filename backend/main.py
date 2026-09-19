"""Inngest RAG workflows served over FastAPI (local durable execution).

This is the optional, durable path: each step is retried and memoized
independently, and runs are visible in the Inngest dashboard.

On AWS, lambda_handler.py mounts these functions onto the api.py app, so
Inngest Cloud reaches them at /api/inngest. Both paths call into rag.py, so
the two cannot drift apart.

Run with:  uv run uvicorn main:app --reload --port 8000
"""

import logging
import os
from pathlib import Path

import inngest
import inngest.fast_api
from dotenv import load_dotenv
from fastapi import FastAPI

import rag
import uploads
from custom_types import RAGQueryResult, RAGSearchResult, RAGUpsertResult

load_dotenv()

inngest_client = inngest.Inngest(
    app_id="rag_app",
    logger=logging.getLogger("uvicorn"),
    # Production mode the moment a signing key exists, so there is no second
    # switch to forget. Unset locally = dev mode, unchanged.
    is_production=bool(os.getenv("INNGEST_SIGNING_KEY")),
    serializer=inngest.PydanticSerializer(),
)


@inngest_client.create_function(
    fn_id="RAG: Ingest PDF",
    trigger=inngest.TriggerEvent(event="rag/ingest_pdf"),
)
async def rag_ingest_pdf(ctx: inngest.Context):
    s3_key = ctx.event.data["s3_key"]
    source_id = ctx.event.data["source_id"]

    def _ingest() -> RAGUpsertResult:
        # Downloaded inside the step, not outside it: a retry lands in a fresh
        # container whose /tmp is empty, so the fetch has to be part of the
        # retried unit of work.
        path = uploads.fetch_to_tmp(s3_key)
        try:
            return RAGUpsertResult(ingested=rag.ingest_pdf(path, source_id))
        finally:
            Path(path).unlink(missing_ok=True)

    def _discard() -> str:
        uploads.discard(s3_key)
        return s3_key

    ingested = await ctx.step.run("embed-and-upsert", _ingest, output_type=RAGUpsertResult)
    # Separate step, and last: a failed ingest retries with the object still
    # staged in S3.
    await ctx.step.run("discard-staged-pdf", _discard)
    return ingested.model_dump()


@inngest_client.create_function(
    fn_id="RAG: Query PDF",
    trigger=inngest.TriggerEvent(event="rag/query_pdf"),
)
async def rag_query_pdf(ctx: inngest.Context):
    question = ctx.event.data["question"]
    top_k = ctx.event.data.get("top_k", rag.TOP_K)

    def _search() -> RAGSearchResult:
        found = rag.search(question, top_k)
        return RAGSearchResult(contexts=found["contexts"], sources=found["sources"])

    found = await ctx.step.run("embed-and-search", _search, output_type=RAGSearchResult)

    def _answer() -> RAGQueryResult:
        return RAGQueryResult(
            answer=rag.answer(question, found.contexts),
            sources=found.sources,
            num_contexts=len(found.contexts),
        )

    result = await ctx.step.run("llm-answer", _answer, output_type=RAGQueryResult)
    return result.model_dump()


app = FastAPI()

inngest.fast_api.serve(app, inngest_client, [rag_ingest_pdf, rag_query_pdf])
