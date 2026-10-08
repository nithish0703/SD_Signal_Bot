"""T2 trend ACCOUNT simulation with a SMALL account ($100) - report only, changes nothing live.

Same data, rules and costs as trend_account.py (Binance USDT-M futures 1h -> 4h candles, real funding,
0.05% taker per side, equity marked to market every 4h, no compounding), model T2 long+short only
(the one the paper bot trades). New here: a $100 account and Binance's real order-size limits.

Order-size limits: quantity is rounded DOWN to the coin's step size and must be at least the minimum order
value (BTC $100, ETH $20, others $5 on USDT-M futures). If the rounded size is too small, the smallest allowed
size is used ONLY if its real risk is at most 1.5x the planned risk; otherwise the trade is skipped (too big for
the account). Open-risk cap and P&L use the real (rounded) size. Limits are read from Binance at run time when
possible, otherwise from the table below (values as of 2026, may change).

Scenarios (all $100 start, fixed $ risk, no compounding, max 12 trades open, total size <= 3x equity):
  A  $0.50 risk (0.5%), NO size limits   - the $1,000 test scaled down (reference only, not realistic)
  B  $0.50 risk (0.5%), real size limits
  C  $1    risk (1%),   real size limits
  D  $2    risk (2%),   real size limits
Pass = profitable in at least 3 of the 4 years, max drawdown below 25%, final above start (same as before).
"""
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import trend_account as TA  # noqa: E402

START_BAL = 100.0
MAX_OPEN = 12
MAX_LEV = 3.0
TAKER = TA.TAKER
YEARS = TA.YEARS
DAY = TA.DAY
TOL = 1.5                       # accept the minimum size if its risk is at most 1.5x the planned risk
# (step size, minimum order value $) on Binance USDT-M perpetuals - fallback if exchangeInfo is not reachable
LIMITS = {"BTCUSDT": (0.001, 100), "ETHUSDT": (0.001, 20), "BNBUSDT": (0.01, 5), "SOLUSDT": (1, 5),
          "XRPUSDT": (0.1, 5), "DOGEUSDT": (1, 5), "ADAUSDT": (1, 5), "AVAXUSDT": (1, 5), "LINKUSDT": (0.01, 5),
          "DOTUSDT": (0.1, 5), "LTCUSDT": (0.001, 5), "BCHUSDT": (0.001, 5), "TRXUSDT": (1, 5), "XLMUSDT": (1, 5),
          "ETCUSDT": (0.01, 5), "ATOMUSDT": (0.01, 5), "FILUSDT": (0.1, 5), "NEARUSDT": (1, 5), "UNIUSDT": (1, 5),
          "AAVEUSDT": (0.1, 5)}


def live_limits():
    """Try Binance futures exchangeInfo (often blocked from GitHub). Returns (limits, source text)."""
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15)
        r.raise_for_status()
        out = {}
        for s in r.json()["symbols"]:
            if s["symbol"] in LIMITS and s.get("contractType") == "PERPETUAL":
                f = {x["filterType"]: x for x in s["filters"]}
                step = float(f.get("MARKET_LOT_SIZE", f["LOT_SIZE"])["stepSize"])
                mn = float(f.get("MIN_NOTIONAL", {}).get("notional", 5))
                out[s["symbol"]] = (step, mn)
        if len(out) >= len(LIMITS) - 2:
            return {**LIMITS, **out}, "Binance exchangeInfo (live)"
    except Exception as e:
        print("exchangeInfo not reachable:", e, flush=True)
    return LIMITS, "built-in table (Binance futures values, may have changed)"


def size(tr, risk_usd, limits):
    """-> (qty, real $ risk) or (None, reason)."""
    qty = risk_usd / tr["risk"]
    if limits is None:
        return qty, risk_usd
    step, mn = limits[tr["sym"]]
    q = math.floor(qty / step + 1e-9) * step
    if q > 0 and q * tr["entry"] >= mn:
        return q, q * tr["risk"]
    q = max(step, math.ceil(mn / tr["entry"] / step - 1e-9) * step)
    if q * tr["risk"] <= TOL * risk_usd:
        return q, q * tr["risk"]
    return None, "small"


def simulate(trades, closes, fund, times, risk_usd, limits):
    by_in = {}
    for k, tr in enumerate(trades):
        by_in.setdefault(tr["t_in"], []).append(k)
    cash, open_ = START_BAL, {}
    taken, sk_small, sk_cap, upsized = 0, 0, 0, 0
    small_by = {}
    curve, peak, mdd, max_open = [], START_BAL, 0.0, 0
    fidx = {s: 0 for s in fund}
    last = {}
    px = lambda p: last.get(p["sym"], p["entry"])
    prev_t = None
    for t in times:
        if prev_t is not None:
            for p in open_.values():
                fl, i = fund.get(p["sym"], []), fidx.get(p["sym"], 0)
                while i < len(fl) and fl[i][0] <= prev_t:
                    i += 1
                while i < len(fl) and fl[i][0] <= t:
                    p["fund"] += p["side"] * p["qty"] * px(p) * fl[i][1]
                    i += 1
            for s in fund:
                fl, i = fund[s], fidx[s]
                while i < len(fl) and fl[i][0] <= t:
                    i += 1
                fidx[s] = i
        for k in [k for k, p in open_.items() if p["t_out"] == t]:
            p = open_.pop(k)
            cash += p["side"] * p["qty"] * (p["exit"] - p["entry"]) - p["qty"] * p["exit"] * TAKER / 100 - p["fund"]
        for k in by_in.get(t, []):
            tr = trades[k]
            qty, real = size(tr, risk_usd, limits)
            if qty is None:
                sk_small += 1
                small_by[tr["sym"][:-4]] = small_by.get(tr["sym"][:-4], 0) + 1
                continue
            equity = cash + sum(p["side"] * p["qty"] * (px(p) - p["entry"]) - p["fund"] for p in open_.values())
            notional = sum(p["qty"] * px(p) for p in open_.values())
            if len(open_) >= MAX_OPEN or notional + qty * tr["entry"] > MAX_LEV * equity:
                sk_cap += 1
                continue
            upsized += real > risk_usd * 1.001
            cash -= qty * tr["entry"] * TAKER / 100
            open_[k] = dict(tr, qty=qty, fund=0.0)
            taken += 1
        max_open = max(max_open, len(open_))
        for s in closes:
            c = closes[s].get(t)
            if c is not None:
                last[s] = c
        eq = cash + sum(p["side"] * p["qty"] * (px(p) - p["entry"]) - p["fund"] for p in open_.values())
        curve.append((t, eq))
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        prev_t = t
    return {"final": curve[-1][1] if curve else START_BAL, "curve": curve, "mdd": mdd, "taken": taken,
            "sk_small": sk_small, "sk_cap": sk_cap, "upsized": upsized, "max_open": max_open, "small_by": small_by}


def main():
    t0 = time.time()
    now = time.time() * 1000
    start = now - YEARS * 365.25 * DAY
    hours = int(YEARS * 365.25 * 24) + 60 * 24
    limits, lim_src = live_limits()
    data, fund, notes = {}, {}, []
    for sym in TA.COINS:
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
            notes.append(f"{sym[:-4]} skipped (futures history incomplete)")
            continue
        H = bot._aggregate(c1, 4)
        fl, miss = TA.funding(sym, start - 5 * DAY)
        if miss:
            notes.append(f"{sym[:-4]}: {miss} funding month(s) missing (counted as 0)")
        data[sym] = H
        fund[sym] = fl
        print(f"{sym}: {len(H)} 4h candles, {len(fl)} funding prints", flush=True)
    closes = {s: {x["t"]: x["c"] for x in H} for s, H in data.items()}
    times = sorted({t for s in closes for t in closes[s] if t >= start})
    trades = []
    for sym, H in data.items():
        for side, t_in, entry, risk, t_out, ex in TA.t2_trades(H, False):
            if t_in >= start:
                trades.append({"sym": sym, "side": side, "t_in": t_in, "entry": entry, "risk": risk,
                               "t_out": t_out, "exit": ex, "model": "T2"})
    scen = [("A  $0.50 risk, no size limits (reference)", 0.5, None),
            ("B  $0.50 risk, real Binance size limits", 0.5, limits),
            ("C  $1 risk, real Binance size limits", 1.0, limits),
            ("D  $2 risk, real Binance size limits", 2.0, limits)]
    yb = [start + i * 365.25 * DAY for i in range(YEARS + 1)]
    yname = [f"{datetime.fromtimestamp(yb[i] / 1000, timezone.utc):%b %y}" for i in range(YEARS)]
    out = [f"## T2 trend - $100 account, {len(data)} coins, futures + real funding, {YEARS} years\n",
           f"No compounding, max {MAX_OPEN} open, size <= {MAX_LEV:g}x equity, 0.10% fees. Size limits: {lim_src}.\n",
           "| Scenario | " + " | ".join(f"Year from {y}" for y in yname)
           + " | Final | Return | Max DD | Worst month | Months up | Taken | Skipped too small | Skipped cap | "
             "Min-size (risk above plan) | Pass? |",
           "|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"💼 <b>T2 trend — $100 account test</b> ({len(data)} coins, futures + real funding, {YEARS} years)",
          f"No compounding. Real Binance order-size limits ({bot.esc(lim_src)}). "
          "Pass = ≥3 of 4 years up, max DD &lt; 25%, final above $100."]
    for name, risk_usd, lim in scen:
        r = simulate(trades, closes, fund, times, risk_usd, lim)

        def eq_at(ms, curve=r["curve"]):
            v = START_BAL
            for t, e in curve:
                if t > ms:
                    break
                v = e
            return v
        ychg = [eq_at(yb[i + 1]) - eq_at(yb[i]) for i in range(YEARS)]
        months = {}
        for t, e in r["curve"]:
            months[datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m")] = e
        mchg, prev = [], START_BAL
        for k in sorted(months):
            mchg.append(months[k] - prev)
            prev = months[k]
        worst = min(mchg) if mchg else 0.0
        pos_m = sum(1 for x in mchg if x > 0)
        ok = sum(1 for x in ychg if x > 0) >= 3 and r["mdd"] < 0.25 and r["final"] > START_BAL
        out.append(f"| {name} | " + " | ".join(f"${x:+,.1f}" for x in ychg)
                   + f" | ${r['final']:,.1f} | {(r['final'] / START_BAL - 1) * 100:+.0f}% | −{r['mdd'] * 100:.1f}% | "
                     f"${worst:+,.1f} | {pos_m}/{len(mchg)} | {r['taken']} | {r['sk_small']} | {r['sk_cap']} | "
                     f"{r['upsized']} | {'✅' if ok else '❌'} |")
        small = ", ".join(f"{k} {v}" for k, v in sorted(r["small_by"].items(), key=lambda kv: -kv[1])[:6])
        tg.append(f"{'✅' if ok else '❌'} <b>{bot.esc(name)}</b>: $100 → <b>${r['final']:,.1f}</b> "
                  f"({(r['final'] / START_BAL - 1) * 100:+.0f}%), max DD −{r['mdd'] * 100:.1f}%, worst month "
                  f"${worst:+,.1f}, {pos_m}/{len(mchg)} months up\n   years: " + " · ".join(f"${x:+,.1f}" for x in ychg)
                  + f"\n   trades {r['taken']}, skipped too small {r['sk_small']}"
                  + (f" ({small})" if small else "") + f", skipped cap {r['sk_cap']}, max {r['max_open']} open")
    if notes:
        out.append("\nNotes: " + "; ".join(notes))
        tg.append("Notes: " + bot.esc("; ".join(notes)))
    out.append("\nCaveats: fills at the candle open (no slippage); no compounding; past results do not guarantee "
               "future results.")
    tg.append(f"Report only, nothing changed live. {(time.time() - t0) / 60:.0f} min.")
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
