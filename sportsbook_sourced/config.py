from __future__ import annotations

from dataclasses import dataclass, field


SUPPORTED_LEAGUES: tuple[str, ...] = ("nba", "nfl")
SUPPORTED_MARKET_TYPES: tuple[str, ...] = ("moneyline",)


@dataclass(frozen=True)
class SourceWeights:
    """Weights used when combining no-vig bookmaker probabilities."""

    sharp_books: dict[str, float] = field(default_factory=lambda: {
        "pinnacle": 3.0,
        "circa": 2.5,
    })
    default_book_weight: float = 1.0
    stale_book_weight: float = 0.0


@dataclass(frozen=True)
class ScannerConfig:
    """Conservative defaults for the first paper-trading scanner."""

    min_net_edge_cents: float = 3.0
    min_mapping_confidence: float = 0.92
    max_odds_staleness_seconds: int = 180

    # Kalshi game markets close for *settlement*, which is hours after the
    # event starts (a ~2.5h NBA game typically closes 3-4h after tip; NFL
    # runs longer). So the temporal same-game check is directional: a market
    # closing before the event starts is a genuine mismatch, a market closing
    # a few hours after it is normal.
    max_close_before_commence_minutes: int = 15
    max_settlement_window_minutes: int = 480

    max_book_disagreement_cents: float = 8.0
    min_source_count: int = 2
    min_sharp_source_count: int = 0
    max_position_usd: float = 10.0
    liquidity_buffer_cents: float = 1.0
    stale_odds_buffer_cents: float = 1.0
    mapping_risk_buffer_cents: float = 1.0


DEFAULT_SOURCE_WEIGHTS = SourceWeights()
DEFAULT_SCANNER_CONFIG = ScannerConfig()

