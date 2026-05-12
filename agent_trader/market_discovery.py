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
    volume_24h: int
    open_interest: int
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


def discover_eligible_markets(
    client,
    min_daily_volume: int = 100,
    min_hours_to_close: int = 8,
    max_pages: int = 50,
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

    while pages < max_pages:
        markets, cursor = client.list_markets(status="open", limit=200, cursor=cursor)
        pages += 1
        for m in markets:
            volume = int(m.get("volume_24h") or m.get("volume") or 0)
            open_interest = int(m.get("open_interest") or 0)
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
                    yes_bid_cents=int(m.get("yes_bid") or 0),
                    yes_ask_cents=int(m.get("yes_ask") or 0),
                    last_price_cents=int(m.get("last_price") or 0),
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
