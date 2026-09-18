"""Pydantic models passed between Inngest steps.

Inngest serializes step output, so every value crossing a step boundary is one
of these rather than a loose dict.
"""

import pydantic


class RAGUpsertResult(pydantic.BaseModel):
    ingested: int


class RAGSearchResult(pydantic.BaseModel):
    contexts: list[str]
    sources: list[str]


class RAGQueryResult(pydantic.BaseModel):
    answer: str
    sources: list[str]
    num_contexts: int
