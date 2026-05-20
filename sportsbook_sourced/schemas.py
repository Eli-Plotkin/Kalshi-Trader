from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional


League = Literal["nba", "nfl"]
MarketType = Literal["moneyline"]
Side = Literal["yes", "no"]
Action = Literal["buy", "skip"]


@dataclass(frozen=True)
class SportsbookEvent:
    event_id: str
    league: League
    home_team: str
    away_team: str
    commence_time: datetime
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SportsbookOdds:
    event_id: str
    bookmaker: str
    market_type: MarketType
    outcome_name: str
    american_odds: int
    last_update: datetime
    collected_at: datetime
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FairPrice:
    event_id: str
    league: League
    market_type: MarketType
    home_team: str
    away_team: str
    home_prob: float
    away_prob: float
    source_count: int
    sharp_source_count: int
    staleness_seconds: int
    book_disagreement_cents: float
    confidence: float
    computed_at: datetime


@dataclass(frozen=True)
class KalshiMarketSnapshot:
    ticker: str
    title: str
    yes_subtitle: Optional[str]
    close_time: Optional[datetime]
    yes_bid_cents: int
    yes_ask_cents: int
    volume: float
    open_interest: float
    collected_at: datetime
    raw_market: dict = field(default_factory=dict)
    raw_orderbook: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EventMapping:
    mapping_id: str
    kalshi_ticker: str
    sportsbook_event_id: str
    mapped_yes_outcome: Literal["home", "away"]
    confidence: float
    mismatch_flags: list[str]
    created_at: datetime


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    kalshi_ticker: str
    sportsbook_event_id: str
    side: Side
    action: Action
    fair_prob: float
    kalshi_price_cents: int
    gross_edge_cents: float
    fee_cents_per_contract: float
    net_edge_cents: float
    max_contracts: int
    reason: str
    computed_at: datetime


@dataclass(frozen=True)
class PaperOrder:
    paper_order_id: str
    opportunity_id: str
    ticker: str
    side: Side
    action: Literal["buy"]
    count: int
    limit_price_cents: int
    status: Literal["filled", "resting", "rejected"]
    fill_model_version: str
    created_at: datetime


@dataclass(frozen=True)
class TradeEvaluation:
    opportunity_id: str
    evaluated_at: datetime
    entry_price_cents: int
    fair_prob_at_entry: float
    fair_prob_at_close: Optional[float]
    clv_cents: Optional[float]
    resolved_side: Optional[Side]
    pnl_cents: Optional[float]

