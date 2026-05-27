"""Test suite for build_multisport_research_dataset.

Covers the pure-function layer: ticker parsing (variable team lengths,
doubleheader start times), team-code normalization, and the doubleheader-aware
event-to-schedule resolver. HTTP-bound fetchers are not exercised here.
"""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from build_multisport_research_dataset import (
    FIELDNAMES,
    KALSHI_TO_API_ABBR,
    KNOWN_TEAMS_BY_SPORT,
    SPORTS,
    _index_mlb_games_from_payload,
    _mlb_month_ranges,
    _split_team_codes,
    append_rows,
    normalize_kalshi_codes,
    parse_generic_ticker,
    resolve_events_to_metadata,
)


def sport_parser(name):
    """Return the lambda parser registered for `name` in SPORTS."""
    for sport in SPORTS:
        if sport.name == name:
            return sport.ticker_parser
    raise KeyError(name)


# ----------------------------------------------------------------------------
# _split_team_codes
# ----------------------------------------------------------------------------


class TestSplitTeamCodes:
    def test_3_3_split(self):
        nfl = KNOWN_TEAMS_BY_SPORT["NFL"]
        assert _split_team_codes("HOUPIT", nfl) == ("HOU", "PIT")

    def test_2_3_split(self):
        nfl = KNOWN_TEAMS_BY_SPORT["NFL"]
        assert _split_team_codes("LASEA", nfl) == ("LA", "SEA")

    def test_3_2_split(self):
        nfl = KNOWN_TEAMS_BY_SPORT["NFL"]
        assert _split_team_codes("HOUNE", nfl) == ("HOU", "NE")

    def test_2_2_split(self):
        nfl = KNOWN_TEAMS_BY_SPORT["NFL"]
        assert _split_team_codes("LASF", nfl) == ("LA", "SF")

    def test_ambiguous_prefers_longer_left(self):
        # LACHI: LA+CHI (valid) vs LAC+HI (HI not valid). Should pick LA+CHI.
        nfl = KNOWN_TEAMS_BY_SPORT["NFL"]
        assert _split_team_codes("LACHI", nfl) == ("LA", "CHI")

    def test_3_3_wins_when_both_splits_valid(self):
        # Construct a fake universe where both 3+3 and 2+3 are valid for input.
        # E.g. teams "ABC", "DEF", "AB", "CDEF" — give input "ABCDEF":
        # 3+3: ABC+DEF ✓, 2+4 not tried (right_len must be 2 or 3).
        # Here both ABC and DEF are valid, single answer.
        teams = {"ABC", "DEF", "AB", "CDE", "F", "BC"}
        assert _split_team_codes("ABCDEF", teams) == ("ABC", "DEF")

    def test_no_valid_split_returns_none(self):
        nfl = KNOWN_TEAMS_BY_SPORT["NFL"]
        assert _split_team_codes("ZZZZZZ", nfl) is None

    def test_unknown_teams_falls_back_to_3_3(self):
        # When known_teams is None, parser falls back to 3+3 (legacy).
        assert _split_team_codes("LALBOS", None) == ("LAL", "BOS")

    def test_too_short(self):
        assert _split_team_codes("XYZ", KNOWN_TEAMS_BY_SPORT["NFL"]) is None


# ----------------------------------------------------------------------------
# parse_generic_ticker
# ----------------------------------------------------------------------------


class TestParseGenericTicker:
    def test_nba_3_3(self):
        result = parse_generic_ticker(
            "KXNBAGAME-26JAN14LALBOS",
            "KXNBAGAME",
            known_teams=KNOWN_TEAMS_BY_SPORT["NBA"],
        )
        assert result is not None
        date, matchup, hhmm = result
        assert date == datetime(2026, 1, 14).date()
        assert matchup == ("BOS", "LAL")
        assert hhmm is None

    def test_nfl_2_3_rams_at_seahawks(self):
        result = sport_parser("NFL")("KXNFLGAME-26JAN25LASEA")
        assert result is not None
        date, matchup, hhmm = result
        assert date == datetime(2026, 1, 25).date()
        assert matchup == ("LA", "SEA")

    def test_nfl_3_2_texans_at_pats(self):
        result = sport_parser("NFL")("KXNFLGAME-26JAN18HOUNE")
        assert result is not None
        _, matchup, _ = result
        assert matchup == ("HOU", "NE")

    def test_nfl_2_2_rams_at_niners(self):
        # Hypothetical 2+2 — LA + SF
        result = sport_parser("NFL")("KXNFLGAME-26JAN18LASF")
        assert result is not None
        _, matchup, _ = result
        assert matchup == ("LA", "SF")

    def test_nhl_2_3_tampa_at_montreal(self):
        result = sport_parser("NHL")("KXNHLGAME-26MAY01TBMTL")
        assert result is not None
        _, matchup, _ = result
        assert matchup == ("MTL", "TB")

    def test_nhl_3_2_colorado_at_kings(self):
        result = sport_parser("NHL")("KXNHLGAME-26APR26COLLA")
        assert result is not None
        _, matchup, _ = result
        assert matchup == ("COL", "LA")

    def test_nhl_legacy_3_3_codes(self):
        # Older Kalshi NHL tickers (2024-25 era) used full 3-letter API codes.
        # Parser must still recognize LAK/TBL/NJD/SJS.
        result = sport_parser("NHL")("KXNHLGAME-25APR29NJDCAR")
        assert result is not None
        _, matchup, _ = result
        assert matchup == ("CAR", "NJD")

    def test_nhl_legacy_lak(self):
        result = sport_parser("NHL")("KXNHLGAME-25MAY01LAKEDM")
        assert result is not None
        _, matchup, _ = result
        assert matchup == ("EDM", "LAK")

    def test_mlb_az_diamondbacks(self):
        result = sport_parser("MLB")("KXMLBGAME-26MAY251705AZSF")
        assert result is not None
        date, matchup, hhmm = result
        assert matchup == ("AZ", "SF")
        assert hhmm == 1705

    def test_mlb_legacy_no_hhmm_format(self):
        # 2025 MLB tickers omitted HHMM; parser must fall back to date+teams layout.
        result = sport_parser("MLB")("KXMLBGAME-25APR18ATHMIL")
        assert result is not None
        date, matchup, hhmm = result
        assert date == datetime(2025, 4, 18).date()
        assert matchup == ("ATH", "MIL")
        assert hhmm is None

    def test_mlb_legacy_no_hhmm_az_chc(self):
        result = sport_parser("MLB")("KXMLBGAME-25APR18AZCHC")
        assert result is not None
        date, matchup, hhmm = result
        assert matchup == ("AZ", "CHC")
        assert hhmm is None

    def test_mlb_legacy_no_hhmm_lad_tex(self):
        result = sport_parser("MLB")("KXMLBGAME-25APR18LADTEX")
        assert result is not None
        _, matchup, hhmm = result
        assert matchup == ("LAD", "TEX")
        assert hhmm is None

    def test_mlb_legacy_2_3_teams(self):
        # 2025 ticker + 2-letter team (TB) — must parse without HHMM.
        result = sport_parser("MLB")("KXMLBGAME-25APR18NYYTB")
        assert result is not None
        _, matchup, hhmm = result
        assert matchup == ("NYY", "TB")
        assert hhmm is None

    def test_mlb_with_start_time(self):
        result = sport_parser("MLB")("KXMLBGAME-26MAY252140SEAATH")
        assert result is not None
        date, matchup, hhmm = result
        assert date == datetime(2026, 5, 25).date()
        assert matchup == ("ATH", "SEA")
        assert hhmm == 2140

    def test_mlb_doubleheader_distinct_hhmm(self):
        early = sport_parser("MLB")("KXMLBGAME-26MAY251710SEAATH")
        late = sport_parser("MLB")("KXMLBGAME-26MAY252140SEAATH")
        assert early is not None and late is not None
        assert early[2] == 1710
        assert late[2] == 2140
        assert early[0] == late[0]
        assert early[1] == late[1]

    def test_market_suffix_stripped(self):
        # `KXNBAGAME-26JAN14LALBOS-LAL` should parse the same as event ticker.
        result = parse_generic_ticker(
            "KXNBAGAME-26JAN14LALBOS-LAL",
            "KXNBAGAME",
            known_teams=KNOWN_TEAMS_BY_SPORT["NBA"],
        )
        assert result is not None
        assert result[1] == ("BOS", "LAL")

    def test_wrong_series_prefix(self):
        result = parse_generic_ticker(
            "KXNFLGAME-26JAN14LALBOS",
            "KXNBAGAME",
            known_teams=KNOWN_TEAMS_BY_SPORT["NBA"],
        )
        assert result is None

    def test_invalid_date(self):
        result = parse_generic_ticker(
            "KXNBAGAME-26ZZZ14LALBOS",
            "KXNBAGAME",
            known_teams=KNOWN_TEAMS_BY_SPORT["NBA"],
        )
        assert result is None

    def test_empty_ticker(self):
        assert parse_generic_ticker("", "KXNBAGAME") is None

    def test_no_known_teams_falls_back_to_3_3(self):
        result = parse_generic_ticker("KXNBAGAME-26JAN14LALBOS", "KXNBAGAME")
        assert result is not None
        assert result[1] == ("BOS", "LAL")


# ----------------------------------------------------------------------------
# normalize_kalshi_codes
# ----------------------------------------------------------------------------


class TestNormalizeKalshiCodes:
    def test_nfl_jac_to_jax(self):
        assert normalize_kalshi_codes("NFL", ("BUF", "JAC")) == ("BUF", "JAX")

    def test_nfl_was_to_wsh(self):
        assert normalize_kalshi_codes("NFL", ("PHI", "WAS")) == ("PHI", "WSH")

    def test_nfl_la_to_lar(self):
        assert normalize_kalshi_codes("NFL", ("LA", "SEA")) == ("LAR", "SEA")

    def test_nhl_tb_to_tbl(self):
        assert normalize_kalshi_codes("NHL", ("MTL", "TB")) == ("MTL", "TBL")

    def test_nhl_la_to_lak(self):
        assert normalize_kalshi_codes("NHL", ("COL", "LA")) == ("COL", "LAK")

    def test_nhl_legacy_lak_passes_through(self):
        # Legacy NHL tickers already use the API code — no rewrite needed.
        assert normalize_kalshi_codes("NHL", ("EDM", "LAK")) == ("EDM", "LAK")

    def test_mlb_az_passthrough(self):
        # 2026 Kalshi uses "AZ" — matches MLB Stats API.
        assert normalize_kalshi_codes("MLB", ("AZ", "SF")) == ("AZ", "SF")

    def test_mlb_legacy_ari_to_az(self):
        # 2025 Kalshi tickers used "ARI" — alias to "AZ" to match MLB Stats API.
        assert normalize_kalshi_codes("MLB", ("ARI", "SD")) == ("AZ", "SD")

    def test_nba_no_aliases(self):
        assert normalize_kalshi_codes("NBA", ("LAL", "BOS")) == ("BOS", "LAL")

    def test_unknown_sport(self):
        assert normalize_kalshi_codes("CRICKET", ("AAA", "BBB")) == ("AAA", "BBB")

    def test_output_is_sorted(self):
        # Even after rewriting, the returned matchup must be sorted so it can
        # serve as a dict key alongside the schedule index.
        result = normalize_kalshi_codes("NFL", ("LA", "BUF"))
        assert result == tuple(sorted(result))


# ----------------------------------------------------------------------------
# resolve_events_to_metadata
# ----------------------------------------------------------------------------


def _make_metadata(start_utc):
    return {
        "home_team": "X",
        "away_team": "Y",
        "scheduled_tipoff_utc": start_utc,
        "adjusted_tipoff_utc": start_utc + timedelta(minutes=12),
        "game_label": "Regular",
    }


class TestResolveEventsToMetadata:
    def test_simple_one_to_one(self):
        events = {"KXNBAGAME-26JAN14LALBOS": [{}, {}]}
        schedule = {
            (datetime(2026, 1, 14).date(), ("BOS", "LAL")): [
                _make_metadata(datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc))
            ]
        }
        resolved = resolve_events_to_metadata(events, schedule, sport_parser("NBA"), "NBA")
        assert "KXNBAGAME-26JAN14LALBOS" in resolved
        assert resolved["KXNBAGAME-26JAN14LALBOS"]["scheduled_tipoff_utc"].hour == 1

    def test_mlb_doubleheader_paired_by_time_order(self):
        early_ticker = "KXMLBGAME-26MAY251710SEAATH"
        late_ticker = "KXMLBGAME-26MAY252140SEAATH"
        events = {early_ticker: [{}, {}], late_ticker: [{}, {}]}
        early_game = _make_metadata(datetime(2026, 5, 25, 20, 10, tzinfo=timezone.utc))
        late_game = _make_metadata(datetime(2026, 5, 26, 0, 40, tzinfo=timezone.utc))
        schedule = {
            (datetime(2026, 5, 25).date(), ("ATH", "SEA")): [late_game, early_game],
        }
        resolved = resolve_events_to_metadata(events, schedule, sport_parser("MLB"), "MLB")
        assert resolved[early_ticker]["scheduled_tipoff_utc"] == early_game["scheduled_tipoff_utc"]
        assert resolved[late_ticker]["scheduled_tipoff_utc"] == late_game["scheduled_tipoff_utc"]

    def test_nfl_alias_normalization(self):
        # Kalshi has JAC, schedule has JAX — resolver must normalize before lookup.
        events = {"KXNFLGAME-26JAN11BUFJAC": [{}, {}]}
        schedule = {
            (datetime(2026, 1, 11).date(), ("BUF", "JAX")): [
                _make_metadata(datetime(2026, 1, 11, 18, 0, tzinfo=timezone.utc))
            ]
        }
        resolved = resolve_events_to_metadata(events, schedule, sport_parser("NFL"), "NFL")
        assert "KXNFLGAME-26JAN11BUFJAC" in resolved

    def test_nhl_variable_length_with_alias(self):
        # Kalshi: TB+MTL → after alias TBL+MTL → matches schedule key (MTL, TBL).
        events = {"KXNHLGAME-26MAY01TBMTL": [{}, {}]}
        schedule = {
            (datetime(2026, 5, 1).date(), ("MTL", "TBL")): [
                _make_metadata(datetime(2026, 5, 2, 0, 30, tzinfo=timezone.utc))
            ]
        }
        resolved = resolve_events_to_metadata(events, schedule, sport_parser("NHL"), "NHL")
        assert "KXNHLGAME-26MAY01TBMTL" in resolved

    def test_no_schedule_match_omitted(self):
        events = {"KXNBAGAME-26JAN14LALBOS": [{}, {}]}
        schedule = {}
        resolved = resolve_events_to_metadata(events, schedule, sport_parser("NBA"), "NBA")
        assert resolved == {}

    def test_unparseable_ticker_omitted(self):
        events = {"GARBAGE-TICKER": [{}, {}]}
        schedule = {}
        resolved = resolve_events_to_metadata(events, schedule, sport_parser("NBA"), "NBA")
        assert resolved == {}


# ----------------------------------------------------------------------------
# CSV output: append_rows
# ----------------------------------------------------------------------------


def _example_row():
    row = {f: "" for f in FIELDNAMES}
    row.update({
        "Sport": "NBA",
        "Series_Ticker": "KXNBAGAME",
        "Event_Ticker": "KXNBAGAME-26JAN14LALBOS",
        "Date": "2026-01-14",
        "Home_Team": "BOS",
        "Away_Team": "LAL",
        "Scheduled_Start_UTC": "2026-01-15T01:00:00+00:00",
        "Adjusted_Start_UTC": "2026-01-15T01:12:00+00:00",
        "Window_Start_UTC": "2026-01-15T00:45:00+00:00",
        "Favorite_Market_Ticker": "KXNBAGAME-26JAN14LALBOS-BOS",
        "Favorite_Team": "Boston",
        "Favorite_Avg_Ask_Cents": 92,
        "Favorite_Total_Volume": 1234.5,
        "Favorite_Won": True,
        "Favorite_Hold_To_Settle_PnL_Cents": 8,
    })
    return row


class TestAppendRows:
    def test_fresh_write_creates_header_and_description_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            append_rows(path, [_example_row()])
            with open(path) as f:
                reader = csv.reader(f)
                rows = list(reader)
            assert rows[0] == FIELDNAMES
            assert rows[1][0].startswith("Description: "), "second row must be description"
            assert rows[2][2] == "KXNBAGAME-26JAN14LALBOS"

    def test_append_skips_duplicate_event_ticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            append_rows(path, [_example_row()])
            append_rows(path, [_example_row()])
            with open(path) as f:
                rows = list(csv.reader(f))
            # header + description + 1 data row (no dupe)
            assert len(rows) == 3

    def test_schema_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Wrong", "Schema"])
            with pytest.raises(ValueError, match="schema mismatch"):
                append_rows(path, [_example_row()])

    def test_empty_rows_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            append_rows(path, [])
            assert not os.path.exists(path)


# ----------------------------------------------------------------------------
# MLB schedule helpers
# ----------------------------------------------------------------------------


class TestMlbMonthRanges:
    def test_single_month_returns_one_chunk(self):
        start = datetime(2026, 5, 1).date()
        end = datetime(2026, 5, 31).date()
        chunks = list(_mlb_month_ranges(start, end))
        assert chunks == [(start, end)]

    def test_spans_three_months(self):
        start = datetime(2026, 4, 15).date()
        end = datetime(2026, 6, 10).date()
        chunks = list(_mlb_month_ranges(start, end))
        assert chunks == [
            (datetime(2026, 4, 15).date(), datetime(2026, 4, 30).date()),
            (datetime(2026, 5, 1).date(), datetime(2026, 5, 31).date()),
            (datetime(2026, 6, 1).date(), datetime(2026, 6, 10).date()),
        ]

    def test_year_boundary(self):
        start = datetime(2025, 12, 20).date()
        end = datetime(2026, 1, 10).date()
        chunks = list(_mlb_month_ranges(start, end))
        assert chunks == [
            (datetime(2025, 12, 20).date(), datetime(2025, 12, 31).date()),
            (datetime(2026, 1, 1).date(), datetime(2026, 1, 10).date()),
        ]

    def test_chunks_are_contiguous_with_no_overlap(self):
        start = datetime(2025, 3, 5).date()
        end = datetime(2026, 9, 20).date()
        chunks = list(_mlb_month_ranges(start, end))
        # Each chunk's end must be exactly 1 day before the next chunk's start.
        for prev, nxt in zip(chunks, chunks[1:]):
            assert nxt[0] == prev[1] + timedelta(days=1)
        # First and last chunks span the requested range.
        assert chunks[0][0] == start
        assert chunks[-1][1] == end


class TestIndexMlbGamesFromPayload:
    def _payload(self):
        return {
            "dates": [
                {
                    "games": [
                        {
                            "gameDate": "2026-05-26T02:40:00Z",  # 10:40pm ET May 25
                            "teams": {
                                "home": {"team": {"id": 133}},  # ATH
                                "away": {"team": {"id": 136}},  # SEA
                            },
                            "seriesDescription": "Regular Season",
                        }
                    ]
                }
            ]
        }

    def test_indexes_by_local_et_date_not_utc(self):
        team_abbr = {133: "ATH", 136: "SEA"}
        index = {}
        added = _index_mlb_games_from_payload(self._payload(), team_abbr, index)
        assert added == 1
        # The UTC date is May 26, but ET local date is May 25.
        assert (datetime(2026, 5, 25).date(), ("ATH", "SEA")) in index

    def test_missing_team_id_dropped(self):
        team_abbr = {133: "ATH"}  # SEA missing
        index = {}
        added = _index_mlb_games_from_payload(self._payload(), team_abbr, index)
        assert added == 0
        assert index == {}

    def test_empty_payload_safe(self):
        index = {}
        added = _index_mlb_games_from_payload(None, {}, index)
        assert added == 0
        assert index == {}

    def test_doubleheader_produces_two_entries(self):
        payload = {
            "dates": [
                {
                    "games": [
                        {
                            "gameDate": "2026-05-25T17:10:00Z",
                            "teams": {
                                "home": {"team": {"id": 133}},
                                "away": {"team": {"id": 136}},
                            },
                        },
                        {
                            "gameDate": "2026-05-25T21:40:00Z",
                            "teams": {
                                "home": {"team": {"id": 133}},
                                "away": {"team": {"id": 136}},
                            },
                        },
                    ]
                }
            ]
        }
        team_abbr = {133: "ATH", 136: "SEA"}
        index = {}
        _index_mlb_games_from_payload(payload, team_abbr, index)
        key = (datetime(2026, 5, 25).date(), ("ATH", "SEA"))
        assert len(index[key]) == 2


# ----------------------------------------------------------------------------
# Sport registry sanity
# ----------------------------------------------------------------------------


class TestSportRegistry:
    def test_each_sport_has_required_fields(self):
        for sport in SPORTS:
            assert sport.name
            assert sport.series_ticker
            assert callable(sport.ticker_parser)
            assert callable(sport.schedule_fetcher)

    def test_known_teams_cover_each_sport(self):
        for sport in SPORTS:
            assert sport.name in KNOWN_TEAMS_BY_SPORT, (
                f"missing KNOWN_TEAMS_BY_SPORT entry for {sport.name}"
            )

    def test_aliases_map_to_known_api_codes(self):
        # Loose check: every Kalshi code that has an alias should itself be a
        # known Kalshi team code for that sport.
        for sport_name, alias_map in KALSHI_TO_API_ABBR.items():
            known = KNOWN_TEAMS_BY_SPORT.get(sport_name, set())
            for kalshi_code in alias_map:
                assert kalshi_code in known, (
                    f"{sport_name} alias '{kalshi_code}' is not in KNOWN_TEAMS_BY_SPORT"
                )
