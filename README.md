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
  build_lambda.sh    builds lambda.zip + model.tar.gz
  requirements.txt   Lambda-only deps (no inngest, no llama-index)
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

Durable retries, step memoization, and a run dashboard. Optional: with the
Inngest secrets unset, `/api/ingest` chunks and embeds inline and nothing below
applies. Locally the React UI never sends events; on AWS it does, once
`INNGEST_SIGNING_KEY` and `INNGEST_EVENT_KEY` are set (see *Durable ingest*).

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

### Durable ingest (AWS)

Set `INNGEST_SIGNING_KEY` and `INNGEST_EVENT_KEY` as repo secrets and sync the
app in Inngest Cloud against `https://<your-domain>/api/inngest`. `/api/ingest`
then stages the PDF in S3, emits `rag/ingest_pdf` and returns
`{queued: true, source}` immediately; the frontend polls `/api/library` until
the name lands.

S3 is not incidental. Lambda's `/tmp` belongs to one container, so a PDF
written while handling the upload is gone by the time Inngest calls back for a
step — that callback is a separate invocation. The bytes are staged under
`uploads/` in the artifacts bucket, downloaded *inside* the step so a retry in
a fresh container still works, and deleted by the final step. A lifecycle rule
expires anything left behind after a day.

Unset either secret and the whole path is skipped: `/api/ingest` embeds inline
and returns a chunk count, exactly as it did before. That is what local
development uses, and why `uploads.py` is a no-op there.

### When something fails

Any 5xx is logged once, centrally, by an exception handler in `api.py` rather
than by a log call in each `except` block — so routes added later are covered
for free. The traceback logged is the *original* cause, not the `HTTPException`
wrapping it. 4xx stays quiet: that is a caller being told no.

With `INNGEST_EVENT_KEY` set, the same handler also emits `rag/request_failed`
carrying the path, method, status and (truncated) detail, so a failure that
happened to a stranger at 3am shows up on the dashboard next to the ingest
runs instead of only in that person's browser.

`rag/request_failed` triggers no function on purpose — it is a log entry, not
work. `Functions triggered: —` against it is correct, not a missing sync.
Sending it is best-effort: if Inngest is unreachable the request still returns
its original error, and the CloudWatch line is still written.

---

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/health` | — | `{status, budget}` — open, never rate limited |
| GET | `/api/library` | — | `{chunks, documents[]}` |
| POST | `/api/ingest` | multipart `file` | `{ingested, source}`, or `{queued, source}` on the durable path |
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

Two GitHub Actions workflows. Pushing to `main` deploys automatically; each one
only runs when its own directory changed.

| Workflow | Triggered by | Deploys to |
|---|---|---|
| `.github/workflows/backend.yml` | `backend/**` | Lambda + API Gateway |
| `.github/workflows/frontend.yml` | `frontend/**` | S3 (private) + CloudFront |

Both are idempotent — they create the buckets, IAM role, Lambda, HTTP API,
Origin Access Control and CloudFront distribution if missing, and update them
otherwise. There is no separate bootstrap step and no Terraform state to manage.

```
                    CloudFront (https)
                   /                  \
        /  ──> S3 bucket          /api/*  ──> API Gateway ──> Lambda
              (private, OAC)                   (HTTP API)     (FastAPI
                                                               via Mangum)
```

Serving the API through the same distribution means the browser sees **one
origin**, so CORS never applies and `VITE_API_BASE` stays empty.

### One-time AWS setup

The pipeline authenticates with **OIDC**, so no long-lived AWS keys are stored
anywhere. Create the identity provider and role once:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REPO="<your-github-user>/<your-repo>"

aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

aws iam create-role --role-name pdf-qa-github-actions \
  --assume-role-policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Federated\":\"arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com\"},
      \"Action\":\"sts:AssumeRoleWithWebIdentity\",
      \"Condition\":{
        \"StringEquals\":{\"token.actions.githubusercontent.com:aud\":\"sts.amazonaws.com\"},
        \"StringLike\":{\"token.actions.githubusercontent.com:sub\":\"repo:${REPO}:*\"}
      }}]}"

aws iam attach-role-policy --role-name pdf-qa-github-actions \
  --policy-arn arn:aws:iam::aws:policy/PowerUserAccess

# PowerUser cannot manage IAM, and the workflow creates the Lambda's role.
aws iam put-role-policy --role-name pdf-qa-github-actions \
  --policy-name manage-lambda-role --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Action":[
      "iam:CreateRole","iam:GetRole","iam:PassRole",
      "iam:AttachRolePolicy","iam:PutRolePolicy"],"Resource":"*"}]}'
```

The `StringLike` condition pins the trust to your repository, so no other repo
and no fork can assume the role.

> **If the role is rejected with `Not authorized to perform
> sts:AssumeRoleWithWebIdentity`**, GitHub is probably sending the *immutable
> identifier* form of the subject claim, which embeds numeric database IDs:
>
> ```
> repo:OWNER@123456/REPO@7890123:environment:production
> ```
>
> rather than the plain `repo:OWNER/REPO:...`. The names alone will not match.
> Find the exact claim AWS saw:
>
> ```bash
> aws cloudtrail lookup-events \
>   --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity \
>   --max-results 1 --region <your-region> \
>   --query 'Events[0].CloudTrailEvent' --output text \
>   | python3 -c "import sys,json;print(json.load(sys.stdin)['userIdentity']['principalId'])"
> ```
>
> then add that pattern to the trust policy. `StringLike` takes a list, so you
> can allow both forms. The ID form is in fact the safer one: IDs survive a
> rename and cannot be reused by someone re-creating a repo with the same name.

### GitHub secrets and variables

Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/pdf-qa-github-actions` |
| `QDRANT_URL` | from your `.env` |
| `QDRANT_API_KEY` | from your `.env` |
| `GEMINI_API_KEY` | from your `.env` |
| `APP_ACCESS_KEY` | optional; gates the public API |
| `INNGEST_SIGNING_KEY` | optional; turns on the durable ingest path |
| `INNGEST_EVENT_KEY` | optional; required alongside the signing key |

| Variable | Default |
|---|---|
| `AWS_REGION` | `ap-south-1` |
| `PUBLIC_BASE_URL` | the CloudFront domain; set it to your custom domain so the Inngest sync registers that URL |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` |
| `DAILY_QUERY_BUDGET` | `200` |

With the `gh` CLI:

```bash
gh secret set AWS_ROLE_ARN   --body "arn:aws:iam::<account>:role/pdf-qa-github-actions"
gh secret set QDRANT_URL     --body "$(grep ^QDRANT_URL .env     | cut -d= -f2- | tr -d '\"')"
gh secret set QDRANT_API_KEY --body "$(grep ^QDRANT_API_KEY .env | cut -d= -f2- | tr -d '\"')"
gh secret set GEMINI_API_KEY --body "$(grep ^GEMINI_API_KEY .env | cut -d= -f2- | tr -d '\"')"
```

### Ordering is automatic

`frontend.yml` needs the API Gateway endpoint as a CloudFront origin, so the
backend must deploy first. That is handled for you:

```
push to main
   |
   +--> Backend   (backend/** changed)  -> Lambda + API Gateway
   |        |
   |        +-- on success, triggers ------> Frontend -> S3 + CloudFront
   |
   +--> Frontend  (frontend/** changed) -> runs directly
```

`frontend.yml` listens for `workflow_run` on **Backend**, and a `preflight` job
checks whether the Lambda exists. If it does not, the run is **skipped with a
notice rather than failing** — the backend's completion re-triggers it moments
later. So a single push that touches both directories deploys them in the right
order with no red runs and nothing manual.

A frontend-only change still deploys straight away via the `push` trigger,
without waiting on the backend.

> On the very first push, GitHub occasionally does not fire `workflow_run` for a
> workflow it has just registered. If the frontend does not start on its own
> after the backend finishes, run it once by hand:
> `gh workflow run frontend.yml`. Every push after that chains normally.

### The 250MB problem

Lambda's zip limit is **250MB unzipped**. This app does not fit naively:

| | Size |
|---|---|
| Dependencies with `llama-index` | 384 MB |
| After replacing it with `pypdf` | 214 MB |
| Embedding model | 65 MB |

Two changes make it fit:

1. **`pypdf` instead of `llama-index`** — llama-index dragged in pandas,
   sqlalchemy, nltk and aiohttp for what is text extraction plus a splitter.
   `data_loader.py` now does both directly.
2. **The model is not in the zip.** `build_lambda.sh` produces `lambda.zip`
   (188MB unzipped) and `model.tar.gz` (59MB) separately. The model is stored in
   S3, and `data_loader._ensure_model_cache()` pulls it into `/tmp` on the first
   call in a container. Warm invocations skip it.

`PIL` (21MB) and `grpc` (18MB) look removable but are not: fastembed imports PIL
even for text models, and qdrant-client loads 20 grpc modules on the REST path.
Both were checked rather than assumed.

Build locally to check the size before pushing:

```bash
cd backend && ./build_lambda.sh
```

It fails the build if the package exceeds 250MB, so the limit is caught on your
machine rather than at deploy time.

### Frontend hosting

`frontend.yml` publishes the build to a **private** S3 bucket and serves it
through CloudFront. The bucket blocks all public access; only the distribution
can read it, via an Origin Access Control policy scoped to that distribution's
ARN.

The distribution carries two origins:

| Path | Origin | Caching |
|---|---|---|
| `/*` | S3 bucket | `Managed-CachingOptimized` |
| `/api/*` | API Gateway HTTP API | `Managed-CachingDisabled` |

`/api/*` also uses the `Managed-AllViewerExceptHostHeader` origin request
policy. That detail matters: the origin **must not receive CloudFront's `Host`
header**, or API Gateway cannot match the request to its API.

`403` and `404` both return `/index.html` with a `200`, so client-side routes
resolve instead of hitting an S3 error.

Assets are uploaded with `max-age=31536000,immutable` because their filenames
are content-hashed; `index.html` is uploaded `no-cache` so browsers pick up new
asset names immediately. Every deploy invalidates `/*`.

> **First deploy is slow.** A new distribution takes roughly 5-15 minutes to
> propagate. The smoke test waits ~13 minutes and then warns rather than
> failing silently — if it times out, the site is usually fine a few minutes
> later. Subsequent deploys are quick.

`/api/*` is served by an **API Gateway HTTP API** in front of the Lambda, not by
a Lambda Function URL.

> **Why not a Function URL?** It is the simpler design and it is what this
> project used first, but Function URLs returned `403 Forbidden` for every
> caller in this AWS account — in **both** `AuthType: NONE` (with a correct
> `Principal: "*"` resource policy) and `AWS_IAM` (with a CloudFront Origin
> Access Control). The request never reached the function: no CloudWatch logs
> were produced at all. There was no SCP, no organization, and no
> public-access-block API to explain it. API Gateway invokes the Lambda over a
> different path and worked on the first attempt.

API Gateway's free tier is 1M calls for 12 months, then about $1 per million —
pennies at this traffic.

The distribution has **no `CustomErrorResponses`**. An earlier version mapped
403/404 to `/index.html` for single-page-app routing, but those rules apply
distribution-wide, so an API error came back as the HTML page with status 200
and hid the real failure. This app has no client-side routes, so the rules were
never needed.

### Cost

Lambda's free tier (1M requests + 400k GB-seconds/month) is perpetual, as is
CloudFront's (1TB out + 10M requests/month). S3's 5GB is 12 months, after which
the site and artefacts cost cents. Qdrant and Gemini free tiers cover this
workload.

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
- **10MB upload cap.** API Gateway limits request payloads to 10MB (the API
  still enforces 6MB);
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
| Inngest dashboard shows no events | Locally, expected — trigger one manually. On AWS, check both Inngest secrets are set. |
| Upload says "still indexing" and stops | The run failed. Open the Inngest dashboard; the staged PDF expires from `uploads/` after a day. |
| `502 Could not queue ingestion` | The function cannot reach S3 or Inngest. Check the `model-read` role policy covers `uploads/*`. |
| A query failed but you only heard from a user | `aws logs tail /aws/lambda/rag-app-api --since 1h`, or filter `rag/request_failed` on the Inngest dashboard. |
| Lambda times out on first call | Cold start plus model load. Timeout 60s, memory 1024MB. |
| Code changes have no effect | uvicorn without `--reload` keeps old modules in memory. Restart it. |
| `frontend.yml` fails on "function URL not found" | Run `backend.yml` first; the frontend needs it as a CloudFront origin. |
| Smoke test times out on the first frontend deploy | A new distribution needs 5-15 minutes to propagate. Check the URL again shortly. |
| Site returns 403 from CloudFront | The bucket policy step did not run, or the OAC is not attached. Re-run the workflow. |
| `/api/*` returns 403 through CloudFront | API Gateway cannot invoke the Lambda. Check the `AllowAPIGatewayInvoke` permission; re-run `backend.yml`. |
| `/api/*` returns the HTML page instead of JSON | `CustomErrorResponses` are rewriting API errors. They should be `Quantity: 0`. |
| Lambda Function URL returns 403 | Known in this account; that is why the deployment uses API Gateway instead. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The trust policy's `sub` pattern does not match the token. See the note under **One-time AWS setup**. |
| Build fails: "exceeds Lambda's 250MB limit" | A new dependency pushed the package over. See **The 250MB problem**. |
| First request after idle is slow | Cold start pulls the 59MB model from S3 into `/tmp`. Subsequent calls are warm. |
