---
name: ambient-agent-pipeline
description: >
  End-to-end guide for building a HITL Manager Dashboard and a Pub/Sub
  event pipeline in front of a deployed ADK Agent Runtime agent.
  Covers FastAPI dashboard scaffolding, Cloud Run deployment, Pub/Sub
  topic + push subscription setup, and IAM wiring — including every
  real gotcha encountered in production.
metadata:
  author: Karthikeyan TS
  version: 1.0.0
  requires:
    bins:
      - gcloud
      - docker
      - uv
    apis:
      - run.googleapis.com
      - pubsub.googleapis.com
      - cloudbuild.googleapis.com
      - artifactregistry.googleapis.com
      - aiplatform.googleapis.com
---

# Ambient Agent Pipeline — HITL Dashboard + Pub/Sub Event Pipeline

A battle-tested guide for wiring an ADK Agent Runtime agent into a
production-grade event pipeline with a manager approval dashboard.
Built and validated on the `ambient-expense-agent` project.

---

## Architecture Overview

```
Publisher (CLI / webhook / scheduler)
        │
        ▼
[Pub/Sub: expense-reports topic]
        │
        └── push subscription (OIDC auth, --push-no-wrapper)
                │
                ▼ (needs Cloud Run processor to reshape payload)
        Agent Runtime :query REST API
                │
                ├── auto-approve path  (< threshold, no HITL)
                │
                └── HITL path  (session paused, awaiting human)
                        │
                        ▼
        [Manager Dashboard — Cloud Run]
          GET /api/pending   →  VertexAiSessionService.list_sessions()
          POST /api/action   →  engine.stream_query() with resume payload
                        │
                        ▼
        [expense-reports-dead-letter topic]  ← failed messages after N attempts
```

---

## Part 1 — Manager Dashboard (FastAPI + Cloud Run)

### 1.1 Scaffold `submission_frontend/`

Create `submission_frontend/` as a **standalone** service — separate from
the agent project. Minimum files:

```
submission_frontend/
├── main.py            ← FastAPI app (3 endpoints + embedded HTML)
├── pyproject.toml
├── Dockerfile
└── .gcloudignore
```

### 1.2 pyproject.toml

```toml
[project]
name = "expense-manager-dashboard"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.100.0",
    "uvicorn[standard]>=0.20.0",
    "google-adk[gcp]>=2.0.0,<3.0.0",
    "google-cloud-aiplatform[agent-engines]>=1.156.0",
    "python-dotenv>=1.0.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
only-include = ["main.py"]      # ← REQUIRED — hatchling can't auto-detect flat layout
```

> **Gotcha #1 — hatchling flat layout:** Without `only-include = ["main.py"]`,
> `uv run uvicorn main:app` fails with:
> `ValueError: Unable to determine which files to ship inside the wheel`.

### 1.3 Three FastAPI Endpoints

#### GET `/` — Dashboard HTML
Serve the embedded manager UI (glassmorphism dark theme, Inter font,
tab nav: Pending Approvals + Submit Expense).

#### GET `/api/pending` — List Paused Sessions

```python
# VertexAiSessionService methods are ASYNC COROUTINES — await directly.
# NEVER wrap in asyncio.to_thread() — it returns the coroutine unawaited.

raw = await session_service.list_sessions(app_name=AGENT_RUNTIME_ID)
# No user_id filter — list all sessions across all users.

full = await session_service.get_session(
    app_name=AGENT_RUNTIME_ID,
    user_id=session.user_id,   # use the value FROM the session object
    session_id=session.id,
)
```

Detect pending HITL by scanning events for unresolved `adk_request_input`
function calls:

```python
def _find_pending(events):
    resolved = set()
    for ev in events:
        for p in (ev.content and ev.content.parts or []):
            fr = getattr(p, "function_response", None)
            if fr and fr.name == "adk_request_input":
                resolved.add(fr.id)

    calls = []
    for ev in events:
        for p in (ev.content and ev.content.parts or []):
            fc = getattr(p, "function_call", None)
            if fc and fc.name == "adk_request_input" and fc.id not in resolved:
                calls.append({"interrupt_id": fc.id})
    return calls
```

> **Gotcha #2 — user_id is `'cli-user'`, not `'default-user'`:**
> `agents-cli run` creates sessions with `user_id='cli-user'`.
> Filtering `list_sessions(user_id="default-user")` returns zero results.
> Always read `user_id` from the session object and pass it through.

#### POST `/api/action/{session_id}` — Resume Paused Session

Pass the resume payload **directly as the dict value** of `message=`.
Use the `user_id` that owns the session (from the pending item, not hardcoded):

```python
message = {
    "role": "user",
    "parts": [{
        "function_response": {
            "id": interrupt_id,
            "name": "adk_request_input",
            "response": {"approved": decision == "approve"},
        }
    }],
}

def _stream():
    engine = agent_engines.get(AGENT_RUNTIME_ID)
    parts = []
    for event in engine.stream_query(
        user_id=user_id,        # from request body — must match session owner
        session_id=session_id,
        message=message,        # pass dict directly, not JSON string
    ):
        if event.content and event.content.parts:
            for p in event.content.parts:
                if getattr(p, "text", None):
                    parts.append(p.text)
    return parts

text_parts = await asyncio.to_thread(_stream)
```

#### POST `/api/submit` — Submit New Expense

```python
expense_json = json.dumps(body)   # parse_expense() expects a JSON string

def _stream():
    engine = agent_engines.get(AGENT_RUNTIME_ID)
    ...
    for event in engine.stream_query(user_id="cli-user", message=expense_json):
        ...

text_parts = await asyncio.to_thread(_stream)
status = "auto_approved" if text_parts else "pending_review"
```

### 1.4 Environment Variables

| Variable | Example value |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | `mcp-test-487013` |
| `GOOGLE_CLOUD_LOCATION` | `us-east1` |
| `AGENT_RUNTIME_ID` | `projects/PROJECT_NUMBER/locations/REGION/reasoningEngines/ENGINE_ID` |

### 1.5 Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml main.py ./
RUN uv pip install --system --no-cache \
    "fastapi>=0.100.0" \
    "uvicorn[standard]>=0.20.0" \
    "google-adk[gcp]>=2.0.0,<3.0.0" \
    "google-cloud-aiplatform[agent-engines]>=1.156.0" \
    "python-dotenv>=1.0.0"
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### 1.6 .gcloudignore

```
.gcloudignore
.git
.venv
__pycache__
*.pyc
.env
```

---

## Part 2 — Deploy Dashboard to Cloud Run

### Step 1 — Build Docker image locally

> **Gotcha #3 — Cloud Build storage permissions:** When `run.googleapis.com`
> and `cloudbuild.googleapis.com` are newly enabled, the default compute SA
> lacks `storage.objects.get` on the Cloud Build staging bucket.
> `gcloud run deploy --source` fails with a 403.
>
> Fix — grant before deploying (or build locally and push):
> ```bash
> gcloud projects add-iam-policy-binding PROJECT_ID \
>   --member "serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
>   --role "roles/storage.objectAdmin"
> ```
>
> Alternatively, build locally and push to bypass Cloud Build entirely:

```bash
# Build locally
docker build -t SERVICE_NAME:latest ./submission_frontend

# Configure Docker auth
gcloud auth configure-docker REGION-docker.pkg.dev --quiet

# Tag and push
docker tag SERVICE_NAME:latest \
  REGION-docker.pkg.dev/PROJECT_ID/cloud-run-source-deploy/SERVICE_NAME:latest
docker push REGION-docker.pkg.dev/PROJECT_ID/cloud-run-source-deploy/SERVICE_NAME:latest
```

### Step 2 — Deploy from image

```bash
gcloud run deploy expense-manager-dashboard \
  --image REGION-docker.pkg.dev/PROJECT_ID/cloud-run-source-deploy/SERVICE_NAME:latest \
  --region REGION \
  --project PROJECT_ID \
  --allow-unauthenticated \
  --port 8080 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=PROJECT_ID,GOOGLE_CLOUD_LOCATION=REGION,AGENT_RUNTIME_ID=projects/PROJECT_NUMBER/locations/REGION/reasoningEngines/ENGINE_ID"
```

### Step 3 — Grant Cloud Run SA the Agent Runtime roles

```bash
# Get the runtime SA assigned to the service
SA=$(gcloud run services describe SERVICE_NAME \
  --region REGION --project PROJECT_ID \
  --format 'value(spec.template.spec.serviceAccountName)')

# Grant Vertex AI User (covers list_sessions, get_session, stream_query)
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member "serviceAccount:${SA}" \
  --role "roles/aiplatform.user" \
  --condition=None
```

---

## Part 3 — Pub/Sub Event Pipeline

### Step 1 — Create topics

```bash
# Main ingestion topic
gcloud pubsub topics create expense-reports --project PROJECT_ID

# Dead-letter topic (failed messages land here after N attempts)
gcloud pubsub topics create expense-reports-dead-letter --project PROJECT_ID
```

### Step 2 — Create `pubsub-invoker` service account

```bash
gcloud iam service-accounts create pubsub-invoker \
  --display-name "Pub/Sub Agent Runtime Invoker" \
  --project PROJECT_ID

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member "serviceAccount:pubsub-invoker@PROJECT_ID.iam.gserviceaccount.com" \
  --role "roles/aiplatform.user" \
  --condition=None
```

### Step 3 — Wire dead-letter IAM (easy to miss — do this BEFORE creating the subscription)

> **Gotcha #4 — Dead-letter forwarding silently fails without these two bindings:**
> Pub/Sub uses its own managed SA to forward failed messages. Without publisher
> on the DL topic AND subscriber on the source subscription, the forward fails
> and messages are dropped — silently.

```bash
PUBSUB_SA="service-PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com"

# Publisher on dead-letter topic
gcloud pubsub topics add-iam-policy-binding expense-reports-dead-letter \
  --member "serviceAccount:${PUBSUB_SA}" \
  --role "roles/pubsub.publisher" \
  --project PROJECT_ID

# Subscriber on source subscription (grant AFTER creating the subscription below)
gcloud pubsub subscriptions add-iam-policy-binding expense-reports-push \
  --member "serviceAccount:${PUBSUB_SA}" \
  --role "roles/pubsub.subscriber" \
  --project PROJECT_ID
```

### Step 4 — Create OIDC-authenticated push subscription

```bash
gcloud pubsub subscriptions create expense-reports-push \
  --topic expense-reports \
  --push-endpoint "https://REGION-aiplatform.googleapis.com/v1/projects/PROJECT_NUMBER/locations/REGION/reasoningEngines/ENGINE_ID:query" \
  --push-auth-service-account "pubsub-invoker@PROJECT_ID.iam.gserviceaccount.com" \
  --push-no-wrapper \
  --ack-deadline 600 \
  --dead-letter-topic "projects/PROJECT_ID/topics/expense-reports-dead-letter" \
  --max-delivery-attempts 5 \
  --project PROJECT_ID
```

Flag reference:

| Flag | Purpose |
|---|---|
| `--push-auth-service-account` | Pub/Sub mints an OIDC token for this SA on every push |
| `--push-no-wrapper` | Strips Pub/Sub envelope; raw base64-decoded body goes to endpoint |
| `--ack-deadline 600` | 10 min — needed for LLM calls + HITL session setup |
| `--max-delivery-attempts 5` | After 5 failures, forward to dead-letter topic |

> **Gotcha #5 — Payload format for Agent Runtime `:query`:**
> With `--push-no-wrapper`, the raw message body hits the endpoint directly.
> Agent Runtime `:query` expects:
> ```json
> {"class_method": "stream_query", "input": {"user_id": "...", "message": "..."}}
> ```
> A raw expense JSON payload will not match this format. Insert a lightweight
> Cloud Run processor between Pub/Sub and Agent Runtime to reshape the message,
> or publish pre-shaped payloads to the topic at the source.

---

## Known Gotchas Summary

| # | Symptom | Root Cause | Fix |
|---|---|---|---|
| 1 | `hatchling ValueError` on `uv run` | Flat layout — no package dir matches project name | Add `[tool.hatch.build.targets.wheel]\nonly-include = ["main.py"]` |
| 2 | `/api/pending` returns empty list | `asyncio.to_thread()` wrapping async coroutine returns unawaited object | `await session_service.list_sessions()` directly |
| 3 | Sessions not found with `user_id="default-user"` | `agents-cli run` creates sessions with `user_id="cli-user"` | Read `user_id` from session object; don't hardcode |
| 4 | `gcloud run deploy --source` fails with 403 | Compute SA lacks storage.objects.get on staging bucket | Grant `roles/storage.objectAdmin` to compute SA, or build and push locally |
| 5 | Dead-letter messages silently dropped | Pub/Sub managed SA missing publisher on DL topic or subscriber on subscription | Grant both before testing |
| 6 | `$45` becomes `5` in shell | Shell expands `$4` as env var inside double quotes | Use **single quotes** for JSON payloads in CLI: `'{"amount": 45}'` |

---

## Quick Reference

```bash
# Run dashboard locally
cd submission_frontend
uv run uvicorn main:app --reload --port 8080

# Rebuild and redeploy after code changes
docker build -t expense-manager-dashboard:latest ./submission_frontend
docker tag expense-manager-dashboard:latest \
  REGION-docker.pkg.dev/PROJECT_ID/cloud-run-source-deploy/expense-manager-dashboard:latest
docker push REGION-docker.pkg.dev/PROJECT_ID/cloud-run-source-deploy/expense-manager-dashboard:latest
gcloud run deploy expense-manager-dashboard \
  --image REGION-docker.pkg.dev/PROJECT_ID/cloud-run-source-deploy/expense-manager-dashboard:latest \
  --region REGION --project PROJECT_ID

# Get live Cloud Run URL
gcloud run services describe expense-manager-dashboard \
  --region REGION --project PROJECT_ID \
  --format "value(status.url)"

# Publish a test expense event to Pub/Sub
gcloud pubsub topics publish expense-reports \
  --message '{"amount": 250.00, "submitter": "Alice", "category": "travel", "description": "Flight to NYC", "date": "2026-06-24"}' \
  --project PROJECT_ID

# Inspect dead-letter messages
gcloud pubsub subscriptions pull expense-reports-dead-letter-sub \
  --auto-ack --limit 10 --project PROJECT_ID

# List all sessions for the agent
uv run python -c "
import asyncio, vertexai
from google.adk.sessions import VertexAiSessionService
async def main():
    vertexai.init(project='PROJECT_ID', location='REGION')
    svc = VertexAiSessionService(project='PROJECT_ID', location='REGION')
    r = await svc.list_sessions(app_name='AGENT_RUNTIME_ID')
    for s in r.sessions:
        print(s.id, s.user_id, list(s.state.keys()))
asyncio.run(main())
"
```
