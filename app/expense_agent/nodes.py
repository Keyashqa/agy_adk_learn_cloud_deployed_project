# ruff: noqa
import base64
import datetime
import json
import re

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.genai import types

from . import config
from .schemas import ApprovalOutcome, Expense, RiskAssessment


# ---------------------------------------------------------------------------
# Security patterns (module-level, compiled once)
# ---------------------------------------------------------------------------

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b")
_CC_RE = re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")

# Phrases that suggest someone is trying to hijack the LLM's behaviour.
# Ordered from most-specific to most-general to reduce false positives.
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(previous|all|the)\s+(instructions?|rules?|context)",
        r"forget\s+(everything|your\s+instructions?|all\s+previous)",
        r"new\s+instructions?\s*:",
        r"you\s+are\s+now\b",
        r"override\s+(the|your|all)\s+\w+",
        r"(must|should|shall)\s+(auto[- ]?approv|approv)\w+\s+this",
        r"\bauto[- ]?approv\w+",
        r"bypass\s+(the|all|your|approval)",
        r"disregard\s+(the|your|all|previous)",
        r"system\s*:\s*",           # "System: new persona"
        r"<\s*/?system\s*>",        # XML-style injection
        r"\[INST\]|\[/INST\]",      # Llama-style control tokens
        r"```\s*system",            # fenced code-block injection
    ]
]


# ---------------------------------------------------------------------------
# Node 1 — parse_expense
# ---------------------------------------------------------------------------

def parse_expense(node_input: types.Content) -> Expense:
    """Decode Pub/Sub envelope or plain JSON, return a typed Expense."""
    raw = node_input.parts[0].text
    payload = json.loads(raw)

    data = payload.get("data", payload)
    if isinstance(data, str):
        # Real Pub/Sub: inner payload is base64-encoded
        data = json.loads(base64.b64decode(data).decode())

    return Expense(**data)


# ---------------------------------------------------------------------------
# Node 2 — route_expense
# ---------------------------------------------------------------------------

def route_expense(node_input: Expense) -> Event:
    """Apply the dollar threshold and select a route.

    Routes:
      "auto_approve"  → amount < APPROVAL_THRESHOLD (no LLM needed)
      "needs_review"  → amount >= APPROVAL_THRESHOLD (goes to security_check)

    Writes the raw expense to state so downstream nodes can reference it.
    The security checkpoint will overwrite expense_json with a scrubbed copy
    before the LlmAgent ever sees it.
    """
    expense_dict = node_input.model_dump()
    route = (
        "auto_approve"
        if node_input.amount < config.APPROVAL_THRESHOLD
        else "needs_review"
    )
    return Event(
        output=expense_dict,
        route=route,
        state={
            "expense": expense_dict,
            "expense_json": json.dumps(expense_dict, indent=2),
        },
    )


# ---------------------------------------------------------------------------
# Node 3a — auto_approve  (terminal, cheap path)
# ---------------------------------------------------------------------------

def auto_approve(node_input: dict):
    """Instantly approve — no LLM involved."""
    outcome = ApprovalOutcome(
        decision="approved",
        decided_by="auto",
        expense=node_input,
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    result_text = json.dumps(outcome.model_dump(), indent=2)
    yield Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"AUTO-APPROVED\n{result_text}")],
        )
    )
    yield Event(output=outcome.model_dump())


# ---------------------------------------------------------------------------
# Node 3b — security_check  (runs before ANY LLM call on the review path)
# ---------------------------------------------------------------------------

def security_check(node_input: dict) -> Event:
    """Security checkpoint: scrub PII and detect prompt-injection.

    Two responsibilities:
      1. Redact SSNs and credit-card numbers from the description.
         Records which PII categories were found in state so the human
         reviewer knows the original contained sensitive data.
      2. Scan the (post-redaction) description for injection patterns.
         If any are found, route "injection" — the LLM is never called.

    The scrubbed expense dict becomes node_input for risk_review so the
    model sees only clean data via both node_input and {expense_json}.

    Routes:
      "clean"     → safe for risk_review
      "injection" → skip LLM, go straight to request_human
    """
    desc = node_input.get("description", "")
    redacted_fields: list[str] = []

    # ── 1. PII scrubbing ─────────────────────────────────────────────────
    if _SSN_RE.search(desc):
        desc = _SSN_RE.sub("[SSN REDACTED]", desc)
        redacted_fields.append("ssn")

    if _CC_RE.search(desc):
        desc = _CC_RE.sub("[CC# REDACTED]", desc)
        redacted_fields.append("credit_card")

    scrubbed = {**node_input, "description": desc}
    scrubbed_json = json.dumps(scrubbed, indent=2)

    # ── 2. Prompt-injection detection (on already-scrubbed text) ─────────
    security_flags: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        m = pattern.search(desc)
        if m:
            security_flags.append(f"matched {pattern.pattern!r} → {m.group(0)!r}")

    if security_flags:
        return Event(
            output=scrubbed,
            route="injection",
            state={
                "expense":           scrubbed,
                # Overwrite expense_json with a hard sentinel so the
                # instruction template can never leak injected text to the LLM.
                "expense_json":      "[REDACTED — SECURITY HOLD]",
                "injection_detected": True,
                "security_flags":    security_flags,
                "redacted_fields":   redacted_fields,
            },
        )

    return Event(
        output=scrubbed,
        route="clean",
        state={
            "expense":            scrubbed,
            "expense_json":       scrubbed_json,   # clean copy for the LLM
            "injection_detected": False,
            "redacted_fields":    redacted_fields,
        },
    )


# ---------------------------------------------------------------------------
# Node 3c — risk_review  (LlmAgent, clean path only)
# ---------------------------------------------------------------------------
# The instruction uses {expense_json} — ADK substitutes from session state.
# security_check guarantees that value is scrubbed before this node runs.

risk_review = LlmAgent(
    name="risk_review",
    model=config.MODEL,
    instruction=(
        "You are an expense risk reviewer. Analyze the expense below for risk factors "
        "such as an amount that seems high for the category, a vague description, "
        "a suspicious vendor, or other policy concerns.\n\n"
        "Expense:\n{expense_json}\n\n"
        "Set risk_level to 'low', 'medium', or 'high'. "
        "List specific, concise flags (e.g. 'amount unusually high for meals'). "
        "Write a single-sentence summary."
    ),
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


# ---------------------------------------------------------------------------
# Node 4 — request_human  (HITL gate, must be wrapped rerun_on_resume=True)
# ---------------------------------------------------------------------------
# Reached from two paths:
#   risk_review   → normal review (risk_assessment in state)
#   security_check → injection hold (injection_detected=True in state)
# Reads everything from ctx.state so the display is correct either way.

async def request_human(ctx: Context, node_input: dict):
    """Pause the workflow and ask a human to approve or reject.

    First pass  → yields RequestInput (workflow suspends).
    On resume   → ctx.resume_inputs["human_decision"] has the human's text.
    """
    if not ctx.resume_inputs:
        expense = ctx.state.get("expense", {})
        injection_detected = ctx.state.get("injection_detected", False)
        redacted = ctx.state.get("redacted_fields", [])
        redacted_note = (
            f"\n  PII scrubbed : {', '.join(redacted)}" if redacted else ""
        )

        if injection_detected:
            flags = ctx.state.get("security_flags", [])
            flags_text = "\n    ".join(flags) if flags else "none"
            msg = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️  SECURITY ALERT — PROMPT INJECTION DETECTED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"  Submitter : {expense.get('submitter')}\n"
                f"  Amount    : ${expense.get('amount')}\n"
                f"  Category  : {expense.get('category')}\n"
                f"  Date      : {expense.get('date')}\n"
                f"{redacted_note}\n"
                f"  Flags     :\n    {flags_text}\n\n"
                "  Description was NOT sent to the AI model.\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Type 'approve' or 'reject':"
            )
        else:
            risk = ctx.state.get("risk_assessment") or {}
            flags_text = ", ".join(risk.get("flags") or ["none"])
            msg = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "EXPENSE REVIEW REQUIRED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"  Submitter : {expense.get('submitter')}\n"
                f"  Amount    : ${expense.get('amount')}\n"
                f"  Category  : {expense.get('category')}\n"
                f"  Date      : {expense.get('date')}\n"
                f"  Notes     : {expense.get('description')}\n"
                f"{redacted_note}\n"
                f"  Risk      : {risk.get('risk_level', '').upper()}\n"
                f"  Summary   : {risk.get('summary', '')}\n"
                f"  Flags     : {flags_text}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Type 'approve' or 'reject':"
            )

        yield RequestInput(interrupt_id="human_decision", message=msg)
        return

    raw = ctx.resume_inputs.get("human_decision", "").strip().lower()
    decision = "approved" if raw == "approve" else "rejected"
    yield Event(output={"decision": decision, "decided_by": "human"})


# ---------------------------------------------------------------------------
# Node 5 — record_decision  (terminal, HITL branch)
# ---------------------------------------------------------------------------

def record_decision(ctx: Context, node_input: dict):
    """Stamp the human verdict and emit the final ApprovalOutcome."""
    expense = ctx.state.get("expense", {})
    risk = ctx.state.get("risk_assessment")
    injection = ctx.state.get("injection_detected", False)

    outcome = ApprovalOutcome(
        decision=node_input.get("decision", "rejected"),
        decided_by=node_input.get("decided_by", "human"),
        expense=expense,
        risk_assessment=risk,
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    result_text = json.dumps(outcome.model_dump(), indent=2)
    label = "APPROVED" if outcome.decision == "approved" else "REJECTED"
    suffix = " [SECURITY HOLD]" if injection else ""

    yield Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"{label}{suffix} (human)\n{result_text}")],
        )
    )
    yield Event(output=outcome.model_dump())
