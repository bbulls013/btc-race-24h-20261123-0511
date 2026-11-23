#!/usr/bin/env python3
"""Lumen Drift — a deterministic cbBTC minute-bar trading bot.

Speaks agent-fun/bars-replay/v1: reads newline-delimited JSON on stdin
(hello, batch..., done) and answers every batch with exactly one reply
line on stdout echoing the batch seq.

Strategy in one paragraph: stay in cash unless the short trend is up.
When the fast EMA sits above the slow EMA, the slow EMA is rising, and
price pulls back well below its rolling mean (or breaks out of its
recent range), open a long with most of the equity. Leave on a
take-profit, a stop-loss, a wide trailing stop, a trend flip, or a
long time stop, then sit out a cooldown before re-entering. A small
fallback entry mid-window guarantees at least one trade, and a
drawdown guard halts new entries long before the 25% race limit.

Standard library only. No network, no wall clock, no file access.
Pure function of the input stream, so two runs on the same bars emit
byte-identical orders.
"""

import json
import math
import sys

# ---- tunables (fixed constants; tuned on the kit dev window) ----
FAST = 20            # fast EMA length, bars
SLOW = 75            # slow EMA length, bars
MEAN_WIN = 60        # rolling mean / stdev window for the z-score
ENTRY_Z = -0.6       # buy on a real pullback: z-score at or below this
BREAKOUT_WIN = 60    # breakout lookback, bars
BREAKOUT_PAD = 0.0004  # breakout must clear the lookback high by this
TAKE_PROFIT = 0.035  # +3.5% from entry fill estimate
STOP_LOSS = 0.012    # -1.2% from entry fill estimate
TRAIL = 0.03         # trailing stop: exit 3.0% below the peak close since entry
TIME_STOP = 1400     # bars before a position is closed regardless
ALLOC = 0.98         # share of equity deployed per entry
MAX_WEIGHT = 0.92    # hard cap on position value / equity
DD_HALT = 0.08       # stop opening entries past this drawdown
DD_FLAT = 0.13       # flatten everything past this drawdown
FALLBACK_BAR = 700   # fallback entry no later than this bar
FALLBACK_ALLOC = 0.22
MAX_SLIP_BPS = 80
MIN_BARS = 120       # bars of history required before any entry
SLOPE_WIN = 60       # slow-EMA slope lookback, bars
COOLDOWN = 240       # bars to sit out after any exit


class Bot:
    def __init__(self):
        self.symbol = None
        self.price_scale = 5
        self.qty_scale = 8
        self.quote_scale = 6
        self.min_notional = 1_000_000
        self.warmup = 0
        self.closes = []
        self.ema_fast = None
        self.ema_slow = None
        self.slow_hist = []
        self.cooldown_until = -1
        self.bar = -1
        self.entry_bar = None
        self.entry_px = None
        self.peak_px = None
        self.trades = 0
        self.peak_equity = None
        self.order_n = 0
        self.pending = set()  # client_ids sent, fill not yet reported

    # ---------- helpers ----------
    def px(self, raw):
        return int(raw) / (10 ** self.price_scale)

    def next_id(self, seq):
        self.order_n += 1
        return "o%d-%d" % (seq, self.order_n)

    def zscore(self):
        win = self.closes[-MEAN_WIN:]
        if len(win) < 20:
            return 0.0
        mean = sum(win) / len(win)
        var = sum((x - mean) ** 2 for x in win) / len(win)
        sd = math.sqrt(var)
        if sd <= 0:
            return 0.0
        return (self.closes[-1] - mean) / sd

    # ---------- protocol ----------
    def on_hello(self, msg):
        syms = msg.get("symbols") or []
        if syms:
            self.symbol = syms[0].get("symbol")
            self.price_scale = int(syms[0].get("price_scale", self.price_scale))
            self.qty_scale = int(syms[0].get("qty_scale", self.qty_scale))
        cap = msg.get("capital") or {}
        self.quote_scale = int(cap.get("quote_scale", self.quote_scale))
        fees = msg.get("fees") or {}
        if fees.get("min_notional_quote_units") is not None:
            self.min_notional = int(fees["min_notional_quote_units"])
        self.warmup = int(msg.get("warmup_bars") or 0)

    def on_batch(self, msg):
        seq = msg.get("seq", 0)
        self.bar += 1
        bars = msg.get("bars") or []
        account = msg.get("account") or {}
        cash = int(account.get("cash_quote_units") or 0)
        equity = int(account.get("equity_quote_units") or cash)
        pos_qty = 0
        for pos in account.get("positions") or []:
            if pos.get("symbol") == self.symbol:
                pos_qty = int(pos.get("qty") or 0)
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

        bar = None
        for b in bars:
            if b.get("symbol") == self.symbol:
                bar = b
                break
        if bar is None and bars:
            bar = bars[0]
            self.symbol = bar.get("symbol", self.symbol)
        if bar is None:
            return {"type": "reply", "seq": seq, "orders": []}

        close = self.px(bar["c"])
        self.closes.append(close)
        kf = 2.0 / (FAST + 1)
        ks = 2.0 / (SLOW + 1)
        self.ema_fast = close if self.ema_fast is None else (
            self.ema_fast + kf * (close - self.ema_fast))
        self.ema_slow = close if self.ema_slow is None else (
            self.ema_slow + ks * (close - self.ema_slow))
        self.slow_hist.append(self.ema_slow)

        # fills tell us when an entry actually landed
        for fill in msg.get("fills") or []:
            self.pending.discard(fill.get("client_id"))
            if fill.get("status") == "filled":
                self.trades += 1
                if fill.get("side") == "buy":
                    self.entry_bar = self.bar
                    self.entry_px = self.px(fill["price"])
                    self.peak_px = self.entry_px
                elif fill.get("side") == "sell":
                    self.entry_bar = None
                    self.entry_px = None
                    self.peak_px = None
                    self.cooldown_until = self.bar + COOLDOWN

        orders = []
        if self.bar < self.warmup:
            return {"type": "reply", "seq": seq, "orders": orders}

        drawdown = 0.0
        if self.peak_equity:
            drawdown = (self.peak_equity - equity) / self.peak_equity

        holding = pos_qty > 0
        if holding:
            if self.peak_px is None or close > self.peak_px:
                self.peak_px = close
            exit_now = False
            if self.entry_px:
                ret = close / self.entry_px - 1.0
                if ret >= TAKE_PROFIT or ret <= -STOP_LOSS:
                    exit_now = True
            if self.peak_px and close <= self.peak_px * (1.0 - TRAIL):
                exit_now = True
            if self.ema_fast < self.ema_slow:
                exit_now = True
            if self.entry_bar is not None and self.bar - self.entry_bar >= TIME_STOP:
                exit_now = True
            if drawdown >= DD_FLAT:
                exit_now = True
            if exit_now and not self.pending:
                cid = self.next_id(seq)
                self.pending.add(cid)
                orders.append({
                    "side": "sell",
                    "client_id": cid,
                    "symbol": self.symbol,
                    "qty": str(pos_qty),
                    "max_slippage_bps": MAX_SLIP_BPS,
                })
        else:
            self.entry_bar = None
            self.entry_px = None
            self.peak_px = None
            if (drawdown < DD_HALT and len(self.closes) >= MIN_BARS
                    and not self.pending and self.bar >= self.cooldown_until):
                slope_up = (len(self.slow_hist) > SLOPE_WIN
                            and self.slow_hist[-1] > self.slow_hist[-1 - SLOPE_WIN])
                trend_up = (self.ema_fast > self.ema_slow
                            and close > self.ema_slow and slope_up)
                pullback = trend_up and self.zscore() <= ENTRY_Z
                breakout = False
                if len(self.closes) > BREAKOUT_WIN:
                    hi = max(self.closes[-(BREAKOUT_WIN + 1):-1])
                    breakout = trend_up and close > hi * (1.0 + BREAKOUT_PAD)
                fallback = (self.trades == 0 and self.bar >= FALLBACK_BAR)
                if pullback or breakout or fallback:
                    frac = FALLBACK_ALLOC if (fallback and not pullback
                                              and not breakout) else ALLOC
                    want = int(equity * frac)
                    room = int(equity * MAX_WEIGHT)
                    notional = min(want, room, int(cash * 0.995))
                    if notional >= self.min_notional:
                        cid = self.next_id(seq)
                        self.pending.add(cid)
                        orders.append({
                            "side": "buy",
                            "client_id": cid,
                            "symbol": self.symbol,
                            "notional_quote_units": str(notional),
                            "max_slippage_bps": MAX_SLIP_BPS,
                        })
        return {"type": "reply", "seq": seq, "orders": orders}


def main():
    bot = Bot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "hello":
            bot.on_hello(msg)
        elif kind == "batch":
            reply = bot.on_batch(msg)
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
