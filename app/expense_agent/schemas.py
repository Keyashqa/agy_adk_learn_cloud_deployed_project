from __future__ import annotations

from pydantic import BaseModel


class Expense(BaseModel):
    amount: float
    submitter: str
    category: str
    description: str
    date: str


class RiskAssessment(BaseModel):
    risk_level: str          # "low" | "medium" | "high"
    flags: list[str]
    summary: str


class ApprovalOutcome(BaseModel):
    decision: str            # "approved" | "rejected"
    decided_by: str          # "auto" | "human"
    expense: dict
    risk_assessment: dict | None = None
    timestamp: str
