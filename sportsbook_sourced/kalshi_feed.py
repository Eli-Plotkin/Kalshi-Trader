from __future__ import annotations

from datetime import datetime, timezone

from .schemas import KalshiMarketSnapshot


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
    series_ticker: str,
    max_pages: int = 10,
    limit: int = 200,
) -> list[KalshiMarketSnapshot]:
    """Fetch open Kalshi sports markets for a series and snapshot best quotes."""
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

