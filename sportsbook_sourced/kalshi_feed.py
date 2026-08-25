from __future__ import annotations

from datetime import datetime, timezone

from .config import MONEYLINE_SERIES_TICKERS
from .schemas import KalshiMarketSnapshot, League


def dollars_str_to_cents(value) -> int:
    """Convert Kalshi dollar-string prices (e.g. "0.4200") to integer cents.

    Kalshi quotes prices as decimal-dollar strings via the REST API. Downstream
    code in this package uses integer cents throughout. Returns 0 for None,
    empty string, or any value that can't be parsed as a float — defensive
    against malformed payloads.
    """
    if value is None or value == "":
        return 0
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def _parse_ts(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def snapshot_from_market(
    *,
    market: dict,
    orderbook: dict | None = None,
    collected_at: datetime | None = None,
) -> KalshiMarketSnapshot:
    collected_at = collected_at or datetime.now(timezone.utc)
    return KalshiMarketSnapshot(
        ticker=market["ticker"],
        title=market.get("title", ""),
        yes_subtitle=market.get("yes_sub_title") or market.get("yes_subtitle"),
        close_time=_parse_ts(market.get("close_time")),
        yes_bid_cents=dollars_str_to_cents(market.get("yes_bid_dollars")),
        yes_ask_cents=dollars_str_to_cents(market.get("yes_ask_dollars")),
        volume=float(market.get("volume_24h_fp") or market.get("volume_fp") or 0),
        open_interest=float(market.get("open_interest_fp") or 0),
        collected_at=collected_at,
        raw_market=market,
        raw_orderbook=orderbook or {},
    )


def list_sports_markets(
    *,
    kalshi_client,
    league: League,
    max_pages: int = 10,
    limit: int = 200,
) -> list[KalshiMarketSnapshot]:
    """Fetch open Kalshi moneyline markets for a league and snapshot best quotes.

    Takes a `league`, not a free-form series ticker: `MONEYLINE_SERIES_TICKERS`
    is the single place that maps a league to its moneyline series, so it's
    structurally impossible to point this at a non-moneyline series (spreads,
    totals) by accident. `KalshiMarketSnapshot` itself carries no market-type
    field, so this is the actual enforcement boundary for this package's
    moneyline-only scope (`config.SUPPORTED_MARKET_TYPES`), not just a
    convention callers are trusted to follow.
    """
    try:
        series_ticker = MONEYLINE_SERIES_TICKERS[league]
    except KeyError:
        raise ValueError(
            f"no known moneyline series ticker for league {league!r} -- "
            f"supported leagues: {sorted(MONEYLINE_SERIES_TICKERS)}"
        ) from None

    out: list[KalshiMarketSnapshot] = []
    cursor = None
    pages = 0
    while pages < max_pages:
        markets, cursor = kalshi_client.list_markets(
            status="open",
            limit=limit,
            cursor=cursor,
            series_ticker=series_ticker,
        )
        pages += 1
        for market in markets:
            orderbook = kalshi_client.get_orderbook(market["ticker"])
            out.append(snapshot_from_market(market=market, orderbook=orderbook))
        if not cursor:
            break
    return out

