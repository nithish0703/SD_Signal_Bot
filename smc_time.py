"""Time-filter test (report only, changes nothing live): what happens if signals may come at ANY hour?

Base = today's live rules (A+B+C: 8 filters from smc_filters.json). Variants remove the two time filters:
  1. Live A+B+C (killzone + London/NY)          2. without killzone (sweep at any hour)
  3. without London/NY session (signal any hour) 4. without both = signals 24/7
Same setups, exits and fees as the live bot; 50 futures coins, 1h, last 24 months, $1 risk -> R = $.
Per variant: trades, win, avg R, total R, max drawdown, loss streak, year 1 / year 2, profitable months,
walk-forward test months (same 4 folds as smc_wf), and for variant 4 the result by IST time of the signal.

Pass rule for adding more hours (fixed before running): versus live A+B+C, a variant must have
higher avg R in BOTH years, max drawdown not larger, and higher total R.
"""
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import smc_wf as wf  # noqa: E402
from smc_expd import curve  # noqa: E402

VARIANTS = [("1 Live A+B+C (killzone + London/NY)", ()),
            ("2 Without killzone", ("SMC_KILLZONE",)),
            ("3 Without London/NY session", ("SMC_LDN_NY",)),
            ("4 Without both (24/7)", ("SMC_KILLZONE", "SMC_LDN_NY"))]
IST_BUCKETS = [("Asia 5:30 AM–1:30 PM IST (00–08 UTC)", 0, 8),
               ("London 1:30–6:30 PM IST (08–13 UTC)", 8, 13),
               ("London–US overlap 6:30–9:30 PM IST (13–16 UTC)", 13, 16),
               ("US 9:30 PM–2:30 AM IST (16–21 UTC)", 16, 21),
               ("Late US 2:30–5:30 AM IST (21–24 UTC)", 21, 24)]


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
    span = lambda a, b: [t for t in trades if a <= t["time"] < b]

    saved = dict(bot.SMC_ACTIVE)
    res = {}
    for name, drop in VARIANTS:
        bot.SMC_ACTIVE.clear()
        bot.SMC_ACTIVE.update({k: v for k, v in saved.items() if k not in drop})
        allv = wf.live_filter(trades)
        n, avg, tot, win = wf.agg(allv)
        dd, ls = curve(allv)
        yrs = [wf.agg([t for t in allv if (t["time"] < mid) == first]) for first in (True, False)]
        months = defaultdict(float)
        for t in allv:
            months[time.strftime("%Y-%m", time.gmtime(t["time"] / 1000))] += wf.net(t)
        oos = [wf.agg(wf.live_filter(span(a, b))) for a, b in folds]
        hours = defaultdict(list)
        for t in allv:
            h = t.get("sig_hour", 12)
            hours[next(lbl for lbl, a, b in IST_BUCKETS if a <= h < b)].append(t)
        res[name] = dict(n=n, avg=avg, tot=tot, win=win, dd=dd, ls=ls, yrs=yrs, months=months, oos=oos, hours=hours)
    bot.SMC_ACTIVE.clear()
    bot.SMC_ACTIVE.update(saved)

    base = res[VARIANTS[0][0]]
    out = [f"## Time-filter test ({len(bot.SYMBOLS)} futures coins, 1h, {wf.MONTHS} months, $1 risk → R = $)\n",
           "| Variant | Trades | Win | Avg R | Total R | Max DD | Loss streak | Year 1 | Year 2 | Profitable months | WF test months | More hours OK? |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🕒 <b>Time-filter test</b> ({len(bot.SYMBOLS)} coins, {wf.MONTHS} months, $1 risk)"]
    for name, _ in VARIANTS:
        v = res[name]
        pm = f"{sum(1 for x in v['months'].values() if x > 0)}/{len(v['months'])}"
        oos = sum(x[2] for x in v["oos"])
        ok = "–" if v is base else ("✅" if all(v["yrs"][i][0] > 0 and v["yrs"][i][1] > base["yrs"][i][1] for i in (0, 1))
                                    and v["dd"] >= base["dd"] and v["tot"] > base["tot"] else "❌")
        out.append(f"| {name} | {v['n']} | {v['win']:.0f}% | {v['avg']:+.2f} | {v['tot']:+.1f} | {v['dd']:.1f} | {v['ls']} | "
                   f"{v['yrs'][0][0]} / {v['yrs'][0][2]:+.1f} | {v['yrs'][1][0]} / {v['yrs'][1][2]:+.1f} | {pm} | {oos:+.1f} | {ok} |")
        tg.append(f"{'' if ok == '–' else ok + ' '}{name}: {v['n']} trades, win {v['win']:.0f}%, {v['avg']:+.2f}R, total "
                  f"{v['tot']:+.0f}R, DD {v['dd']:.0f}R, streak {v['ls']}, Y1 {v['yrs'][0][2]:+.0f} · Y2 {v['yrs'][1][2]:+.0f}")
    full = res[VARIANTS[-1][0]]
    out.append("\n### 24/7 variant: result by IST time of the signal\n")
    out.append("| IST window | Trades | Win | Avg R | Total R |\n|---|---|---|---|---|")
    tg.append("24/7 by IST time of signal:")
    for lbl, _, _ in IST_BUCKETS:
        n, avg, tot, win = wf.agg(full["hours"].get(lbl, []))
        out.append(f"| {lbl} | {n} | {win:.0f}% | {avg:+.2f} | {tot:+.1f} |")
        tg.append(f"• {lbl}: {n} trades, {avg:+.2f}R, total {tot:+.0f}R")
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
