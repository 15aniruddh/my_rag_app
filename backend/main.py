"""Inngest RAG workflows served over FastAPI (local durable execution).

This is the optional, durable path: each step is retried and memoized
independently, and runs are visible in the Inngest dashboard.

The AWS deployment does not use this file - it ships api.py behind Lambda.
Both call into rag.py, so the two paths cannot drift apart.

Run with:  uv run uvicorn main:app --reload --port 8000
"""

import logging

import inngest
import inngest.fast_api
from dotenv import load_dotenv
from fastapi import FastAPI

import rag
from custom_types import RAGQueryResult, RAGSearchResult, RAGUpsertResult

load_dotenv()

inngest_client = inngest.Inngest(
    app_id="rag_app",
    logger=logging.getLogger("uvicorn"),
    is_production=False,
    serializer=inngest.PydanticSerializer(),
)


@inngest_client.create_function(
    fn_id="RAG: Ingest PDF",
    trigger=inngest.TriggerEvent(event="rag/ingest_pdf"),
)
async def rag_ingest_pdf(ctx: inngest.Context):
    pdf_path = ctx.event.data["pdf_path"]
    source_id = ctx.event.data.get("source_id", pdf_path)

    def _ingest() -> RAGUpsertResult:
        return RAGUpsertResult(ingested=rag.ingest_pdf(pdf_path, source_id))

    def _delete_upload() -> str:
        # The chunks are in Qdrant now, so the PDF is dead weight on disk.
        # missing_ok because Inngest may replay this step.
        from pathlib import Path

        Path(pdf_path).unlink(missing_ok=True)
        return pdf_path

    ingested = await ctx.step.run("embed-and-upsert", _ingest, output_type=RAGUpsertResult)
    # Separate step, and last: a failed ingest retries with the file still there.
    await ctx.step.run("delete-uploaded-pdf", _delete_upload)
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
