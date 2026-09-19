"""Self-check for 5xx reporting.

The point of this path is that a failure nobody was watching still leaves a
record. That only holds if 5xx is logged and reported, 4xx stays quiet, and a
broken reporter cannot take the request down with it.

Run: python test_error_reporting.py
"""

import logging
import os

os.environ["INNGEST_EVENT_KEY"] = "test-key"

import api  # noqa: E402  (must follow the env var _report reads)
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@api.app.get("/_t/boom500")
def _boom500():
    try:
        raise ValueError("qdrant exploded")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Query failed: {exc}") from exc


@api.app.get("/_t/boom404")
def _boom404():
    raise HTTPException(status_code=404, detail="nope")


@api.app.get("/_t/unhandled")
def _unhandled():
    return 1 / 0


class Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def demo():
    sent = []

    class FakeClient:
        async def send(self, event):
            sent.append(event)

    api.inngest_client = FakeClient()
    logs = Collector()
    api.logger.addHandler(logs)
    api.logger.setLevel(logging.DEBUG)
    client = TestClient(api.app, raise_server_exceptions=False)

    # 4xx: the caller being told no, not the service breaking.
    assert client.get("/_t/boom404").status_code == 404
    assert not sent, "4xx must not be reported"
    assert not logs.records, "4xx must not be logged as an error"

    # 5xx: logged with the ORIGINAL traceback, and reported.
    assert client.get("/_t/boom500").status_code == 500
    assert len(sent) == 1, sent
    assert sent[0].name == "rag/request_failed"
    assert sent[0].data["path"] == "/_t/boom500"
    assert sent[0].data["status"] == 500
    assert len(logs.records) == 1
    assert logs.records[0].exc_info[0] is ValueError, "should carry the cause, not the HTTPException"

    # A bug outside any try block, with nothing leaked to the caller.
    resp = client.get("/_t/unhandled")
    assert resp.status_code == 500, resp.status_code
    assert resp.json() == {"detail": "Internal server error."}
    assert "ZeroDivisionError" not in resp.text
    assert len(sent) == 2 and sent[1].data["path"] == "/_t/unhandled"

    # A broken reporter must not turn a 500 into something worse.
    class DeadClient:
        async def send(self, event):
            raise RuntimeError("inngest unreachable")

    api.inngest_client = DeadClient()
    assert client.get("/_t/boom500").status_code == 500

    # With no event key the log still happens; only the event is skipped.
    os.environ.pop("INNGEST_EVENT_KEY")
    api.inngest_client = FakeClient()
    before = len(sent)
    assert client.get("/_t/boom500").status_code == 500
    assert len(sent) == before, "no key: nothing sent"

    print("error reporting: OK")


if __name__ == "__main__":
    demo()
