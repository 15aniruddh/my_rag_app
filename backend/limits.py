"""Abuse controls: rate limiting, a daily budget, and optional access control.

Threat model for a public deployment:
  1. Someone spams /api/query and burns the Gemini free-tier DAILY quota.
  2. Someone uploads PDFs until the Qdrant free cluster is full.
  3. Someone sends huge questions to inflate prompt size.
  4. Anyone with the URL can use the app at all.

Each control below maps to one of those.

ponytail: counters are per-process. One uvicorn process is exactly right; on
Lambda each warm container keeps its own, so effective limits multiply by the
number of live containers. Upgrade path when that matters: move
`_WINDOWS`/`_budget` into DynamoDB or Redis behind the same check() calls.
"""

import os
import threading
import time
from collections import defaultdict, deque
from datetime import date

from fastapi import Header, HTTPException, Request

# --- tunables (all overridable from .env) -----------------------------------

QUERY_PER_MIN = int(os.getenv("RATE_QUERY_PER_MIN", "6"))
INGEST_PER_HOUR = int(os.getenv("RATE_INGEST_PER_HOUR", "10"))
READ_PER_MIN = int(os.getenv("RATE_READ_PER_MIN", "60"))
DELETE_PER_HOUR = int(os.getenv("RATE_DELETE_PER_HOUR", "30"))

# Hard ceiling on LLM calls per day across ALL callers. This is the control
# that actually protects the Gemini free tier, since per-IP limits do nothing
# against a spread of addresses.
DAILY_QUERY_BUDGET = int(os.getenv("DAILY_QUERY_BUDGET", "200"))

MAX_QUESTION_CHARS = int(os.getenv("MAX_QUESTION_CHARS", "1000"))
MAX_DOCUMENTS = int(os.getenv("MAX_DOCUMENTS", "25"))
MAX_CHUNKS = int(os.getenv("MAX_CHUNKS", "2000"))

# Set APP_ACCESS_KEY to require an X-Access-Key header. Unset (the default)
# leaves the API open, which is what you want locally.
ACCESS_KEY = os.getenv("APP_ACCESS_KEY", "").strip()

# Only trust X-Forwarded-For when a proxy you control sets it (CloudFront,
# Lambda Function URL). Trusting it blindly lets anyone spoof their identity
# and bypass per-IP limits entirely.
TRUST_PROXY = os.getenv("TRUST_PROXY", "").lower() in {"1", "true", "yes"}

# Bound the key space: an attacker rotating IPs must not grow this dict
# without limit, which would be its own denial of service.
MAX_TRACKED_KEYS = 10_000

_lock = threading.Lock()
_WINDOWS: dict[str, dict[str, deque]] = defaultdict(lambda: defaultdict(deque))


def client_key(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_window(bucket: str, key: str, limit: int, window_s: int) -> None:
    """Sliding window. Raises 429 with Retry-After when the limit is hit."""
    now = time.monotonic()
    with _lock:
        buckets = _WINDOWS[bucket]
        if len(buckets) > MAX_TRACKED_KEYS:
            # Drop everything rather than grow forever. Crude, but the
            # alternative is unbounded memory under IP rotation.
            buckets.clear()
        hits = buckets[key]
        while hits and now - hits[0] > window_s:
            hits.popleft()
        if len(hits) >= limit:
            retry = int(window_s - (now - hits[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {limit} per {window_s}s. Retry in {retry}s.",
                headers={"Retry-After": str(retry)},
            )
        hits.append(now)


class _DailyBudget:
    """Counts LLM calls per calendar day, resetting at UTC midnight."""

    def __init__(self) -> None:
        self.day = date.today()
        self.used = 0

    def consume(self, limit: int) -> None:
        with _lock:
            today = date.today()
            if today != self.day:
                self.day, self.used = today, 0
            if self.used >= limit:
                raise HTTPException(
                    status_code=429,
                    detail="Daily question budget reached. Try again tomorrow.",
                    headers={"Retry-After": "3600"},
                )
            self.used += 1

    def snapshot(self) -> dict:
        return {"used": self.used, "limit": DAILY_QUERY_BUDGET, "day": str(self.day)}


_budget = _DailyBudget()
budget_status = _budget.snapshot


# --- FastAPI dependencies ---------------------------------------------------


def require_access_key(x_access_key: str = Header(default="")) -> None:
    """No-op unless APP_ACCESS_KEY is set."""
    if ACCESS_KEY and x_access_key != ACCESS_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing access key.")


def limit_read(request: Request) -> None:
    _check_window("read", client_key(request), READ_PER_MIN, 60)


def limit_query(request: Request) -> None:
    """Per-IP throttle only.

    The daily budget is NOT consumed here: dependencies run before request-body
    validation, so a stream of malformed requests would drain the quota without
    ever reaching the model. The endpoint calls consume_budget() itself, once
    the request is known to be valid and an LLM call is actually imminent.
    """
    _check_window("query", client_key(request), QUERY_PER_MIN, 60)


def consume_budget() -> None:
    """Spend one unit of the global daily LLM allowance."""
    _budget.consume(DAILY_QUERY_BUDGET)


def limit_ingest(request: Request) -> None:
    _check_window("ingest", client_key(request), INGEST_PER_HOUR, 3600)


def limit_delete(request: Request) -> None:
    _check_window("delete", client_key(request), DELETE_PER_HOUR, 3600)
