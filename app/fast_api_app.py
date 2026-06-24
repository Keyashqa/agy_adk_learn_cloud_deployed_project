"""
Ambient expense-approval service.

Accepts Google Cloud Pub/Sub push messages. Each message triggers
the expense-approval workflow automatically — no interactive chat.

Pub/Sub push message shape:
    {
      "message": {
        "data": "<base64(expense_json)>",
        "messageId": "7061114295726791",
        "publishTime": "2026-06-22T10:00:00Z"
      },
      "subscription": "projects/<project>/subscriptions/<name>"
    }

For local testing POST a plain expense JSON object to /pubsub — no
Pub/Sub envelope needed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.expense_agent.agent import app as expense_app

# ---------------------------------------------------------------------------
# Logging — standard Python, no Cloud Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADK runner — one instance shared across all requests
# ---------------------------------------------------------------------------
_runner: InMemoryRunner | None = None

# Hold references so Python doesn't GC tasks before they finish
_active_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _runner
    # InMemoryRunner creates its own in-process session service.
    # otel_to_cloud is not applicable here: we emit no cloud traces locally.
    _runner = InMemoryRunner(app=expense_app)
    logger.info("Expense workflow runner ready (app=%s)", expense_app.name)
    yield
    logger.info("Service shutting down — %d tasks still active", len(_active_tasks))


web_app = FastAPI(title="ambient-expense-agent", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_subscription(subscription: str) -> str:
    """Trim a fully-qualified Pub/Sub path to the short subscription name.

    "projects/my-project/subscriptions/expense-reports-sub"
        → "expense-reports-sub"

    A short name or anything without a '/' is returned unchanged.
    """
    return subscription.rsplit("/", 1)[-1]


async def _run_workflow(user_id: str, session_id: str, expense_text: str) -> None:
    """Drive the expense-approval workflow for one event.

    Called as a background asyncio task so the HTTP handler can return 200
    immediately without waiting for the (potentially long) LLM step.
    """
    assert _runner is not None, "Runner not initialised"

    await _runner.session_service.create_session(
        app_name=expense_app.name,
        user_id=user_id,
        session_id=session_id,
    )
    logger.info("[%s] workflow started (user=%s)", session_id, user_id)

    try:
        async for event in _runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(
                role="user",
                parts=[types.Part.from_text(text=expense_text)],
            ),
        ):
            # Log any text the workflow emits (skip raw function-call parts)
            if event.content:
                for part in event.content.parts or []:
                    if part.text:
                        logger.info(
                            "[%s] %s",
                            session_id,
                            part.text.replace("\n", " ")[:300],
                        )

            # Workflow paused at the HITL gate (request_human node)
            if event.long_running_tool_ids:
                logger.warning(
                    "[%s] ⏸  HITL required — paused waiting for human "
                    "(interrupt_ids=%s)",
                    session_id,
                    list(event.long_running_tool_ids),
                )

            # Final ApprovalOutcome emitted by auto_approve or record_decision
            out = event.output
            if isinstance(out, dict) and "decision" in out:
                logger.info(
                    "[%s] ✅ OUTCOME decision=%s decided_by=%s amount=%s",
                    session_id,
                    out.get("decision"),
                    out.get("decided_by"),
                    out.get("expense", {}).get("amount"),
                )

    except Exception:
        logger.exception("[%s] workflow error", session_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@web_app.post("/pubsub")
async def handle_pubsub(request: Request) -> JSONResponse:
    """Pub/Sub push endpoint.

    Accepts the standard Cloud Pub/Sub push envelope or a plain expense JSON
    object for local testing.  Returns 200 immediately so Pub/Sub won't retry;
    the workflow runs in the background.

    Session naming:
      user_id    = normalized subscription name  (readable, stable per source)
      session_id = Pub/Sub messageId             (unique per message, traceable)
    """
    body: dict[str, Any] = await request.json()

    # Derive a short, readable user identity from the subscription path
    raw_sub = body.get("subscription", "local")
    user_id = _normalize_subscription(raw_sub)

    message = body.get("message")
    if message:
        # ── Real Pub/Sub push envelope ────────────────────────────────────
        # messageId is globally unique per message; use it as the session ID
        session_id = message.get("messageId") or uuid.uuid4().hex[:12]
        raw_b64    = message.get("data", "")
        try:
            expense_text = base64.b64decode(raw_b64).decode()
        except Exception as exc:
            logger.error("[%s] bad Pub/Sub data field: %s", session_id, exc)
            raise HTTPException(status_code=400, detail="Invalid base64 data")
    else:
        # ── Plain JSON for local testing (no Pub/Sub envelope) ────────────
        session_id   = uuid.uuid4().hex[:12]
        expense_text = json.dumps(body)

    logger.info(
        "Received event  subscription=%s session=%s bytes=%d",
        user_id, session_id, len(expense_text),
    )

    # Fire and forget: return 200 before the workflow finishes
    task = asyncio.create_task(
        _run_workflow(user_id, session_id, expense_text)
    )
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)

    return JSONResponse(
        status_code=200,
        content={"status": "accepted", "session_id": session_id, "user_id": user_id},
    )


@web_app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Direct entry-point for `python -m app.fast_api_app`
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(web_app, host="0.0.0.0", port=8080)
