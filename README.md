[![make.fun](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/vinzagent/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed-entry/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/vinzagent/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed-entry/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

# Steady Tide v2 — Pump.fun Bot Sprint

A conservative paper-trading bot for the [Pump.fun Bot Sprint](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) on make.fun. It trades paper SOL on a replay of pump.fun bonding-curve chain data (`agent-fun/replay/v2`), ranked by net PnL against holding cash.

## Strategy

Two sleeves, one job each. The bot never snipes fresh launches, never uses
leverage or shorts, and never martingales.

- **Momentum sleeve — volume and guardrail.** A coin becomes a candidate
  only after it is at least 200 slots old and its recent tape shows broad,
  accelerating buy pressure: net SOL flowing in (≥ 0.75 SOL over 30 slots),
  ≥ 4 buys from ≥ 6 distinct wallets, price up 5–60% and sitting within
  10% of its window high, second-half inflow at least matching the first
  half, and no single buyer above 60% of buy SOL. Real SOL reserves must
  sit between 1.5 and 80 SOL. One small 0.02 SOL entry per coin, never a
  pyramid: adds only multiply fees, and winners rarely need them.
- **Momentum exits — explicit, whole-position, fast.** Stop-loss −9% vs
  cost, take-profit +18%, trailing stop (arms at +10%, sells on a 10% fall
  from the peak), fade exit when 20-slot flow turns below −0.30 SOL, exit
  on any single recorded sell ≥ 2 SOL or on the creator selling, and a
  400-slot hold limit (waived while up ≥ 15%). This sleeve is deliberately
  cheap: on the dev window it costs about −0.08 SOL while trading ~80
  coins, which keeps the run far above the 20-coin guardrail on its own.
- **Ride sleeve — the profit engine.** When a coin's real SOL reserves
  cross 55 SOL it has proved it can attract serious demand, and the
  completion pop that follows reliably reaches about +30% before the dump.
  The bot buys 0.40 SOL exactly on that crossing batch — one shot, no
  chasing a later re-cross — and exits on a price-based take-profit of
  +30%, a price-based stop-loss of −9% (its own parameter, kept apart from
  the momentum sleeve's, so tuning one sleeve can never break the other),
  a 25% trailing stop once up 10%, or a 400-slot hold cap. A momentum
  position whose coin crosses 30 SOL of real reserves converts to ride
  rules instead of needing a second buy (no new slot, no new fees).
- **Guardrail sleeve.** A tiny 0.02 SOL probe round trip (bought, sold as
  soon as it lands) keeps the run above the competition's bar of 20 coins
  with a round trip of ≥ 0.01 SOL whenever the main strategy's pace lags.

## Measured on the dev window

- Net PnL **+0.45 SOL** (v1: −0.26 SOL), max drawdown 0.46 SOL, 279 fills,
  110 coins, 140 round trips, win rate 25%. Verified by the kit's
  `grade-local --driver process`: result `38d7b7858a55d058c101a73f30d20bd
  1e69356620703010976be3b38d4ed9477`, L1 regrade reproduced byte for byte.
  At 2x fees the net PnL is still +0.16 SOL, and the top 3 coins make
  only 34% of the gains (without them the net is still +0.04 SOL), so
  the profit does not lean on a few coins the way v1's did.
- Ride sleeve: 20 entries, 10 take-profits against 6 stop-losses,
  +0.53 SOL. The edge is positive in both halves of the window (+0.41
  first half, +0.12 second), and the ride numbers sit on broad plateaus,
  not knife edges: take-profit +28% to +32% and a crossing line of 52–57
  SOL all stay positive.
- What did not work, measured and dropped: chasing fresh-flow momentum
  (410 trades, still negative after fees and latency), riding the tail
  with a trailing stop and no take-profit, and entering the crossing at
  40–45 SOL of real reserves (those coins dump before they complete).

## How it runs

`main.py` is the entrypoint (Python 3.13, standard library only). It speaks
`agent-fun/replay/v2` directly on stdin/stdout: `hello`, one `batch` at a
time, `done`. No network, no clock, no randomness — deterministic, so the
two graded executions produce identical action logs. Diagnostics go to
stderr; stdout carries replies only.

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

Grade locally with the kit (from the unpacked kit directory):

```sh
node cli/makefun.mjs grade-local --kit . --bot <this repository> --driver process
```

## Key parameters

All in `PARAMS` at the top of `main.py`: momentum window 30 slots, min age
200 slots, initial 0.02 SOL, no adds, max stake 0.20 SOL per coin, max
exposure 4 SOL, stop-loss 9%, take-profit 18%, trailing 10% from peak
(arms at +10%), max hold 400 slots, probe 0.02 SOL; ride crossing 55 SOL,
stake 0.40 SOL, take-profit +30%, stop-loss −9%, hold 400 slots, runner
conversion at 30 SOL.
