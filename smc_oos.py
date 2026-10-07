"""Honest out-of-sample research (report only - changes nothing live).

The problem: today's live filters were chosen on the last ~12 months and lost money in the 3 years before.
This study builds a rule ONLY from old data and then tests it ONCE on newer data it never saw.

Protocol (fixed before running, do not change after seeing results):
  Data     4 years of 1h SPOT candles (longest history), SMC sweep -> MSS -> FVG 50% limit entry, liquidity TP,
           SL >= 1%, discount/premium rule (the core setup the bot always uses). Fees 0.10% round trip.
  Coins    point-in-time: every day the top 50 by the PREVIOUS day's $ volume, out of a pool of today's top 150.
  TRAIN    the first 2 years (two train years: T1, T2).
  TEST     the last 2 years (two test years: S1, S2) - used exactly once, at the end.
  Selection (TRAIN only): greedy, start with no quality filter; at each step add the candidate filter that raises
           the TRAIN total R the most, but only if it raises the total in BOTH train years and keeps >= 60 train
           trades. Stop when nothing qualifies (max 8 filters). Candidates = every filter in bot.SMC_QUALITY
           (except sequence/diagnostic ones) plus a few extra values of the live parameters.
  PASS     the selected rule must be profitable in BOTH test years and keep >= 40 test trades.
  Also shown (for reference only): today's live filters on the same data (TRAIN years are out-of-sample for them,
           the TEST years overlap the period they were tuned on).
"""
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

YEARS = 4
POOL = int(os.getenv("OOS_POOL", "150"))
TOP = 50
DAY = 86400000
YEAR_MS = 365.25 * DAY
SKIP = {"SMC_COOLDOWN", "SMC_BREAKER", "SMC_LIQ_TP"}          # need trade sequences / diagnostic only
EXTRA = [("SMC_MAX_FILL", 3), ("SMC_MAX_FILL", 6), ("SMC_MAX_SL", 3), ("SMC_MAX_DEPTH", 0.5), ("SMC_MIN_RR", 2.0)]


def candidates():
    fn = {var: f for _, var, _, f in bot.SMC_QUALITY}
    lbl = {var: l for l, var, _, _ in bot.SMC_QUALITY}
    out = [(var, val) for _, var, val, _ in bot.SMC_QUALITY if var not in SKIP]
    out += [x for x in EXTRA if x not in out]
    return [(f"{bot._quality_label(lbl[v], d, val)}", v, val, fn[v])
            for v, val in out for d in [next(dd for _, vv, dd, _ in bot.SMC_QUALITY if vv == v)]]


def build(total, start_ms):
    bot.DATA_SOURCE = "spot"
    pool, src = bot.volume_top_symbols(POOL)
    try:
        HB, _ = bot.fetch_history("BTCUSDT", bot.HTF, total // 4 + bot.EMA_LEN + 50)
        btc_fn = bot.btc_trend_fn(HB)
    except Exception as e:
        print("BTC data error", e)
        btc_fn = None
    trades, dvol, coins = [], {}, 0
    for sym in pool:
        try:
            C, _ = bot.fetch_history(sym, "1h", total)
            H, _ = bot.fetch_history(sym, bot.HTF, total // 4 + bot.EMA_LEN + 50)
        except Exception as e:
            print(sym, "data error", e, flush=True)
            continue
        coins += 1
        vol, cnt = {}, {}
        for x in C:
            d = x["t"] // DAY
            vol[d] = vol.get(d, 0.0) + x["v"] * x["c"]
            cnt[d] = cnt.get(d, 0) + 1
        dvol[sym] = {d: v for d, v in vol.items() if cnt[d] >= 24}
        n0 = len(trades)
        for st in bot.tag_btc(bot.find_smc_setups(C, H, bot.SMC_ENTRY), sym, btc_fn):
            if st["time"] < start_ms or bot.sl_pct(st) < bot.MIN_SL_PCT:
                continue
            if bot.SMC_REQUIRE_DISCOUNT and not st["discount"]:
                continue
            if not bot.passes_filters(st):
                continue
            r = bot._smc(bot._smc_liq)(C, st)
            if r is None:
                continue
            st["sym"], st["slp"] = sym, bot.sl_pct(st)
            st["r"] = r - bot.FEE_PCT / st["slp"]
            trades.append(st)
        print(f"{sym}: {len(C)} candles, {len(trades) - n0} setups", flush=True)
        time.sleep(0.05)
    # point-in-time coin list: top TOP by the previous complete day's volume (falls back up to 3 days for gaps)
    lists = {}

    def day_list(day):
        if day not in lists:
            rows = []
            for s, v in dvol.items():
                val = next((v[d] for d in range(day - 1, day - 5, -1) if d in v), None)
                if val is not None:
                    rows.append((val, s))
            lists[day] = {s for _, s in sorted(rows, reverse=True)[:TOP]}
        return lists[day]
    kept = [t for t in trades if t["sym"] in day_list(int(t["time"] // DAY))]
    return kept, coins, len(trades), src


def stats(ts):
    ts = sorted(ts, key=lambda t: t["time"])
    n = len(ts)
    tot = sum(t["r"] for t in ts)
    win = sum(1 for t in ts if t["r"] > 0)
    eq = peak = dd = 0.0
    streak = worst = 0
    for t in ts:
        eq += t["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if t["r"] < 0 else 0
        worst = max(worst, streak)
    return {"n": n, "tot": tot, "avg": tot / n if n else 0.0, "win": win / n * 100 if n else 0.0, "dd": dd, "streak": worst}


def main():
    t0 = time.time()
    now = time.time() * 1000
    start = now - YEARS * YEAR_MS
    split = start + 2 * YEAR_MS
    total = int(YEARS * 365.25 * 24) + 400
    bot.TOP_N = TOP
    trades, coins, raw, src = build(total, start)
    yrs = [(start + i * YEAR_MS, start + (i + 1) * YEAR_MS) for i in range(4)]
    yname = [f"{datetime.fromtimestamp(a / 1000, timezone.utc):%b %y}–{datetime.fromtimestamp(min(b, now) / 1000, timezone.utc):%b %y}"
             for a, b in yrs]
    by_year = lambda ts: [[t for t in ts if a <= t["time"] < b] for a, b in yrs]
    train = [t for t in trades if t["time"] < split]
    test = [t for t in trades if t["time"] >= split]

    # ---------- greedy selection on TRAIN only
    cands = candidates()
    chosen, cur = [], train
    log = []
    while len(chosen) < 8:
        cy = by_year(cur)
        best = None
        for lbl, var, val, fn in cands:
            if any(var == c[1] for c in chosen):
                continue
            sel = [t for t in cur if fn(t, val)]
            if len(sel) < 60:
                continue
            sy = by_year(sel)
            r1, r2 = sum(t["r"] for t in sy[0]), sum(t["r"] for t in sy[1])
            c1, c2 = sum(t["r"] for t in cy[0]), sum(t["r"] for t in cy[1])
            if r1 > c1 and r2 > c2:
                gain = (r1 + r2) - (c1 + c2)
                if best is None or gain > best[0]:
                    best = (gain, (lbl, var, val, fn), sel, r1, r2)
        if best is None or best[0] < 1.0:
            break
        chosen.append(best[1])
        cur = best[2]
        log.append(f"+ {best[1][0]}: train {len(cur)} trades, T1 {best[3]:+.1f}R, T2 {best[4]:+.1f}R")

    rule = lambda t: all(fn(t, val) for _, _, val, fn in chosen)
    live = lambda t: bot.smc_quality_ok(t)

    def block(name, fn):
        sel = [t for t in trades if fn(t)]
        ys = by_year(sel)
        st = [stats(y) for y in ys]
        tr, te = stats(ys[0] + ys[1]), stats(ys[2] + ys[3])
        return sel, st, tr, te

    rows, tg_rows = [], []
    results = {}
    for name, fn in [("No quality filter (core setup only)", lambda t: True),
                     ("NEW rule (picked on train only)", rule),
                     ("Today's live filters (reference)", live)]:
        sel, st, tr, te = block(name, fn)
        results[name] = (st, tr, te)
        rows.append(f"| {name} | " + " | ".join(f"{s['n']} / {s['tot']:+.1f}R" for s in st)
                    + f" | {tr['n']} / {tr['tot']:+.1f}R | {te['n']} / {te['tot']:+.1f}R | {te['win']:.0f}% | −{te['dd']:.1f}R |")
        tg_rows.append(f"<b>{name}</b>\n" + " · ".join(f"{yname[i]}: {s['tot']:+.1f}R ({s['n']})" for i, s in enumerate(st))
                       + f"\nTRAIN {tr['tot']:+.1f}R ({tr['n']}) · TEST {te['tot']:+.1f}R ({te['n']}), win {te['win']:.0f}%, "
                         f"DD −{te['dd']:.1f}R, streak {te['streak']}")
    st_new = results["NEW rule (picked on train only)"][0]
    te_new = results["NEW rule (picked on train only)"][2]
    passed = st_new[2]["tot"] > 0 and st_new[3]["tot"] > 0 and te_new["n"] >= 40

    out = [f"## Out-of-sample research ({coins} coins pool, point-in-time top {TOP}, 1h spot, {YEARS} years)\n",
           f"Setups: {raw} core setups, {len(trades)} inside the daily top {TOP}. TRAIN = {yname[0]} + {yname[1]}, "
           f"TEST = {yname[2]} + {yname[3]}. R = net of 0.10% fees, 1R = $1 at your sizing.\n",
           "### Filters picked on TRAIN only (each had to help BOTH train years)\n",
           ("\n".join(f"- {x}" for x in log) if log else "- none: no filter helped both train years") + "\n",
           "### Results (trades / net R)\n",
           f"| Rule | {' | '.join(yname)} | TRAIN | TEST | Test win | Test DD |\n|---|---|---|---|---|---|---|---|---|",
           *rows,
           f"\n**Verdict: {'PASS ✅ - the rule built on old data also made money in both unseen years' if passed else 'FAIL ❌ - the rule built on old data did not hold up on unseen data'}** "
           "(pass = profitable in BOTH test years, ≥40 test trades).\n",
           "Caveats: pool = today's top 150 (coins that died are missing → old years look better than reality); "
           "spot prices; one position per coin and funding/slippage not modelled."]
    tg = [f"🔬 <b>Out-of-sample research</b> ({YEARS}y, PIT top {TOP} of {coins}, spot 1h)",
          f"TRAIN {yname[0]} + {yname[1]} → TEST {yname[2]} + {yname[3]} (test used once)",
          "<b>Picked on TRAIN</b> (must help both train years):\n" + ("\n".join(bot.esc(x) for x in log) if log else "none"),
          *[bot.esc(r).replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>") for r in tg_rows],
          f"<b>Verdict: {'PASS ✅' if passed else 'FAIL ❌'}</b> (pass = new rule profitable in BOTH test years, ≥40 trades)",
          f"Report only, nothing changed live. {(time.time() - t0) / 60:.0f} min. Full table: Actions → run summary."]
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
