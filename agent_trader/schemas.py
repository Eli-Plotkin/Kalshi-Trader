from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


Action = Literal["buy_yes", "buy_no", "close_position", "hold", "skip"]


class TriageScore(BaseModel):
    ticker: str
    mispriced_score: float = Field(ge=0.0, le=1.0)
    rationale: str


class TriageOutput(BaseModel):
    top_tickers: list[TriageScore]
    skipped_count: int


class ResearchQuestion(BaseModel):
    question: str
    why_it_matters: str


class ResearchPlan(BaseModel):
    questions: list[ResearchQuestion]
    required_datapoints: list[str]
    variables_to_consider: list[str]
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    tool_call_budget: int = Field(ge=1)


class DecisionFramework(BaseModel):
    thresholds: dict[str, float]
    sizing_rule: str
    abort_conditions: list[str]


class Finding(BaseModel):
    question: str
    answer: str
    sources: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


class Findings(BaseModel):
    findings: list[Finding]
    unanswered: list[str]
    tool_calls_used: int


class ExpectedOutcome(BaseModel):
    direction: Literal["up", "down", "flat"]
    eod_price_target_cents: Optional[int] = None
    resolution_price_target_cents: Optional[int] = None
    predicted_resolution: Optional[Literal["yes", "no", "unresolved"]] = None
    confidence: float = Field(ge=0.0, le=1.0)


class Decision(BaseModel):
    action: Action
    size_usd: float = Field(ge=0.0)
    reasoning: str
    framework_criteria_hit: dict[str, bool]
    expected_outcome: ExpectedOutcome


class ReflectionProposal(BaseModel):
    target_prompt: Literal["research_plan", "decision_framework"]
    proposed_filename: str
    diff_summary: str
    body: str
