#!/bin/sh
# Bot launcher. BOT_MODE controls whether real orders can be placed.
#   BOT_MODE=dry   (default)  python -m nba_trading.main            (no orders)
#   BOT_MODE=live             python -m nba_trading.main --live     (REAL MONEY)
#
# The bot's --live flag normally asks you to type LIVE on a terminal. A
# restarting container has no terminal, so the bot also accepts BOT_LIVE_CONFIRM=LIVE
# in the environment as the explicit confirmation. Live mode here requires both
# BOT_MODE=live and BOT_LIVE_CONFIRM=LIVE; anything else refuses to start.
set -eu

mode="${BOT_MODE:-dry}"

case "$mode" in
  dry)
    echo "entrypoint: BOT_MODE=dry -> dry run, no orders will be placed"
    exec python -m nba_trading.main
    ;;
  live)
    if [ "${BOT_LIVE_CONFIRM:-}" != "LIVE" ]; then
      echo "entrypoint: BOT_MODE=live requires BOT_LIVE_CONFIRM=LIVE. Refusing to start." >&2
      exit 64
    fi
    echo "entrypoint: BOT_MODE=live -> REAL MONEY (KALSHI_ENV=${KALSHI_ENV:-prod})"
    exec python -m nba_trading.main --live
    ;;
  *)
    echo "entrypoint: unknown BOT_MODE='$mode' (expected dry or live). Refusing to start." >&2
    exit 64
    ;;
esac
