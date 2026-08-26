"""Tests for `pipeline.py`: the N x M event-pairing function and the
odds -> fair price -> mapping -> scan -> paper -> store orchestrator.

Pairing tests use production-shaped names (Kalshi's short subtitle vs a
sportsbook's full club name) since that mismatch is the entire point of
`find_event_for_market` — a self-consistent synthetic name would never
exercise it. Orchestration tests care about wiring and error isolation, not
mapping subtlety, so they use one obviously-correct pairing throughout.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sportsbook_sourced import pipeline, storage
from sportsbook_sourced.config import DEFAULT_SOURCE_WEIGHTS, ScannerConfig
from sportsbook_sourced.kalshi_feed import snapshot_from_market
from sportsbook_sourced.paper import PaperPortfolio
from sportsbook_sourced.schemas import SportsbookEvent, SportsbookOdds


TIP = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)  # ~8pm Eastern, Jan 14
NOW = TIP - timedelta(hours=1)
CONFIG = ScannerConfig()


def _event(event_id, home, away, commence_time=TIP):
    return SportsbookEvent(
        event_id=event_id, league="nba", home_team=home, away_team=away,
        commence_time=commence_time,
    )


def _market(*, ticker, yes_sub_title, close_offset_hours=3.0, commence_time=TIP,
            yes_bid="0.55", yes_ask="0.57"):
    return snapshot_from_market(
        market={
            "ticker": ticker,
            "title": f"{yes_sub_title} game",
            "yes_sub_title": yes_sub_title,
            "close_time": (commence_time + timedelta(hours=close_offset_hours))
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": yes_ask,
            "volume_24h_fp": "8500",
            "open_interest_fp": "12000",
        },
        collected_at=NOW,
    )


def _odds(event, quotes, *, age_seconds=30):
    """`quotes` maps bookmaker -> (home_american, away_american)."""
    rows = []
    for bookmaker, (home_price, away_price) in quotes.items():
        for name, price in ((event.home_team, home_price), (event.away_team, away_price)):
            rows.append(SportsbookOdds(
                event_id=event.event_id, bookmaker=bookmaker, market_type="moneyline",
                outcome_name=name, american_odds=price,
                last_update=NOW - timedelta(seconds=age_seconds), collected_at=NOW,
            ))
    return rows


MISPRICED_QUOTES = {"draftkings": (185, -220), "fanduel": (190, -225)}
FAIR_QUOTES = {"draftkings": (-135, 115), "fanduel": (-138, 118), "betmgm": (-133, 113)}


# ────────────────────────────────────────────────────────────────────────────
# find_event_for_market
# ────────────────────────────────────────────────────────────────────────────


class TestFindEventForMarket:
    def test_matches_the_correct_event_among_several_decoys(self):
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        correct = _event("e1", "Los Angeles Lakers", "Boston Celtics")
        same_day_wrong_teams = _event("e2", "Golden State Warriors", "Phoenix Suns")
        right_teams_wrong_day = _event("e3", "Los Angeles Lakers", "Boston Celtics",
                                       commence_time=TIP - timedelta(days=1))

        found = pipeline.find_event_for_market(
            market, [same_day_wrong_teams, right_teams_wrong_day, correct], CONFIG,
        )
        assert found is correct

    def test_returns_none_when_no_candidate_shares_a_team(self):
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        unrelated = _event("e2", "Golden State Warriors", "Phoenix Suns")
        assert pipeline.find_event_for_market(market, [unrelated], CONFIG) is None

    def test_a_matching_ticker_date_on_the_wrong_calendar_day_is_excluded(self):
        """Same teams, but the only candidate plays a day off from the ticker
        date — the date check must reject it outright, not let team-name
        similarity overrule a wrong day."""
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        wrong_day = _event("e3", "Los Angeles Lakers", "Boston Celtics",
                           commence_time=TIP - timedelta(days=1))
        assert pipeline.find_event_for_market(market, [wrong_day], CONFIG) is None

    def test_a_genuine_same_city_ambiguity_returns_none(self):
        """Mirrors the P0.2 same-city ambiguity, one level up: 'Los Angeles'
        shares tokens with both LA teams' full names. That's a real tie, not
        a fabricated one (see mapper.py's note on why 'LA' -> 'los angeles'
        expansion was deliberately not implemented)."""
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Los Angeles")
        lakers_game = _event("e1", "Los Angeles Lakers", "Boston Celtics")
        clippers_game = _event("e2", "Los Angeles Clippers", "Boston Celtics")
        assert pipeline.find_event_for_market(market, [lakers_game, clippers_game], CONFIG) is None


# ────────────────────────────────────────────────────────────────────────────
# run_scan
# ────────────────────────────────────────────────────────────────────────────


class TestRunScan:
    def _table_count(self, conn, table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_a_single_market_flows_through_every_stage_and_persists(self, tmp_path):
        event = _event("4b1c2d3e", "Los Angeles Lakers", "Boston Celtics")
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        odds = _odds(event, MISPRICED_QUOTES)
        conn = storage.init_db(tmp_path / "t.sqlite")
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})

        result = pipeline.run_scan(
            markets=[market], events=[event], odds=odds, portfolio=portfolio,
            conn=conn, weights=DEFAULT_SOURCE_WEIGHTS, config=CONFIG, now=NOW,
        )

        assert len(result) == 1
        assert result[0].kalshi_ticker == market.ticker
        assert self._table_count(conn, "sportsbook_events") == 1
        assert self._table_count(conn, "kalshi_market_snapshots") == 1
        assert self._table_count(conn, "sportsbook_odds_snapshots") == len(odds)
        assert self._table_count(conn, "fair_price_snapshots") == 1
        assert self._table_count(conn, "event_mappings") == 1
        assert self._table_count(conn, "opportunities") == 1
        assert self._table_count(conn, "paper_orders") == 1

    def test_a_fully_stale_book_for_one_market_does_not_kill_the_scan(self, tmp_path):
        """`build_moneyline_fair_price` raises `ValueError` when an event has
        no usable two-sided odds at all (see P2.12). One market hitting that
        should not stop the other from scanning and persisting normally."""
        good_event = _event("4b1c2d3e", "Los Angeles Lakers", "Boston Celtics")
        good_market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        good_odds = _odds(good_event, MISPRICED_QUOTES)

        broken_event = _event("broken-1", "Golden State Warriors", "Phoenix Suns")
        broken_market = _market(ticker="KXNBAGAME-26JAN14GSWPHX-GSW", yes_sub_title="Warriors")
        # No odds rows at all for broken_event -> build_moneyline_fair_price raises.

        conn = storage.init_db(tmp_path / "t.sqlite")
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})

        result = pipeline.run_scan(
            markets=[broken_market, good_market],
            events=[good_event, broken_event],
            odds=good_odds,
            portfolio=portfolio,
            conn=conn,
            weights=DEFAULT_SOURCE_WEIGHTS,
            config=CONFIG,
            now=NOW,
        )

        assert len(result) == 1
        assert result[0].kalshi_ticker == good_market.ticker
        assert self._table_count(conn, "opportunities") == 1
        # Isolation is per-market, not transactional: the parent event and
        # Kalshi snapshot for the *failed* market were still written before
        # the exception fired downstream at the fair-price stage. Locking
        # this down so the behavior stays intentional, not accidental.
        assert conn.execute(
            "SELECT 1 FROM sportsbook_events WHERE event_id = ?", (broken_event.event_id,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM kalshi_market_snapshots WHERE ticker = ?", (broken_market.ticker,)
        ).fetchone() is not None
        # But nothing downstream of the failure point exists for it.
        assert conn.execute(
            "SELECT 1 FROM event_mappings WHERE kalshi_ticker = ?", (broken_market.ticker,)
        ).fetchone() is None

    def test_a_skip_opportunity_still_gets_a_paper_order_recorded(self, tmp_path):
        """CLAUDE.md claims the audit trail covers scans that didn't trade,
        not just fills — `paper_buy` already no-ops non-'buy' actions to a
        'rejected' PaperOrder, but that's only useful if run_scan actually
        calls it for skips too."""
        event = _event("4b1c2d3e", "Los Angeles Lakers", "Boston Celtics")
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        odds = _odds(event, FAIR_QUOTES)
        conn = storage.init_db(tmp_path / "t.sqlite")
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})

        result = pipeline.run_scan(
            markets=[market], events=[event], odds=odds, portfolio=portfolio,
            conn=conn, weights=DEFAULT_SOURCE_WEIGHTS, config=CONFIG, now=NOW,
        )

        assert result[0].action == "skip"
        row = conn.execute(
            "SELECT status, count FROM paper_orders WHERE opportunity_id = ?",
            (result[0].opportunity_id,),
        ).fetchone()
        assert row == ("rejected", 0)
        assert portfolio.cash_cents == 100_000, "a skipped opportunity must not touch cash"

    def test_a_buy_opportunity_debits_the_shared_portfolio(self, tmp_path):
        event = _event("4b1c2d3e", "Los Angeles Lakers", "Boston Celtics")
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        odds = _odds(event, MISPRICED_QUOTES)
        conn = storage.init_db(tmp_path / "t.sqlite")
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})

        result = pipeline.run_scan(
            markets=[market], events=[event], odds=odds, portfolio=portfolio,
            conn=conn, weights=DEFAULT_SOURCE_WEIGHTS, config=CONFIG, now=NOW,
        )

        assert result[0].action == "buy"
        row = conn.execute(
            "SELECT status, count FROM paper_orders WHERE opportunity_id = ?",
            (result[0].opportunity_id,),
        ).fetchone()
        assert row[0] == "filled"
        assert row[1] > 0
        assert portfolio.cash_cents < 100_000

    def test_only_odds_rows_belonging_to_the_matched_event_are_stored(self, tmp_path):
        """A real scan's `odds` list spans every event fetched that league-wide
        cycle, not just the one market being processed — `run_scan` must
        filter by `event_id` per market, not dump the whole list in."""
        event = _event("4b1c2d3e", "Los Angeles Lakers", "Boston Celtics")
        other_event = _event("other-event", "Golden State Warriors", "Phoenix Suns")
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        odds = _odds(event, MISPRICED_QUOTES) + _odds(other_event, MISPRICED_QUOTES)
        conn = storage.init_db(tmp_path / "t.sqlite")
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})

        pipeline.run_scan(
            markets=[market], events=[event, other_event], odds=odds,
            portfolio=portfolio, conn=conn, weights=DEFAULT_SOURCE_WEIGHTS,
            config=CONFIG, now=NOW,
        )

        stored_event_ids = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT event_id FROM sportsbook_odds_snapshots"
            ).fetchall()
        }
        assert stored_event_ids == {event.event_id}

    def test_a_second_market_resolving_to_an_already_scanned_event_is_skipped(self, tmp_path):
        """`run_scan` assumes exactly one moneyline market per event per
        scan. If a second ticker also resolves to an event another market in
        this same call already scanned (e.g. a regulation-time market and an
        overtime-inclusive market for the same game), it must be skipped
        rather than silently re-appending duplicate odds/fair-price rows for
        that event."""
        event = _event("4b1c2d3e", "Los Angeles Lakers", "Boston Celtics")
        first_market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        second_market = _market(ticker="KXNBAGAMEOT-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        odds = _odds(event, MISPRICED_QUOTES)
        conn = storage.init_db(tmp_path / "t.sqlite")
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})

        result = pipeline.run_scan(
            markets=[first_market, second_market], events=[event], odds=odds,
            portfolio=portfolio, conn=conn, weights=DEFAULT_SOURCE_WEIGHTS,
            config=CONFIG, now=NOW,
        )

        assert len(result) == 1
        assert result[0].kalshi_ticker == first_market.ticker
        assert self._table_count(conn, "sportsbook_odds_snapshots") == len(odds)
        assert self._table_count(conn, "fair_price_snapshots") == 1
        assert self._table_count(conn, "event_mappings") == 1
        assert self._table_count(conn, "opportunities") == 1

    def test_mapping_and_fair_price_fields_are_correct_not_just_present(self, tmp_path):
        """Row-count assertions alone would pass even if the wrong values were
        written to each column; check actual content for the fields that
        matter downstream (CLV depends on `fair_prob_at_entry`, which is
        derived from `mapped_yes_outcome` and `home_prob`/`away_prob`)."""
        event = _event("4b1c2d3e", "Los Angeles Lakers", "Boston Celtics")
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        odds = _odds(event, MISPRICED_QUOTES)
        conn = storage.init_db(tmp_path / "t.sqlite")
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})

        result = pipeline.run_scan(
            markets=[market], events=[event], odds=odds, portfolio=portfolio,
            conn=conn, weights=DEFAULT_SOURCE_WEIGHTS, config=CONFIG, now=NOW,
        )

        mapped_yes_outcome, mapping_confidence = conn.execute(
            "SELECT mapped_yes_outcome, confidence FROM event_mappings "
            "WHERE kalshi_ticker = ?", (market.ticker,),
        ).fetchone()
        assert mapped_yes_outcome == "home", "'Lakers' subtitle should resolve to the home team"
        assert mapping_confidence > CONFIG.min_mapping_confidence

        home_prob = conn.execute(
            "SELECT home_prob FROM fair_price_snapshots WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()[0]
        # MISPRICED_QUOTES implies the home side (Lakers) is a heavy underdog.
        assert home_prob < 0.5
        assert result[0].side == "no"


# ────────────────────────────────────────────────────────────────────────────
# run_settlement_check
# ────────────────────────────────────────────────────────────────────────────


class FakeKalshiClient:
    """Maps ticker -> a canned `get_market` payload (or an exception to
    raise, to exercise per-opportunity error isolation)."""

    def __init__(self, responses):
        self._responses = responses
        self.tickers_checked = []

    def get_market(self, ticker):
        self.tickers_checked.append(ticker)
        response = self._responses.get(ticker)
        if isinstance(response, Exception):
            raise response
        return response


class TestRunSettlementCheck:
    def _scan_one_opportunity(self, conn, *, quotes=MISPRICED_QUOTES):
        """Run a real scan so a genuine, filled opportunity exists to settle
        -- settlement should operate on real scan output, not a hand-built
        fixture that could drift from what run_scan actually persists."""
        event = _event("4b1c2d3e", "Los Angeles Lakers", "Boston Celtics")
        market = _market(ticker="KXNBAGAME-26JAN14LALBOS-LAL", yes_sub_title="Lakers")
        odds = _odds(event, quotes)
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})
        opportunities = pipeline.run_scan(
            markets=[market], events=[event], odds=odds, portfolio=portfolio,
            conn=conn, weights=DEFAULT_SOURCE_WEIGHTS, config=CONFIG, now=NOW,
        )
        return opportunities[0]

    def test_scores_a_resolved_no_win_and_persists_it(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        opp = self._scan_one_opportunity(conn)
        assert opp.side == "no", "fixture assumption: MISPRICED_QUOTES picks the NO side"
        client = FakeKalshiClient({opp.kalshi_ticker: {"result": "no"}})

        results = pipeline.run_settlement_check(conn=conn, kalshi_client=client, evaluated_at=NOW)

        assert len(results) == 1
        assert results[0].resolved_side == "no"
        assert results[0].pnl_cents > 0, "NO side, resolved no -> a win"
        row = conn.execute(
            "SELECT resolved_side, pnl_cents FROM trade_evaluations WHERE opportunity_id = ?",
            (opp.opportunity_id,),
        ).fetchone()
        assert row == (results[0].resolved_side, results[0].pnl_cents)

    def test_does_not_reprocess_an_already_settled_opportunity(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        opp = self._scan_one_opportunity(conn)
        client = FakeKalshiClient({opp.kalshi_ticker: {"result": "no"}})

        first = pipeline.run_settlement_check(conn=conn, kalshi_client=client, evaluated_at=NOW)
        second = pipeline.run_settlement_check(conn=conn, kalshi_client=client, evaluated_at=NOW)

        assert len(first) == 1
        assert len(second) == 0
        assert client.tickers_checked == [opp.kalshi_ticker], (
            "a settled opportunity must not be checked again on the next pass"
        )

    def test_skips_a_market_that_has_not_settled_yet(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        opp = self._scan_one_opportunity(conn)
        client = FakeKalshiClient({opp.kalshi_ticker: {"result": ""}})

        results = pipeline.run_settlement_check(conn=conn, kalshi_client=client, evaluated_at=NOW)

        assert results == []
        assert self._table_count(conn, "trade_evaluations") == 0
        assert storage.list_opportunities_pending_settlement(conn) == [opp.opportunity_id], (
            "still-open markets must remain pending for the next check"
        )

    def test_one_failing_lookup_does_not_stop_the_rest(self, tmp_path):
        conn = storage.init_db(tmp_path / "t.sqlite")
        good_opp = self._scan_one_opportunity(conn)

        broken_event = _event("broken-1", "Golden State Warriors", "Phoenix Suns")
        broken_market = _market(ticker="KXNBAGAME-26JAN14GSWPHX-GSW", yes_sub_title="Warriors")
        broken_odds = _odds(broken_event, MISPRICED_QUOTES)
        portfolio = PaperPortfolio(cash_cents=100_000, positions={})
        broken_opps = pipeline.run_scan(
            markets=[broken_market], events=[broken_event], odds=broken_odds,
            portfolio=portfolio, conn=conn, weights=DEFAULT_SOURCE_WEIGHTS,
            config=CONFIG, now=NOW,
        )
        broken_opp = broken_opps[0]

        client = FakeKalshiClient({
            good_opp.kalshi_ticker: {"result": "no"},
            broken_opp.kalshi_ticker: RuntimeError("Kalshi API is down"),
        })

        results = pipeline.run_settlement_check(conn=conn, kalshi_client=client, evaluated_at=NOW)

        assert len(results) == 1
        assert results[0].opportunity_id == good_opp.opportunity_id
        assert storage.list_opportunities_pending_settlement(conn) == [broken_opp.opportunity_id]

    def test_a_skip_only_opportunity_is_never_checked(self, tmp_path):
        """No filled order exists for a skip -- it must not even reach
        kalshi_client.get_market, let alone be scored."""
        conn = storage.init_db(tmp_path / "t.sqlite")
        opp = self._scan_one_opportunity(conn, quotes=FAIR_QUOTES)
        assert opp.action == "skip", "fixture assumption: FAIR_QUOTES produces a skip"
        client = FakeKalshiClient({opp.kalshi_ticker: {"result": "no"}})

        results = pipeline.run_settlement_check(conn=conn, kalshi_client=client, evaluated_at=NOW)

        assert results == []
        assert client.tickers_checked == []

    def _table_count(self, conn, table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
