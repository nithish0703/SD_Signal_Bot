"""Trend-following ACCOUNT simulation (report only - changes nothing live).

Same two models and rules as trend_engine.py (T1 Donchian 20/10 daily, T2 EMA 50/200 + 3 ATR chandelier 4h),
but now as one real account trading all 20 coins at the same time:
  Data      Binance USDT-M FUTURES 1h candles (data.binance.vision) -> 4h and daily, and the REAL funding rate history
            (paid every 8h: longs pay a positive rate, shorts receive it).
  Account   $1,000 start. Risk per trade = 0.5% of the STARTING balance ($5, no compounding), position size =
            risk / initial stop distance. A new trade is skipped if the open risk of all positions would exceed
            6% of the starting balance ($60 = 12 trades at full risk) or the total notional would exceed 3x equity.
  Costs     0.05% taker fee on entry and on exit (0.10% round trip) + real funding.
  Equity    marked to market at every 4h close -> max drawdown includes open positions.
Rules fixed BEFORE running (no tuning). Pass = profitable in at least 3 of the 4 years, max drawdown below 25%,
and the final balance above the start.
"""
import csv
import io
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

YEARS = 4
COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
         "DOTUSDT", "LTCUSDT", "BCHUSDT", "TRXUSDT", "XLMUSDT", "ETCUSDT", "ATOMUSDT", "FILUSDT", "NEARUSDT",
         "UNIUSDT", "AAVEUSDT"]
START_BAL = 1000.0
RISK = 0.005 * START_BAL          # $ risk per trade (fixed, no compounding)
MAX_OPEN_RISK = 0.06 * START_BAL
MAX_LEV = 3.0
TAKER = 0.05                       # % per side
H4, DAY = 4 * 3600000, 86400000
FUND_URL = "https://data.binance.vision/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip"


def atr(C, n):
    out, prev = [], None
    for i, x in enumerate(C):
        tr = x["h"] - x["l"] if i == 0 else max(x["h"], C[i - 1]["c"]) - min(x["l"], C[i - 1]["c"])
        prev = tr if prev is None else (prev * (n - 1) + tr) / n
        out.append(prev if i >= n - 1 else None)
    return out


def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def t1_trades(D, long_only):
    """Donchian 20/10 on daily candles -> list of (side, t_in, entry, risk, t_out, exit)."""
    A = atr(D, 20)
    out, pos = [], None
    for t in range(21, len(D) - 1):
        x, nxt = D[t], D[t + 1]
        if pos:
            side, t_in, entry, risk, stop = pos
            if (x["l"] <= stop) if side > 0 else (x["h"] >= stop):
                px = min(x["o"], stop) if side > 0 else max(x["o"], stop)
                out.append((side, t_in, entry, risk, x["t"], px))
                pos = None
            else:
                lo10 = min(D[k]["l"] for k in range(t - 10, t))
                hi10 = max(D[k]["h"] for k in range(t - 10, t))
                if (side > 0 and x["c"] < lo10) or (side < 0 and x["c"] > hi10):
                    out.append((side, t_in, entry, risk, nxt["t"], nxt["o"]))
                    pos = None
                continue
        if pos is None and A[t]:
            hi20 = max(D[k]["h"] for k in range(t - 20, t))
            lo20 = min(D[k]["l"] for k in range(t - 20, t))
            side = 1 if x["c"] > hi20 else -1 if x["c"] < lo20 and not long_only else 0
            if side:
                risk = 2 * A[t]
                pos = (side, nxt["t"], nxt["o"], risk, nxt["o"] - side * risk)
    if pos:
        side, t_in, entry, risk, _ = pos
        out.append((side, t_in, entry, risk, None, None))                 # still open at the end
    return out


def t2_trades(H, long_only):
    """EMA 50/200 cross + 3 ATR chandelier on 4h candles -> list of (side, t_in, entry, risk, t_out, exit)."""
    cl = [x["c"] for x in H]
    e50, e200, A = ema(cl, 50), ema(cl, 200), atr(H, 14)
    out, pos = [], None
    for t in range(201, len(H) - 1):
        x, nxt = H[t], H[t + 1]
        up = e50[t] > e200[t] and e50[t - 1] <= e200[t - 1]
        dn = e50[t] < e200[t] and e50[t - 1] >= e200[t - 1]
        if pos:
            side, t_in, entry, risk, best = pos
            best = max(best, x["c"]) if side > 0 else min(best, x["c"])
            trail = best - side * 3 * A[t]
            if (side > 0 and (x["c"] < trail or dn)) or (side < 0 and (x["c"] > trail or up)):
                out.append((side, t_in, entry, risk, nxt["t"], nxt["o"]))
                pos = None
            else:
                pos = (side, t_in, entry, risk, best)
                continue
        if pos is None and A[t]:
            side = 1 if up else -1 if dn and not long_only else 0
            if side:
                pos = (side, nxt["t"], nxt["o"], 3 * A[t], nxt["o"])
    if pos:
        side, t_in, entry, risk, _ = pos
        out.append((side, t_in, entry, risk, None, None))
    return out


def funding(sym, start_ms):
    """Real funding history -> sorted list of (time ms, rate)."""
    months, m = [], datetime.fromtimestamp(start_ms / 1000, timezone.utc).replace(day=1)
    now = datetime.now(timezone.utc)
    while (m.year, m.month) < (now.year, now.month):
        months.append(m.strftime("%Y-%m"))
        m = m.replace(year=m.year + (m.month == 12), month=m.month % 12 + 1)

    def one(mm):
        for attempt in range(3):
            try:
                r = requests.get(FUND_URL.format(s=sym, m=mm), timeout=20)
                if r.status_code == 404:
                    return []
                if r.status_code == 200:
                    with zipfile.ZipFile(io.BytesIO(r.content)) as z, z.open(z.namelist()[0]) as f:
                        rows = list(csv.reader(io.TextIOWrapper(f, "utf-8")))
                    return [(int(x[0]), float(x[2])) for x in rows if x and x[0].strip().isdigit()]
            except Exception:
                time.sleep(2 * (attempt + 1))
        return None
    with ThreadPoolExecutor(6) as ex:
        parts = list(ex.map(one, months))
    missing = sum(1 for p in parts if p is None)
    return sorted({t: r for p in parts if p for t, r in p}.items()), missing


def simulate(trades, closes, fund, times):
    """trades: list of dicts {sym, side, t_in, entry, risk, t_out, exit}. Returns account stats."""
    by_in = {}
    for k, tr in enumerate(trades):
        by_in.setdefault(tr["t_in"], []).append(k)
    cash, open_, taken, skipped = START_BAL, {}, 0, 0
    eq_curve, peak, mdd, max_open = [], START_BAL, 0.0, 0
    fidx = {s: 0 for s in fund}
    realized = []
    last = {}                                       # last known close per coin (carried over a missing candle)
    px = lambda p: last.get(p["sym"], p["entry"])
    prev_t = None
    for t in times:
        # 1) funding paid/received for every funding time in (prev_t, t]
        if prev_t is not None:
            for k, p in open_.items():
                fl, i = fund.get(p["sym"], []), fidx.get(p["sym"], 0)
                while i < len(fl) and fl[i][0] <= prev_t:
                    i += 1
                j = i
                while j < len(fl) and fl[j][0] <= t:
                    p["fund"] += p["side"] * p["qty"] * px(p) * fl[j][1]
                    j += 1
            for s in fund:                                              # advance pointers
                fl, i = fund[s], fidx[s]
                while i < len(fl) and fl[i][0] <= t:
                    i += 1
                fidx[s] = i
        # 2) exits at this candle's open
        for k in [k for k, p in open_.items() if p["t_out"] == t]:
            p = open_.pop(k)
            pnl = p["side"] * p["qty"] * (p["exit"] - p["entry"]) - p["qty"] * p["exit"] * TAKER / 100 - p["fund"]
            cash += pnl                                                 # the entry fee was taken at entry
            realized.append((p["t_in"], pnl - p["qty"] * p["entry"] * TAKER / 100, p["model"]))
        # 3) entries at this candle's open
        for k in by_in.get(t, []):
            tr = trades[k]
            qty = RISK / tr["risk"]
            open_risk = sum(RISK for _ in open_)
            equity = cash + sum(p["side"] * p["qty"] * (px(p) - p["entry"]) - p["fund"] for p in open_.values())
            notional = sum(p["qty"] * px(p) for p in open_.values())
            if open_risk + RISK > MAX_OPEN_RISK or notional + qty * tr["entry"] > MAX_LEV * equity:
                skipped += 1
                continue
            cash -= qty * tr["entry"] * TAKER / 100                     # entry fee
            open_[k] = dict(tr, qty=qty, fund=0.0)
            taken += 1
        max_open = max(max_open, len(open_))
        # 4) mark to market at this 4h close
        for s in closes:
            c = closes[s].get(t)
            if c is not None:
                last[s] = c
        unreal = sum(p["side"] * p["qty"] * (px(p) - p["entry"]) - p["fund"] for p in open_.values())
        eq = cash + unreal
        eq_curve.append((t, eq))
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        prev_t = t
    return {"final": eq_curve[-1][1] if eq_curve else START_BAL, "curve": eq_curve, "mdd": mdd, "taken": taken,
            "skipped": skipped, "max_open": max_open, "realized": realized,
            "open_end": len(open_)}


def main():
    t0 = time.time()
    now = time.time() * 1000
    start = now - YEARS * 365.25 * DAY
    hours = int(YEARS * 365.25 * 24) + 60 * 24
    data, fund, used, notes = {}, {}, [], []
    for sym in COINS:
        try:
            c1 = bot.fetch_futures_1h(sym, hours)
        except Exception as e:
            notes.append(f"{sym[:-4]} data error")
            print(sym, "data error", e, flush=True)
            continue
        bot._FUT_CACHE.pop(sym, None)
        span = [x for x in c1 if x["t"] >= start - 50 * DAY]
        expect = (c1[-1]["t"] - (start - 50 * DAY)) / 3600000
        gap = max((b["t"] - a["t"] for a, b in zip(span, span[1:])), default=0) / 3600000
        if not c1 or c1[0]["t"] > start - 50 * DAY or len(span) < 0.98 * expect or gap > 24:
            notes.append(f"{sym[:-4]} skipped (futures history incomplete: {len(span)}/{expect:.0f} hours, "
                         f"largest gap {gap:.0f}h)")
            continue
        H = bot._aggregate(c1, 4)
        D = bot._aggregate(c1, 24)
        fl, miss = funding(sym, start - 5 * DAY)
        if miss:
            notes.append(f"{sym[:-4]}: {miss} funding month(s) failed to download (counted as 0)")
        data[sym] = (D, H)
        fund[sym] = fl
        used.append(sym)
        print(f"{sym}: {len(H)} 4h candles, {len(fl)} funding prints", flush=True)
    closes = {s: {x["t"]: x["c"] for x in H} for s, (D, H) in data.items()}
    times = sorted({t for s in closes for t in closes[s] if t >= start})
    scenarios = []
    for name, models in [("T2 EMA trend (4h) long+short", [("T2", False)]),
                         ("T1 Donchian (daily) long+short", [("T1", False)]),
                         ("T2 EMA trend (4h) long only", [("T2", True)]),
                         ("T1 + T2 together long+short", [("T1", False), ("T2", False)])]:
        trades = []
        for sym, (D, H) in data.items():
            for mdl, lo in models:
                raw = t1_trades(D, lo) if mdl == "T1" else t2_trades(H, lo)
                for side, t_in, entry, risk, t_out, ex in raw:
                    if t_in < start:
                        continue
                    trades.append({"sym": sym, "side": side, "t_in": t_in, "entry": entry, "risk": risk,
                                   "t_out": t_out, "exit": ex, "model": mdl})
        scenarios.append((name, simulate(trades, closes, fund, times), len(trades)))
    yb = [start + i * 365.25 * DAY for i in range(YEARS + 1)]
    yname = [f"{datetime.fromtimestamp(yb[i] / 1000, timezone.utc):%b %y}" for i in range(YEARS)]
    out = [f"## Trend-following ACCOUNT simulation - {len(used)} coins, futures + real funding, {YEARS} years\n",
           f"${START_BAL:,.0f} start, ${RISK:g} risk per trade (0.5%, no compounding), open risk cap ${MAX_OPEN_RISK:g}, "
           f"max {MAX_LEV:g}x notional, 0.10% fees. Equity marked to market every 4h.\n",
           "| Scenario | " + " | ".join(f"Year from {y}" for y in yname)
           + " | Final | Return | Max DD (MTM) | Worst month | Profitable months | Trades taken / skipped | Max open | Pass? |",
           "|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|---|"]
    tg = [f"💼 <b>Trend ACCOUNT simulation</b> ({len(used)} coins, futures + real funding, {YEARS} years)",
          f"${START_BAL:,.0f} start, ${RISK:g} risk/trade (no compounding), open-risk cap ${MAX_OPEN_RISK:g}. "
          "Pass = ≥3 of 4 years profitable, max DD &lt; 25%, final above start."]
    for name, r, n_all in scenarios:
        curve = r["curve"]

        def eq_at(ms):
            last = START_BAL
            for t, e in curve:
                if t > ms:
                    break
                last = e
            return last
        ychg = [eq_at(yb[i + 1]) - eq_at(yb[i]) for i in range(YEARS)]
        months = {}
        for t, e in curve:
            k = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m")
            months.setdefault(k, [e, e])[1] = e
        keys = sorted(months)
        mchg, prev = [], START_BAL
        for k in keys:
            mchg.append(months[k][1] - prev)
            prev = months[k][1]
        worst = min(mchg) if mchg else 0.0
        pos_m = sum(1 for x in mchg if x > 0)
        ok = sum(1 for x in ychg if x > 0) >= 3 and r["mdd"] < 0.25 and r["final"] > START_BAL
        out.append(f"| {name} | " + " | ".join(f"${x:+,.0f}" for x in ychg)
                   + f" | ${r['final']:,.0f} | {(r['final'] / START_BAL - 1) * 100:+.0f}% | −{r['mdd'] * 100:.1f}% | "
                     f"${worst:+,.0f} | {pos_m}/{len(mchg)} | {r['taken']} / {r['skipped']} | {r['max_open']} | "
                     f"{'✅' if ok else '❌'} |")
        tg.append(f"{'✅' if ok else '❌'} <b>{name}</b>: ${START_BAL:,.0f} → <b>${r['final']:,.0f}</b> "
                  f"({(r['final'] / START_BAL - 1) * 100:+.0f}%), max DD −{r['mdd'] * 100:.1f}%, worst month ${worst:+,.0f}, "
                  f"{pos_m}/{len(mchg)} months up\n   years: " + " · ".join(f"${x:+,.0f}" for x in ychg)
                  + f" | trades {r['taken']} (skipped {r['skipped']}), max {r['max_open']} open")
    if notes:
        out.append("\nNotes: " + "; ".join(notes))
        tg.append("Notes: " + bot.esc("; ".join(notes)))
    out.append("\nCaveats: fills at the candle open (no slippage); T1 stops are taken at the stop price even if hit "
               "late in the day; no compounding; past results do not guarantee future results.")
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
