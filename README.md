# PDF Q&A

Ask questions about your own PDFs. Upload a document, and a local embedding
model plus a hosted LLM answer questions about it with the source cited.

Runs at **zero API cost**: embeddings are computed on your machine, the vector
store is Qdrant Cloud's free tier, and answers come from Gemini's free tier.

```
frontend/  React + Vite  ──HTTP──>  backend/  FastAPI
                                        │
                     ┌──────────────────┼──────────────────┐
                     ▼                  ▼                  ▼
              fastembed (local)   Qdrant Cloud        Gemini API
              384-dim, CPU        free tier           free tier
```

---

## Layout

```
backend/
  rag.py             core logic: chunk, embed, upsert, search, answer
  api.py             REST API — used by React and by AWS Lambda
  main.py            Inngest workflows — optional durable path
  limits.py          rate limiting, daily budget, access control
  data_loader.py     PDF parsing, chunking, local embeddings
  vector_db.py       Qdrant wrapper
  custom_types.py    Pydantic models for Inngest steps
  lambda_handler.py  Mangum adapter (handler = lambda_handler.handler)
  build_lambda.sh    builds the deployment zip
  requirements.txt   Lambda-only deps (no inngest)
  pyproject.toml     Python project root; uv.lock beside it
  .venv/             virtual environment (gitignored)

frontend/
  src/api.js         every backend call; switchable via VITE_API_BASE
  src/App.jsx        UI
  src/index.css      theming and layout
  vite.config.js     dev server + /api proxy
```

**Two entry points, one core.** `api.py` and `main.py` both call `rag.py`, so
the REST and Inngest paths cannot drift apart. The AWS deployment ships only
`api.py`; Inngest is local-only and excluded from the Lambda package.

---

## Setup

Requires **Python 3.13** with [uv](https://docs.astral.sh/uv/), and **Node 20+**.

```bash
cd backend     && uv sync        # creates backend/.venv
cd ../frontend && npm install
cd ..
```

> The Python project root is `backend/` — that is where `pyproject.toml` and
> `uv.lock` live. Run every `uv` command from inside `backend/`, or uv will
> create a second environment at the repo root.

Create `.env` in the **project root**:

```bash
QDRANT_URL="https://<your-cluster>.aws.cloud.qdrant.io:6333"
QDRANT_API_KEY="<your-qdrant-api-key>"

GEMINI_API_KEY="<your-google-ai-studio-key>"
GEMINI_ENDPOINT="https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_MODEL="gemini-3.5-flash-lite"
```

- **Qdrant** — free cluster at <https://cloud.qdrant.io>, then copy the cluster
  URL and an API key.
- **Gemini** — key at <https://aistudio.google.com/app/apikey>. Leave billing
  **disabled** on the project to stay strictly on the free tier.

`.env` is gitignored. Never commit it.

---

## Running

Two terminals:

```bash
# 1. backend
cd backend && uv run uvicorn api:app --reload --port 8000

# 2. frontend
cd frontend && npm run dev
```

Open **<http://localhost:5173>**.

> Vite binds IPv6-only, so use `localhost:5173`. `127.0.0.1:5173` will not
> connect.

`vite.config.js` proxies `/api` to port 8000, so requests are same-origin in
dev and CORS never applies — the same code path as production.

### Optional: the Inngest path

Durable retries, step memoization, and a run dashboard. Not needed for normal
use, and the React UI does not send events to it.

```bash
# 1. dev server (dashboard on :8288)
npx inngest-cli@latest dev -u http://127.0.0.1:8001/api/inngest

# 2. the functions, on 8001 so they do not collide with the REST API
cd backend && uv run uvicorn main:app --reload --port 8001
```

Trigger a run by hand:

```bash
curl -X POST http://localhost:8288/e/dev_key \
  -H 'Content-Type: application/json' \
  -d '{"name":"rag/query_pdf","data":{"question":"what is this about?","top_k":5}}'
```

The dashboard shows "No events found" until something sends an event. That is
expected, not a fault.

---

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/health` | — | `{status, budget}` — open, never rate limited |
| GET | `/api/library` | — | `{chunks, documents[]}` |
| POST | `/api/ingest` | multipart `file` | `{ingested, source}` |
| POST | `/api/query` | `{question, top_k?}` | `{answer, sources[], num_contexts}` |
| DELETE | `/api/documents/{source}` | — | `{deleted}` |
| DELETE | `/api/documents` | — | `{cleared}` |

```bash
curl http://localhost:8000/api/health
curl http://localhost:8000/api/library
curl -X POST http://localhost:8000/api/ingest -F "file=@mydoc.pdf"
curl -X POST http://localhost:8000/api/query \
  -H 'Content-Type: application/json' -d '{"question":"what is this about?"}'
curl -X DELETE "http://localhost:8000/api/documents/mydoc.pdf"
```

---

## Abuse controls

All optional, all overridable in `.env`. Defaults shown.

| Variable | Default | Protects against |
|---|---|---|
| `RATE_QUERY_PER_MIN` | 6 | LLM spam per IP |
| `RATE_INGEST_PER_HOUR` | 10 | upload flooding |
| `RATE_READ_PER_MIN` | 60 | scraping |
| `RATE_DELETE_PER_HOUR` | 30 | malicious wipes |
| `DAILY_QUERY_BUDGET` | 200 | **Gemini free-tier quota (global)** |
| `MAX_QUESTION_CHARS` | 1000 | prompt-size inflation |
| `MAX_DOCUMENTS` | 25 | Qdrant cluster fill |
| `MAX_CHUNKS` | 2000 | Qdrant cluster fill |
| `APP_ACCESS_KEY` | unset | casual public access |
| `TRUST_PROXY` | unset | IP spoofing |

`DAILY_QUERY_BUDGET` is the important one: per-IP limits do nothing against a
spread of addresses, so a global daily ceiling is what actually protects the
free tier. Remaining budget is visible on `/api/health`.

Set `APP_ACCESS_KEY` to require an `X-Access-Key` header, and set
`VITE_ACCESS_KEY` in `frontend/.env` to match.

**Two real limits, stated plainly:**

- Counters are **per-process**. Correct for one uvicorn process; on Lambda each
  warm container keeps its own, so effective limits multiply by container
  count. Upgrade path is noted in `limits.py`.
- `VITE_ACCESS_KEY` is **not authentication** — anything shipped to a browser is
  readable by the user. It deters casual abuse only.

Leave `TRUST_PROXY` unset unless you are behind CloudFront or a Lambda Function
URL: trusting `X-Forwarded-For` blindly lets anyone spoof their IP and bypass
per-IP limits.

---

## Deploying to AWS

**Backend → Lambda.** The API-only dependency set is ~163MB unzipped, under
Lambda's 250MB zip limit, so no container image and no ECR is needed.

```bash
cd backend && ./build_lambda.sh
```

Create the function:

- Runtime **Python 3.13**, handler **`lambda_handler.handler`**
- Upload `lambda.zip` (via S3 if over 50MB)
- Memory **1024MB**, timeout **60s**
- Env vars: the five from `.env`, plus
  `FASTEMBED_CACHE_PATH=/var/task/model_cache`,
  `ALLOWED_ORIGINS=https://<your-cloudfront-domain>`, and `TRUST_PROXY=1`
- Enable a **Function URL** — not API Gateway, whose free tier is 12 months
  while Lambda's 1M requests/month is perpetual

`build_lambda.sh` pulls `manylinux2014_x86_64` wheels (onnxruntime ships native
binaries, so macOS wheels would not run) and bakes the 720KB quantized
embedding model into the package, so cold starts never call HuggingFace.
Measured warm-up: ~0.35s.

**Frontend → S3 + CloudFront.**

```bash
cd frontend && npm run build
aws s3 sync dist/ s3://<your-bucket>/ --delete
```

Serve the bucket through CloudFront and add a second origin routing `/api/*` to
the Function URL. That keeps the site same-origin, so `VITE_API_BASE` stays
empty and CORS stays out of it. Otherwise point `VITE_API_BASE` at the Function
URL and set `ALLOWED_ORIGINS` on the Lambda.

### Cost

Lambda's free tier is perpetual. S3's 5GB is 12 months, after which a small
static site is cents per month. Qdrant and Gemini free tiers cover this
workload. Realistically under $1/month after year one, not $0.

---

## How it works

**Ingest** — the PDF is parsed, split into 1000-char chunks with 200-char
overlap, embedded locally with `BAAI/bge-small-en-v1.5` (384-dim, quantized
ONNX), and upserted into Qdrant. The uploaded file is deleted immediately
afterwards.

**Query** — the question is embedded with the same model, the top 5 chunks are
retrieved by cosine similarity, and those chunks plus the question are sent to
Gemini. The answer is rendered as markdown with its sources listed.

Notes worth knowing:

- **Uploads are ephemeral.** `api.py` writes to the system temp dir (`/tmp` on
  Lambda, the only writable path there) and deletes in a `finally`, so a failed
  ingest still cleans up.
- **6MB upload cap.** Lambda Function URLs limit request payloads to about 6MB;
  larger PDFs get a clear 413. For bigger files, upload to S3 with a presigned
  PUT and have the Lambda read from there.
- **Re-ingesting the same PDF is safe.** Point IDs are `uuid5(source_id + chunk
  index)`, so a re-ingest updates existing points instead of duplicating.
- **Deleting a PDF does not delete its embeddings.** The file is only the input;
  the text lives in Qdrant. Use the `×` on a document chip, or
  `DELETE /api/documents/{source}`.
- **Delete needs a payload index.** Qdrant refuses to filter on an unindexed
  field, so `vector_db.py` ensures a keyword index on `source` at startup.
- **Embedding dimension is read off the model**, never hardcoded. Changing
  models changes the dimension — clear the collection and re-ingest, or upserts
  fail on a mismatch.
- **Free-tier 429s are rate limits, not charges.**

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `nodename nor servname provided` | DNS cannot resolve the Qdrant host. Use `1.1.1.1` / `8.8.8.8`. |
| `KeyError: 'GEMINI_API_KEY'` | Missing `.env` entry, or `.env` is not in the project root. |
| `404 ... model is no longer available` | Gemini retired the model; the error names the replacement. Update `GEMINI_MODEL`. |
| "Vector store unreachable" in the sidebar | Backend cannot reach Qdrant — check `.env` and DNS. |
| `429 Rate limit exceeded` while browsing | Defaults are tuned for one user. Raise `RATE_READ_PER_MIN`. |
| `429 Daily question budget reached` | `DAILY_QUERY_BUDGET` hit. Raise it, or wait for UTC midnight. |
| `507 Library is full` | `MAX_DOCUMENTS` / `MAX_CHUNKS` hit. Delete a document. |
| CORS error in the browser | `VITE_API_BASE` is off-origin without `ALLOWED_ORIGINS` set on the backend. |
| `127.0.0.1:5173` refuses to connect | Vite binds IPv6-only. Use `localhost:5173`. |
| Inngest dashboard shows no events | Expected — the React UI does not send events. Trigger one manually. |
| Lambda times out on first call | Cold start plus model load. Timeout 60s, memory 1024MB. |
| Code changes have no effect | uvicorn without `--reload` keeps old modules in memory. Restart it. |
