"""
Diagnose why market_discovery returns 0 eligible markets.

Paginates Kalshi /markets?status=open and tallies, per-market, which filter
rejected it (or whether it passed). Also prints volume / hours-to-close
distributions so you can pick sensible thresholds.

Usage:
  python -m agent_trader.testing_scripts.diagnose_discovery
  python -m agent_trader.testing_scripts.diagnose_discovery --min-volume 50 --min-hours 24
  python -m agent_trader.testing_scripts.diagnose_discovery --max-pages 5 --show-passing
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from kalshi.client import KalshiClient
from kalshi.config import API_KEY_ID, BASE_URL, PRIVATE_KEY_PATH


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-volume", type=int, default=100)
    parser.add_argument("--min-hours", type=int, default=48)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--show-passing", action="store_true",
                        help="Print the tickers of markets that passed all filters.")
    parser.add_argument("--show-top-volume", type=int, default=10,
                        help="Print N markets with highest volume (regardless of pass/fail).")
    parser.add_argument("--dump-first", action="store_true",
                        help="Dump the full raw JSON of the first market returned by the API. "
                             "Use this to inspect field names when filters mysteriously reject everything.")
    parser.add_argument("--dump-keys", action="store_true",
                        help="Print the set of keys present on each market response (sorted, deduped).")
    parser.add_argument("--series-ticker", type=str, default=None,
                        help="Narrow the API query to a single series (e.g. KXNBAGAME, KXPRES). "
                             "Useful for probing whether liquid markets exist when the unfiltered "
                             "list is dominated by provisional placeholders.")
    args = parser.parse_args()

    client = KalshiClient(BASE_URL, API_KEY_ID, PRIVATE_KEY_PATH)
    now = datetime.now(timezone.utc)

    reasons = {
        "total_returned": 0,
        "low_volume": 0,
        "no_open_interest": 0,
        "no_close_time": 0,
        "closing_too_soon": 0,
        "passed": 0,
    }
    volume_dist: list[int] = []
    hours_dist: list[float] = []
    passing: list[tuple[str, int, float]] = []
    all_markets: list[tuple[str, int, int, float | None]] = []

    cursor = None
    pages = 0
    api_error = None
    first_dumped = False
    all_keys: set[str] = set()
    while pages < args.max_pages:
        try:
            extra = {"series_ticker": args.series_ticker} if args.series_ticker else {}
            markets, cursor = client.list_markets(
                status="open", limit=args.page_size, cursor=cursor, **extra
            )
        except Exception as e:
            api_error = repr(e)
            break
        pages += 1
        if not markets:
            break
        if args.dump_first and not first_dumped and markets:
            import json as _json
            print("\n--- RAW FIRST MARKET ---")
            print(_json.dumps(markets[0], indent=2, default=str))
            print("--- END RAW FIRST MARKET ---\n")
            first_dumped = True
        if args.dump_keys:
            for m in markets:
                all_keys.update(m.keys())
        for m in markets:
            reasons["total_returned"] += 1
            vol = float(m.get("volume_24h_fp") or m.get("volume_fp") or 0)
            oi = float(m.get("open_interest_fp") or 0)
            ct_raw = m.get("close_time")
            ticker = m.get("ticker", "?")

            volume_dist.append(vol)

            hours = None
            if ct_raw:
                try:
                    close_dt = datetime.fromisoformat(ct_raw.replace("Z", "+00:00"))
                    hours = (close_dt - now).total_seconds() / 3600.0
                except Exception:
                    hours = None
            all_markets.append((ticker, vol, oi, hours))

            if vol < args.min_volume:
                reasons["low_volume"] += 1
                continue
            if oi <= 0:
                reasons["no_open_interest"] += 1
                continue
            if hours is None:
                reasons["no_close_time"] += 1
                continue
            hours_dist.append(hours)
            if hours < args.min_hours:
                reasons["closing_too_soon"] += 1
                continue
            reasons["passed"] += 1
            passing.append((ticker, vol, hours))
        if not cursor:
            break

    print("\n" + "=" * 60)
    print(f"Pages walked: {pages} (max {args.max_pages})")
    if api_error:
        print(f"API error encountered: {api_error}")
    print(f"Filters: min_volume={args.min_volume}, min_hours={args.min_hours}")
    print("=" * 60)
    print("\nRejection tally:")
    for k, v in reasons.items():
        pct = (100.0 * v / reasons["total_returned"]) if reasons["total_returned"] else 0.0
        print(f"  {k:<22} {v:>6}  ({pct:5.1f}%)")

    if volume_dist:
        sv = sorted(volume_dist)
        n = len(sv)
        print(f"\nVolume_24h distribution (n={n}):")
        print(f"  min      = {sv[0]}")
        print(f"  p25      = {sv[n // 4]}")
        print(f"  median   = {sv[n // 2]}")
        print(f"  p75      = {sv[3 * n // 4]}")
        print(f"  p95      = {sv[int(0.95 * n)]}")
        print(f"  max      = {sv[-1]}")
        print(f"  >0       = {sum(1 for v in sv if v > 0)} markets")
        print(f"  >=10     = {sum(1 for v in sv if v >= 10)} markets")
        print(f"  >=100    = {sum(1 for v in sv if v >= 100)} markets")

    if hours_dist:
        sh = sorted(hours_dist)
        n = len(sh)
        print(f"\nHours-to-close distribution (post volume + OI filter, n={n}):")
        print(f"  min      = {sh[0]:.1f}")
        print(f"  median   = {sh[n // 2]:.1f}")
        print(f"  max      = {sh[-1]:.1f}")

    if args.show_top_volume:
        top = sorted(all_markets, key=lambda x: -x[1])[: args.show_top_volume]
        print(f"\nTop {args.show_top_volume} markets by volume:")
        print(f"  {'ticker':<40} {'volume':>8} {'OI':>6} {'hours':>8}")
        for ticker, vol, oi, hours in top:
            hours_str = f"{hours:.1f}" if hours is not None else "?"
            print(f"  {ticker:<40} {vol:>8} {oi:>6} {hours_str:>8}")

    if args.dump_keys and all_keys:
        print(f"\nAll keys seen across {reasons['total_returned']} markets:")
        for k in sorted(all_keys):
            print(f"  {k}")

    if args.show_passing and passing:
        print(f"\nPassing markets ({len(passing)}):")
        for ticker, vol, hours in passing[:50]:
            print(f"  {ticker:<40} vol={vol:>6} hours={hours:.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
