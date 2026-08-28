from __future__ import annotations

from dataclasses import dataclass, field


SUPPORTED_LEAGUES: tuple[str, ...] = ("nba", "nfl")
SUPPORTED_MARKET_TYPES: tuple[str, ...] = ("moneyline",)

# The moneyline ("who wins") series ticker per league -- verified against
# kalshi_multisport_research_dataset.csv (see CLAUDE.md P0.1). Kalshi may run
# separate series for spreads/totals under different tickers; this table is
# the enforcement point that keeps `kalshi_feed.list_sports_markets` scoped
# to moneyline only, since a `KalshiMarketSnapshot` has no market-type field
# of its own to check after the fact.
MONEYLINE_SERIES_TICKERS: dict[str, str] = {
    "nba": "KXNBAGAME",
    "nfl": "KXNFLGAME",
}


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
    # 5% of the $1,000 starting paper bankroll (cli.py::STARTING_PAPER_CASH_CENTS)
    # -- a per-position budget cap, not a trading threshold, so this doesn't
    # carry the same "don't guess ahead of data" caveat as min_net_edge_cents
    # or the liquidity/mapping thresholds. Raising it from the original $10
    # is also what makes P3.13's Kelly-proportional sizing load-bearing by
    # default rather than perpetually inert behind a tighter budget ceiling
    # (see P3.13's finding: at $10, even the minimum viable edge produced a
    # Kelly count larger than the ceiling allowed).
    max_position_usd: float = 50.0
    liquidity_buffer_cents: float = 1.0
    stale_odds_buffer_cents: float = 1.0

    # Liquidity buffer scaling (P2.9): `liquidity_buffer_cents` above is now
    # the buffer at `liquidity_reference_size` depth, not a flat value --
    # scanner.py scales it by `reference_size / actual_top_of_book_size`,
    # capped at `liquidity_buffer_cap_cents`. Neither number is calibrated
    # against real observed depth yet (no data on typical Kalshi NBA
    # moneyline book depth has been gathered) -- treat these as a
    # placeholder shape, not a tuned threshold, same caveat as
    # `mapper.MIN_TEAM_SIMILARITY` before it was investigated.
    liquidity_reference_size: int = 100
    liquidity_buffer_cap_cents: float = 50.0


DEFAULT_SOURCE_WEIGHTS = SourceWeights()
DEFAULT_SCANNER_CONFIG = ScannerConfig()

