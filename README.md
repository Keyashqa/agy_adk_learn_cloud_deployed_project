# Ambient Expense Agent

An AI-powered expense approval pipeline built on **Google ADK 2.0** and deployed to **Vertex AI Agent Runtime**. It automatically approves low-value expenses and routes high-value ones through an LLM risk review followed by a human-in-the-loop (HITL) approval gate — all accessible through a web-based Manager Dashboard.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Event Sources                             │
│  agents-cli run  /  Manager Dashboard  /  Pub/Sub Publisher      │
└───────────────────────────┬──────────────────────────────────────┘
                            │  JSON expense payload
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│           Vertex AI Agent Runtime  (us-east1)                    │
│                                                                  │
│  Node 1: parse_expense                                           │
│    └─ Accepts JSON or base64-encoded Pub/Sub envelope            │
│                                                                  │
│  Node 2: route_expense                                           │
│    ├─ amount < $100  →  auto_approve  (no LLM)  ──────────────► DONE
│    └─ amount ≥ $100  →  security_check                           │
│                                                                  │
│  Node 3: security_check                                          │
│    ├─ Scrubs SSN / credit-card numbers from description          │
│    ├─ Detects prompt-injection attempts                          │
│    ├─ injection detected  →  request_human  (security hold)      │
│    └─ clean  →  risk_review                                      │
│                                                                  │
│  Node 4: risk_review  (Gemini 2.5 Flash Lite)                    │
│    └─ Returns risk_level (low/medium/high), flags, summary       │
│                                                                  │
│  Node 5: request_human  ◄── session PAUSES here (HITL gate)     │
│    └─ Resumes when manager approves or rejects                   │
│                                                                  │
│  Node 6: record_decision  →  ApprovalOutcome JSON               │
└──────────────────────────────────────────────────────────────────┘
                            │
                            │  Paused sessions visible here
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│           Manager Dashboard  (Cloud Run — public)                │
│                                                                  │
│  Tab 1 — Pending Approvals                                       │
│    GET  /api/pending   Lists all paused HITL sessions            │
│    POST /api/action    Approve or reject; resumes the session    │
│                                                                  │
│  Tab 2 — Submit Expense                                          │
│    POST /api/submit    Sends a new expense to Agent Runtime      │
└──────────────────────────────────────────────────────────────────┘
```

---

## How It Works

### Expense Payload Format

Every expense submitted to the agent must be a JSON object:

```json
{
  "amount": 250.00,
  "submitter": "Alice",
  "category": "travel",
  "description": "Flight to NYC for client meeting",
  "date": "2026-06-24"
}
```

The agent also accepts a **Pub/Sub-wrapped payload** (base64-encoded `data` field) automatically — `parse_expense` unwraps it transparently.

### Routing Logic

| Condition | Path | LLM called? |
|---|---|---|
| `amount < $100` | Auto-approve immediately | No |
| `amount >= $100`, no security issues | LLM risk review → human decision | Yes |
| `amount >= $100`, prompt injection detected | Skip LLM, human decision (security hold) | No |

### HITL (Human-in-the-Loop)

When a session reaches `request_human`, the Agent Runtime **pauses** it and waits. The session stays suspended in Vertex AI Sessions until a human sends a resume message via the Manager Dashboard or CLI.

Resume payload format (sent by the dashboard on approve/reject):

```json
{
  "role": "user",
  "parts": [{
    "function_response": {
      "id": "human_decision",
      "name": "adk_request_input",
      "response": { "approved": true }
    }
  }]
}
```

---

## Project Structure

```
ambient-expense-agent/
├── app/
│   ├── expense_agent/
│   │   ├── agent.py          # ADK workflow graph (nodes wired together)
│   │   ├── nodes.py          # All node implementations (parse, route, review, HITL)
│   │   ├── schemas.py        # Pydantic models: Expense, RiskAssessment, ApprovalOutcome
│   │   └── config.py         # APPROVAL_THRESHOLD = 100.0, MODEL = gemini-2.5-flash-lite
│   ├── agent_runtime_app.py  # AdkApp entry point (Agent Runtime wrapper)
│   ├── fast_api_app.py       # Local FastAPI server (for playground)
│   └── app_utils/            # Deploy helpers and utilities
├── submission_frontend/
│   ├── main.py               # Manager Dashboard (FastAPI, 3 endpoints + embedded UI)
│   ├── Dockerfile            # Container image for Cloud Run deployment
│   └── pyproject.toml        # Dashboard dependencies
├── tests/
│   ├── unit/                 # Unit tests for node logic
│   └── integration/          # Integration tests against Agent Runtime
├── deployment_metadata.json  # Deployed engine ID written by agents-cli deploy
├── pyproject.toml            # Agent project dependencies (google-adk[gcp])
└── .claude/skills/
    └── ambient-agent-pipeline/SKILL.md   # Deployment runbook for this stack
```

---

## Cloud Deployment (What Was Built)

### Agent Runtime

The agent is deployed to **Vertex AI Agent Runtime** in `us-east1`:

```
Resource ID: projects/1064204015476/locations/us-east1/reasoningEngines/5773720275405242368
```

Deployed using `agents-cli deploy`. The runtime wraps the ADK workflow in an `AdkApp`, which exposes a `:query` REST endpoint and manages session state via `VertexAiSessionService`.

### Manager Dashboard

A standalone **FastAPI** service deployed to **Cloud Run** in `us-east1`:

- Public (no authentication required)
- Polls Agent Runtime for paused sessions every 30 seconds
- Lets managers approve or reject from a browser — no CLI needed

The dashboard uses `google-cloud-aiplatform[agent-engines]` to call the Agent Runtime Python SDK (`agent_engines.get()` + `stream_query()`).

### Pub/Sub Event Pipeline (Optional)

When you want expenses submitted via an event-driven pipeline rather than direct API calls:

```
Publisher → [expense-reports topic]
               └── expense-reports-push subscription
                     └── OIDC-authenticated push → Agent Runtime :query
                               │ on 5 failures
                               ▼
                     [expense-reports-dead-letter topic]
```

Key properties of the subscription:
- **Auth**: OIDC token minted for `pubsub-invoker` service account
- **Ack deadline**: 600 seconds (10 min) — allows time for LLM + session setup
- **Dead-letter**: after 5 failed delivery attempts
- **Payload**: `--push-no-wrapper` sends raw JSON directly to the endpoint

> Note: the Agent Runtime `:query` REST endpoint expects the body in the format
> `{"class_method": "stream_query", "input": {"user_id": "...", "message": "..."}}`.
> A Cloud Run processor is needed between Pub/Sub and Agent Runtime to reshape
> the raw expense JSON into that format.

---

## Local Development

### Prerequisites

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install agents-cli
uv tool install google-agents-cli

# Authenticate with Google Cloud
gcloud auth application-default login
gcloud config set project mcp-test-487013
```

### Run the Agent Locally

```bash
agents-cli install        # install dependencies
agents-cli playground     # interactive local server at http://localhost:8080
```

### Run the Manager Dashboard Locally

```bash
cd submission_frontend
uv run uvicorn main:app --reload --port 8080
# Open http://localhost:8080
```

Set these env vars (or create a `.env` file in `submission_frontend/`):

```
GOOGLE_CLOUD_PROJECT=mcp-test-487013
GOOGLE_CLOUD_LOCATION=us-east1
AGENT_RUNTIME_ID=projects/1064204015476/locations/us-east1/reasoningEngines/5773720275405242368
```

---

## Testing the Deployed Agent

```bash
# Auto-approve path (amount < $100 — no LLM, instant)
agents-cli run \
  '{"amount": 45.00, "submitter": "Alice", "category": "meals", "description": "Lunch", "date": "2026-06-24"}' \
  --url "https://us-east1-aiplatform.googleapis.com/v1/projects/1064204015476/locations/us-east1/reasoningEngines/5773720275405242368" \
  --mode adk

# Human-review path (amount >= $100 — LLM risk review + HITL gate)
agents-cli run \
  '{"amount": 250.00, "submitter": "Alice", "category": "travel", "description": "Flight to NYC", "date": "2026-06-24"}' \
  --url "https://us-east1-aiplatform.googleapis.com/v1/projects/1064204015476/locations/us-east1/reasoningEngines/5773720275405242368" \
  --mode adk
```

Use **single quotes** around the JSON — double quotes cause shell to expand `$amount` as an environment variable.

After submitting a high-value expense, open the Manager Dashboard to approve or reject it.

---

## Commands Reference

| Command | Purpose |
|---|---|
| `agents-cli playground` | Interactive local testing |
| `agents-cli deploy` | Deploy agent to Vertex AI Agent Runtime |
| `agents-cli deploy --dry-run` | Validate config without deploying |
| `agents-cli deploy --status` | Check status of a running deployment |
| `uv run pytest tests/unit tests/integration` | Run all tests |
| `agents-cli eval generate` | Run agent against eval dataset |
| `agents-cli eval grade` | Score evaluation traces |
| `agents-cli lint` | Check code quality |

---

## Security Features

The agent has three layers of protection before any LLM is invoked:

1. **Dollar threshold routing** — expenses under $100 never touch the LLM at all.
2. **PII scrubbing** — SSNs and credit card numbers in the description are redacted before the expense is sent to the model. The human reviewer is told which PII categories were found.
3. **Prompt-injection detection** — the description is scanned for patterns like "ignore previous instructions", "auto-approve this", XML control tokens, etc. If detected, the LLM is skipped entirely and the expense goes straight to a human with a security hold flag.

---

## Environment Variables

| Variable | Where used | Value |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | Agent + Dashboard | `mcp-test-487013` |
| `GOOGLE_CLOUD_LOCATION` | Agent + Dashboard | `us-east1` |
| `GOOGLE_GENAI_USE_VERTEXAI` | Agent | `True` |
| `AGENT_RUNTIME_ID` | Dashboard | `projects/1064204015476/locations/us-east1/reasoningEngines/5773720275405242368` |

---

## Re-deploying the Dashboard

```bash
# Build and push image
docker build -t expense-manager-dashboard:latest ./submission_frontend
gcloud auth configure-docker us-east1-docker.pkg.dev --quiet
docker tag expense-manager-dashboard:latest \
  us-east1-docker.pkg.dev/mcp-test-487013/cloud-run-source-deploy/expense-manager-dashboard:latest
docker push \
  us-east1-docker.pkg.dev/mcp-test-487013/cloud-run-source-deploy/expense-manager-dashboard:latest

# Deploy
gcloud run deploy expense-manager-dashboard \
  --image us-east1-docker.pkg.dev/mcp-test-487013/cloud-run-source-deploy/expense-manager-dashboard:latest \
  --region us-east1 \
  --project mcp-test-487013 \
  --allow-unauthenticated \
  --port 8080 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=mcp-test-487013,GOOGLE_CLOUD_LOCATION=us-east1,AGENT_RUNTIME_ID=projects/1064204015476/locations/us-east1/reasoningEngines/5773720275405242368"
```

For more details on the full deployment process, known gotchas, and Pub/Sub pipeline setup, see `.claude/skills/ambient-agent-pipeline/SKILL.md`.
