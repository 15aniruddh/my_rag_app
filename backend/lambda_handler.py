"""AWS Lambda entry point.

Mangum translates between Lambda's event payload and ASGI, so api.py stays a
plain FastAPI app that also runs under uvicorn locally.

Set the Lambda handler to: lambda_handler.handler
"""

from mangum import Mangum

from api import app

handler = Mangum(app, lifespan="off")
