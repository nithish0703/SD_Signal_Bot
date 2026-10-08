"""Trend-following engine - two classic models, tested on 4 years (report only - changes nothing live).

All rules fixed BEFORE running (textbook values, no tuning), so all 4 years are out-of-sample.

T1 Donchian / Turtle (daily candles, UTC days)
   Long : daily close above the highest high of the previous 20 days -> buy at the next day's open.
   Short: daily close below the lowest low of the previous 20 days  -> sell at the next day's open.
   Stop : 2 x ATR20 from the entry (checked intraday; a gap past it exits at the open).
   Exit : daily close below the 10-day low (long) / above the 10-day high (short) -> exit next open.
T2 EMA trend (4h candles)
   Long : EMA50 crosses above EMA200 at a 4h close -> buy at the next open. Short: the mirror.
   Stop : chandelier, 3 x ATR14 below the highest close since entry (above the lowest close for shorts),
          checked on 4h closes -> exit next open; also exit on the opposite cross. Initial risk = 3 x ATR14.
Both run "long+short" and "long only" (crypto's long-term drift is up; decided before running).
One position per coin per model. 1R = the initial stop distance, $1 risk per trade.
Costs  0.10% round trip fees + funding 0.03% per day held (longs pay, shorts receive; Binance base rate
       0.01% per 8h). Both converted to R with the trade's stop %.
Pass   total R > 0, profitable in at least 3 of the 4 years, at least 100 trades.
Coins  20 large coins that traded on Binance spot for all 4 years (no survivorship bias). Spot candles.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

YEARS = 4
COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
         "DOTUSDT", "LTCUSDT", "BCHUSDT", "TRXUSDT", "XLMUSDT", "ETCUSDT", "ATOMUSDT", "FILUSDT", "NEARUSDT",
         "UNIUSDT", "AAVEUSDT"]
FEE = 0.10              # % round trip
FUND_DAY = 0.03         # % of notional per day held (longs pay, shorts receive)
DAY = 86400000


def atr(C, n):
    out, prev = [], None
    for i, x in enumerate(C):
        tr = x["h"] - x["l"] if i == 0 else max(x["h"], C[i - 1]["c"]) - min(x["l"], C[i - 1]["c"])
        prev = tr if prev is None else (prev * (n - 1) + tr) / n          # Wilder smoothing
        out.append(prev if i >= n - 1 else None)
    return out


def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def close_trade(side, entry, exit_px, risk, t_in, t_out, trades, model):
    """R net of fees and funding. side +1 long / -1 short, risk = initial stop distance (price)."""
    stop_pct = risk / entry * 100
    gross = side * (exit_px - entry) / risk
    days = max(0.0, (t_out - t_in) / DAY)
    cost = FEE / stop_pct + side * FUND_DAY * days / stop_pct
    trades.append({"t": t_in, "side": "LONG" if side > 0 else "SHORT", "r": gross - cost, "days": days, "model": model})


def donchian(D, long_only):
    """T1 on daily candles D. Returns trades."""
    A = atr(D, 20)
    trades, pos = [], None                    # pos = (side, entry, stop, risk, t_in)
    for t in range(21, len(D) - 1):
        x, nxt = D[t], D[t + 1]
        if pos:
            side, entry, stop, risk, t_in = pos
            hit = x["l"] <= stop if side > 0 else x["h"] >= stop
            if hit:                                                   # intraday stop (gap -> open price)
                px = min(x["o"], stop) if side > 0 else max(x["o"], stop)
                close_trade(side, entry, px, risk, t_in, x["t"], trades, "T1")
                pos = None
            else:
                lo10 = min(D[k]["l"] for k in range(t - 10, t))
                hi10 = max(D[k]["h"] for k in range(t - 10, t))
                if (side > 0 and x["c"] < lo10) or (side < 0 and x["c"] > hi10):
                    close_trade(side, entry, nxt["o"], risk, t_in, nxt["t"], trades, "T1")
                    pos = None
                continue
        if pos is None and A[t]:
            hi20 = max(D[k]["h"] for k in range(t - 20, t))
            lo20 = min(D[k]["l"] for k in range(t - 20, t))
            side = 1 if x["c"] > hi20 else -1 if x["c"] < lo20 and not long_only else 0
            if side:
                entry, risk = nxt["o"], 2 * A[t]
                pos = (side, entry, entry - side * risk, risk, nxt["t"])
    if pos:                                                           # still open: mark at the last close
        side, entry, stop, risk, t_in = pos
        close_trade(side, entry, D[-1]["c"], risk, t_in, D[-1]["t"], trades, "T1")
    return trades


def ema_trend(H, long_only):
    """T2 on 4h candles H. Returns trades."""
    cl = [x["c"] for x in H]
    e50, e200, A = ema(cl, 50), ema(cl, 200), atr(H, 14)
    trades, pos = [], None                    # pos = (side, entry, risk, t_in, best_close)
    for t in range(201, len(H) - 1):
        x, nxt = H[t], H[t + 1]
        up = e50[t] > e200[t] and e50[t - 1] <= e200[t - 1]
        dn = e50[t] < e200[t] and e50[t - 1] >= e200[t - 1]
        if pos:
            side, entry, risk, t_in, best = pos
            best = max(best, x["c"]) if side > 0 else min(best, x["c"])
            trail = best - side * 3 * A[t]
            out = (side > 0 and (x["c"] < trail or dn)) or (side < 0 and (x["c"] > trail or up))
            if out:
                close_trade(side, entry, nxt["o"], risk, t_in, nxt["t"], trades, "T2")
                pos = None
            else:
                pos = (side, entry, risk, t_in, best)
                continue
        if pos is None and A[t]:
            side = 1 if up else -1 if dn and not long_only else 0
            if side:
                pos = (side, nxt["o"], 3 * A[t], nxt["t"], nxt["o"])
    if pos:
        side, entry, risk, t_in, best = pos
        close_trade(side, entry, H[-1]["c"], risk, t_in, H[-1]["t"], trades, "T2")
    return trades


def stats(ts):
    ts = sorted(ts, key=lambda t: t["t"])
    n = len(ts)
    tot = sum(t["r"] for t in ts)
    eq = peak = dd = 0.0
    streak = worst = 0
    for t in ts:
        eq += t["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if t["r"] < 0 else 0
        worst = max(worst, streak)
    win = sum(1 for t in ts if t["r"] > 0)
    hold = sum(t["days"] for t in ts) / n if n else 0.0
    return n, tot, (win / n * 100 if n else 0.0), dd, worst, hold


def main():
    t0 = time.time()
    bot.DATA_SOURCE = "spot"
    now = time.time() * 1000
    start = now - YEARS * 365.25 * DAY
    res, used = {}, []
    for sym in COINS:
        try:
            D, _ = bot.fetch_history(sym, "1d", int(YEARS * 365.25) + 60)
            H, _ = bot.fetch_history(sym, "4h", int(YEARS * 365.25 * 6) + 400)
        except Exception as e:
            print(sym, "data error", e, flush=True)
            continue
        if not D or D[0]["t"] > start - 20 * DAY or H[0]["t"] > start - 30 * DAY:
            print(sym, "skipped: history shorter than 4 years + warm-up", flush=True)
            continue
        used.append(sym)
        for lo in (False, True):
            key = "long only" if lo else "long+short"
            res.setdefault(("T1 Donchian 20/10 (daily)", key), []).extend(t for t in donchian(D, lo) if t["t"] >= start)
            res.setdefault(("T2 EMA 50/200 + 3ATR trail (4h)", key), []).extend(t for t in ema_trend(H, lo) if t["t"] >= start)
        print(f"{sym}: {len(D)} days, {len(H)} 4h candles", flush=True)
    yb = [start + i * 365.25 * DAY for i in range(YEARS + 1)]
    yname = [f"{datetime.fromtimestamp(yb[i] / 1000, timezone.utc):%b %y}" for i in range(YEARS)]
    out = [f"## Trend-following engine - 2 classic models, {len(used)} coins, spot, last {YEARS} years (rules fixed, no tuning)\n",
           f"Coins: {', '.join(s[:-4] for s in used)}. Costs: {FEE}% fees + {FUND_DAY}% funding/day (longs pay).\n",
           "| Model | Sides | " + " | ".join(f"Year from {y}" for y in yname)
           + " | Total | Trades | Win % | Avg R | Max DD | Streak | Avg hold | Pass? |",
           "|---|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|---|"]
    tg = [f"📈 <b>Trend-following engine test</b> ({len(used)} coins, {YEARS} years, rules fixed - no tuning)",
          "Pass = total &gt; 0, profitable in ≥3 of 4 years, ≥100 trades"]
    for (name, key), ts in sorted(res.items()):
        ys = [[t for t in ts if yb[i] <= t["t"] < yb[i + 1]] for i in range(YEARS)]
        yr = [sum(t["r"] for t in y) for y in ys]
        n, tot, win, dd, streak, hold = stats(ts)
        ok = tot > 0 and sum(1 for r in yr if r > 0) >= 3 and n >= 100
        longs = sum(t["r"] for t in ts if t["side"] == "LONG")
        out.append(f"| {name} | {key} | " + " | ".join(f"{len(y)} / {r:+.1f}R" for y, r in zip(ys, yr))
                   + f" | {tot:+.1f}R | {n} | {win:.0f}% | {tot / n if n else 0:+.2f} | −{dd:.1f}R | {streak} | "
                     f"{hold:.0f}d | {'✅' if ok else '❌'} |")
        tg.append(f"{'✅' if ok else '❌'} <b>{name}</b> ({key}): {n} trades, total {tot:+.1f}R, win {win:.0f}%, "
                  f"avg {tot / n if n else 0:+.2f}R, DD −{dd:.0f}R, hold {hold:.0f}d\n   years: "
                  + " · ".join(f"{r:+.0f}" for r in yr) + f" | longs {longs:+.0f}R, shorts {tot - longs:+.0f}R")
    out.append("\nCaveats: spot prices; funding is a flat estimate (real funding swings with the market); "
               "positions on 20 coins are correlated, so many trades win or lose together.")
    tg.append(f"Report only, nothing changed live. {(time.time() - t0) / 60:.0f} min. Full table: Actions → run summary.")
    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report + "\n")
    msg = "\n\n".join(tg)
    print(re.sub(r"</?b>", "", msg))
    for i in range(0, len(msg), 3900):
        bot.tg(msg[i:i + 3900])


if __name__ == "__main__":
    main()
