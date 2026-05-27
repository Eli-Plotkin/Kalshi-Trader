"""Tests for agent_trader.schemas — Pydantic validation contracts.

The schemas are the trust boundary between LLM output and downstream code.
If an LLM returns a malformed/out-of-range value, Pydantic must reject it
before the executor sees it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_trader.schemas import (
    CoarseFilterDecision,
    Decision,
    DecisionFramework,
    ExpectedOutcome,
    Finding,
    Findings,
    ReflectionProposal,
    ResearchPlan,
    ResearchQuestion,
    TriageOutput,
    TriageScore,
)


# ----------------------------------------------------------------------------
# CoarseFilterDecision
# ----------------------------------------------------------------------------


class TestCoarseFilterDecision:
    def test_valid_keep(self):
        d = CoarseFilterDecision(keep=True, reason="liquid moneyline")
        assert d.keep is True
        assert d.reason == "liquid moneyline"

    def test_valid_drop(self):
        d = CoarseFilterDecision(keep=False, reason="props market — out of scope")
        assert d.keep is False

    def test_missing_keep_field(self):
        with pytest.raises(ValidationError):
            CoarseFilterDecision(reason="incomplete")


# ----------------------------------------------------------------------------
# TriageScore / TriageOutput — score must be in [0, 1]
# ----------------------------------------------------------------------------


class TestTriageScore:
    def test_valid(self):
        s = TriageScore(ticker="KX-A", mispriced_score=0.75, rationale="cheap fav")
        assert s.mispriced_score == 0.75

    def test_score_above_one_rejected(self):
        with pytest.raises(ValidationError):
            TriageScore(ticker="KX-A", mispriced_score=1.5, rationale="x")

    def test_score_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            TriageScore(ticker="KX-A", mispriced_score=-0.1, rationale="x")

    def test_boundary_values_accepted(self):
        TriageScore(ticker="KX-A", mispriced_score=0.0, rationale="zero")
        TriageScore(ticker="KX-A", mispriced_score=1.0, rationale="one")


class TestTriageOutput:
    def test_valid_with_zero_top_tickers(self):
        out = TriageOutput(top_tickers=[], skipped_count=10)
        assert out.top_tickers == []
        assert out.skipped_count == 10

    def test_with_multiple_tickers(self):
        out = TriageOutput(
            top_tickers=[
                TriageScore(ticker="A", mispriced_score=0.9, rationale="x"),
                TriageScore(ticker="B", mispriced_score=0.85, rationale="y"),
            ],
            skipped_count=20,
        )
        assert len(out.top_tickers) == 2


# ----------------------------------------------------------------------------
# ResearchPlan
# ----------------------------------------------------------------------------


class TestResearchPlan:
    def _question(self):
        return ResearchQuestion(question="What is X?", why_it_matters="Drives Y")

    def test_valid(self):
        plan = ResearchPlan(
            questions=[self._question()],
            required_datapoints=["dp1"],
            variables_to_consider=["v1"],
            confidence_threshold=0.7,
            tool_call_budget=5,
        )
        assert plan.confidence_threshold == 0.7
        assert plan.tool_call_budget == 5

    def test_confidence_threshold_out_of_range(self):
        with pytest.raises(ValidationError):
            ResearchPlan(
                questions=[],
                required_datapoints=[],
                variables_to_consider=[],
                confidence_threshold=1.5,
                tool_call_budget=1,
            )

    def test_tool_call_budget_below_one_rejected(self):
        with pytest.raises(ValidationError):
            ResearchPlan(
                questions=[],
                required_datapoints=[],
                variables_to_consider=[],
                confidence_threshold=0.5,
                tool_call_budget=0,
            )


# ----------------------------------------------------------------------------
# Finding / Findings
# ----------------------------------------------------------------------------


class TestFinding:
    def test_valid(self):
        f = Finding(
            question="q", answer="a", sources=["s1"], confidence=0.8
        )
        assert f.confidence == 0.8

    def test_confidence_out_of_range(self):
        with pytest.raises(ValidationError):
            Finding(question="q", answer="a", sources=[], confidence=2.0)


class TestFindings:
    def test_empty_findings(self):
        f = Findings(findings=[], unanswered=["q1"], tool_calls_used=0)
        assert f.findings == []
        assert f.unanswered == ["q1"]


# ----------------------------------------------------------------------------
# ExpectedOutcome
# ----------------------------------------------------------------------------


class TestExpectedOutcome:
    def test_valid_minimal(self):
        eo = ExpectedOutcome(direction="up", confidence=0.7)
        assert eo.direction == "up"
        assert eo.eod_price_target_cents is None

    def test_invalid_direction(self):
        with pytest.raises(ValidationError):
            ExpectedOutcome(direction="sideways", confidence=0.7)  # type: ignore[arg-type]

    def test_valid_resolution_yes(self):
        eo = ExpectedOutcome(
            direction="up", confidence=0.7, predicted_resolution="yes"
        )
        assert eo.predicted_resolution == "yes"

    def test_invalid_resolution(self):
        with pytest.raises(ValidationError):
            ExpectedOutcome(
                direction="up", confidence=0.7,
                predicted_resolution="maybe",  # type: ignore[arg-type]
            )


# ----------------------------------------------------------------------------
# Decision — the executor's input contract
# ----------------------------------------------------------------------------


class TestDecision:
    def _eo(self):
        return ExpectedOutcome(direction="up", confidence=0.7)

    def test_valid_buy(self):
        d = Decision(
            action="buy_yes",
            size_usd=5.0,
            reasoning="reason",
            framework_criteria_hit={"a": True, "b": False},
            expected_outcome=self._eo(),
        )
        assert d.action == "buy_yes"

    def test_size_usd_negative_rejected(self):
        with pytest.raises(ValidationError):
            Decision(
                action="hold",
                size_usd=-1.0,
                reasoning="x",
                framework_criteria_hit={},
                expected_outcome=self._eo(),
            )

    def test_size_usd_zero_accepted(self):
        # hold/skip/close_position legitimately have zero size.
        Decision(
            action="hold",
            size_usd=0.0,
            reasoning="x",
            framework_criteria_hit={},
            expected_outcome=self._eo(),
        )

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            Decision(
                action="dance",  # type: ignore[arg-type]
                size_usd=1.0,
                reasoning="x",
                framework_criteria_hit={},
                expected_outcome=self._eo(),
            )

    def test_all_valid_actions(self):
        for action in ("buy_yes", "buy_no", "close_position", "hold", "skip"):
            Decision(
                action=action,  # type: ignore[arg-type]
                size_usd=0.0,
                reasoning="x",
                framework_criteria_hit={},
                expected_outcome=self._eo(),
            )


# ----------------------------------------------------------------------------
# ReflectionProposal — target_prompt must be one of two known values
# ----------------------------------------------------------------------------


class TestReflectionProposal:
    def test_valid_research_plan_target(self):
        p = ReflectionProposal(
            target_prompt="research_plan",
            proposed_filename="research_plan_v2.md",
            diff_summary="added X",
            body="new prompt body",
        )
        assert p.target_prompt == "research_plan"

    def test_valid_decision_framework_target(self):
        ReflectionProposal(
            target_prompt="decision_framework",
            proposed_filename="decision_framework_v2.md",
            diff_summary="z",
            body="b",
        )

    def test_invalid_target_rejected(self):
        with pytest.raises(ValidationError):
            ReflectionProposal(
                target_prompt="random_other_prompt",  # type: ignore[arg-type]
                proposed_filename="x.md",
                diff_summary="y",
                body="z",
            )


# ----------------------------------------------------------------------------
# DecisionFramework
# ----------------------------------------------------------------------------


class TestDecisionFramework:
    def test_valid(self):
        f = DecisionFramework(
            thresholds={"min_edge": 5.0, "max_loss": 10.0},
            sizing_rule="kelly_quarter",
            abort_conditions=["liquidity_dried_up"],
        )
        assert f.thresholds["min_edge"] == 5.0

    def test_missing_field_rejected(self):
        with pytest.raises(ValidationError):
            DecisionFramework(
                thresholds={"x": 1.0},
                sizing_rule="fixed",
                # missing abort_conditions
            )
