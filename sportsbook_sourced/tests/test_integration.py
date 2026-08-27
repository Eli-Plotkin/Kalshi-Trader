"""End-to-end tests for the pipeline the README advertises.

    Sportsbook odds snapshots -> de-vig / weighted fair price
    Kalshi market + orderbook snapshots -> event mapping
    Fair price + Kalshi quote -> fee-adjusted EV scan
    Opportunity -> paper/live execution
    Trade result -> CLV + PnL evaluation

The existing per-module suites test each stage in isolation, and they do it
with *self-consistent synthetic names*: `test_scanner.py` builds a market whose
`yes_subtitle` is "Lakers" alongside a `FairPrice` whose `home_team` is also
"Lakers". Real inputs never line up that way — Kalshi says "Lakers", The Odds
API says "Los Angeles Lakers" — so the mapping stage is never actually
stressed. These tests use payloads shaped like the real providers.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sportsbook_sourced import evaluation, mapper, paper, pricing, scanner, storage
from sportsbook_sourced.config import DEFAULT_SOURCE_WEIGHTS, ScannerConfig
from sportsbook_sourced.kalshi_feed import snapshot_from_market, top_of_book
from sportsbook_sourced.odds import TheOddsApiProvider
from sportsbook_sourced.schemas import (
    EventMapping,
    Opportunity,
    SportsbookEvent,
    SportsbookOdds,
    TradeEvaluation,
)


REPO_ROOT = Path(__file__).resolve().parents[2]

TIP = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)
NOW = TIP - timedelta(hours=1)
CONFIG = ScannerConfig()


# ────────────────────────────────────────────────────────────────────────────
# Production-shaped fixtures
# ────────────────────────────────────────────────────────────────────────────


def _event_for(event_id):
    """A minimal SportsbookEvent for satisfying a foreign key in a
    persistence test that isn't exercising event content itself."""
    return SportsbookEvent(
        event_id=event_id, league="nba", home_team="Home", away_team="Away",
        commence_time=TIP,
    )


def odds_api_event(home="Los Angeles Lakers", away="Boston Celtics"):
    """An event as The Odds API returns it: full club names."""
    return SportsbookEvent(
        event_id="4b1c2d3e",
        league="nba",
        home_team=home,
        away_team=away,
        commence_time=TIP,
    )


def kalshi_market(*, yes_ask="0.57", yes_bid="0.55",
                  yes_sub_title="Lakers",
                  close_offset_hours=3.0):
    """A market as Kalshi's REST API returns it.

    Two details matter and are easy to get wrong in a synthetic fixture:
      - `yes_sub_title` is a short team name, not the sportsbook's club name.
      - `close_time` is the market's *settlement* deadline, which sits hours
        after tip-off, not at tip-off.
    """
    return snapshot_from_market(
        market={
            "ticker": "KXNBAGAME-26JAN14LALBOS-LAL",
            "title": "Los Angeles Lakers vs Boston Celtics Winner",
            "yes_sub_title": yes_sub_title,
            "close_time": (TIP + timedelta(hours=close_offset_hours))
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": yes_ask,
            "volume_24h_fp": "8500",
            "open_interest_fp": "12000",
        },
        orderbook={
            "yes_dollars": [["0.54", "300"], ["0.55", "500"]],
            "no_dollars": [["0.42", "400"], ["0.43", "250"]],
        },
        collected_at=NOW,
    )


def book_odds(event, quotes, *, age_seconds=30):
    """Build two-sided moneyline rows for a set of books.

    `quotes` maps bookmaker -> (home_american, away_american).
    """
    rows = []
    for bookmaker, (home_price, away_price) in quotes.items():
        for name, price in ((event.home_team, home_price),
                            (event.away_team, away_price)):
            rows.append(SportsbookOdds(
                event_id=event.event_id,
                bookmaker=bookmaker,
                market_type="moneyline",
                outcome_name=name,
                american_odds=price,
                last_update=NOW - timedelta(seconds=age_seconds),
                collected_at=NOW,
            ))
    return rows


# A consensus well below Kalshi's ask: books imply ~35% for the home side
# while Kalshi asks 57c. That is a ~22c gross edge on the NO side — far larger
# than anything the buffers and fees can eat, so any correctly wired pipeline
# must surface it.
MISPRICED_QUOTES = {"draftkings": (185, -220),
                    "fanduel": (190, -225),
                    "betmgm": (183, -218)}

FAIR_QUOTES = {"draftkings": (-135, 115),
               "fanduel": (-138, 118),
               "betmgm": (-133, 113)}


def fair_price_for(event, quotes, **kwargs):
    return pricing.build_moneyline_fair_price(
        event=event,
        odds=book_odds(event, quotes, **kwargs),
        weights=DEFAULT_SOURCE_WEIGHTS,
        max_staleness_seconds=CONFIG.max_odds_staleness_seconds,
        now=NOW,
    )


def run_pipeline(*, market, event, quotes, config=CONFIG):
    """Odds -> fair price -> mapping -> scan, exactly as the README describes."""
    fair = fair_price_for(event, quotes)
    mapping = mapper.build_mapping(market=market, event=event, config=config,
                                   created_at=NOW)
    opportunity = scanner.scan_opportunity(
        market=market, fair_price=fair, mapping=mapping, config=config,
        computed_at=NOW,
    )
    return fair, mapping, opportunity


# ────────────────────────────────────────────────────────────────────────────
# Stage 1-2: fair price from real odds
# ────────────────────────────────────────────────────────────────────────────


class TestFairPriceFromRealOdds:
    def test_devigs_a_three_book_consensus(self):
        fair = fair_price_for(odds_api_event(), FAIR_QUOTES)
        assert fair.source_count == 3
        assert 0.55 < fair.home_prob < 0.59
        assert fair.home_prob + fair.away_prob == pytest.approx(1.0)

    def test_probabilities_sum_to_one_after_devig(self):
        fair = fair_price_for(odds_api_event(), MISPRICED_QUOTES)
        assert fair.home_prob + fair.away_prob == pytest.approx(1.0)

    def test_all_books_stale_refuses_to_produce_a_fair_price(self):
        """`stale_book_weight` is 0, so a fully stale slate leaves no usable
        sources. Raising is correct — a stale fair price is worse than none.
        """
        with pytest.raises(ValueError, match="no usable"):
            fair_price_for(odds_api_event(), FAIR_QUOTES, age_seconds=9_999)

    def test_fresh_books_are_kept_and_staleness_is_reported(self):
        fair = fair_price_for(odds_api_event(), FAIR_QUOTES, age_seconds=30)
        assert fair.source_count == 3
        assert fair.staleness_seconds == pytest.approx(30, abs=2)

    def test_book_name_mismatch_is_not_silently_ignored(self):
        """Books quote outcome names that don't always match the event's own
        team strings (e.g. "LA Lakers" vs "Los Angeles Lakers"). Dropping
        those rows silently shrinks the consensus without warning.
        """
        event = odds_api_event()
        rows = book_odds(event, FAIR_QUOTES)
        renamed = [
            SportsbookOdds(
                event_id=r.event_id, bookmaker=r.bookmaker,
                market_type=r.market_type,
                outcome_name="LA Lakers" if r.outcome_name == event.home_team
                             else r.outcome_name,
                american_odds=r.american_odds, last_update=r.last_update,
                collected_at=r.collected_at,
            )
            for r in rows
        ]
        with pytest.raises(ValueError):
            pricing.build_moneyline_fair_price(
                event=event, odds=renamed, weights=DEFAULT_SOURCE_WEIGHTS,
                max_staleness_seconds=CONFIG.max_odds_staleness_seconds,
                now=NOW,
            )


class TestSharpBookWeighting:
    def test_configured_sharp_books_are_reachable_from_the_odds_request(self):
        """`SourceWeights` upweights Pinnacle and Circa 2.5-3x, and the thesis
        leans on them as the fair-value anchor. Neither is offered by The Odds
        API's `us` region, so the weighting never applies in production.
        """
        source = inspect.getsource(TheOddsApiProvider.list_moneyline_odds)
        sharp_books = set(DEFAULT_SOURCE_WEIGHTS.sharp_books)
        regions = [r for r in ("us", "us2", "eu", "uk", "au") if f'"{r}"' in source]
        assert "eu" in regions, (
            f"sharp books {sorted(sharp_books)} are only quoted in the 'eu' "
            f"region, but the provider requests {regions}"
        )

    def test_sharp_source_count_is_populated_for_a_sharp_book(self):
        event = odds_api_event()
        fair = fair_price_for(event, {"pinnacle": (-135, 115),
                                      "draftkings": (-133, 113)})
        assert fair.sharp_source_count == 1


# ────────────────────────────────────────────────────────────────────────────
# Stage 3: mapping real Kalshi markets to real sportsbook events
# ────────────────────────────────────────────────────────────────────────────


class TestMappingRealPayloads:
    @pytest.mark.parametrize("yes_sub_title,expected", [
        ("Lakers", "home"),
        ("Los Angeles Lakers", "home"),
        ("LAL", "home"),
        ("Celtics", "away"),
        ("Boston Celtics", "away"),
        ("BOS", "away"),
    ])
    def test_resolves_which_side_yes_means(self, yes_sub_title, expected):
        """Kalshi's `yes_sub_title` is a short team name or tricode. Mapping it
        to the sportsbook's full club name is the whole job of this stage.
        """
        market = kalshi_market(yes_sub_title=yes_sub_title)
        outcome, score = mapper.infer_yes_outcome(market, odds_api_event())
        assert outcome == expected, (
            f"{yes_sub_title!r} -> {outcome!r} (score {score:.2f}); "
            f"expected {expected!r}"
        )

    def test_settlement_close_time_is_not_treated_as_a_mismatch(self):
        """Kalshi game markets close for settlement hours after tip-off. That
        is normal, not a mapping error, and must not poison confidence.

        Uses the full club name as `yes_sub_title` so the team-matching gate is
        satisfied and this test isolates the temporal check.
        """
        mapping = mapper.build_mapping(
            market=kalshi_market(yes_sub_title="Los Angeles Lakers",
                                 close_offset_hours=3.0),
            event=odds_api_event(), config=CONFIG, created_at=NOW,
        )
        assert mapping.mismatch_flags == [], (
            f"normal settlement offset flagged as a mismatch: "
            f"{mapping.mismatch_flags}"
        )
        assert mapping.confidence >= CONFIG.min_mapping_confidence

    def test_ticker_date_binds_the_mapping_to_the_right_calendar_day(self):
        """Kalshi opens game markets days ahead, so the same fixture appears
        more than once. The Eastern date in the ticker separates them.

        The fixture tips at 01:00 UTC on Jan 15, which is 8pm ET on Jan 14 —
        so a naive UTC-date comparison would reject the correct market.
        """
        event = odds_api_event()
        good = kalshi_market(yes_sub_title="Los Angeles Lakers")  # ...26JAN14...
        assert mapper.game_date_from_ticker(good.ticker) == date(2026, 1, 14)
        assert mapper.build_mapping(market=good, event=event, config=CONFIG,
                                    created_at=NOW).mismatch_flags == []

        wrong_day = snapshot_from_market(
            market={**good.raw_market,
                    "ticker": "KXNBAGAME-26JAN16LALBOS-LAL"},
            collected_at=NOW,
        )
        mapping = mapper.build_mapping(market=wrong_day, event=event,
                                       config=CONFIG, created_at=NOW)
        assert any(f.startswith("game_date_mismatch:")
                   for f in mapping.mismatch_flags)
        assert mapping.confidence == 0.0

    def test_a_correct_pairing_clears_the_confidence_gate(self):
        mapping = mapper.build_mapping(
            market=kalshi_market(), event=odds_api_event(),
            config=CONFIG, created_at=NOW,
        )
        assert mapping.confidence >= CONFIG.min_mapping_confidence, (
            f"a correctly paired market scores {mapping.confidence:.2f}, "
            f"below the {CONFIG.min_mapping_confidence} gate — no real market "
            f"can ever be traded"
        )

    def test_a_genuinely_wrong_pairing_is_rejected(self):
        """Negative control: a different game must not map."""
        market = kalshi_market(yes_sub_title="Suns")
        event = odds_api_event()
        outcome, _ = mapper.infer_yes_outcome(market, event)
        assert outcome is None

    def test_ambiguous_mapping_does_not_silently_default_to_home(self):
        """`build_mapping` sets `mapped_yes_outcome = "home"` whenever the
        inference is ambiguous. Today a mismatch flag forces a skip, but the
        stored mapping is a coin flip — if the gate is ever relaxed, the fair
        probability is inverted for half of all markets.
        """
        market = kalshi_market(yes_sub_title="???")
        mapping = mapper.build_mapping(
            market=market, event=odds_api_event(), config=CONFIG,
            created_at=NOW,
        )
        assert mapping.mapped_yes_outcome is None, (
            "ambiguous inference was recorded as a definite 'home' mapping"
        )


# ────────────────────────────────────────────────────────────────────────────
# Stage 4: the scan
# ────────────────────────────────────────────────────────────────────────────


class TestScanOnRealPayloads:
    def test_surfaces_a_large_genuine_mispricing(self):
        """Kalshi asks 57c on a side the books price near 35c. A ~22c gross
        edge is the clearest possible buy signal; if this skips, nothing trades.
        """
        _, mapping, opp = run_pipeline(
            market=kalshi_market(), event=odds_api_event(),
            quotes=MISPRICED_QUOTES,
        )
        assert opp.action == "buy", (
            f"skipped a ~20c mispricing: {opp.reason} "
            f"(mapping confidence {mapping.confidence:.2f})"
        )

    def test_passes_on_an_efficiently_priced_market(self):
        """Negative control: books agree with Kalshi, so there is no edge."""
        _, _, opp = run_pipeline(
            market=kalshi_market(), event=odds_api_event(),
            quotes=FAIR_QUOTES,
        )
        assert opp.action == "skip"
        assert "net_edge" in opp.reason

    def test_edge_math_is_correct_for_the_chosen_side(self):
        market = kalshi_market()
        fair, _, opp = run_pipeline(
            market=market, event=odds_api_event(),
            quotes=MISPRICED_QUOTES,
        )
        expected_fee = pricing.kalshi_fee_cents_per_contract(
            opp.kalshi_price_cents)
        # Liquidity buffer is size-derived (P2.9), not the flat config
        # default -- compute it the same way scan_opportunity does, from
        # the actual top-of-book depth on the side that was chosen.
        book = top_of_book(market.raw_orderbook, opp.side)
        liquidity_buffer = scanner._liquidity_buffer_cents(
            book[1] if book is not None else None, CONFIG)
        buffers = (liquidity_buffer
                   + CONFIG.stale_odds_buffer_cents
                   + CONFIG.mapping_risk_buffer_cents)
        assert opp.net_edge_cents == pytest.approx(
            opp.gross_edge_cents - expected_fee - buffers)

    def test_picks_the_no_side_when_kalshi_is_too_expensive_on_yes(self):
        _, _, opp = run_pipeline(
            market=kalshi_market(), event=odds_api_event(),
            quotes=MISPRICED_QUOTES,
        )
        assert opp.side == "no"

    def test_liquidity_buffer_reflects_the_orderbook(self):
        """The thesis says trades clear only after "fees, spread, liquidity...".
        A thin book and a deep book must not price the same risk identically.
        """
        deep = kalshi_market()
        thin = snapshot_from_market(
            market=deep.raw_market,
            orderbook={"yes_dollars": [["0.55", "1"]],
                       "no_dollars": [["0.43", "1"]]},
            collected_at=NOW,
        )
        _, _, deep_opp = run_pipeline(market=deep, event=odds_api_event(),
                                      quotes=MISPRICED_QUOTES)
        _, _, thin_opp = run_pipeline(market=thin, event=odds_api_event(),
                                      quotes=MISPRICED_QUOTES)
        assert deep_opp.net_edge_cents != thin_opp.net_edge_cents, (
            "orderbook depth does not affect the scan; the liquidity buffer "
            "is a flat constant and the fetched orderbook is unused"
        )


# ────────────────────────────────────────────────────────────────────────────
# Stage 5-6: paper execution and evaluation
# ────────────────────────────────────────────────────────────────────────────


class TestPaperAndEvaluation:
    def _tradeable(self):
        """A tradeable NO-side opportunity, built directly.

        Constructed rather than scanned so that execution and evaluation are
        tested independently of the mapping gates — those are covered by
        `TestMappingRealPayloads`, and routing through them here would only
        duplicate that failure.
        """
        price = 43  # NO side: 100 - 57c yes bid
        return Opportunity(
            opportunity_id="opp-1",
            kalshi_ticker="KXNBAGAME-26JAN14LALBOS-LAL",
            sportsbook_event_id="4b1c2d3e",
            side="no",
            action="buy",
            fair_prob=0.65,
            kalshi_price_cents=price,
            gross_edge_cents=22.0,
            fee_cents_per_contract=pricing.kalshi_fee_cents_per_contract(price),
            net_edge_cents=17.3,
            max_contracts=23,
            reason="tradeable",
            computed_at=NOW,
        )

    def test_paper_fill_debits_cash_and_records_the_position(self):
        opp = self._tradeable()
        assert opp.action == "buy", f"fixture is not tradeable: {opp.reason}"
        portfolio = paper.PaperPortfolio(cash_cents=100_000, positions={})
        order = paper.paper_buy(opp, portfolio=portfolio)
        assert order.status == "filled"
        assert portfolio.cash_cents == 100_000 - order.count * opp.kalshi_price_cents
        assert portfolio.positions[opp.kalshi_ticker] != 0

    def test_paper_rejects_when_cash_is_short(self):
        opp = self._tradeable()
        portfolio = paper.PaperPortfolio(cash_cents=1, positions={})
        order = paper.paper_buy(opp, portfolio=portfolio)
        assert order.status == "rejected"
        assert portfolio.cash_cents == 1

    def test_evaluation_scores_a_winning_no_position(self):
        opp = self._tradeable()
        result = evaluation.evaluate_opportunity(
            opportunity=opp, fair_prob_at_close=0.30, resolved_yes=False,
            count=10, evaluated_at=NOW,
        )
        assert result.pnl_cents > 0
        assert result.clv_cents is not None
        assert result.resolved_side == "no"

    def test_realized_pnl_accounts_for_fees(self):
        """PnL that ignores the fee overstates every result, and the fee is the
        difference between a marginal edge and a losing one."""
        opp = self._tradeable()
        count = 10
        gross = evaluation.realized_pnl_cents(
            side=opp.side, entry_price_cents=opp.kalshi_price_cents,
            count=count, resolved_yes=False,
        )
        fee = pricing.exact_kalshi_fee_cents(count, opp.kalshi_price_cents)
        result = evaluation.evaluate_opportunity(
            opportunity=opp, fair_prob_at_close=0.30, resolved_yes=False,
            count=count, evaluated_at=NOW,
        )
        assert result.pnl_cents == pytest.approx(gross - fee), (
            f"PnL reported as {result.pnl_cents} (gross {gross}); the "
            f"{fee}c entry fee is never deducted"
        )


# ────────────────────────────────────────────────────────────────────────────
# Stage 7: persistence
# ────────────────────────────────────────────────────────────────────────────


class TestPersistence:
    """`storage.py` is described as "SQLite schema and persistence helpers" and
    defines eight tables, but exposes only `init_db` and `to_json`. Nothing in
    the package can write a row, so no scan result survives the process.
    """

    EXPECTED_WRITERS = [
        "insert_sportsbook_event",
        "insert_odds_snapshot",
        "insert_fair_price",
        "insert_kalshi_snapshot",
        "insert_mapping",
        "insert_opportunity",
        "insert_paper_order",
        "insert_trade_evaluation",
    ]

    def test_storage_exposes_writers_for_every_table(self):
        missing = [n for n in self.EXPECTED_WRITERS if not hasattr(storage, n)]
        assert not missing, (
            f"storage.py defines tables with no way to write to them: {missing}"
        )

    def test_an_opportunity_round_trips_through_sqlite(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        opp = TestPaperAndEvaluation()._tradeable()
        # FK: opportunities.sportsbook_event_id -> sportsbook_events.event_id.
        storage.insert_sportsbook_event(conn, _event_for(opp.sportsbook_event_id))
        writer = getattr(storage, "insert_opportunity", None)
        if writer is None:
            pytest.fail("storage.insert_opportunity does not exist")
        writer(conn, opp)
        rows = conn.execute(
            "SELECT opportunity_id, action FROM opportunities").fetchall()
        assert rows == [(opp.opportunity_id, opp.action)]

    def test_an_ambiguous_mapping_persists_with_a_null_outcome(self, tmp_path):
        """P0.3 made `mapped_yes_outcome` genuinely Optional; the storage
        layer must accept that rather than reject it with a NOT NULL
        constraint error, which is what the schema did before P1.4."""
        conn = storage.init_db(tmp_path / "t.sqlite")
        mapping = EventMapping(
            mapping_id="m1", kalshi_ticker="KXNBAGAME-XYZ",
            sportsbook_event_id="e1", mapped_yes_outcome=None,
            confidence=0.0, mismatch_flags=["ambiguous_yes_outcome"],
            created_at=NOW,
        )
        # FK: event_mappings.sportsbook_event_id -> sportsbook_events.event_id.
        storage.insert_sportsbook_event(conn, _event_for("e1"))
        storage.insert_mapping(conn, mapping)
        row = conn.execute(
            "SELECT mapped_yes_outcome FROM event_mappings WHERE mapping_id = ?",
            (mapping.mapping_id,),
        ).fetchone()
        assert row == (None,)

    def test_trade_evaluation_upsert_preserves_the_earlier_phase(self, tmp_path):
        """The two-phase evaluator (P1.7) writes this table twice per trade:
        once at close with CLV fields set and settlement fields None, once at
        settlement with the reverse. The second write must not blank out
        what the first one recorded.
        """
        conn = storage.init_db(tmp_path / "t.sqlite")
        at_close = TradeEvaluation(
            opportunity_id="opp-1", evaluated_at=NOW,
            entry_price_cents=43, fair_prob_at_entry=0.65,
            fair_prob_at_close=0.30, clv_cents=13.0,
            resolved_side=None, pnl_cents=None,
        )
        # FK: trade_evaluations.opportunity_id -> opportunities.opportunity_id.
        storage.insert_sportsbook_event(conn, _event_for("4b1c2d3e"))
        storage.insert_opportunity(
            conn, replace(TestPaperAndEvaluation()._tradeable(),
                          opportunity_id="opp-1"))
        storage.insert_trade_evaluation(conn, at_close)

        at_settlement = TradeEvaluation(
            opportunity_id="opp-1", evaluated_at=NOW + timedelta(hours=3),
            entry_price_cents=43, fair_prob_at_entry=0.65,
            fair_prob_at_close=None, clv_cents=None,
            resolved_side="no", pnl_cents=57.0,
        )
        storage.insert_trade_evaluation(conn, at_settlement)

        rows = conn.execute(
            "SELECT opportunity_id, clv_cents, resolved_side, pnl_cents "
            "FROM trade_evaluations"
        ).fetchall()
        assert rows == [("opp-1", 13.0, "no", 57.0)], (
            "settlement-phase write should fill in resolved_side/pnl_cents "
            "without erasing the close-phase clv_cents"
        )


# ────────────────────────────────────────────────────────────────────────────
# The advertised command
# ────────────────────────────────────────────────────────────────────────────


class TestScanCommand:
    def _run_cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-m", "sportsbook_sourced.cli", *args],
            cwd=str(cwd or REPO_ROOT), capture_output=True, text=True,
            timeout=120,
        )

    def test_scan_command_exits_cleanly(self):
        result = self._run_cli("scan", "--dry-run")
        assert result.returncode == 0, result.stderr[-300:]

    def test_scan_command_actually_scans(self):
        """The README calls this "the first useful command". It should report
        markets examined and opportunities found, not a scaffold status.
        """
        result = self._run_cli("scan", "--dry-run")
        assert "scaffold" not in result.stdout.lower(), (
            f"`scan --dry-run` only prints its own configuration and never "
            f"examines a market:\n{result.stdout.strip()[:400]}"
        )

    def test_dry_run_is_opt_out(self):
        """`--dry-run` uses `action="store_true", default=True`, so the flag
        can never be turned off and live mode is unreachable."""
        result = self._run_cli("scan", "--help")
        assert "--live" in result.stdout or "--no-dry-run" in result.stdout, (
            "--dry-run defaults to True with no way to disable it, so there "
            "is no path to non-dry execution"
        )
