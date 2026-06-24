---
name: adk-agent-runtime-deploy
description: >
  Step-by-step deployment guide for ADK agents to Google Agent Runtime
  (formerly Vertex AI Agent Engine), based on real deployment experience.
  Covers scaffolding production files, locking dependencies, dry-run,
  deploying, testing, and teardown — including known gotchas and fixes.
metadata:
  author: Karthikeyan TS
  version: 1.0.0
  requires:
    bins:
      - agents-cli
      - uv
      - gcloud
    install: "uv tool install google-agents-cli"
---

# ADK Agent Runtime Deployment Guide

A practical, battle-tested guide for deploying ADK agents to Google Agent Runtime.
Captures real issues encountered and their fixes.

---

## Prerequisites

```bash
uv tool install google-agents-cli   # install agents-cli
gcloud auth login                    # gcloud CLI auth
gcloud auth application-default login  # ADC — REQUIRED for deploy (see Gotcha #2)
```

---

## Step 1 — Scaffold Production Files

If the project was created with `--prototype` (no deployment target), add Agent Runtime support:

```bash
agents-cli scaffold enhance . --deployment-target agent_runtime
```

What gets generated:
- `app/agent_runtime_app.py` — Agent Runtime entry point (`AgentEngineApp`)
- `deployment/terraform/single-project/` — Terraform for IAM, service accounts, storage, telemetry
- `deployment/terraform/single-project/vars/env.tfvars` — fill in your GCP project ID
- `deployment_metadata.json` — tracks the deployed engine ID
- `tests/integration/test_agent_runtime_app.py` — integration tests

After enhancing, update `.env`:
```
GOOGLE_GENAI_USE_VERTEXAI=True
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-east1
```

---

## Step 2 — Lock Dependencies

```bash
uv lock
```

The enhance step adds new packages (`google-cloud-aiplatform[agent-engines]`, `protobuf`, etc.).
Always relock after scaffold enhance to capture transitive deps.

---

## Step 3 — Dry Run

```bash
agents-cli deploy --dry-run --project <PROJECT_ID> --no-confirm-project
```

Validates config and prints deployment parameters without touching the cloud.
Also auto-fills `env.tfvars` with the project ID.

---

## Step 4 — Deploy

```bash
# Start deployment (non-blocking — Agent Runtime takes 5-10 min)
agents-cli deploy --project <PROJECT_ID> --no-confirm-project --no-wait

# Poll until done
agents-cli deploy --status --project <PROJECT_ID> --no-confirm-project
```

On success you get:
```
✅ Deployment successful!
Agent Runtime ID: projects/<PROJECT_NUMBER>/locations/<REGION>/reasoningEngines/<ID>
```

The `deployment_metadata.json` is updated with the engine ID automatically.

---

## Step 5 — Test the Deployed Agent

```bash
agents-cli run '<your_prompt_or_json>' \
  --url "https://<REGION>-aiplatform.googleapis.com/v1/projects/<PROJECT_NUMBER>/locations/<REGION>/reasoningEngines/<ENGINE_ID>" \
  --mode adk
```

Add `--verbose` to see full event payloads (node routing, state deltas, outputs).

To test with session continuity:
```bash
agents-cli run "follow-up message" \
  --url "<same-url>" \
  --mode adk \
  --session-id <SESSION_ID_FROM_PREVIOUS_RUN>
```

---

## Step 6 — Teardown

`agents-cli` has no delete command. Use the vertexai SDK:

```bash
uv run python -c "
import vertexai
from vertexai import agent_engines

vertexai.init(project='<PROJECT_ID>', location='<REGION>')
engine = agent_engines.get('projects/<PROJECT_NUMBER>/locations/<REGION>/reasoningEngines/<ENGINE_ID>')
engine.delete(force=True)  # force=True removes child sessions too
print('Deleted.')
"
```

> `force=True` is required if the engine has sessions (created by test runs).
> Without it you get a `FailedPrecondition: contains child resources: sessions` error.

---

## Known Gotchas

### Gotcha 1 — Non-standard project layout: `ModuleNotFoundError`

**Symptom:**
```
ModuleNotFoundError: No module named 'expense_agent'
```

**Cause:**
`agents-cli deploy` only bundles the `agent_directory` (default: `app/`) as the source package.
Any package living outside `app/` at the project root is NOT included in the deployment bundle.

**Fix:**
Move the package inside `app/`:
```
expense_agent/          →   app/expense_agent/
```
Update absolute imports:
```python
# Before
from expense_agent.agent import app, root_agent
# After
from app.expense_agent.agent import app, root_agent
```
Relative imports inside the package (`from . import nodes`) need no changes.

Verify locally before redeploying:
```bash
uv run python -c "from app.expense_agent.agent import app; print(app.name)"
```

---

### Gotcha 2 — ADC not configured: `DefaultCredentialsError`

**Symptom:**
```
google.auth.exceptions.DefaultCredentialsError: Your default credentials were not found.
```

**Cause:**
`gcloud auth login` authenticates the CLI but does NOT set up Application Default Credentials.
`agents-cli deploy` needs ADC separately.

**Fix:**
```bash
gcloud auth application-default login
```
Run this once interactively. After login, deploy works normally.

---

### Gotcha 3 — `agents-cli run` has no `--project` flag

**Symptom:**
```
Error: No such option: '--project'
```

**Fix:**
`agents-cli run` uses `--url` instead. Always pass the full Agent Runtime resource URL:
```bash
agents-cli run "prompt" \
  --url "https://<REGION>-aiplatform.googleapis.com/v1/projects/<PROJECT_NUMBER>/locations/<REGION>/reasoningEngines/<ENGINE_ID>" \
  --mode adk
```

---

### Gotcha 4 — Empty agent response

**Symptom:**
Agent returns `[agent_name]:` with no text.

**Cause:**
The agent may return structured output (dict/object) rather than text. The CLI only prints `content.parts[].text`.

**Fix:**
Add `--verbose` to see all event payloads including `output` fields:
```bash
agents-cli run "prompt" --url "<url>" --mode adk --verbose
```

---

## Quick Reference

| Task | Command |
|------|---------|
| Add Agent Runtime to prototype | `agents-cli scaffold enhance . --deployment-target agent_runtime` |
| Lock deps | `uv lock` |
| Dry run | `agents-cli deploy --dry-run --project <ID> --no-confirm-project` |
| Deploy (non-blocking) | `agents-cli deploy --project <ID> --no-confirm-project --no-wait` |
| Check deploy status | `agents-cli deploy --status --project <ID> --no-confirm-project` |
| Test deployed agent | `agents-cli run "<prompt>" --url <url> --mode adk` |
| Delete engine | `engine.delete(force=True)` via vertexai SDK |
