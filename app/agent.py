# ruff: noqa
# Thin shim — agents-cli expects app.agent to export `app` and `root_agent`.
# All logic lives in app/expense_agent/.
from app.expense_agent.agent import app, root_agent

__all__ = ["app", "root_agent"]
