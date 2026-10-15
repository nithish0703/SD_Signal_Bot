"""T2 trend DIAGNOSTICS on the Top 30 setup - report only, changes nothing live, no baseline file is modified.

Universe, data, sizing, caps, fees and funding exactly as trend_robust.py / trend_top50.py (Top 30 each month,
no BTC/ETH, $300 start, $1 fixed risk, max 12 open, size <= 3x equity, real Binance minimum sizes).
Reference = "Realistic": emergency exchange stop (trail -/+ 1.5 ATR) + 0.10% slippage per side.

Variants (each changes ONE thing vs Realistic):
  Close-only cross    opposite EMA cross closes the trade but does NOT open the reverse trade
  Slope-confirmed     the reverse trade on an opposite cross opens only if EMA200 slopes that way
  reverse             (EMA200 now vs 6 candles ago); entries from flat are unchanged
  Long only / Short only
  Emergency 3 ATR     exchange stop at trail -/+ 3 ATR instead of 1.5 ATR (safety vs cost)
  No emergency stop   close-based exits only (NOT a recommendation: no crash protection)

Pre-registered rule (fixed before any result was seen): a variant counts as better than Realistic only if its
P&L is at least $5 (5R) higher in BOTH halves (years 1-2 AND years 3-4) and its max drawdown is at most 1 point worse.

Also: P&L attribution (price move / slippage / fees / funding), direction split, exit reasons, emergency-stop
diagnosis (each stopped trade vs the same trade without the stop), big winners a variant misses.
Trade lists -> trend_diag_trades.csv (workflow artifact).
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
import trend_robust as R  # noqa: E402
import trend_small as S  # noqa: E402
import trend_top50 as T  # noqa: E402

YEARS, DAY, H4 = TA.YEARS, TA.DAY, TA.H4
RISK = 1.0
MIN_GAIN = 5.0                  # $ per half needed to call a variant better (noise guard, fixed in advance)
SLIP = 0.001
SLOPE_N = 6
CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trend_diag_trades.csv")


def t2v(H, emergency=None, reverse="sr", sides="both"):
    """trend_robust.t2 with options. reverse: 'sr' stop-and-reverse (baseline), 'close' close only,
    'slope' reverse only if EMA200 slope agrees. sides: 'both', 'long', 'short' (which entries are allowed)."""
    cl = [x["c"] for x in H]
    e50, e200, A = TA.ema(cl, 50), TA.ema(cl, 200), TA.atr(H, 14)
    out, pos = [], None
    for t in range(201, len(H) - 1):
        x, nxt = H[t], H[t + 1]
        if pos and emergency is not None:
            s, lvl = pos["side"], pos["em"]
            if (s > 0 and x["l"] <= lvl) or (s < 0 and x["h"] >= lvl):
                px = min(x["o"], lvl) if s > 0 else max(x["o"], lvl)
                out.append(dict(side=s, t_in=pos["t_in"], entry=pos["entry"], risk=pos["risk"], t_out=x["t"],
                                exit=px, why="emergency stop", intrabar=True))
                pos = None
        up = e50[t] > e200[t] and e50[t - 1] <= e200[t - 1]
        dn = e50[t] < e200[t] and e50[t - 1] >= e200[t - 1]
        reversal = False
        if pos:
            s = pos["side"]
            pos["best"] = max(pos["best"], x["c"]) if s > 0 else min(pos["best"], x["c"])
            trail = pos["best"] - s * 3 * A[t]
            why = ("trail" if (s > 0 and x["c"] < trail) or (s < 0 and x["c"] > trail) else
                   "opposite cross" if (s > 0 and dn) or (s < 0 and up) else None)
            if why:
                out.append(dict(side=s, t_in=pos["t_in"], entry=pos["entry"], risk=pos["risk"], t_out=nxt["t"],
                                exit=nxt["o"], why=why, intrabar=False))
                pos = None
                reversal = why == "opposite cross"
            else:
                if (trail - pos["note"]) * s >= 0.5 * A[t]:
                    pos["note"] = trail
                    if emergency is not None:
                        pos["em"] = trail - s * emergency * A[t]
                continue
        if pos is None and A[t] and (up or dn):
            s = 1 if up else -1
            if sides == "long" and s < 0 or sides == "short" and s > 0:
                continue
            if reversal and reverse == "close":
                continue
            if reversal and reverse == "slope" and (e200[t] - e200[t - SLOPE_N]) * s <= 0:
                continue
            entry, risk = nxt["o"], 3 * A[t]
            pos = dict(side=s, t_in=nxt["t"], entry=entry, risk=risk, best=entry, note=entry - s * risk)
            pos["em"] = pos["note"] - s * (emergency or 0) * A[t]
    if pos:
        out.append(dict(side=pos["side"], t_in=pos["t_in"], entry=pos["entry"], risk=pos["risk"], t_out=None,
                        exit=None, why="open", intrabar=False))
    return out


def build(data, allowed, end_t, **kw):
    """trend_robust.build with t2v options."""
    trades = []
    for sym, full in data.items():
        cuts = [0] + [i + 1 for i in range(len(full) - 1) if full[i + 1]["t"] - full[i]["t"] > 3 * DAY] + [len(full)]
        for a, b in zip(cuts, cuts[1:]):
            H = full[a:b]
            if len(H) < 260:
                continue
            for tr in t2v(H, **kw):
                if not allowed(sym, tr["t_in"]) or tr["t_in"] - H[0]["t"] < T.MIN_HIST:
                    continue
                if tr["t_out"] is None and H[-1]["t"] < end_t - 3 * DAY:
                    tr.update(t_out=H[-1]["t"] + H4, exit=H[-1]["c"], why="delisted / data stopped")
                tr.update(sym=sym, model="T2")
                trades.append(tr)
    return trades


def attribution(log, slip):
    """-> dict price, slippage, fees, funding, net (sums in $). Raw prices recovered exactly from the fills."""
    price = slipc = 0.0
    for x in log:
        sd = 1 if x["side"] == "LONG" else -1
        raw_in = x["entry"] / (1 + sd * slip)
        raw_out = x["exit"] if x["why"] == "still open" else x["exit"] / (1 - sd * slip)
        p = sd * x["qty"] * (raw_out - raw_in)
        price += p
        slipc += x["gross"] - p
    return {"price": price, "slippage": slipc, "fees": -sum(x["fees"] for x in log),
            "funding": sum(x["funding"] for x in log), "net": sum(x["net"] for x in log)}


def stats(log):
    closed = [x for x in log if x["why"] != "still open"]
    wins = sum(x["net"] for x in closed if x["net"] > 0)
    loss = -sum(x["net"] for x in closed if x["net"] <= 0)
    n = len(closed)
    return {"n": n, "pf": wins / loss if loss else float("inf"),
            "avgR": sum(x["R"] for x in closed) / n if n else 0.0,
            "win": sum(1 for x in closed if x["net"] > 0) / n * 100 if n else 0.0}


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
    closes = {s: {x["t"]: x["c"] for x in H} for s, H in data.items()}
    times = sorted({t for s in closes for t in closes[s] if t >= start})
    end_t = times[-1]

    def in_top30(sym, t):
        return sym in top30.get(datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m"), ())
    yb = [start + i * 365.25 * DAY for i in range(YEARS + 1)]
    yname = [f"{datetime.fromtimestamp(yb[i] / 1000, timezone.utc):%b %y}" for i in range(YEARS)]
    E = R.EMERGENCY
    variants = [  # name, build options, slippage
        ("Baseline (no costs extra, as before)", dict(), 0.0),
        ("Realistic (reference)", dict(emergency=E), SLIP),
        ("Close-only on opposite cross", dict(emergency=E, reverse="close"), SLIP),
        (f"Slope-confirmed reverse (EMA200 vs {SLOPE_N} candles ago)", dict(emergency=E, reverse="slope"), SLIP),
        ("Long only", dict(emergency=E, sides="long"), SLIP),
        ("Short only", dict(emergency=E, sides="short"), SLIP),
        ("Emergency stop 3 ATR beyond trail", dict(emergency=3.0), SLIP),
        ("No emergency stop (no crash protection)", dict(), SLIP)]
    res = {}
    for name, kw, slip in variants:
        trades = build(data, in_top30, end_t, **kw)
        r = R.simulate(trades, closes, fund, times, RISK, limits, slip)
        y = R.yearly(r["curve"], yb)
        w, up, nm = R.worst_month(r["curve"])
        res[name] = dict(r=r, y=y, h1=y[0] + y[1], h2=y[2] + y[3], w=w, up=up, nm=nm, st=stats(r["log"]),
                         at=attribution(r["log"], slip), slip=slip)
        print(name, round(r["final"], 2), flush=True)
    ref = res["Realistic (reference)"]
    big_ref = {(x["sym"], x["t_in"]) for x in ref["r"]["log"] if x["R"] >= 5}

    def verdict(name):
        v = res[name]
        if name.startswith(("Baseline", "Realistic")):
            return "reference"
        ok = v["h1"] >= ref["h1"] + MIN_GAIN and v["h2"] >= ref["h2"] + MIN_GAIN and v["r"]["mdd"] <= ref["r"]["mdd"] + 0.01
        return "BETTER (both halves)" if ok else "not better"
    out = [f"## T2 trend DIAGNOSTICS - Top 30 (no BTC/ETH), $300, ${RISK:g} risk, {YEARS} years\n",
           f"Reference = Realistic (emergency stop {E:g} ATR + {SLIP * 100:.2f}% slippage). Rule fixed in advance: "
           f"better = at least ${MIN_GAIN:g} more P&L in BOTH halves and max DD at most 1 point worse. Size limits: " + lim_src + ".\n",
           "| Variant | " + " | ".join(f"Year from {y}" for y in yname)
           + " | Half 1 | Half 2 | Final | Max DD | Worst month | Trades | Win % | PF | Avg R | Big winners missed | Verdict |",
           "|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🔬 <b>T2 trend DIAGNOSTICS</b> — Top 30, $300, ${RISK:g} risk, {YEARS} years\n"
          f"Reference = Realistic (emergency stop + {SLIP * 100:.2f}% slip). Rule fixed before running: a variant is "
          f"better only if it earns ≥${MIN_GAIN:g} more in BOTH halves (yrs 1-2 and 3-4) and DD is ≤1 pt worse."]
    lines = []
    for name, _, _ in variants:
        v = res[name]
        keys = {(x["sym"], x["t_in"]) for x in v["r"]["log"]}
        missed = len(big_ref - keys)
        st = v["st"]
        out.append(f"| {name} | " + " | ".join(f"${x:+,.1f}" for x in v["y"])
                   + f" | ${v['h1']:+,.1f} | ${v['h2']:+,.1f} | ${v['r']['final']:,.1f} | −{v['r']['mdd'] * 100:.1f}% | "
                     f"${v['w']:+,.1f} | {st['n']} | {st['win']:.0f}% | {st['pf']:.2f} | {st['avgR']:+.3f} | "
                     f"{missed}/{len(big_ref)} | {verdict(name)} |")
        lines.append(f"{'✅' if verdict(name).startswith('BETTER') else '▫️'} <b>{bot.esc(name)}</b>: "
                     f"<b>${v['r']['final']:,.0f}</b>, DD −{v['r']['mdd'] * 100:.1f}%, halves "
                     f"${v['h1']:+,.0f} / ${v['h2']:+,.0f}, PF {st['pf']:.2f}, {st['n']} trades"
                     + (f", misses {missed}/{len(big_ref)} big winners" if missed and not name.startswith("Realistic")
                        else "") + f" → {verdict(name)}")
    tg.append("\n".join(lines))
    # attribution + direction + exits for the reference
    out.append("\n### Where the money comes from / goes (per variant)\n")
    out.append("| Variant | Price move | Slippage | Fees | Funding | Net |")
    out.append("|---|---|---|---|---|---|")
    for name, _, _ in variants:
        a = res[name]["at"]
        out.append(f"| {name} | ${a['price']:+,.1f} | ${a['slippage']:+,.1f} | ${a['fees']:+,.1f} | "
                   f"${a['funding']:+,.1f} | ${a['net']:+,.1f} |")
    lg = ref["r"]["log"]
    a = ref["at"]
    ref_lines = [f"Money: price ${a['price']:+,.1f}, slippage ${a['slippage']:+,.1f}, fees ${a['fees']:+,.1f}, "
                 f"funding ${a['funding']:+,.1f} = net ${a['net']:+,.1f}"]
    for side in ("LONG", "SHORT"):
        xs = [x for x in lg if x["side"] == side]
        st = stats(xs)
        ref_lines.append(f"{side}: {len(xs)} trades, net ${sum(x['net'] for x in xs):+,.1f}, win {st['win']:.0f}%, "
                         f"PF {st['pf']:.2f}, avg {st['avgR']:+.3f}R, funding ${sum(x['funding'] for x in xs):+,.1f}")
    reasons = {}
    for x in lg:
        rr = reasons.setdefault(x["why"], [0, 0.0])
        rr[0] += 1
        rr[1] += x["net"]
    ref_lines.append("Exits: " + "; ".join(f"{k} {v[0]} (${v[1]:+,.1f})" for k, v in
                                           sorted(reasons.items(), key=lambda kv: -kv[1][0])))
    # emergency-stop diagnosis: each stopped trade vs the same trade without the stop (same slippage)
    nostop = {(x["sym"], x["t_in"]): x for x in res["No emergency stop (no crash protection)"]["r"]["log"]}
    hits = [x for x in lg if x["why"] == "emergency stop"]
    pairs = [(x, nostop.get((x["sym"], x["t_in"]))) for x in hits]
    pairs = [(x, y) for x, y in pairs if y]
    diff = sum(x["net"] - y["net"] for x, y in pairs)
    saved = sum(1 for x, y in pairs if x["net"] > y["net"])
    recov = sum(1 for x, y in pairs if y["net"] > 0)
    em_lines = [f"Emergency stop hit {len(hits)} times. Same trades without the stop: ended in profit "
                f"{recov}/{len(pairs)}; the stop was better in {saved}/{len(pairs)}; net effect ${diff:+,.1f}."]
    worst = sorted(pairs, key=lambda p: p[0]["net"] - p[1]["net"])[:5]
    ts = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%d %b %y")
    if worst:
        em_lines.append("Costliest stops: " + "; ".join(
            f"{x['sym'][:-4]} {x['side']} {ts(x['t_in'])}: ${x['net']:+.1f} vs ${y['net']:+.1f} without"
            for x, y in worst))
    best = sorted(pairs, key=lambda p: p[1]["net"] - p[0]["net"])[:3]
    if best and best[0][1]["net"] < best[0][0]["net"]:
        em_lines.append("Stops that saved most: " + "; ".join(
            f"{x['sym'][:-4]} {x['side']} {ts(x['t_in'])}: ${x['net']:+.1f} vs ${y['net']:+.1f} without"
            for x, y in best if y["net"] < x["net"]))
    out.append("\n### Realistic reference - breakdown\n")
    out += [f"- {x}" for x in ref_lines + em_lines]
    tg.append("🔍 <b>Realistic breakdown</b>\n" + "\n".join("• " + bot.esc(x) for x in ref_lines))
    tg.append("🧯 <b>Emergency stop diagnosis</b>\n" + "\n".join("• " + bot.esc(x) for x in em_lines))
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "sym", "side", "entry_time", "exit_time", "entry_fill", "exit_fill", "qty", "risk_usd",
                    "gross", "fees", "funding", "net", "R", "why"])
        for name, _, _ in variants:
            for x in res[name]["r"]["log"]:
                tf = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else ""
                w.writerow([name, x["sym"], x["side"], tf(x["t_in"]), tf(x["t_out"]), f"{x['entry']:.8g}",
                            f"{x['exit']:.8g}", f"{x['qty']:.8g}", f"{x['risk_usd']:.4f}", f"{x['gross']:.4f}",
                            f"{x['fees']:.4f}", f"{x['funding']:.4f}", f"{x['net']:.4f}", f"{x['R']:.4f}", x["why"]])
    if failed:
        notes.append(f"{failed} volume file(s) failed to download")
    if notes:
        out.append("\nNotes: " + "; ".join(notes))
        tg.append("Notes: " + bot.esc("; ".join(notes[:15])))
    out.append("\nBig winners missed = trades of >= 5R in the Realistic reference that the variant does not take. "
               "Past results do not guarantee future results.")
    tg.append(f"Report only, nothing changed live. Trades: Actions → this run → Artifacts → trend-diag-trades. "
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
