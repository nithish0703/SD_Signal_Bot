"""Session diagnostic (report only, changes nothing live): which TIME WINDOW suits the frozen A+B+C setup best?

A+B+C stays exactly as live (all 6 non-time filters from smc_filters.json untouched). Only the time rule changes:
  baseline  = live killzone + London/NY filters (as on main today)
  others    = both live time filters removed, then only signals whose SIGNAL candle (UTC hour) is inside the
              chosen session(s) are kept -- the window is applied before the live rules, like a live time filter.
Sessions (signal hour, UTC -> IST):
  Asia 00-08 (5:30 AM-1:30 PM) | London 08-13 (1:30-6:30 PM) | Overlap 13-16 (6:30-9:30 PM)
  US 16-21 (9:30 PM-2:30 AM) | Late US 21-24 (2:30-5:30 AM, only inside "All sessions")
Same setups, exits and fees as the live bot; 50 futures coins, 1h, last 24 months, $1 risk -> R = $.

Pass rule (fixed before running): versus the baseline, a window must have higher avg R in BOTH years,
max drawdown not larger, and higher total R. Otherwise the live killzone stays.
"""
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import smc_wf as wf  # noqa: E402
from smc_expd import curve  # noqa: E402

TIME_KEYS = ("SMC_KILLZONE", "SMC_LDN_NY")
S = {"Asia": (0, 8), "London": (8, 13), "Overlap": (13, 16), "US": (16, 21), "Late": (21, 24)}
VARIANTS = [("1 A+B+C baseline (live killzone)", None),
            ("2 Asia only", ("Asia",)),
            ("3 London only", ("London",)),
            ("4 London/NY overlap only", ("Overlap",)),
            ("5 US only", ("US",)),
            ("6 London + overlap", ("London", "Overlap")),
            ("7 London + US", ("London", "US")),
            ("8 All sessions (24/7)", tuple(S))]


def in_window(t, sess):
    h = t.get("sig_hour", -1)
    return any(S[s][0] <= h < S[s][1] for s in sess)


def pf(ts):
    rs = [wf.net(t) for t in ts]
    loss = -sum(r for r in rs if r < 0)
    return sum(r for r in rs if r > 0) / loss if loss else float("inf")


def main():
    t0 = time.time()
    bot.TOP_N = int(os.getenv("WF_COINS", "50"))
    bot.resolve_symbols()
    trades = wf.build_trades(int(wf.MONTHS * 730.5) + 300)
    end = time.time() * 1000
    start = end - wf.MONTHS * wf.MONTH_MS
    mid = start + (end - start) / 2
    trades = [t for t in trades if t["time"] >= start]
    folds, f = [], start + wf.TRAIN_M * wf.MONTH_MS
    while f + 0.5 * wf.TEST_M * wf.MONTH_MS <= end:
        folds.append((f, min(end, f + wf.TEST_M * wf.MONTH_MS)))
        f += wf.TEST_M * wf.MONTH_MS

    saved = dict(bot.SMC_ACTIVE)
    res = {}
    try:
        for name, sess in VARIANTS:
            bot.SMC_ACTIVE.clear()
            bot.SMC_ACTIVE.update(saved if sess is None else {k: v for k, v in saved.items() if k not in TIME_KEYS})
            pool = trades if sess is None else [t for t in trades if in_window(t, sess)]
            allv = wf.live_filter(pool)
            n, avg, tot, win = wf.agg(allv)
            dd, ls = curve(allv)
            yrs = [wf.agg([t for t in allv if (t["time"] < mid) == first]) for first in (True, False)]
            months = defaultdict(float)
            for t in allv:
                months[time.strftime("%Y-%m", time.gmtime(t["time"] / 1000))] += wf.net(t)
            oos = [wf.agg(wf.live_filter([t for t in pool if a <= t["time"] < b])) for a, b in folds]
            res[name] = dict(n=n, avg=avg, tot=tot, win=win, dd=dd, ls=ls, pf=pf(allv), yrs=yrs, months=months,
                             oos=oos)
    finally:
        bot.SMC_ACTIVE.clear()
        bot.SMC_ACTIVE.update(saved)

    base = res[VARIANTS[0][0]]
    out = [f"## Session diagnostic — A+B+C frozen ({len(bot.SYMBOLS)} futures coins, 1h, {wf.MONTHS} months, "
           "$1 risk → R = $)\n",
           "| Variant | Trades | Win | Avg R (R/trade) | Total R | R/month | Max DD | Profit factor | Loss streak | "
           f"Year 1 | Year 2 | Profitable months (of months with trades) | WF test ({len(folds)} folds) | WF folds + | Pass? |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🕒 <b>Session diagnostic</b> — A+B+C frozen ({len(bot.SYMBOLS)} coins, {wf.MONTHS} months, $1 risk)"]
    for name, _ in VARIANTS:
        v = res[name]
        pm = f"{sum(1 for x in v['months'].values() if x > 0)}/{len(v['months'])}"
        oos = sum(x[2] for x in v["oos"])
        fp = f"{sum(1 for x in v['oos'] if x[2] > 0)}/{len(v['oos'])}"
        ok = "–" if v is base else ("✅" if all(v["yrs"][i][0] > 0 and v["yrs"][i][1] > base["yrs"][i][1] for i in (0, 1))
                                    and v["dd"] >= base["dd"] and v["tot"] > base["tot"] else "❌")
        pft = "–" if not v["n"] else ("∞" if v["pf"] == float("inf") else f"{v['pf']:.2f}")
        out.append(f"| {name} | {v['n']} | {v['win']:.0f}% | {v['avg']:+.2f} | {v['tot']:+.1f} | "
                   f"{v['tot'] / wf.MONTHS:+.2f} | {v['dd']:.1f} | {pft} | {v['ls']} | "
                   f"{v['yrs'][0][0]} / {v['yrs'][0][2]:+.1f} | {v['yrs'][1][0]} / {v['yrs'][1][2]:+.1f} | {pm} | "
                   f"{oos:+.1f} | {fp} | {ok} |")
        tg.append(f"{'' if ok == '–' else ok + ' '}{name}: {v['n']} trades, win {v['win']:.0f}%, {v['avg']:+.2f}R, "
                  f"total {v['tot']:+.0f}R ({v['tot'] / wf.MONTHS:+.1f}/mo), PF {pft}, DD {v['dd']:.0f}R, "
                  f"streak {v['ls']}, Y1 {v['yrs'][0][2]:+.0f} · Y2 {v['yrs'][1][2]:+.0f}, months+ {pm}, WF {fp}")
    out.append("\nSessions by signal hour: Asia 00–08 UTC (5:30 AM–1:30 PM IST) · London 08–13 (1:30–6:30 PM) · "
               "Overlap 13–16 (6:30–9:30 PM) · US 16–21 (9:30 PM–2:30 AM) · All = 24 h incl. late US 21–24.")
    tg.append(f"Report only, nothing changed live. Not financial advice. {(time.time() - t0) / 60:.0f} min. "
              "Full table: Actions → run summary.")
    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as fh:
            fh.write(report + "\n")
    msg = "\n\n".join(tg)
    print(msg)
    for i in range(0, len(msg), 3900):
        bot.tg(msg[i:i + 3900])


if __name__ == "__main__":
    main()
