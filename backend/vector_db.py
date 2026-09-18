"""Qdrant vector store wrapper (Qdrant Cloud)."""

import os
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)
from data_loader import EMBED_DIM

load_dotenv()

# os.environ (not os.getenv) so a missing var fails loudly at import, naming it,
# instead of passing None down into the client and failing later as a 401.
QDRANT_URL = os.environ["QDRANT_URL"]
QDRANT_API_KEY = os.environ["QDRANT_API_KEY"]


class QdrantStorage:
    def __init__(self, url=QDRANT_URL, api_key=QDRANT_API_KEY, collection="docs", dim=EMBED_DIM):
        self.client = QdrantClient(url=url, api_key=api_key, timeout=30)
        self.collection = collection
        self.dim = dim
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        self._ensure_source_index()

    def _ensure_source_index(self) -> None:
        """Qdrant refuses to filter on an unindexed payload field, so
        delete-by-source needs this index to exist. Safe to call repeatedly;
        it also upgrades collections created before the index was added."""
        try:
            self.client.create_payload_index(
                collection_name=self.collection,
                field_name="source",
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass  # already present

    def upsert(self, ids, vectors, payloads):
        points = [
            PointStruct(id=id, vector=vector, payload=payload)
            for id, vector, payload in zip(ids, vectors, payloads)
        ]
        self.client.upsert(collection_name=self.collection, points=points)

    def search(self, query_vector, top_k: int = 5) -> dict:
        # query_points, not search: .search was removed in qdrant-client 1.x.
        # It returns a QueryResponse, so the hits are under .points.
        hits = self.client.query_points(
            collection_name=self.collection,
            query=query_vector,
            with_payload=True,
            limit=top_k,
        ).points
        contexts = []
        sources = set()
        for hit in hits:
            payload = hit.payload or {}
            text = payload.get("text", "")
            if text:
                contexts.append(text)
                sources.add(payload.get("source", ""))
        return {"contexts": contexts, "sources": list(sources)}

    def delete_source(self, source: str) -> None:
        """Remove every chunk belonging to one document.

        Filter-based rather than id-based: the caller does not know how many
        chunks the document produced.
        """
        self.client.delete(
            collection_name=self.collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="source", match=MatchValue(value=source))]
                )
            ),
        )

    def clear(self) -> None:
        """Drop and rebuild the collection.

        Cheaper than deleting every point, and rebuilding here (rather than
        relying on __init__) keeps a long-lived instance usable afterwards.
        """
        self.client.delete_collection(self.collection)
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )
        self._ensure_source_index()
