"""SL study (report only, changes nothing live): WHY do A+B+C trades hit SL, and can stop placement or a
same-direction limit fix it?  Live A+B+C entry rules stay frozen; 50 futures coins, 1h, last 24 months, $1 risk -> R = $.

Part 1  SL autopsy of every live A+B+C loss:
        - was the trade in profit first (best move before SL, in R)?
        - how fast was SL hit (candles after fill)?
        - after SL, did price still reach the original TP within 48 candles ("right idea, stop too tight"),
          and would a stop 0.25 / 0.5 / 1 ATR lower have survived to that TP?
        - how many losses came while 2+ other trades in the same direction were open (one market move)?
Part 2  Stop buffer below the sweep wick: live 0.1 ATR vs 0.25 / 0.5 / 0.75 / 1.0 ATR. Setups are rebuilt
        for each buffer, so SL%, 1R target, max-SL and fill rules all react exactly like live would.
        $1 risk stays fixed: a wider stop means a smaller position and fewer R at the same target.
Part 3  Portfolio limits on the live setup: max 1 / max 2 open trades in the same direction,
        and pause new trades after 2 SL in 24h.
Part 4  Exit management on all the same live trades: stop to breakeven
        at +1R / +1.5R, and 50% profit at +1R with the rest to the liquidity TP. (Drawdown order uses the
        live close time, a close approximation.)

Pass rule (fixed before running): versus live, higher avg R in BOTH years, max drawdown not larger,
and higher total R. Only a passing change is worth a dev account test.
Second label "🛡️ safer" (added after a 10-coin preview, so weaker evidence): total R at least 90% of live,
max drawdown at least 30% smaller and a shorter loss streak -- less pain for nearly the same profit.
"""
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import smc_wf as wf  # noqa: E402
from smc_expd import curve  # noqa: E402

BUFFERS = [0.1, 0.25, 0.5, 0.75, 1.0]
AFTER = 48
_cache = {}
_orig_fetch = None


def _fetch(symbol, interval, total, source=None):
    key = (symbol, interval, total, source, bot.DATA_SOURCE)
    if key not in _cache:
        _cache[key] = _orig_fetch(symbol, interval, total, source)
    return _cache[key]


def pf(ts):
    rs = [wf.net(t) for t in ts]
    loss = -sum(r for r in rs if r < 0)
    return "–" if not rs else ("∞" if not loss else f"{sum(r for r in rs if r > 0) / loss:.2f}")


def curve0(ts):
    """Max drawdown and longest losing streak; a breakeven exit (net exactly 0) neither extends nor breaks it."""
    dd, _ = curve(ts)
    streak = best = 0
    for t in sorted(ts, key=lambda t: t.get("close_ct") or t["time"]):
        r = wf.net(t)
        if r < 0:
            streak += 1
            best = max(best, streak)
        elif r > 0:
            streak = 0
    return dd, best


def stats(ts, mid, folds):
    n, avg, tot, win = wf.agg(ts)
    dd, ls = curve0(ts)
    yrs = [wf.agg([t for t in ts if (t["time"] < mid) == first]) for first in (True, False)]
    months = defaultdict(float)
    for t in ts:
        months[time.strftime("%Y-%m", time.gmtime(t["time"] / 1000))] += wf.net(t)
    fp = sum(1 for a, b in folds if wf.agg([t for t in ts if a <= t["time"] < b])[2] > 0)
    return dict(n=n, avg=avg, tot=tot, win=win, dd=dd, ls=ls, pf=pf(ts), yrs=yrs,
                pm=f"{sum(1 for x in months.values() if x > 0)}/{len(months)}", wf=f"{fp}/{len(folds)}")


def passes(v, base):
    return all(v["yrs"][i][0] > 0 and v["yrs"][i][1] > base["yrs"][i][1] for i in (0, 1)) \
        and v["dd"] >= base["dd"] and v["tot"] > base["tot"]


def safer(v, base):
    return base["tot"] > 0 and v["tot"] >= 0.9 * base["tot"] and v["dd"] >= 0.7 * base["dd"] and v["ls"] < base["ls"]


def autopsy(t, C):
    """Loss details in LONG terms (SHORT mirrored)."""
    side = t["side"]
    E, S = bot._px(t["entry"], side), bot._px(t["sl"], side)
    TP, risk, atr = bot._px(t["tp"], side), E - S, t["atr"] or 1e-9
    _, ci = bot._exit_detail(C, t, side, "price", None, t["sl"])
    if ci is None:
        return None
    hl = lambda x: (x["h"], x["l"]) if side == "LONG" else (-x["l"], -x["h"])
    mfe = max([hl(C[i])[0] for i in range(t["e"] + 1, ci)] or [E]) - E
    d = {"mfe": mfe / risk, "bars": ci - t["e"]}
    rescued, hit_tp = {}, False
    for i in range(ci, min(len(C), ci + AFTER + 1)):
        hi, lo = hl(C[i])
        if i > ci and hi >= TP:
            hit_tp = True
            break
    d["tp_after"] = hit_tp
    for x in (0.25, 0.5, 1.0):
        ok = False
        for i in range(ci, min(len(C), ci + AFTER + 1)):
            hi, lo = hl(C[i])
            if lo < S - x * atr:
                break
            if i > ci and hi >= TP:
                ok = True
                break
        rescued[x] = ok
    d["rescued"] = rescued
    d["over"] = (S - hl(C[ci])[1]) / atr                 # wick through the stop on the SL candle
    return d


def main():
    global _orig_fetch
    t0 = time.time()
    _orig_fetch = bot.fetch_history
    bot.fetch_history = _fetch
    bot.TOP_N = int(os.getenv("WF_COINS", "50"))
    bot.resolve_symbols()
    total = int(wf.MONTHS * 730.5) + 300
    end = time.time() * 1000
    start = end - wf.MONTHS * wf.MONTH_MS
    mid = start + (end - start) / 2
    folds, f = [], start + wf.TRAIN_M * wf.MONTH_MS
    while f + 0.5 * wf.TEST_M * wf.MONTH_MS <= end:
        folds.append((f, min(end, f + wf.TEST_M * wf.MONTH_MS)))
        f += wf.TEST_M * wf.MONTH_MS

    live_buf = bot.SMC_SL_BUFFER_ATR
    by_buf = {}
    try:
        for b in BUFFERS:
            bot.SMC_SL_BUFFER_ATR = b
            ts = [t for t in wf.build_trades(total) if t["time"] >= start]
            by_buf[b] = wf.live_filter(ts)
    finally:
        bot.SMC_SL_BUFFER_ATR = live_buf
    live = by_buf[0.1]
    base = stats(live, mid, folds)

    # ---- Part 1: autopsy
    losses = [t for t in live if t["res"][wf.EX] is not None and t["res"][wf.EX] < 0]
    rows = []
    for t in losses:
        C, _ = _fetch(t["sym"], "1h", total)
        a = autopsy(t, C)
        if a:
            rows.append((t, a))
    opened = sorted(live, key=lambda t: t["time"])
    def crowd(t):
        return sum(1 for o in opened if o is not t and o["side"] == t["side"] and o["time"] <= t["time"]
                   and (o.get("close_ct") or 9e18) > t["time"])
    nl = len(rows) or 1
    pct = lambda k: f"{k} ({k / nl * 100:.0f}%)"
    A = {
        "Losses analysed": str(len(rows)),
        "Never reached +0.5R before SL (wrong from the start)": pct(sum(1 for _, a in rows if a["mfe"] < 0.5)),
        "Reached +0.5R–1R, then SL": pct(sum(1 for _, a in rows if 0.5 <= a["mfe"] < 1)),
        "Reached ≥ +1R, then SL (gave back profit)": pct(sum(1 for _, a in rows if a["mfe"] >= 1)),
        "SL within 2 candles of fill": pct(sum(1 for _, a in rows if a["bars"] <= 2)),
        "SL in 3–12 candles": pct(sum(1 for _, a in rows if 3 <= a["bars"] <= 12)),
        "SL after 12+ candles": pct(sum(1 for _, a in rows if a["bars"] > 12)),
        f"Price still hit the TP within {AFTER} candles after SL": pct(sum(1 for _, a in rows if a["tp_after"])),
        "…and a stop 0.25 ATR lower would have won": pct(sum(1 for _, a in rows if a["rescued"][0.25])),
        "…and a stop 0.5 ATR lower would have won": pct(sum(1 for _, a in rows if a["rescued"][0.5])),
        "…and a stop 1 ATR lower would have won": pct(sum(1 for _, a in rows if a["rescued"][1.0])),
        "SL candle wick < 0.5 ATR past SL (stop-hunt wick)": pct(sum(1 for _, a in rows if a["over"] < 0.5)),
        "SL candle went > 1 ATR past SL (strong move against)": pct(sum(1 for _, a in rows if a["over"] > 1)),
        "Loss while 2+ other same-direction trades open": pct(sum(1 for t, _ in rows if crowd(t) >= 2)),
        "LONG losses / SHORT losses": f"{sum(1 for t, _ in rows if t['side'] == 'LONG')} / "
                                      f"{sum(1 for t, _ in rows if t['side'] == 'SHORT')}",
    }

    # ---- Part 2 + 3: variants
    var = [("1 Live A+B+C (SL 0.1 ATR below sweep)", base)]
    for b in BUFFERS[1:]:
        var.append((f"SL {b:g} ATR below sweep", stats(by_buf[b], mid, folds)))
    for k in (1, 2):
        sel = [dict(t) for t in live]
        bot.tag_sequence(sel, wf.EX, max_dir=k)
        var.append((f"Max {k} open trade{'s' if k > 1 else ''} same direction",
                    stats([t for t in sel if t["dir_ok"]], mid, folds)))
    sel = [dict(t) for t in live]
    bot.tag_sequence(sel, wf.EX, breaker_n=2)
    var.append(("Pause after 2 SL in 24h", stats([t for t in sel if t["breaker_ok"]], mid, folds)))
    exits = [("Breakeven stop at +1R", bot._smc(lambda C, s: bot._smc_liq_be(C, s, 1.0))),
             ("Breakeven stop at +1.5R", bot._smc(lambda C, s: bot._smc_liq_be(C, s, 1.5))),
             ("50% profit at +1R, rest to TP", bot._smc(bot._smc_liq_partial))]
    for name, fn in exits:
        sel = []
        for t in live:
            r = fn(_fetch(t["sym"], "1h", total)[0], t)
            if r is not None:
                x = dict(t, res={wf.EX: r})
                if abs(wf.net(x)) < 1e-9:                 # breakeven exit: exactly 0 after fees, not a win
                    x["res"] = {wf.EX: bot.FEE_PCT / t["slp"]}
                sel.append(x)
        var.append((name, stats(sel, mid, folds)))

    out = [f"## SL study — A+B+C entries frozen ({len(bot.SYMBOLS)} futures coins, 1h, {wf.MONTHS} months, "
           "$1 risk → R = $)\n", "### Part 1 — why live trades hit SL\n", "| Question | Losses |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in A.items()]
    out += ["\n### Part 2–4 — stop placement, same-direction limits, exit management\n",
            "| Variant | Trades | Win | Avg R | Total R | Max DD | Profit factor | Loss streak | Year 1 | Year 2 | "
            "Profitable months | WF folds + | Pass? (✅ pass · 🛡️ safer · ❌) |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🔬 <b>SL study</b> — A+B+C frozen ({len(bot.SYMBOLS)} coins, {wf.MONTHS} months, $1 risk)",
          "<b>Why live trades hit SL</b>\n" + "\n".join(f"• {bot.esc(k)}: {v}" for k, v in A.items()),
          "<b>Fix candidates</b>"]
    for name, v in var:
        ok = "–" if v is base else ("✅" if passes(v, base) else "🛡️" if safer(v, base) else "❌")
        out.append(f"| {name} | {v['n']} | {v['win']:.0f}% | {v['avg']:+.2f} | {v['tot']:+.1f} | {v['dd']:.1f} | "
                   f"{v['pf']} | {v['ls']} | {v['yrs'][0][0]} / {v['yrs'][0][2]:+.1f} | "
                   f"{v['yrs'][1][0]} / {v['yrs'][1][2]:+.1f} | {v['pm']} | {v['wf']} | {ok} |")
        tg.append(f"{'' if ok == '–' else ok + ' '}{name}: {v['n']} trades, win {v['win']:.0f}%, {v['avg']:+.2f}R, "
                  f"total {v['tot']:+.0f}R, PF {v['pf']}, DD {v['dd']:.0f}R, streak {v['ls']}, "
                  f"Y1 {v['yrs'][0][2]:+.0f} · Y2 {v['yrs'][1][2]:+.0f}, WF {v['wf']}")
    tg.append(f"Report only, nothing changed live. Not financial advice. {(time.time() - t0) / 60:.0f} min. "
              "Full table: Actions → run summary.")
    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as fh:
            fh.write(report + "\n")
    msg = "\n\n".join(tg)
    print(msg)
    part = ""
    for block in tg:
        if part and len(part) + len(block) + 2 > 3900:
            bot.tg(part)
            part = ""
        part = f"{part}\n\n{block}" if part else block
    if part:
        bot.tg(part)


if __name__ == "__main__":
    main()
