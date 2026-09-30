"""FX lab: honest backtest of evidence-based USD/INR strategies (report only, nothing live).

Data: Yahoo Finance "INR=X" (spot USD/INR) - hourly for the last ~730 days, daily since 2010.
Spot is used as a proxy for NSE USDINR futures during the NSE session (09:00-17:00 IST).

Intraday strategies (flat at the end of the session, one trade a day at most):
  1. Opening-range breakout: range = first hour; enter on a later hourly close beyond it, exit at the close.
  2. Intraday momentum (Gao et al. / Elaut et al.): direction of the first hour -> hold the last 2 hours.
  3. Morning fade (mean reversion): opposite of the move from the open to midday -> hold to the close.
  4. Session drift (Ranaldo time-of-day): long (and short) USD/INR from the open to the close every day.
Daily strategy:
  5. Time-series momentum (AQR): average sign of the 1, 3, 12 month return -> long / short, weekly
     rebalance. Futures carry: a long USDINR future pays the forward premium (assumed 3%/yr), a short earns it.

Costs per round trip per lot ($1,000): spread 1 tick (0.25 paise) + slippage 1 tick + brokerage
Rs 20/order x 2 + 18% GST, spread over the lots traded (1 lot vs 10 lots). 1 paise = Rs 10 per lot.
Pre-registered pass rule: net profit in BOTH halves of the sample at the 10-lot cost, with >= 50 trades each.
"""
import math
import os
import re
import sys
from datetime import timedelta

import pandas as pd

TICK = 0.0025                      # Rs per USD
BROKERAGE_RT = 20 * 2 * 1.18       # Rs per round trip (per order, not per lot)
LOT = 1000                         # USD
CARRY = 0.03                       # assumed forward premium per year (long pays, short earns)


# (name, Yahoo ticker, session tz, session start hour, end hour, pip size, round-trip cost in pips, note)
INSTRUMENTS = [
    ("USD/INR", "INR=X", "Asia/Kolkata", 9, 17, 0.01, None, "NSE, paise, 10-lot cost"),
    ("EUR/USD", "EURUSD=X", "Europe/London", 8, 17, 0.0001, 0.8, "NSE cross allowed"),
    ("GBP/USD", "GBPUSD=X", "Europe/London", 8, 17, 0.0001, 1.2, "NSE cross allowed"),
    ("USD/JPY", "JPY=X", "Europe/London", 8, 17, 0.01, 1.0, "NSE cross allowed"),
    ("AUD/USD", "AUDUSD=X", "Europe/London", 8, 17, 0.0001, 1.0, "not tradable legally from India"),
    ("USD/CHF", "CHF=X", "Europe/London", 8, 17, 0.0001, 1.3, "not tradable legally from India"),
    ("Gold (XAU/USD)", "GC=F", "Europe/London", 8, 17, 0.1, 3.0, "MCX gold is the legal route"),
]


def cost_paise(lots):
    """Round-trip cost per lot in paise (per USD)."""
    return (2 * TICK * 100) + BROKERAGE_RT / lots / LOT * 100


def load(interval, ticker="INR=X", tz="Asia/Kolkata", **kw):
    import yfinance as yf
    df = yf.Ticker(ticker).history(interval=interval, auto_adjust=False, **kw)
    if df is None or df.empty:
        raise RuntimeError(f"no {interval} data")
    df = df[["Open", "High", "Low", "Close"]].dropna()
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df.index = idx.tz_convert(tz)
    return df


def sessions(h, start=9, end=17):
    """Hourly bars inside the session (local time), grouped per day (needs >= 6 bars)."""
    s = h[(h.index.hour >= start) & (h.index.hour < end)]
    out = []
    for day, g in s.groupby(s.index.date):
        if len(g) >= 6:
            out.append((pd.Timestamp(day), g))
    return out


def intraday_trades(days):
    """-> {strategy: [(day, side, entry, exit)]}"""
    res = {"1. Opening-range breakout": [], "2. Intraday momentum": [], "3. Morning fade": [],
           "4a. Session drift LONG": [], "4b. Session drift SHORT": []}
    k = list(res)
    for day, g in days:
        o, c, hi, lo = g["Open"].values, g["Close"].values, g["High"].values, g["Low"].values
        n = len(g)
        # 1. ORB
        rh, rl = hi[0], lo[0]
        for j in range(1, n - 1):
            if c[j] > rh:
                res[k[0]].append((day, 1, c[j], c[-1]))
                break
            if c[j] < rl:
                res[k[0]].append((day, -1, c[j], c[-1]))
                break
        # 2. momentum: first hour direction, enter at the close of bar n-3, exit at the close
        first = c[0] - o[0]
        if first != 0 and n >= 4:
            res[k[1]].append((day, 1 if first > 0 else -1, c[n - 3], c[-1]))
        # 3. fade: move from the open to midday, reversed
        mid = n // 2
        mv = c[mid - 1] - o[0]
        if mv != 0:
            res[k[2]].append((day, -1 if mv > 0 else 1, c[mid - 1], c[-1]))
        # 4. session drift
        res[k[3]].append((day, 1, o[0], c[-1]))
        res[k[4]].append((day, -1, o[0], c[-1]))
    return res


def evaluate(trades, split_day, pip, cost_pips):
    """Pips per trade, gross and net of the round-trip cost, for both halves."""
    h1 = [t for t in trades if t[0] < split_day]
    h2 = [t for t in trades if t[0] >= split_day]
    out = {}
    for name, sub in (("h1", h1), ("h2", h2)):
        gross = [(e2 - e1) * side / pip for _, side, e1, e2 in sub]
        net = [g - cost_pips for g in gross]
        out[name] = {"n": len(sub), "gross": sum(gross) / len(sub) if sub else 0.0,
                     "win": sum(1 for g in net if g > 0) / len(sub) * 100 if sub else 0.0,
                     "net": sum(net) / len(sub) if sub else 0.0, "tot": sum(net)}
    return out


def tsmom_daily(d, carry=0.0, cost_frac=0.0):
    """AQR time-series momentum on daily closes; weekly rebalance (Mondays); carry for futures."""
    c = d["Close"]
    pos, rows = 0.0, []
    dates = list(c.index)
    for i in range(253, len(dates) - 1):
        t = dates[i]
        if t.weekday() == 0 or pos == 0 and i == 253:
            sig = sum((1 if c.iloc[i] > c.iloc[i - L] else -1) for L in (21, 63, 252)) / 3
            new = 1.0 if sig > 0 else -1.0
            if new != pos:
                rows.append(("trade", t))
                # cost of the change (10-lot cost level), in return terms on the notional
                rows.append(("ret", t, -abs(new - pos) * cost_frac))
            pos = new
        r = c.iloc[i + 1] / c.iloc[i] - 1
        days = (dates[i + 1] - t).days
        rows.append(("ret", dates[i + 1], pos * r - pos * carry * days / 365))
    agg = {}
    for kind, t, *rest in rows:
        if kind == "ret":
            agg[t] = agg.get(t, 0.0) + rest[0]
    rets = pd.Series(agg).sort_index()
    n_trades = sum(1 for r in rows if r[0] == "trade")
    return rets, n_trades


def year_table(rets):
    by = {}
    for t, r in rets.items():
        by.setdefault(t.year, 1.0)
        by[t.year] *= 1 + r
    return {y: v - 1 for y, v in by.items()}


def daily_stats(rets):
    eq = (1 + rets).prod()
    yrs = max(0.5, (rets.index[-1] - rets.index[0]).days / 365)
    mdd, peak, e = 0.0, 1.0, 1.0
    for r in rets:
        e *= 1 + r
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    sd = rets.std() * math.sqrt(252)
    half = len(rets) // 2
    return {"cagr": eq ** (1 / yrs) - 1, "sharpe": rets.mean() * 252 / sd if sd else 0.0, "mdd": mdd,
            "h1": (1 + rets.iloc[:half]).prod() - 1, "h2": (1 + rets.iloc[half:]).prod() - 1,
            "years": year_table(rets)}


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bot
    head = ("💱 <b>FX lab</b> – intraday strategies on hourly data (last ~730 days, split in 2 halves) + daily trend "
            "since 2010. Sessions: USD/INR 09–17 IST (NSE), others London 08–17 (most liquid hours). Costs = tight "
            "best-case round trip (EUR 0.8 pip, GBP 1.2, JPY 1.0, AUD 1.0, CHF 1.3, gold $0.30; USD/INR 10-lot NSE cost "
            f"{cost_paise(10):.2f} paise). Pass ✅ = net profit in BOTH halves with ≥50 trades each.\n"
            "Strategies: 1 opening-range breakout · 2 intraday momentum (first hour → last 2 h) · 3 morning fade · "
            "4a/4b session drift long/short.")
    blocks, summary = [], []
    for name, tick, tz, st, en, pip, cpips, note in INSTRUMENTS:
        cp = cost_paise(10) if cpips is None else cpips
        unit = "paise" if cpips is None else ("$" if "Gold" in name else "pips")
        lines = [f"\n<b>{name}</b> ({note})"]
        try:
            h = load("1h", tick, tz, period="730d")
            days = sessions(h, st, en)
            if len(days) < 120:
                raise RuntimeError(f"only {len(days)} sessions")
            split = days[len(days) // 2][0]
            rng = sum((g["High"].max() - g["Low"].min()) / pip for _, g in days) / len(days)
            scale = 10 if "Gold" in name else 1                      # gold: show $ (pip = $0.10)
            lines.append(f"{len(days)} sessions {days[0][0]:%b %Y}→{days[-1][0]:%b %Y}, avg session range "
                         f"{rng / scale:.1f} {unit}, cost {cp / scale:.2f} {unit}")
            for sname, tr in intraday_trades(days).items():
                ev = evaluate(tr, split, pip, cp)
                ok = all(ev[x]["n"] >= 50 and ev[x]["net"] > 0 for x in ("h1", "h2"))
                if ok:
                    summary.append(f"{name}: {sname}")
                lines.append(f"{sname} {'✅' if ok else '❌'} net/trade {ev['h1']['net'] / scale:+.2f} | {ev['h2']['net'] / scale:+.2f} {unit} "
                             f"(gross {ev['h1']['gross'] / scale:+.2f} | {ev['h2']['gross'] / scale:+.2f}, "
                             f"{ev['h1']['n']}+{ev['h2']['n']} trades)")
        except Exception as ex:
            lines.append(f"intraday data error: {ex}")
        try:
            d = load("1d", tick, tz, start="2010-01-01")
            cf = (cp * pip) / float(d["Close"].iloc[-1])
            rets, ntr = tsmom_daily(d, CARRY if cpips is None else 0.0, cf)
            ds = daily_stats(rets)
            ok = ds["h1"] > 0 and ds["h2"] > 0
            if ok:
                summary.append(f"{name}: 5 daily trend")
            lines.append(f"5 daily trend (AQR 1/3/12m) {'✅' if ok else '❌'} CAGR {ds['cagr'] * 100:+.1f}%, "
                         f"Sharpe {ds['sharpe']:.2f}, max DD {ds['mdd'] * 100:.0f}%, halves {ds['h1'] * 100:+.0f}% / "
                         f"{ds['h2'] * 100:+.0f}% | " + " ".join(f"{str(y)[2:]}:{v * 100:+.0f}" for y, v in ds["years"].items()))
        except Exception as ex:
            lines.append(f"daily data error: {ex}")
        blocks.append("\n".join(lines))
    tail = ("\n\n<b>Passed (both halves profitable after costs):</b> " + (", ".join(summary) if summary else "none")
            + "\nDaily trend = 1x notional, spot data (USD/INR includes a 3%/yr futures carry). "
              "Report only – not financial advice.")
    msg = head + "".join(blocks) + tail
    print(msg)
    chunk = ""
    for part in [head] + blocks + [tail]:
        if len(chunk) + len(part) > 3800:
            bot.tg(chunk)
            chunk = ""
        chunk += part
    if chunk:
        bot.tg(chunk)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as fh:
            fh.write("## FX lab\n\n" + re.sub(r"</?b>", "**", msg).replace("\n", "  \n") + "\n")


if __name__ == "__main__":
    main()
