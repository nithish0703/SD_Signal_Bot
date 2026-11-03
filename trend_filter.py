"""T2 trend WHIPSAW FILTERS on the Top 30 setup - report only, changes nothing live (paper trader untouched).

Why: the paper trader lost small amounts on EMA crosses that flipped back within days (SUI, PEPE, ENA, ADA, TAO).
Question: does a simple, textbook filter avoid those whipsaws WITHOUT costing more in late / missed big trends?

Everything else exactly as trend_diag.py "Realistic" (the reference): Top 30 each month, no BTC/ETH, $300 start,
$1 fixed risk, max 12 open, size <= 3x equity, real Binance minimum sizes, 0.10% fees, real funding,
0.10% slippage per side, emergency exchange stop at trail -/+ 1.5 ATR.

How a filter works (the same for all of them): an EMA50/200 cross "arms" a trade in its direction. Without a filter
the trade opens at the next open (= the reference, checked at run time). With a filter the armed trade opens at the
next open after the FIRST 4h close where the filter passes, as long as EMA50 is still on the same side of EMA200
(if it crosses back first, the armed trade is dropped). Exits are never changed.
  1. ADX >= 20           4h ADX(14) of the coin at least 20 (a trend is present)
  2. EMA gap >= 0.5 ATR  |EMA50 - EMA200| at least 0.5 x ATR14 (the cross is not just a touch)
  3. 2-close confirm     EMA50 still on the new side one more 4h close after the cross
  4. Cooldown 3 days     after a LOSING trade on a coin, no new entry on that coin for 18 x 4h candles
  5. BTC trend agrees    long only if BTC 4h EMA50 > EMA200, short only if below

Pre-registered rule (fixed before any result was seen): a filter counts as better than the reference only if its
P&L is at least $5 higher in BOTH halves (years 1-2 AND years 3-4) and its max drawdown is at most 1 point worse.
No combinations are tested (picking a combo after seeing single results would be fitting the data).
Trade lists -> trend_filter_trades.csv (workflow artifact).
"""
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import trend_account as TA  # noqa: E402
import trend_diag as D  # noqa: E402
import trend_robust as R  # noqa: E402
import trend_small as S  # noqa: E402
import trend_top50 as T  # noqa: E402

YEARS, DAY, H4 = TA.YEARS, TA.DAY, TA.H4
RISK = float(os.getenv("TF_RISK", "") or 1.0)          # $ risk per trade
DAYS = int(float(os.getenv("TF_DAYS", "") or 0))       # 0 = full 4 years; N = fresh $300 start N days ago
MIN_GAIN = 5.0 * RISK                                   # 5R per half
SLIP = D.SLIP
ADX_MIN, GAP_ATR, COOL_BARS = 20.0, 0.5, 18
CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trend_filter_trades.csv")


def t2f(H, emergency=None, filt=None, btc=None):
    """trend_robust.t2 (stop-and-reverse, 3 ATR chandelier, emergency stop) with an entry filter.
    filt None = reference (entry only at the open after the cross candle)."""
    cl = [x["c"] for x in H]
    e50, e200, A = TA.ema(cl, 50), TA.ema(cl, 200), TA.atr(H, 14)
    ADX = bot.adx([x["h"] for x in H], [x["l"] for x in H], cl) if filt == "adx" else None
    out, pos, armed, cool = [], None, None, -1

    def closed(tr, t):
        nonlocal cool
        out.append(tr)
        if filt == "cool" and tr["side"] * (tr["exit"] - tr["entry"]) < 0:
            cool = t + COOL_BARS

    for t in range(201, len(H) - 1):
        x, nxt = H[t], H[t + 1]
        if pos and emergency is not None:
            s, lvl = pos["side"], pos["em"]
            if (s > 0 and x["l"] <= lvl) or (s < 0 and x["h"] >= lvl):
                px = min(x["o"], lvl) if s > 0 else max(x["o"], lvl)
                closed(dict(side=s, t_in=pos["t_in"], entry=pos["entry"], risk=pos["risk"], t_out=x["t"],
                            exit=px, why="emergency stop", intrabar=True, t_cross=pos["t_cross"]), t)
                pos = None
        up = e50[t] > e200[t] and e50[t - 1] <= e200[t - 1]
        dn = e50[t] < e200[t] and e50[t - 1] >= e200[t - 1]
        if up or dn:
            armed = (1 if up else -1, t)                 # a new cross replaces any older armed trade
        if pos:
            s = pos["side"]
            pos["best"] = max(pos["best"], x["c"]) if s > 0 else min(pos["best"], x["c"])
            trail = pos["best"] - s * 3 * A[t]
            why = ("trail" if (s > 0 and x["c"] < trail) or (s < 0 and x["c"] > trail) else
                   "opposite cross" if (s > 0 and dn) or (s < 0 and up) else None)
            if why:
                closed(dict(side=s, t_in=pos["t_in"], entry=pos["entry"], risk=pos["risk"], t_out=nxt["t"],
                            exit=nxt["o"], why=why, intrabar=False, t_cross=pos["t_cross"]), t)
                pos = None
            else:
                if (trail - pos["note"]) * s >= 0.5 * A[t]:
                    pos["note"] = trail
                    if emergency is not None:
                        pos["em"] = trail - s * emergency * A[t]
                continue
        if pos is not None or armed is None:
            continue
        s, tc = armed
        if (e50[t] - e200[t]) * s <= 0:                  # crossed back before the filter passed
            armed = None
            continue
        if not A[t]:
            continue
        if filt is None:
            ok = t == tc
        elif filt == "adx":
            ok = ADX[t] is not None and ADX[t] >= ADX_MIN
        elif filt == "gap":
            ok = (e50[t] - e200[t]) * s >= GAP_ATR * A[t]
        elif filt == "confirm":
            ok = t >= tc + 1
        elif filt == "cool":
            ok = t >= cool
        elif filt == "btc":
            ok = btc is not None and btc.get(x["t"], 0) == s
        else:
            raise ValueError(filt)
        if filt is None and t > tc:                      # reference: an un-taken cross is never taken later
            armed = None
            continue
        if not ok:
            continue
        armed = None
        entry, risk = nxt["o"], 3 * A[t]
        pos = dict(side=s, t_in=nxt["t"], entry=entry, risk=risk, best=entry, note=entry - s * risk, t_cross=H[tc]["t"])
        pos["em"] = pos["note"] - s * (emergency or 0) * A[t]
    if pos:
        out.append(dict(side=pos["side"], t_in=pos["t_in"], entry=pos["entry"], risk=pos["risk"], t_out=None,
                        exit=None, why="open", intrabar=False, t_cross=pos["t_cross"]))
    return out


def build(data, allowed, end_t, **kw):
    """trend_diag.build with t2f."""
    trades = []
    for sym, full in data.items():
        cuts = [0] + [i + 1 for i in range(len(full) - 1) if full[i + 1]["t"] - full[i]["t"] > 3 * DAY] + [len(full)]
        for a, b in zip(cuts, cuts[1:]):
            H = full[a:b]
            if len(H) < 260:
                continue
            for tr in t2f(H, **kw):
                if not allowed(sym, tr["t_in"]) or tr["t_in"] - H[0]["t"] < T.MIN_HIST:
                    continue
                if tr["t_out"] is None and H[-1]["t"] < end_t - 3 * DAY:
                    tr.update(t_out=H[-1]["t"] + H4, exit=H[-1]["c"], why="delisted / data stopped")
                tr.update(sym=sym, model="T2")
                trades.append(tr)
    return trades


def btc_regime(H):
    """4h open time -> +1 (EMA50 > EMA200) / -1 / 0 (warm-up), from that candle's close."""
    cl = [x["c"] for x in H]
    e50, e200 = TA.ema(cl, 50), TA.ema(cl, 200)
    return {x["t"]: (0 if i < 200 else (1 if e50[i] > e200[i] else -1)) for i, x in enumerate(H)}


def eq_at(curve, ms):
    v = S.START_BAL
    for t, e in curve:
        if t > ms:
            break
        v = e
    return v


def main():
    t0 = time.time()
    now = time.time() * 1000
    start = now - YEARS * 365.25 * DAY
    hours = int(YEARS * 365.25 * 24) + 140 * 24
    S.START_BAL = 300.0
    limits, lim_src = S.live_limits()
    top, n_syms, failed = T.universe(start)
    top30 = {k: set(v[:30]) for k, v in top.items()}
    need = sorted({s for v in top30.values() for s in v})
    data, fund, notes = {}, {}, []
    for i, sym in enumerate(need):
        x = T.load(sym, hours, start)
        if not x:
            notes.append(f"{sym[:-4]} no data")
            continue
        data[sym], fund[sym] = x[0], x[1]
        if x[3]:
            notes.append(f"{sym[:-4]}: {x[3]} funding month(s) missing")
        print(f"[{i + 1}/{len(need)}] {sym}: {len(x[0])} 4h candles", flush=True)
    xb = T.load("BTCUSDT", hours, start)                  # BTC only as a market filter (never traded)
    btc = btc_regime(xb[0]) if xb else None
    if btc is None:
        notes.append("BTC data missing: BTC filter not valid")
    closes = {s: {x["t"]: x["c"] for x in H} for s, H in data.items()}
    times = sorted({t for s in closes for t in closes[s] if t >= start})
    end_t = times[-1]

    def in_top30(sym, t):
        return sym in top30.get(datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m"), ())
    if DAYS:                                            # fresh start: $300, nothing open before, 4 equal parts
        ws = end_t - DAYS * DAY
        yb = [ws + i * DAYS * DAY / 4 for i in range(5)]
        sim_times = [t for t in times if t >= ws]
        span, plabel = f"last {DAYS} days (fresh $300 start, nothing open before)", "Quarter from"
    else:
        ws = None
        yb = [start + i * 365.25 * DAY for i in range(YEARS + 1)]
        sim_times = times
        span, plabel = f"{YEARS} years", "Year from"
    yname = [f"{datetime.fromtimestamp(yb[i] / 1000, timezone.utc):%d %b %y}" for i in range(4)]
    hname = "(parts 1-2 and 3-4)" if DAYS else "(yrs 1-2 and 3-4)"
    E = R.EMERGENCY
    variants = [  # name, filter
        ("Realistic (reference, no filter)", None),
        (f"1. ADX ≥{ADX_MIN:g}", "adx"),
        (f"2. EMA gap ≥{GAP_ATR:g} ATR", "gap"),
        ("3. 2-close confirm", "confirm"),
        (f"4. Cooldown {COOL_BARS * 4 // 24} days after a loss", "cool"),
        ("5. BTC trend agrees", "btc")]
    # self-check: the no-filter path must give exactly the trend_diag Realistic trades
    ref_trades = build(data, in_top30, end_t, emergency=E, btc=btc)
    diag_trades = D.build(data, in_top30, end_t, emergency=E)
    key = lambda tr: (tr["sym"], tr["side"], tr["t_in"], tr["t_out"], tr["entry"], tr["exit"], tr["why"])
    same = sorted(map(key, ref_trades), key=str) == sorted(map(key, diag_trades), key=str)
    res = {}
    for name, filt in variants:
        trades = ref_trades if filt is None else build(data, in_top30, end_t, emergency=E, filt=filt, btc=btc)
        if ws is not None:
            trades = [x for x in trades if x["t_in"] >= ws and x["t_cross"] >= ws - H4]   # signal after the start
        r = R.simulate(trades, closes, fund, sim_times, RISK, limits, SLIP)
        y = R.yearly(r["curve"], yb)
        w, up, nm = R.worst_month(r["curve"])
        lg = r["log"]
        whip = [x for x in lg if x["why"] == "opposite cross" and x["net"] < 0]
        last3 = r["final"] - eq_at(r["curve"], end_t - 91 * DAY)
        res[name] = dict(r=r, y=y, h1=y[0] + y[1], h2=y[2] + y[3], w=w, st=D.stats(lg), whip=whip, last3=last3,
                         em=sum(1 for x in lg if x["why"] == "emergency stop"))
        print(name, round(r["final"], 2), flush=True)
    ref = res[variants[0][0]]
    big_ref = [x for x in ref["r"]["log"] if x["R"] >= 5]

    def missed_big(log):
        """reference winners >= 5R with no same-coin, same-side trade opened during them (a later entry counts)."""
        by = {}
        for x in log:
            by.setdefault((x["sym"], x["side"]), []).append(x["t_in"])
        return sum(1 for b in big_ref if not any(b["t_in"] <= t < (b["t_out"] or float("inf"))
                                                  for t in by.get((b["sym"], b["side"]), ())))

    def verdict(name):
        v = res[name]
        if name == variants[0][0]:
            return "reference"
        ok = v["h1"] >= ref["h1"] + MIN_GAIN and v["h2"] >= ref["h2"] + MIN_GAIN and v["r"]["mdd"] <= ref["r"]["mdd"] + 0.01
        return "BETTER (both halves)" if ok else "not better"
    out = [f"## T2 trend WHIPSAW FILTERS - Top 30 (no BTC/ETH), $300, ${RISK:g} risk, {span}\n",
           f"Reference = Realistic (emergency stop {E:g} ATR + {SLIP * 100:.2f}% slippage, fees, funding). "
           f"Self-check (no-filter path = trend_diag Realistic trades): {'OK' if same else 'MISMATCH - results not valid'}. "
           f"Rule fixed in advance: better = at least ${MIN_GAIN:g} more P&L in BOTH halves and max DD at most 1 point "
           f"worse. Size limits: {lim_src}.\n",
           "| Variant | " + " | ".join(f"{plabel} {y}" for y in yname)
           + " | Half 1 | Half 2 | Final | Max DD | Worst month | Trades | Win % | PF | Whipsaw losses | Emergency stops"
             " | Big winners missed | Last 3 months | Verdict |",
           "|---|" + "---|" * 4 + "---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🧹 <b>T2 trend — whipsaw filters</b> (Top 30, $300, ${RISK:g} risk, {span}, realistic costs)\n"
          f"Self-check: {'OK ✅' if same else 'MISMATCH ❌ (results not valid)'}\n"
          f"Rule fixed before running: a filter is better only if it earns ≥${MIN_GAIN:g} more in BOTH halves "
          f"{hname} and DD is ≤1 pt worse."]
    lines = []
    for name, _ in variants:
        v = res[name]
        missed = missed_big(v["r"]["log"])
        st = v["st"]
        wl = f"{len(v['whip'])} (${sum(x['net'] for x in v['whip']):+,.1f})"
        out.append(f"| {name} | " + " | ".join(f"${x:+,.1f}" for x in v["y"])
                   + f" | ${v['h1']:+,.1f} | ${v['h2']:+,.1f} | ${v['r']['final']:,.1f} | −{v['r']['mdd'] * 100:.1f}% | "
                     f"${v['w']:+,.1f} | {st['n']} | {st['win']:.0f}% | {st['pf']:.2f} | {wl} | {v['em']} | "
                     f"{missed}/{len(big_ref)} | ${v['last3']:+,.1f} | {verdict(name)} |")
        lines.append(f"{'✅' if verdict(name).startswith('BETTER') else '▫️'} <b>{bot.esc(name)}</b>: "
                     f"<b>${v['r']['final']:,.0f}</b>, DD −{v['r']['mdd'] * 100:.1f}%, halves "
                     f"${v['h1']:+,.0f} / ${v['h2']:+,.0f}, {st['n']} trades, whipsaw losses {bot.esc(wl)}"
                     + (f", misses {missed}/{len(big_ref)} big winners" if name != variants[0][0] else "")
                     + f" → {verdict(name)}")
    tg.append("\n".join(lines))
    passed = [n for n, _ in variants if verdict(n).startswith("BETTER")]
    tg.append("Result: " + (f"<b>{bot.esc(', '.join(passed))}</b> passed the rule (worth a forward paper test before "
                            "any change)." if passed else "<b>no filter passed</b> - keep the current rules; whipsaw "
                                                          "losses are the normal cost of this strategy."))
    out.append("\nWhipsaw losses = trades closed by the opposite EMA cross at a loss. Big winners missed = reference trades of "
               ">= 5R where the filter opens no trade on that coin and side while the winner is running. "
               "Last 3 months = change of the account in the last 91 days. Past results do not guarantee "
               "future results.")
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "sym", "side", "entry_time", "exit_time", "entry_fill", "exit_fill", "qty", "risk_usd",
                    "gross", "fees", "funding", "net", "R", "why"])
        tf = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else ""
        for name, _ in variants:
            for x in res[name]["r"]["log"]:
                w.writerow([name, x["sym"], x["side"], tf(x["t_in"]), tf(x["t_out"]), f"{x['entry']:.8g}",
                            f"{x['exit']:.8g}", f"{x['qty']:.8g}", f"{x['risk_usd']:.4f}", f"{x['gross']:.4f}",
                            f"{x['fees']:.4f}", f"{x['funding']:.4f}", f"{x['net']:.4f}", f"{x['R']:.4f}", x["why"]])
    if failed:
        notes.append(f"{failed} volume file(s) failed to download")
    if notes:
        out.append("\nNotes: " + "; ".join(notes))
        tg.append("Notes: " + bot.esc("; ".join(notes[:15])))
    tg.append(f"Report only, nothing changed live. Trades: Actions → this run → Artifacts → trend-filter-trades. "
              f"{(time.time() - t0) / 60:.0f} min.")
    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report + "\n")
    print(re.sub(r"</?b>", "", "\n\n".join(tg)))
    chunks, cur = [], ""
    for part in tg:
        if cur and len(cur) + len(part) + 2 > 3800:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{part}" if cur else part
    if cur:
        chunks.append(cur)
    for c in chunks:
        bot.tg(c)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
