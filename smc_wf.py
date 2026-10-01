"""Walk-forward entry-quality study of the live SMC bot (report only - changes nothing live).

1. Same setups as the live bot (bot.find_smc_setups, FVG entry from smc_filters.json, liquidity TP, discount rule,
   cooldown / breaker), futures 1h candles, top-N futures coins, last MONTHS months.
2. Entry-quality diagnostic: every feature split into buckets (sweep depth, reclaim, MSS speed / displacement /
   close / margin / volume, FVG size, FVG age, 4H trend, 4H POI, OB, FVG+OB overlap, OTE, previous-day liquidity,
   BTC trend / stability, ADX, CHOP, target R, SL %, side), shown for the first and second half of the period.
3. Walk-forward: train TRAIN_M months -> test the next TEST_M months, rolled forward. Each candidate filter
   (the 5 hypotheses + every candidate in bot.SMC_QUALITY) is judged ONLY on the test months it never saw.
   Also: "re-select filters every fold" (what the bot's own validation does) vs keeping today's filters.

Pass rule (fixed before running): a filter is real only if, on the out-of-sample test months, it improves the
average net R per trade in at least 3 of 4 folds AND improves the total out-of-sample result, AND keeps ≥ 20 test trades.
"""
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

MONTHS = int(os.getenv("WF_MONTHS", "24"))
TRAIN_M = int(os.getenv("WF_TRAIN_M", "12"))
TEST_M = int(os.getenv("WF_TEST_M", "3"))
EX = "Prev high/low (liquidity)"
MONTH_MS = 30.44 * 86400000

# ---------------------------------------------------------------- hypotheses (from the review) + candidates
HYP = [
    ("H1 MSS ≤4 candles", lambda t: t["mss_bars"] <= 4),
    ("H1 MSS close ≥70% of candle", lambda t: t["mss_close"] >= 0.7),
    ("H1 MSS close ≥0.2 ATR past level", lambda t: t["mss_margin"] >= 0.2),
    ("H1 MSS volume ≥1.5×", lambda t: t["mss_vol"] >= 1.5),
    ("H1 MSS all four together", lambda t: t["mss_bars"] <= 4 and t["mss_close"] >= 0.7
     and t["mss_margin"] >= 0.2 and t["mss_vol"] >= 1.5),
    ("H2 sweep ≤0.5 ATR", lambda t: t["sweep_depth"] <= 0.5),
    ("H2 sweep 0.5–1 ATR", lambda t: 0.5 < t["sweep_depth"] <= 1.0),
    ("H2 sweep candle reclaimed", lambda t: t["sweep_rej"]),
    ("H3 4H trend aligned", lambda t: t["trend_sig"]),
    ("H4 FVG + OB overlap", lambda t: t.get("ob_fvg", False)),
    ("H5 target ≥1.5R", lambda t: t["tgt_r"] >= 1.5),
    ("H5 target ≥2R", lambda t: t["tgt_r"] >= 2.0),
]
COMBOS = [
    ("C1 sweep ≤1 ATR + sweep candle reclaimed", lambda t: t["sweep_depth"] <= 1.0 and t["sweep_rej"]),
    ("C2 C1 + fresh FVG (fill ≤3 candles)", lambda t: t["sweep_depth"] <= 1.0 and t["sweep_rej"]
     and (t["fill_wait"] or 0) <= 3),
    ("C3 sweep ≤1 ATR only", lambda t: t["sweep_depth"] <= 1.0),
]
SAME_AS_HYP = {"SMC_MSS_FAST4", "SMC_MSS_CLOSE", "SMC_MSS_MARGIN", "SMC_MSS_VOL", "SMC_TREND", "SMC_MIN_RR", "SMC_SWEEP_REJECT"}
CANDS = HYP + COMBOS + [(lbl, (lambda f, v: (lambda t: f(t, v)))(fn, val))
               for lbl, var, val, fn in bot.SMC_QUALITY if var not in bot.SMC_ACTIVE and var not in SAME_AS_HYP]

BUCKETS = [
    ("Sweep depth", lambda t: "shallow ≤0.5 ATR" if t["sweep_depth"] <= 0.5 else "normal 0.5–1" if t["sweep_depth"] <= 1 else "deep >1"),
    ("Sweep reclaim", lambda t: "same candle" if t["reclaim_bars"] == 0 else "delayed"),
    ("MSS speed", lambda t: "≤4 candles" if t["mss_bars"] <= 4 else "5–6" if t["mss_bars"] <= 6 else ">6"),
    ("MSS displacement", lambda t: "weak <0.7 ATR" if t["disp"] < 0.7 else "medium" if t["disp"] < 1.2 else "strong ≥1.2"),
    ("MSS close", lambda t: "strong ≥70%" if t["mss_close"] >= 0.7 else "weak"),
    ("MSS margin", lambda t: "≥0.2 ATR" if t["mss_margin"] >= 0.2 else "<0.2 ATR"),
    ("MSS volume", lambda t: "≥1.5×" if t["mss_vol"] >= 1.5 else "<1.5×"),
    ("FVG size", lambda t: "small <0.2 ATR" if t["fvg_atr"] < 0.2 else "medium" if t["fvg_atr"] < 0.5 else "large ≥0.5"),
    ("FVG age (candles to fill)", lambda t: "fresh ≤3" if (t["fill_wait"] or 0) <= 3 else "old >3"),
    ("4H trend", lambda t: "aligned" if t["trend_sig"] else "against"),
    ("4H POI", lambda t: "yes" if t["htf_poi"] else "no"),
    ("Order block", lambda t: "yes" if t["has_ob"] else "no"),
    ("FVG+OB overlap", lambda t: "yes" if t.get("ob_fvg") else "no"),
    ("OTE overlap", lambda t: "yes" if t["ote_fvg"] else "no"),
    ("Prev-day liquidity", lambda t: "yes" if t["pdl"] else "no"),
    ("BTC trend", lambda t: "aligned" if t["btc_ok"] else "against"),
    ("BTC stability", lambda t: "stable" if t["btc_stable"] else "flip"),
    ("1H ADX", lambda t: "≥20" if t["adx_sig"] >= 20 else "<20"),
    ("CHOP", lambda t: "trending ≤61.8" if t["chop_sig"] <= 61.8 else "ranging"),
    ("Target distance", lambda t: "<1.5R" if t["tgt_r"] < 1.5 else "1.5–2R" if t["tgt_r"] < 2 else "2R+"),
    ("SL %", lambda t: "tight <1.5%" if t["slp"] < 1.5 else "normal 1.5–2.5%" if t["slp"] <= 2.5 else "wide >2.5%"),
    ("Side", lambda t: t["side"]),
]


def net(t):
    return t["res"][EX] - bot.FEE_PCT / t["slp"]


def agg(ts):
    if not ts:
        return 0, 0.0, 0.0, 0.0
    rs = [net(t) for t in ts]
    return len(rs), sum(rs) / len(rs), sum(rs), sum(1 for r in rs if r > 0) / len(rs) * 100


def build_trades(total):
    bot.DATA_SOURCE = "futures"
    try:
        HB, _ = bot.fetch_history("BTCUSDT", bot.HTF, total // 4 + bot.EMA_LEN + 50)
        btc_fn = bot.btc_trend_fn(HB)
    except Exception as e:
        print("BTC data error", e)
        btc_fn = None
    trades = []
    for sym in bot.SYMBOLS:
        try:
            C, _ = bot.fetch_history(sym, "1h", total)
            H, _ = bot.fetch_history(sym, bot.HTF, total // 4 + bot.EMA_LEN + 50)
        except Exception as e:
            print(sym, "data error", e, flush=True)
            continue
        n0 = len(trades)
        for st in bot.tag_btc(bot.find_smc_setups(C, H, bot.SMC_ENTRY), sym, btc_fn):
            if bot.sl_pct(st) < bot.MIN_SL_PCT:
                continue
            if bot.SMC_REQUIRE_DISCOUNT and not st["discount"]:
                continue
            if bot.SMC_REQUIRE_TREND and not st["trend"]:
                continue
            if not bot.passes_filters(st):
                continue
            st["sym"], st["slp"] = sym, bot.sl_pct(st)
            st["res"] = {EX: bot._smc(bot._smc_liq)(C, st)}
            if st["res"][EX] is None:
                continue
            _, ci = bot._exit_detail(C, st, st["side"], "price", None, st["sl"])
            st["close_ct"] = C[ci]["ct"] if ci is not None else None
            trades.append(st)
        bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)
        print(f"{sym}: {len(C)} candles, {len(trades) - n0} setups", flush=True)
    return trades


SEQ_RULES = (("SMC_COOLDOWN", "cool_ok"), ("SMC_BREAKER", "breaker_ok"),
             ("SMC_MAX_PER_BAR", "bar_ok"), ("SMC_MAX_SAME_DIR", "dir_ok"))


def live_filter(ts, extra=None):
    """Exactly the live rules: today's quality filters, and cooldown / breaker / per-candle / same-direction
    limits ONLY if they are active in smc_filters.json (like live_sequence_ok / live_bar_ok).
    The sequence flags are always computed, so cooldown / breaker can still be tested as extra filters."""
    sel = [dict(t) for t in ts if bot.smc_quality_ok(t)]
    bot.tag_sequence(sel, EX)
    on = [flag for var, flag in SEQ_RULES if var in bot.SMC_ACTIVE]
    return [t for t in sel if all(t.get(f, True) for f in on) and (extra is None or extra(t))]


def main():
    t0 = time.time()
    bot.TOP_N = int(os.getenv("WF_COINS", "50"))
    bot.resolve_symbols()
    total = int(MONTHS * 730.5) + 300
    trades = build_trades(total)
    end = time.time() * 1000
    start = end - MONTHS * MONTH_MS
    trades = [t for t in trades if t["time"] >= start]
    live = live_filter(trades)
    mid = start + (end - start) / 2
    out, tg = [], []

    # ---------------- A. baseline
    n, avg, tot, win = agg(live)
    na, avga, tota, wina = agg([dict(t) for t in trades])
    out.append(f"## Walk-forward SMC study ({len(bot.SYMBOLS)} futures coins, 1h, last {MONTHS} months)\n")
    out.append(f"Live rules today ({bot.smc_quality_txt()}): **{n} trades, win {win:.0f}%, {avg:+.3f}R/trade, total {tot:+.1f}R** "
               f"(1R = $1 at your sizing). Without quality filters: {na} trades, {avga:+.3f}R/trade.\n")
    out.append("Note: today's live filters were chosen on part of this period, so the live line is partly in-sample.\n")
    tg.append(f"🔬 <b>Walk-forward SMC study</b> ({len(bot.SYMBOLS)} coins, 1h futures, {MONTHS} months)")
    tg.append(f"Live rules: {n} trades, win {win:.0f}%, {avg:+.2f}R/trade, total {tot:+.0f}R (= ${tot:+.0f} at $1 risk)")

    # ---------------- B. diagnostic buckets
    out.append("\n### Entry-quality diagnostic (live trades)\n")
    out.append("| Feature | Bucket | 1st half n / net R | 2nd half n / net R | Both halves |\n|---|---|---|---|---|")
    flags = []
    for name, fn in BUCKETS:
        groups = defaultdict(lambda: ([], []))
        for t in live:
            groups[fn(t)][0 if t["time"] < mid else 1].append(t)
        for b, (h1, h2) in sorted(groups.items()):
            n1, a1, _, _ = agg(h1)
            n2, a2, _, _ = agg(h2)
            same = "worse" if (a1 < avg - 0.15 and a2 < avg - 0.15) else "better" if (a1 > avg + 0.15 and a2 > avg + 0.15) else ""
            if same and n1 >= 15 and n2 >= 15:
                flags.append(f"{name}: {b} → {same} in both halves ({n1}: {a1:+.2f}R, {n2}: {a2:+.2f}R)")
            out.append(f"| {name} | {b} | {n1} / {a1:+.2f}R | {n2} / {a2:+.2f}R | {same or '–'} |")

    # ---------------- C. walk-forward folds
    folds = []
    f_start = start + TRAIN_M * MONTH_MS
    while f_start + 0.5 * TEST_M * MONTH_MS <= end:
        folds.append((f_start - TRAIN_M * MONTH_MS, f_start, min(end, f_start + TEST_M * MONTH_MS)))
        f_start += TEST_M * MONTH_MS
    span = lambda a, b: [t for t in trades if a <= t["time"] < b]
    base_oos = [live_filter(span(te0, te1)) for _, te0, te1 in folds]
    rows, passed = [], []
    for lbl, fn in CANDS:
        better, oos_tot, oos_n, cells = 0, 0.0, 0, []
        for (tr0, te0, te1), b in zip(folds, base_oos):
            sel = live_filter(span(te0, te1), fn)
            n_c, a_c, s_c, _ = agg(sel)
            n_b, a_b, _, _ = agg(b)
            better += n_c > 0 and a_c > a_b
            oos_tot += s_c
            oos_n += n_c
            cells.append(f"{n_c}: {a_c - a_b:+.2f}")
        base_tot = sum(agg(b)[2] for b in base_oos)
        ok = better >= max(1, len(folds) - 1) and oos_tot > base_tot and oos_n >= 20
        rows.append(f"| {lbl} | {' · '.join(cells)} | {better}/{len(folds)} | {oos_n} / {oos_tot:+.1f}R | {'✅' if ok else '❌'} |")
        if ok:
            passed.append((oos_tot - base_tot, lbl, oos_n, oos_tot))
    base_n = sum(agg(b)[0] for b in base_oos)
    base_tot = sum(agg(b)[2] for b in base_oos)

    # re-select filters every fold on the train months only (what an optimiser would do), judge on test
    resel_tot, resel_n, resel_cells = 0.0, 0, []
    for (tr0, te0, te1), b in zip(folds, base_oos):
        tr_base = live_filter(span(tr0, te0))
        _, tb_avg, _, _ = agg(tr_base)
        chosen = [(lbl, fn) for lbl, fn in CANDS
                  if agg(live_filter(span(tr0, te0), fn))[0] >= 40 and agg(live_filter(span(tr0, te0), fn))[1] >= tb_avg + 0.05]
        rule = (lambda t, ch=chosen: all(f(t) for _, f in ch)) if chosen else None
        n_c, a_c, s_c, _ = agg(live_filter(span(te0, te1), rule))
        resel_tot += s_c
        resel_n += n_c
        resel_cells.append(f"{len(chosen)} filters → {n_c}: {s_c:+.1f}R (live {agg(b)[2]:+.1f}R)")

    out.append(f"\n### Walk-forward ({TRAIN_M} months train → {TEST_M} months test, {len(folds)} folds)\n")
    out.append(f"Baseline (today's live rules) on the test months: **{base_n} trades, total {base_tot:+.1f}R**.\n")
    out.append("Cells = test trades: change in avg net R vs live baseline in that fold.\n")
    out.append("| Extra filter | Per fold | Folds better | Out-of-sample n / total | Real? |\n|---|---|---|---|---|")
    out += rows
    out.append(f"\nRe-selecting filters every fold on train data only: {resel_n} test trades, total {resel_tot:+.1f}R "
               f"vs live {base_tot:+.1f}R.  " + " | ".join(resel_cells))

    # ---------------- D. combo check: each year and each quarter (pass rule fixed before the run)
    out.append("\n### Combo check: year by year and quarter by quarter\n")
    out.append("Pass = better avg net R than live in BOTH years, in ≥6 of 8 quarters, total R higher, ≥50% of trades kept.\n")
    out.append("| Rule | Year 1 n / total | Year 2 n / total | Quarters better | All n / total | Pass? |\n|---|---|---|---|---|---|")
    yr = [(start, mid), (mid, end)]
    qs = [(start + i * (end - start) / 8, start + (i + 1) * (end - start) / 8) for i in range(8)]
    b_y = [agg(live_filter(span(a, b))) for a, b in yr]
    b_q = [agg(live_filter(span(a, b))) for a, b in qs]
    out.append(f"| Live rules today | {b_y[0][0]} / {b_y[0][2]:+.1f}R | {b_y[1][0]} / {b_y[1][2]:+.1f}R | – | "
               f"{b_y[0][0] + b_y[1][0]} / {b_y[0][2] + b_y[1][2]:+.1f}R | – |")
    tg_combo = [f"Live rules: year 1 {b_y[0][2]:+.0f}R ({b_y[0][0]}) · year 2 {b_y[1][2]:+.0f}R ({b_y[1][0]})"]
    for lbl, fn in COMBOS:
        c_y = [agg(live_filter(span(a, b), fn)) for a, b in yr]
        c_q = [agg(live_filter(span(a, b), fn)) for a, b in qs]
        qb = sum(1 for c, b in zip(c_q, b_q) if c[0] > 0 and c[1] > b[1])
        both = all(c[0] > 0 and c[1] > b[1] for c, b in zip(c_y, b_y))
        n_all, t_all = c_y[0][0] + c_y[1][0], c_y[0][2] + c_y[1][2]
        ok = both and qb >= 6 and t_all > b_y[0][2] + b_y[1][2] and n_all >= 0.5 * (b_y[0][0] + b_y[1][0])
        out.append(f"| {lbl} | {c_y[0][0]} / {c_y[0][2]:+.1f}R | {c_y[1][0]} / {c_y[1][2]:+.1f}R | {qb}/8 | "
                   f"{n_all} / {t_all:+.1f}R | {'✅' if ok else '❌'} |")
        tg_combo.append(f"{'✅' if ok else '❌'} {lbl}: year 1 {c_y[0][2]:+.0f}R ({c_y[0][0]}) · year 2 {c_y[1][2]:+.0f}R "
                        f"({c_y[1][0]}) · quarters better {qb}/8")

    # ---------------- Telegram summary
    tg.append(f"Out-of-sample test months ({len(folds)} folds × {TEST_M}m): live rules {base_n} trades, {base_tot:+.0f}R")
    if passed:
        passed.sort(reverse=True)
        tg.append("✅ Extra filters that held up out-of-sample:\n" + "\n".join(
            f"• {lbl}: {n_} trades, {tot_:+.0f}R ({d:+.0f}R vs live)" for d, lbl, n_, tot_ in passed[:6]))
    else:
        tg.append("❌ No extra filter improved the out-of-sample result in ≥3 of 4 folds.")
    tg.append(f"Re-tuning filters each fold: {resel_tot:+.0f}R vs keeping today's {base_tot:+.0f}R → "
              + ("tuning helped" if resel_tot > base_tot else "tuning did NOT help"))
    tg.append("<b>Combo check</b> (pass = better in both years + ≥6/8 quarters)\n" + "\n".join(tg_combo))
    if flags:
        tg.append("Diagnostic (same in both halves, ≥15 trades each):\n" + "\n".join("• " + f for f in flags[:8]))
    else:
        tg.append("Diagnostic: no bucket was clearly better/worse in both halves.")
    tg.append(f"Report only, nothing changed live. Funding/slippage not included. Not financial advice. "
              f"{(time.time() - t0) / 60:.0f} min. Full tables: Actions → run summary.")

    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report + "\n")
    msg = "\n\n".join(tg)
    print(msg)
    for i in range(0, len(msg), 3900):
        bot.tg(msg[i:i + 3900])


if __name__ == "__main__":
    main()
