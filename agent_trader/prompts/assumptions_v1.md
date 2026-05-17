# Trading Assumptions v1

> User-owned. Edit directly; do not version-bump on minor edits. The reflection
> agent MUST NOT propose revisions to this file — it is the human-set anchor that
> grounds every research plan and decision framework downstream.

## Risk policy

- Sizing rule: fractional Kelly, f = 0.25 × edge
- Per-bet cap: 10% of current bankroll
- Per-cycle cap: 25% of current bankroll across all new positions
- Max correlated exposure: 30% of bankroll on bets sharing a primary driver
  (e.g. three NBA games on the same night with overlapping injury news)

## Bankroll

- Amount in account: $100

## Goal

What the agent is optimizing for. 

- Goal: Maximize Profit

## Variables the AI must consider

Not exhaustive list. The agent may add its own per market. These are the floor — research plans that ignore any of these are incomplete.

- Time to Closure 
- Opportunity Cost
- Limit orders that must be placed
- Bet sizing: this should be a math-grounded choice, not arbitrary
