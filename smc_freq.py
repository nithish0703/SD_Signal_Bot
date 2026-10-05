"""Trade-frequency study (report only, changes nothing live): how do we get ~1 trade a day without losing the edge?

The SAME live strategy (sweep -> MSS -> FVG 50% limit, liquidity TP, A+B+C quality filters, SL% >= 1 and <= 4,
target >= 1R) is run as separate streams on three chart timeframes:
  15m chart (HTF 1h) · 1h chart (HTF 4h, = live) · 4h chart (HTF 1d)
each with the live time filters (killzone + London/NY, "KZ") and without them ("24/7").
Portfolios combine streams. Like a real account: ONE open position per coin at a time (any stream) -- a new
fill on a coin that already has an open trade is skipped. 50 futures coins, last 24 months, $1 risk -> R = $.

Goal label 🎯 (fixed before running): >= 0.7 trades/day AND total R >= live AND both years positive
AND max drawdown no worse than 1.5 x live.
"""
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import smc_wf as wf  # noqa: E402
import smc_ltf  # noqa: E402
from smc_expd import curve  # noqa: E402

TIME_KEYS = ("SMC_KILLZONE", "SMC_LDN_NY")
ROWS = [("1h KZ (live)", ("1h_kz",)), ("1h 24/7", ("1h_all",)),
        ("4h KZ", ("4h_kz",)), ("4h 24/7", ("4h_all",)),
        ("15m KZ", ("15m_kz",)), ("15m 24/7", ("15m_all",)),
        ("1h KZ + 4h KZ", ("1h_kz", "4h_kz")), ("1h KZ + 15m KZ", ("1h_kz", "15m_kz")),
        ("1h KZ + 4h KZ + 15m KZ", ("1h_kz", "4h_kz", "15m_kz")),
        ("1h 24/7 + 4h 24/7", ("1h_all", "4h_all")),
        ("All 24/7 (15m + 1h + 4h)", ("1h_all", "4h_all", "15m_all"))]


def stream(C, H, tf, sym, start, act, notime):
    """Live rules on one chart timeframe -> {'kz': [...], 'all': [...]} closed trades."""
    out = {"kz": [], "all": []}
    for st in bot.find_smc_setups(C, H, bot.SMC_ENTRY):
        if st["time"] < start or bot.sl_pct(st) < bot.MIN_SL_PCT:
            continue
        if bot.SMC_REQUIRE_DISCOUNT and not st["discount"]:
            continue
        if bot.SMC_REQUIRE_TREND and not st["trend"]:
            continue
        if not bot.passes_filters(st):
            continue
        st["sym"], st["slp"], st["tf"] = sym, bot.sl_pct(st), tf
        r = bot._smc(bot._smc_liq)(C, st)
        if r is None:
            continue
        _, ci = bot._exit_detail(C, st, st["side"], "price", None, st["sl"])
        st["res"], st["close_ct"] = {wf.EX: r}, (C[ci]["ct"] if ci is not None else None)
        kz = bot.smc_quality_ok(st)
        bot.SMC_ACTIVE.clear(); bot.SMC_ACTIVE.update(notime)
        al = bot.smc_quality_ok(st)
        bot.SMC_ACTIVE.clear(); bot.SMC_ACTIVE.update(act)
        if kz:
            out["kz"].append(st)
        if al:
            out["all"].append(st)
    return out


def one_per_coin(ts):
    """Real-account rule: skip a fill while the same coin still has an open trade (any stream)."""
    busy, out = {}, []
    for t in sorted(ts, key=lambda t: (t["time"], {"4h": 0, "1h": 1, "15m": 2}[t["tf"]])):
        if t["time"] <= busy.get(t["sym"], -1):
            continue
        out.append(t)
        busy[t["sym"]] = t.get("close_ct") or float("inf")
    return out


def stats(ts, mid, folds, days):
    n, avg, tot, win = wf.agg(ts)
    dd, ls = curve(ts)
    yrs = [wf.agg([t for t in ts if (t["time"] < mid) == first]) for first in (True, False)]
    months = defaultdict(float)
    for t in ts:
        months[time.strftime("%Y-%m", time.gmtime(t["time"] / 1000))] += wf.net(t)
    fp = sum(1 for a, b in folds if wf.agg([t for t in ts if a <= t["time"] < b])[2] > 0)
    return dict(n=n, pd=n / days, avg=avg, tot=tot, win=win, dd=dd, ls=ls, yrs=yrs,
                pm=f"{sum(1 for x in months.values() if x > 0)}/{len(months)}", wf=f"{fp}/{len(folds)}")


def main():
    t0 = time.time()
    bot.DATA_SOURCE = "futures"
    bot.TOP_N = int(os.getenv("WF_COINS", "50"))
    bot.resolve_symbols()
    total = int(wf.MONTHS * 730.5) + 300
    end = time.time() * 1000
    start = end - wf.MONTHS * wf.MONTH_MS
    mid = start + (end - start) / 2
    days = wf.MONTHS * 30.44
    folds, f = [], start + wf.TRAIN_M * wf.MONTH_MS
    while f + 0.5 * wf.TEST_M * wf.MONTH_MS <= end:
        folds.append((f, min(end, f + wf.TEST_M * wf.MONTH_MS)))
        f += wf.TEST_M * wf.MONTH_MS
    act = dict(bot.SMC_ACTIVE)
    notime = {k: v for k, v in act.items() if k not in TIME_KEYS}
    S = defaultdict(list)
    skipped = 0
    try:
        for sym in bot.SYMBOLS:
            try:
                C1, _ = bot.fetch_history(sym, "1h", total + 24 * 80)       # extra history for the 1d trend EMA
                C15 = smc_ltf.fetch15(sym, start - 3 * 86400000)
            except Exception as e:
                print(sym, "data error", e, flush=True)
                bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)
                skipped += 1
                continue
            if len(C15) < 1000:
                print(sym, "no 15m data - coin skipped in all streams", flush=True)
                bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)
                skipped += 1
                continue
            H4, D1 = bot._aggregate(C1, 4), bot._aggregate(C1, 24)
            try:
                got = {tf: stream(C, H, tf, sym, start, act, notime)
                       for tf, C, H in (("1h", C1, H4), ("4h", H4, D1), ("15m", C15, C1))}
            except Exception as e:                          # one bad coin must not stop the whole study
                print(sym, "error - coin skipped in all streams:", e, flush=True)
                bot.SMC_ACTIVE.clear(); bot.SMC_ACTIVE.update(act)
                skipped += 1
                continue
            for tf, o in got.items():
                S[f"{tf}_kz"] += o["kz"]
                S[f"{tf}_all"] += o["all"]
            print(f"{sym}: " + " · ".join(f"{k} {sum(1 for t in v if t['sym'] == sym)}" for k, v in sorted(S.items())),
                  flush=True)
            del C15
            bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)
    finally:
        bot.SMC_ACTIVE.clear()
        bot.SMC_ACTIVE.update(act)

    res = {name: stats(one_per_coin([t for k in keys for t in S[k]]), mid, folds, days) for name, keys in ROWS}
    base = res[ROWS[0][0]]

    def goal(v):
        return v["pd"] >= 0.7 and v["tot"] >= base["tot"] and all(v["yrs"][i][2] > 0 for i in (0, 1)) \
            and v["dd"] >= 1.5 * base["dd"]

    out = [f"## Trade-frequency study — live A+B+C rules on 15m / 1h / 4h charts ({len(bot.SYMBOLS) - skipped} "
           f"futures coins, {wf.MONTHS} months, $1 risk → R = $, one open trade per coin)\n",
           "| Streams | Trades | Trades/day | Win | Avg R | Total R | R/month | Max DD | Loss streak | Year 1 | Year 2 | "
           "Profitable months | Year-2 quarters + | Goal 🎯 |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"📈 <b>Trade-frequency study</b> — live rules on 15m / 1h / 4h charts ({len(bot.SYMBOLS) - skipped} coins, "
          f"{wf.MONTHS} months, $1 risk, 1 trade per coin)", "Goal 🎯: ≥0.7 trades/day, total ≥ live, both years "
          "positive, DD ≤ 1.5× live"]
    for name, _ in ROWS:
        v = res[name]
        g = "–" if v is base else ("🎯" if goal(v) else "❌")
        out.append(f"| {name} | {v['n']} | {v['pd']:.2f} | {v['win']:.0f}% | {v['avg']:+.2f} | {v['tot']:+.1f} | "
                   f"{v['tot'] / wf.MONTHS:+.2f} | {v['dd']:.1f} | {v['ls']} | {v['yrs'][0][0]} / {v['yrs'][0][2]:+.1f} | "
                   f"{v['yrs'][1][0]} / {v['yrs'][1][2]:+.1f} | {v['pm']} | {v['wf']} | {g} |")
        tg.append(f"{'' if g == '–' else g + ' '}{bot.esc(name)}: {v['n']} trades ({v['pd']:.2f}/day), win {v['win']:.0f}%, "
                  f"{v['avg']:+.2f}R, total {v['tot']:+.0f}R, DD {v['dd']:.0f}R, streak {v['ls']}, "
                  f"Y1 {v['yrs'][0][2]:+.0f} · Y2 {v['yrs'][1][2]:+.0f}")
    out.append("\nKZ on a 4h chart: the sweep candle must open at 08 or 12 UTC (4h candles open every 4 hours), so 4h KZ "
               "is much stricter than on 1h / 15m.")
    tg.append(f"Report only, nothing changed live. Not financial advice. {(time.time() - t0) / 60:.0f} min. "
              "Full table: Actions → run summary.")
    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as fh:
            fh.write(report + "\n")
    msg = "\n\n".join(tg)
    print(msg)
    bot.tg(msg[:3900])


if __name__ == "__main__":
    main()
