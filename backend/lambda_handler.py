"""AWS Lambda entry point.

Mangum translates between Lambda's event payload and ASGI, so api.py stays a
plain FastAPI app that also runs under uvicorn locally.

The Inngest functions are mounted here rather than in api.py: they are only
reachable in deployments that set INNGEST_SIGNING_KEY, and api.py stays a
plain REST surface for local development.

Set the Lambda handler to: lambda_handler.handler
"""

import inngest.fast_api
from mangum import Mangum

from api import app
from main import inngest_client, rag_ingest_pdf, rag_query_pdf

# Serves GET/POST/PUT /api/inngest. Deliberately outside the require_access_key
# dependency: Inngest Cloud authenticates with request signatures, not our
# header, and its callbacks would be rejected by the key gate.
inngest.fast_api.serve(app, inngest_client, [rag_ingest_pdf, rag_query_pdf])

handler = Mangum(app, lifespan="off")
