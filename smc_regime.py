"""Regime attribution (report only, NO filter is changed or proposed): WHERE does the live +R come from?

The exact live trades (A+B+C rules, 50 futures coins, 1h, last 24 months, $1 risk -> R = $) are labelled with the
market regime known at the moment of entry, then grouped:
  BTC 4h trend (EMA50 + ADX) · BTC 30-day move · BTC volatility (1h ATR% vs its last 30 days)
  coin trend strength (1h ADX) · coin choppiness (1h CHOP) · coin volatility (ATR% percentile)
  killzone (London / NY) · LONG / SHORT · trade direction vs BTC trend · year / quarter
For every group: trades, win %, avg R, total R, and total R in year 1 and year 2 -- plus how the regime mix
differed between year 1 and year 2 (to explain why year 1 was flat).
"""
import bisect
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import smc_wf as wf  # noqa: E402


def btc_context(total):
    """ct -> regime labels for BTC, using only candles closed at that time."""
    C, _ = bot.fetch_history("BTCUSDT", "1h", total + 24 * 40)
    HB = bot._aggregate(C, 4)
    trend_fn = bot.btc_trend_fn(HB)
    h, l, c = [x["h"] for x in C], [x["l"] for x in C], [x["c"] for x in C]
    A = bot.atr(h, l, c, bot.ATR_LEN)
    ap = [(A[i] / c[i] * 100) if A[i] else None for i in range(len(C))]
    cts = [x["ct"] for x in C]

    def ctx(ct):
        k = bisect.bisect_right(cts, ct) - 1
        if k < 720:
            return None
        tr, badx, _ = trend_fn(ct)
        trend = "sideways (ADX<20)" if badx < 20 or tr == 0 else ("bull" if tr > 0 else "bear")
        mv = (c[k] / c[k - 720] - 1) * 100
        move = "up >+10%" if mv > 10 else "down <−10%" if mv < -10 else "flat ±10%"
        win = [v for v in ap[k - 720:k + 1] if v is not None]
        pct = sum(1 for v in win if v <= ap[k]) / len(win) * 100 if win and ap[k] else 50
        vol = "high (top third)" if pct > 66.7 else "low (bottom third)" if pct < 33.3 else "middle"
        return {"btc_trend": trend, "btc_move": move, "btc_vol": vol, "btc_tr": tr}
    return ctx


def label(t, b):
    side = t["side"]
    with_btc = "BTC sideways" if b["btc_trend"].startswith("sideways") else ("with BTC trend" if (b["btc_tr"] > 0) == (side == "LONG")
                                                    else "against BTC trend")
    adx = t.get("adx_sig", 0)                         # values at the signal candle (known before entry)
    ch = t.get("chop_sig", 100)
    ap = t.get("atr_pct", 50)
    sh = t.get("sweep_hour", -1)
    return {
        "BTC 4h trend": b["btc_trend"],
        "BTC 30-day move": b["btc_move"],
        "BTC volatility": b["btc_vol"],
        "Coin trend strength (1h ADX)": "trending ≥25" if adx >= 25 else "weak 20–25" if adx >= 20 else "ranging <20",
        "Coin choppiness (1h CHOP)": "trending ≤38.2" if ch <= 38.2 else "choppy ≥61.8" if ch >= 61.8 else "normal",
        "Coin volatility (ATR% vs 30d)": "high (top third)" if ap > 66.7 else "low (bottom third)" if ap < 33.3
        else "middle",
        "Killzone": "London (06–09 UTC)" if 6 <= sh <= 8 else "New York (12–15 UTC)" if 12 <= sh <= 14 else "other",
        "Side": side,
        "Direction vs BTC trend": with_btc,
    }


def main():
    t0 = time.time()
    bot.TOP_N = int(os.getenv("WF_COINS", "50"))
    bot.resolve_symbols()
    total = int(wf.MONTHS * 730.5) + 300
    end = time.time() * 1000
    start = end - wf.MONTHS * wf.MONTH_MS
    mid = start + (end - start) / 2
    trades = wf.live_filter([t for t in wf.build_trades(total) if t["time"] >= start])
    ctx = btc_context(total)
    rows = []
    for t in trades:
        b = ctx(t["time"] - 3600000)                  # last BTC candle closed BEFORE the fill candle
        if b is None:
            continue
        lab = label(t, b)
        lab["Year"] = "Year 1" if t["time"] < mid else "Year 2"
        lab["Quarter"] = f"Q{int((t['time'] - start) // (wf.MONTH_MS * 3)) + 1} ({time.strftime('%b %Y', time.gmtime((start + int((t['time'] - start) // (wf.MONTH_MS * 3)) * wf.MONTH_MS * 3) / 1000))})"
        rows.append((t, lab))

    def agg(ts):
        rs = [wf.net(t) for t in ts]
        n = len(rs)
        return n, (sum(1 for r in rs if r > 0) / n * 100 if n else 0), (sum(rs) / n if n else 0), sum(rs)

    n, w, a, tot = agg([t for t, _ in rows])
    out = [f"## Regime attribution — live A+B+C trades ({len(bot.SYMBOLS)} futures coins, {wf.MONTHS} months, "
           f"$1 risk → R = $). Report only, no filter changed.\n",
           f"All trades: {n} · win {w:.0f}% · {a:+.2f}R · total {tot:+.1f}R\n"]
    tg = [f"🧭 <b>Regime attribution</b> — where the live +R comes from ({n} trades, {wf.MONTHS} months, "
          f"total {tot:+.0f}R). Report only."]
    dims = list(rows[0][1].keys()) if rows else []
    for d in dims:
        groups = defaultdict(list)
        for t, lab in rows:
            groups[lab[d]].append(t)
        out += [f"\n### {d}\n", "| Regime | Trades | Win | Avg R | Total R | Year 1 R | Year 2 R | Share Y1 → Y2 |",
                "|---|---|---|---|---|---|---|---|"]
        n1 = sum(1 for t, _ in rows if t["time"] < mid) or 1
        n2 = sum(1 for t, _ in rows if t["time"] >= mid) or 1
        lines = []
        order = sorted(groups) if d in ("Year", "Quarter") else sorted(groups, key=lambda g: -agg(groups[g])[3])
        for g in order:
            ts = groups[g]
            gn, gw, ga, gt = agg(ts)
            y1 = [t for t in ts if t["time"] < mid]
            y2 = [t for t in ts if t["time"] >= mid]
            out.append(f"| {g} | {gn} | {gw:.0f}% | {ga:+.2f} | {gt:+.1f} | {agg(y1)[3]:+.1f} | {agg(y2)[3]:+.1f} | "
                       f"{len(y1) / n1 * 100:.0f}% → {len(y2) / n2 * 100:.0f}% |")
            lines.append(f"{bot.esc(g)}: {gn} tr, {ga:+.2f}R, {gt:+.0f}R (Y1 {agg(y1)[3]:+.0f} / Y2 {agg(y2)[3]:+.0f})")
        if d not in ("Year",):
            tg.append(f"<b>{bot.esc(d)}</b>\n" + "\n".join("• " + x for x in lines))
    tg.append(f"Report only, nothing changed live. Not financial advice. {(time.time() - t0) / 60:.0f} min. "
              "Full tables: Actions → run summary.")
    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as fh:
            fh.write(report + "\n")
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
