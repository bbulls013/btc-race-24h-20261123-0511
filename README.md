# Lumen Drift

[![make.fun competition](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![make.fun rank](https://make.fun/badge/btc-race-24h-20261123-0511/bbulls013/btc-race-24h-20261123-0511/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)
[![make.fun reward](https://make.fun/badge/btc-race-24h-20261123-0511/bbulls013/btc-race-24h-20261123-0511/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511)

A deterministic minute-bar trading bot for the make.fun bounty
[Race a BTC trading bot live for 24 hours](https://make.fun/bounty/btc-race-24h-20261123-0511).
Simulation only: paper money against real cbBTC prices, no real funds move.

## How it works

`main.py` speaks the `agent-fun/bars-replay/v1` protocol: it reads one JSON
line per minute on stdin (a hello, then one batch per 1-minute cbBTC bar) and
answers every batch with exactly one reply line carrying its orders.

The strategy, in short:

- Stay in cash unless the trend is up: fast EMA above slow EMA, price above
  the slow EMA, and the slow EMA itself rising.
- Enter long on a not-too-extended bar or a fresh breakout, deploying a
  capped share of equity.
- Leave on a take-profit, a stop-loss, a trailing stop below the peak close
  since entry, a trend flip, or a time stop.
- A small fallback entry late in the window guarantees at least one trade,
  and a drawdown guard halts new entries well before the 25% race limit.

## Properties

- Python 3 standard library only; nothing to install.
- No network, no wall clock, no file access.
- Pure function of the input stream: two runs on the same bars emit
  byte-identical orders.

## Run

```sh
python3 main.py < bars.ndjson
```

## License

MIT. See [LICENSE](LICENSE).
