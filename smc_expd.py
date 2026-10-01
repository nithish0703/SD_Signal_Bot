"""Experiment D - C1 entry quality 2 (report only, changes nothing live).

A = today's dev rules (C1 = sweep <=1 ATR + sweep candle reclaims, on top of the 4 live filters; from smc_filters.json)
B = MSS within 6 candles of the sweep
C = fresh FVG (limit filled within 3 candles of the setup -> fill_wait <= 3)
D = FVG filled within 12 candles (fill_wait <= 12)      note: C implies D, so A+C+D == A+C and A+B+C+D == A+B+C

For each variant, last 24 months, 50 futures coins, 1h, same setups / exits / fees as the live bot:
walk-forward (12m train -> 3m test, 4 folds), trade retention, avg R, total R, max drawdown, loss streak,
LONG / SHORT, year 1 / year 2, month by month, top-coin and biggest-winner dependency.
Plus a stage label for every A trade: the first weak step in Sweep -> MSS -> FVG -> Entry.

Pass rule (fixed before running), versus A:
  better avg net R in >= 3 of 4 walk-forward folds AND higher out-of-sample total R
  AND max drawdown not larger AND >= 60% of A's trades kept AND better avg R in BOTH years
  AND still more total R than A after removing each one's own biggest winner.
"""
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import smc_wf as wf  # noqa: E402

B = lambda t: t["mss_bars"] <= 6
C = lambda t: (t["fill_wait"] or 0) <= 3
D = lambda t: (t["fill_wait"] or 0) <= 12
VARIANTS = [("A", []), ("A+B", [B]), ("A+C", [C]), ("A+D", [D]), ("A+B+C", [B, C]),
            ("A+B+D", [B, D]), ("A+C+D", [C, D]), ("A+B+C+D", [B, C, D])]


def rule(fs):
    return (lambda t: all(f(t) for f in fs)) if fs else None


def curve(ts):
    """Chronological (by close) equity in R -> max drawdown, longest losing streak."""
    seq = sorted(ts, key=lambda t: t.get("close_ct") or t["time"])
    eq = peak = dd = 0.0
    streak = best = 0
    for t in seq:
        r = wf.net(t)
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if r <= 0 else 0
        best = max(best, streak)
    return dd, best


def stage(t):
    """First weak step of the setup (thresholds fixed before running)."""
    if t["sweep_depth"] > 0.5:
        return "1 Sweep: deeper than 0.5 ATR"
    if t["mss_bars"] > 6 or t["mss_margin"] < 0.2:
        return "2 MSS: slow (>6 candles) or weak (<0.2 ATR past level)"
    if t["fvg_atr"] < 0.2:
        return "3 FVG: small (<0.2 ATR)"
    if (t["fill_wait"] or 0) > 3:
        return "4 Entry: late fill (>3 candles)"
    return "5 Clean: no weak step"


def main():
    t0 = time.time()
    bot.TOP_N = int(os.getenv("WF_COINS", "50"))
    bot.resolve_symbols()
    trades = wf.build_trades(int(wf.MONTHS * 730.5) + 300)
    end = time.time() * 1000
    start = end - wf.MONTHS * wf.MONTH_MS
    mid = start + (end - start) / 2
    trades = [t for t in trades if t["time"] >= start]
    span = lambda a, b: [t for t in trades if a <= t["time"] < b]
    folds, f = [], start + wf.TRAIN_M * wf.MONTH_MS
    while f + 0.5 * wf.TEST_M * wf.MONTH_MS <= end:
        folds.append((f, min(end, f + wf.TEST_M * wf.MONTH_MS)))
        f += wf.TEST_M * wf.MONTH_MS

    res = {}
    for name, fs in VARIANTS:
        r = rule(fs)
        allv = wf.live_filter(trades, r)
        n, avg, tot, win = wf.agg(allv)
        dd, ls = curve(allv)
        fold = [wf.agg(wf.live_filter(span(a, b), r)) for a, b in folds]
        yrs = [wf.agg([t for t in allv if (t["time"] < mid) == first]) for first in (True, False)]
        side = {s: wf.agg([t for t in allv if t["side"] == s]) for s in ("LONG", "SHORT")}
        months = defaultdict(float)
        for t in allv:
            months[time.strftime("%Y-%m", time.gmtime(t["time"] / 1000))] += wf.net(t)
        coin = defaultdict(float)
        for t in allv:
            coin[t["sym"]] += wf.net(t)
        top = sorted(coin.items(), key=lambda x: -x[1])
        rs = sorted((wf.net(t) for t in allv), reverse=True)
        res[name] = dict(n=n, avg=avg, tot=tot, win=win, dd=dd, ls=ls, fold=fold, yrs=yrs, side=side,
                         months=months, top=top, no_best=tot - (rs[0] if rs else 0),
                         no_best3=tot - sum(rs[:3]), best=rs[0] if rs else 0)

    a = res["A"]
    out = [f"## Experiment D – C1 entry quality 2 ({len(bot.SYMBOLS)} futures coins, 1h, {wf.MONTHS} months)\n",
           f"A = today's dev rules: {bot.smc_quality_txt()}. B = MSS ≤6 candles, C = fresh FVG (fill ≤3), "
           "D = fill ≤12. C implies D, so A+C+D = A+C and A+B+C+D = A+B+C.\n",
           "| Variant | Trades (kept) | Win | Avg R | Total R | Max DD | Loss streak | Year 1 | Year 2 | WF folds better | WF out-of-sample | Pass? |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🧪 <b>Experiment D – C1 entry quality 2</b> ({len(bot.SYMBOLS)} coins, {wf.MONTHS} months)",
          f"A (C1): {a['n']} trades, {a['avg']:+.2f}R/trade, total {a['tot']:+.0f}R, max DD {a['dd']:.0f}R, "
          f"streak {a['ls']}, year 1 {a['yrs'][0][2]:+.0f}R · year 2 {a['yrs'][1][2]:+.0f}R"]
    for name, _ in VARIANTS:
        v = res[name]
        better = sum(1 for fv, fa in zip(v["fold"], a["fold"]) if fv[0] > 0 and fv[1] > fa[1]) if name != "A" else 0
        oos = sum(x[2] for x in v["fold"])
        keep = v["n"] / a["n"] * 100 if a["n"] else 0
        ok = name != "A" and (better >= max(1, len(folds) - 1) and oos > sum(x[2] for x in a["fold"])
                              and v["dd"] >= a["dd"] and keep >= 60
                              and all(v["yrs"][i][0] > 0 and v["yrs"][i][1] > a["yrs"][i][1] for i in (0, 1))
                              and v["no_best"] > a["no_best"])
        v["ok"] = ok
        out.append(f"| {name} | {v['n']} ({keep:.0f}%) | {v['win']:.0f}% | {v['avg']:+.2f} | {v['tot']:+.1f} | "
                   f"{v['dd']:.1f} | {v['ls']} | {v['yrs'][0][0]} / {v['yrs'][0][2]:+.1f} | {v['yrs'][1][0]} / {v['yrs'][1][2]:+.1f} | "
                   f"{'–' if name == 'A' else f'{better}/{len(folds)}'} | {oos:+.1f} | {'–' if name == 'A' else ('✅' if ok else '❌')} |")
        if name != "A":
            tg.append(f"{'✅' if ok else '❌'} {name}: {v['n']} trades ({keep:.0f}%), {v['avg']:+.2f}R, total {v['tot']:+.0f}R, "
                      f"DD {v['dd']:.0f}R, streak {v['ls']}, Y1 {v['yrs'][0][2]:+.0f} · Y2 {v['yrs'][1][2]:+.0f}, "
                      f"WF {better}/{len(folds)}")

    out.append("\n### LONG / SHORT, concentration\n")
    out.append("| Variant | LONG n / R | SHORT n / R | Top coin share | Top 3 coins | Biggest win | Total without biggest | without top 3 wins |\n|---|---|---|---|---|---|---|---|")
    for name, _ in VARIANTS:
        v = res[name]
        t1 = v["top"][0] if v["top"] else ("-", 0)
        t3 = sum(x[1] for x in v["top"][:3])
        share = (t1[1] / v["tot"] * 100) if v["tot"] > 0 else 0
        out.append(f"| {name} | {v['side']['LONG'][0]} / {v['side']['LONG'][2]:+.1f} | {v['side']['SHORT'][0]} / "
                   f"{v['side']['SHORT'][2]:+.1f} | {t1[0][:-4] if t1[0] != '-' else '-'} {share:.0f}% | {t3:+.1f}R | "
                   f"{v['best']:+.1f}R | {v['no_best']:+.1f}R | {v['no_best3']:+.1f}R |")

    out.append("\n### Month by month (net R)\n")
    mk = sorted({m for v in res.values() for m in v["months"]})
    out.append("| Month | " + " | ".join(n for n, _ in VARIANTS) + " |\n|---|" + "---|" * len(VARIANTS))
    for m in mk:
        out.append(f"| {m} | " + " | ".join(f"{res[n]['months'].get(m, 0):+.1f}" for n, _ in VARIANTS) + " |")
    out.append("| Profitable months | " + " | ".join(
        f"{sum(1 for x in res[n]['months'].values() if x > 0)}/{len(res[n]['months'])}" for n, _ in VARIANTS) + " |")

    # stage labels on A trades
    a_tr = wf.live_filter(trades)
    groups = defaultdict(list)
    for t in a_tr:
        groups[stage(t)].append(t)
    out.append("\n### Where A's setups are weak (first weak step: Sweep → MSS → FVG → Entry)\n")
    out.append("| Stage | Trades | Win | Avg R | Total R | Share of all losses |\n|---|---|---|---|---|---|")
    loss_total = sum(-wf.net(t) for t in a_tr if wf.net(t) < 0) or 1
    st_lines = []
    for k in sorted(groups):
        n, avg, tot, win = wf.agg(groups[k])
        ls = sum(-wf.net(t) for t in groups[k] if wf.net(t) < 0) / loss_total * 100
        out.append(f"| {k} | {n} | {win:.0f}% | {avg:+.2f} | {tot:+.1f} | {ls:.0f}% |")
        st_lines.append(f"{bot.esc(k)}: {n} trades, {avg:+.2f}R, {ls:.0f}% of losses")   # "<" breaks Telegram HTML

    passed = [n for n, _ in VARIANTS if res[n].get("ok")]
    tg.append("Stage of weakness (A trades):\n" + "\n".join("• " + s for s in st_lines))
    tg.append(("✅ Passed all checks: " + ", ".join(passed)) if passed else
              "❌ No variant passed every check (walk-forward, both years, DD, ≥60% trades, biggest-winner).")
    tg.append(f"Report only. Funding/slippage not included. Not financial advice. {(time.time() - t0) / 60:.0f} min. "
              "Full tables: Actions → run summary.")
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
