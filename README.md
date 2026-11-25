[![make.fun](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/vinzagent/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed-entry/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/vinzagent/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed-entry/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

# Steady Tide — Pump.fun Bot Sprint

A conservative paper-trading bot for the [Pump.fun Bot Sprint](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) on make.fun. It trades paper SOL on a replay of pump.fun bonding-curve chain data (`agent-fun/replay/v2`), ranked by net PnL against holding cash.

## Strategy

Survival first, then upside. The bot never snipes fresh launches, never uses
leverage or shorts, and never martingales.

- **Entry — momentum with proof.** A coin becomes a candidate only after it
  is at least 300 slots old and its recent tape shows broad, accelerating buy
  pressure: net SOL flowing in (≥ 0.5 SOL over 40 slots), ≥ 4 buys from ≥ 4
  distinct wallets, price up 5–120% and sitting within 10% of its window
  high, second-half inflow at least matching the first half, and no single
  buyer above 60% of buy SOL. Real SOL reserves must sit between 1.5 and
  80 SOL.
- **Sizing — probe, then pyramid into strength only.** First buy 0.05 SOL.
  Adds of 0.10 SOL, up to 0.50 SOL per coin, only while the price stepped up
  from the last buy, the position is up ≥ 5%, and flow is still positive.
  Total exposure capped at 4 SOL of the 10 SOL capital; at most 5 open
  positions. Every buy respects the 1%-of-real-reserve size cap.
- **Exits — explicit, whole-position, fast.** Stop-loss −9% vs cost,
  take-profit +18%, trailing stop (arms at +10%, sells on a 10% fall from
  the peak), fade exit when 20-slot flow turns below −0.30 SOL, exit on any
  single recorded sell ≥ 2 SOL or on the creator selling, and a 400-slot
  hold limit (waived while up ≥ 15%). Winners are banked quickly and
  losers are cut before they can bleed: the first version's wide
  +50%/−15% exits let losers run while winners rarely arrived.
- **Guardrail sleeve.** A tiny 0.02 SOL probe round trip (bought, sold as
  soon as it lands) keeps the run above the competition's bar of 20 coins
  with a round trip of ≥ 0.01 SOL whenever the main strategy's pace lags.

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

All in `PARAMS` at the top of `main.py`: entry window 30 slots, min age 200
slots, initial 0.05 SOL / add 0.10 SOL / max stake 0.50 SOL per coin, max
exposure 4 SOL, stop-loss 9%, take-profit 18%, trailing 10% from peak
(arms at +10%), max hold 400 slots, probe 0.02 SOL.
