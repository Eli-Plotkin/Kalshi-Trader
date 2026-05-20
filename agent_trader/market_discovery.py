from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class EligibleMarket:
    ticker: str
    title: str
    yes_bid_cents: int
    yes_ask_cents: int
    last_price_cents: int
    volume_24h: float
    open_interest: float
    close_time: Optional[datetime]
    raw_market_response: dict


def _parse_close_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Kalshi returns RFC3339 timestamps.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def dollars_str_to_cents(value) -> int:
    """Kalshi price fields (yes_bid_dollars, yes_ask_dollars, last_price_dollars)
    are dollar-denominated decimal strings like "0.4200". Downstream code (LLM
    prompts, executor, grading) expects integer cents."""
    if value is None or value == "":
        return 0
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def discover_eligible_markets(
    client,
    min_daily_volume: int = 100,
    min_hours_to_close: int = 8,
    max_pages: int = 50,
    series_ticker: Optional[str] = None,
) -> list[EligibleMarket]:
    """
    Paginate Kalshi /markets?status=open and return markets that meet the
    liquidity + horizon floor from the design doc (Open Q3).

    Filters:
      - status=open (handled by API)
      - volume_24h >= min_daily_volume
      - open_interest > 0
      - close_time at least min_hours_to_close from now
    """
    now = datetime.now(timezone.utc)
    eligible: list[EligibleMarket] = []
    cursor = None
    pages = 0

    extra = {"series_ticker": series_ticker} if series_ticker else {}
    while pages < max_pages:
        markets, cursor = client.list_markets(status="open", limit=200, cursor=cursor, **extra)
        pages += 1
        for m in markets:
            volume = float(m.get("volume_24h_fp") or m.get("volume_fp") or 0)
            open_interest = float(m.get("open_interest_fp") or 0)
            # NOTE: do not filter on `is_provisional`. Per Kalshi: "If true, the
            # market may be removed after determination if there is no activity
            # on it." It's a lifecycle/garbage-collection flag, not a
            # tradeability signal — real liquid markets (e.g. NBA games with
            # $1M+ volume) carry is_provisional=true. The volume + OI floor
            # below is what actually rejects placeholder parlays.
            if volume < min_daily_volume or open_interest <= 0:
                continue
            close_time = _parse_close_time(m.get("close_time"))
            if close_time is None:
                continue
            hours_remaining = (close_time - now).total_seconds() / 3600.0
            if hours_remaining < min_hours_to_close:
                continue
            eligible.append(
                EligibleMarket(
                    ticker=m["ticker"],
                    title=m.get("title", ""),
                    yes_bid_cents=dollars_str_to_cents(m.get("yes_bid_dollars")),
                    yes_ask_cents=dollars_str_to_cents(m.get("yes_ask_dollars")),
                    last_price_cents=dollars_str_to_cents(m.get("last_price_dollars")),
                    volume_24h=volume,
                    open_interest=open_interest,
                    close_time=close_time,
                    raw_market_response=m,
                )
            )
        if not cursor:
            break

    logging.info(
        "market_discovery: %d eligible markets across %d page(s)",
        len(eligible),
        pages,
    )
    return eligible
