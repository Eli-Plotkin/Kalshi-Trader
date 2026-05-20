from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from uuid import uuid4

from .config import ScannerConfig
from .schemas import EventMapping, FairPrice, KalshiMarketSnapshot, SportsbookEvent


TEAM_ALIASES: dict[str, str] = {
    "okc": "oklahoma city thunder",
    "oklahoma city": "oklahoma city thunder",
    "sas": "san antonio spurs",
    "san antonio": "san antonio spurs",
    "nyk": "new york knicks",
    "new york": "new york knicks",
    "lal": "los angeles lakers",
    "la lakers": "los angeles lakers",
    "lac": "los angeles clippers",
    "la clippers": "los angeles clippers",
}


def normalize_team_name(value: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", value.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_ALIASES.get(text, text)


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_team_name(a), normalize_team_name(b)).ratio()


def infer_yes_outcome(market: KalshiMarketSnapshot, event: SportsbookEvent) -> tuple[str | None, float]:
    """Infer whether Kalshi YES maps to home or away.

    This is deliberately conservative. Future work can add an LLM verifier, but
    this function should remain the first hard gate.
    """
    yes_text = market.yes_subtitle or market.title
    home_score = title_similarity(yes_text, event.home_team)
    away_score = title_similarity(yes_text, event.away_team)
    if max(home_score, away_score) < 0.55:
        return None, max(home_score, away_score)
    if abs(home_score - away_score) < 0.08:
        return None, max(home_score, away_score)
    return ("home" if home_score > away_score else "away"), max(home_score, away_score)


def build_mapping(
    *,
    market: KalshiMarketSnapshot,
    event: SportsbookEvent,
    config: ScannerConfig,
    created_at: datetime | None = None,
) -> EventMapping:
    created_at = created_at or datetime.now(timezone.utc)
    flags: list[str] = []

    yes_outcome, team_conf = infer_yes_outcome(market, event)
    if yes_outcome is None:
        flags.append("ambiguous_yes_outcome")
        yes_outcome = "home"

    time_conf = 1.0
    if market.close_time is not None:
        delta_minutes = abs((market.close_time - event.commence_time).total_seconds()) / 60.0
        if delta_minutes > config.max_commence_time_delta_minutes:
            flags.append(f"time_delta_minutes:{delta_minutes:.1f}")
            time_conf = max(0.0, 1.0 - delta_minutes / 240.0)

    confidence = min(team_conf, time_conf)
    return EventMapping(
        mapping_id=str(uuid4()),
        kalshi_ticker=market.ticker,
        sportsbook_event_id=event.event_id,
        mapped_yes_outcome=yes_outcome,  # type: ignore[arg-type]
        confidence=confidence,
        mismatch_flags=flags,
        created_at=created_at,
    )


def fair_yes_probability(mapping: EventMapping, fair_price: FairPrice) -> float:
    return fair_price.home_prob if mapping.mapped_yes_outcome == "home" else fair_price.away_prob

