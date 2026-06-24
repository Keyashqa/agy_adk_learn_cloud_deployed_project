from google.adk.apps import App
from google.adk.workflow import FunctionNode, Workflow

from . import nodes

# request_human must rerun on resume so ctx.resume_inputs is populated
# when the human's reply arrives (see nodes.py for the two-pass logic).
_request_human = FunctionNode(func=nodes.request_human, rerun_on_resume=True)

root_agent = Workflow(
    name="expense_approval",
    edges=[
        # ── parse & route ───────────────────────────────────────────────────
        ("START",              nodes.parse_expense),
        (nodes.parse_expense,  nodes.route_expense),

        # ── threshold split ─────────────────────────────────────────────────
        #    "auto_approve" → under $100, done immediately
        #    "needs_review" → $100+, must pass security checkpoint first
        (nodes.route_expense, {
            "auto_approve": nodes.auto_approve,
            "needs_review": nodes.security_check,
        }),

        # ── security checkpoint ─────────────────────────────────────────────
        #    "clean"     → scrubbed expense safe for LLM → risk_review
        #    "injection" → skip LLM entirely, go straight to human
        (nodes.security_check, {
            "clean":     nodes.risk_review,
            "injection": _request_human,
        }),

        # ── LLM review → human gate → record ───────────────────────────────
        (nodes.risk_review,    _request_human),
        (_request_human,       nodes.record_decision),
    ],
)

app = App(root_agent=root_agent, name="app")




