"""T2 trend ROBUSTNESS tests on the Top 30 setup - report only, changes nothing live (no rule is tuned here).

Baseline = exactly trend_top50.py "Top 30 each month (no BTC/ETH)": EMA50/200 cross on 4h, 3 ATR chandelier trail
(close-based), opposite-cross exit + reverse, futures candles + real funding, 0.05% taker per side, no compounding,
max 12 open, size <= 3x equity, real Binance minimum order sizes, start = TREND_BALANCE ($300 default here).

Tests (each changes ONE thing, rules stay textbook):
  1. Slippage   every fill (entry, exit, stop) is worse by 0.05% / 0.10% / 0.20% per side.
  2. Emergency  the exchange stop the paper bot tells you to place: trail -/+ 1.5 ATR (moved when the trail improves
     stop       by >= 0.5 ATR, like the bot). If a 4h candle's wick touches it, the trade is closed there (a gap past
                it fills at the candle open). Shows what crash wicks do to the close-based backtest.
  3. Realistic  emergency stop + 0.10% slippage together.
  4. Signal     when the 12-trade / 3x cap is full, the baseline takes signals in alphabetical order. Here the order
     order      is shuffled 20 times (fixed seeds) -> range of results. No "strongest signal" rule (that would be a
                new tuned parameter).
  5. Breakdown  baseline trade by trade: long vs short, exit reasons, fees, funding paid/received, best/worst coins,
                win rate, profit factor, average R. Trade list saved as trend_robust_trades.csv (workflow artifact).
"""
import csv
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import trend_account as TA  # noqa: E402
import trend_small as S  # noqa: E402
import trend_top50 as T  # noqa: E402

YEARS, DAY, H4 = TA.YEARS, TA.DAY, TA.H4
EMERGENCY = 1.5
SEEDS = 20
CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trend_robust_trades.csv")


def t2(H, emergency=None):
    """T2 trades on 4h candles H (same rules as trend_account.t2_trades) + optional emergency exchange stop.
    -> list of dicts {side, t_in, entry, risk, t_out, exit, why, intrabar}."""
    cl = [x["c"] for x in H]
    e50, e200, A = TA.ema(cl, 50), TA.ema(cl, 200), TA.atr(H, 14)
    out, pos = [], None
    for t in range(201, len(H) - 1):
        x, nxt = H[t], H[t + 1]
        if pos and emergency is not None:                          # wick through the exchange stop during candle t
            s, lvl = pos["side"], pos["em"]
            if (s > 0 and x["l"] <= lvl) or (s < 0 and x["h"] >= lvl):
                px = min(x["o"], lvl) if s > 0 else max(x["o"], lvl)
                out.append(dict(side=s, t_in=pos["t_in"], entry=pos["entry"], risk=pos["risk"], t_out=x["t"],
                                exit=px, why="emergency stop", intrabar=True))
                pos = None
        up = e50[t] > e200[t] and e50[t - 1] <= e200[t - 1]
        dn = e50[t] < e200[t] and e50[t - 1] >= e200[t - 1]
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
            else:
                if (trail - pos["note"]) * s >= 0.5 * A[t]:            # the bot's TRAIL MOVED message
                    pos["note"] = trail
                    if emergency is not None:
                        pos["em"] = trail - s * emergency * A[t]
                continue
        if pos is None and A[t] and (up or dn):
            s = 1 if up else -1
            entry, risk = nxt["o"], 3 * A[t]
            pos = dict(side=s, t_in=nxt["t"], entry=entry, risk=risk, best=entry, note=entry - s * risk)
            pos["em"] = pos["note"] - s * (emergency or 0) * A[t]
    if pos:
        out.append(dict(side=pos["side"], t_in=pos["t_in"], entry=pos["entry"], risk=pos["risk"], t_out=None,
                        exit=None, why="open", intrabar=False))
    return out


def build(data, allowed, end_t, emergency=None):
    """Same as trend_top50.build_trades (gap split, 100-day history, delisting) but with t2() above."""
    trades = []
    for sym, full in data.items():
        cuts = [0] + [i + 1 for i in range(len(full) - 1) if full[i + 1]["t"] - full[i]["t"] > 3 * DAY] + [len(full)]
        for a, b in zip(cuts, cuts[1:]):
            H = full[a:b]
            if len(H) < 260:
                continue
            for tr in t2(H, emergency):
                if not allowed(sym, tr["t_in"]) or tr["t_in"] - H[0]["t"] < T.MIN_HIST:
                    continue
                if tr["t_out"] is None and H[-1]["t"] < end_t - 3 * DAY:
                    tr.update(t_out=H[-1]["t"] + H4, exit=H[-1]["c"], why="delisted / data stopped")
                tr.update(sym=sym, model="T2")
                trades.append(tr)
    return trades


def simulate(trades, closes, fund, times, risk_usd, limits, slip=0.0, seed=None):
    """trend_small.simulate + slippage (fraction per side), intrabar (emergency) exits, shuffled signal order and a
    trade log. With slip=0, seed=None and no intrabar exits it gives exactly trend_small.simulate's numbers."""
    by_in = {}
    for k, tr in enumerate(trades):
        by_in.setdefault(tr["t_in"], []).append(k)
    if seed is not None:
        rng = random.Random(seed)
        for v in by_in.values():
            rng.shuffle(v)
    cash, open_ = S.START_BAL, {}
    taken = sk_small = sk_cap = 0
    curve, peak, mdd, max_open = [], S.START_BAL, 0.0, 0
    fidx = {s: 0 for s in fund}
    last, log = {}, []
    px = lambda p: last.get(p["sym"], p["fill"])
    prev_t = None

    def close(k):
        nonlocal cash
        p = open_.pop(k)
        ex = p["exit"] * (1 - p["side"] * slip)
        fee_out = p["qty"] * ex * S.TAKER / 100
        gross = p["side"] * p["qty"] * (ex - p["fill"])
        cash += gross - fee_out - p["fund"]
        net = gross - fee_out - p["fee_in"] - p["fund"]
        log.append({"sym": p["sym"], "side": "LONG" if p["side"] > 0 else "SHORT", "t_in": p["t_in"],
                    "t_out": p["t_out"], "entry": p["fill"], "exit": ex, "qty": p["qty"], "risk_usd": p["real"],
                    "gross": gross, "fees": p["fee_in"] + fee_out, "funding": -p["fund"], "net": net,
                    "R": net / risk_usd, "why": p["why"]})

    for t in times:
        if prev_t is not None:
            for p in open_.values():
                fl, i = fund.get(p["sym"], []), fidx.get(p["sym"], 0)
                while i < len(fl) and fl[i][0] <= prev_t:
                    i += 1
                while i < len(fl) and fl[i][0] <= t:
                    p["fund"] += p["side"] * p["qty"] * px(p) * fl[i][1]
                    i += 1
            for s in fund:
                fl, i = fund[s], fidx[s]
                while i < len(fl) and fl[i][0] <= t:
                    i += 1
                fidx[s] = i
        for k in [k for k, p in open_.items() if p["t_out"] == t and not p["intrabar"]]:     # exits at the open
            close(k)
        for k in by_in.get(t, []):
            tr = trades[k]
            qty, real = S.size(tr, risk_usd, limits)
            if qty is None:
                sk_small += 1
                continue
            equity = cash + sum(p["side"] * p["qty"] * (px(p) - p["fill"]) - p["fund"] for p in open_.values())
            notional = sum(p["qty"] * px(p) for p in open_.values())
            if len(open_) >= S.MAX_OPEN or notional + qty * tr["entry"] > S.MAX_LEV * equity:
                sk_cap += 1
                continue
            fill = tr["entry"] * (1 + tr["side"] * slip)
            fee_in = qty * fill * S.TAKER / 100
            cash -= fee_in
            open_[k] = dict(tr, qty=qty, fund=0.0, fill=fill, fee_in=fee_in, real=real)
            taken += 1
        for k in [k for k, p in open_.items() if p["t_out"] == t and p["intrabar"]]:          # stops hit inside
            close(k)                                                                           # this candle
        max_open = max(max_open, len(open_))
        for s in closes:
            c = closes[s].get(t)
            if c is not None:
                last[s] = c
        eq = cash + sum(p["side"] * p["qty"] * (px(p) - p["fill"]) - p["fund"] for p in open_.values())
        curve.append((t, eq))
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
        prev_t = t
    for p in open_.values():                                                          # still open at the end
        unreal = p["side"] * p["qty"] * (px(p) - p["fill"])
        log.append({"sym": p["sym"], "side": "LONG" if p["side"] > 0 else "SHORT", "t_in": p["t_in"], "t_out": None,
                    "entry": p["fill"], "exit": px(p), "qty": p["qty"], "risk_usd": p["real"], "gross": unreal,
                    "fees": p["fee_in"], "funding": -p["fund"], "net": unreal - p["fee_in"] - p["fund"],
                    "R": (unreal - p["fee_in"] - p["fund"]) / risk_usd, "why": "still open"})
    return {"final": curve[-1][1] if curve else S.START_BAL, "curve": curve, "mdd": mdd, "taken": taken,
            "sk_small": sk_small, "sk_cap": sk_cap, "max_open": max_open, "log": log}


def yearly(curve, yb):
    def eq_at(ms):
        v = S.START_BAL
        for t, e in curve:
            if t > ms:
                break
            v = e
        return v
    return [eq_at(yb[i + 1]) - eq_at(yb[i]) for i in range(len(yb) - 1)]


def worst_month(curve):
    months = {}
    for t, e in curve:
        months[datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m")] = e
    prev, ch = S.START_BAL, []
    for k in sorted(months):
        ch.append(months[k] - prev)
        prev = months[k]
    return (min(ch) if ch else 0.0), sum(1 for x in ch if x > 0), len(ch)


def breakdown(log, risk_usd):
    closed = [x for x in log if x["why"] != "still open"]
    n = len(closed)
    wins = [x["net"] for x in closed if x["net"] > 0]
    losses = [x["net"] for x in closed if x["net"] <= 0]
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf")
    lines = [f"Closed trades {n}: win {len(wins) / n * 100 if n else 0:.0f}%, median win ${median(wins) if wins else 0:.2f}, "
             f"median loss ${median(losses) if losses else 0:.2f}, profit factor {pf:.2f}, "
             f"avg {sum(x['R'] for x in closed) / n if n else 0:+.3f}R per trade (R = ${risk_usd:g})"]
    for side in ("LONG", "SHORT"):
        xs = [x for x in log if x["side"] == side]
        lines.append(f"{side}: {len(xs)} trades, net ${sum(x['net'] for x in xs):+,.1f}, funding "
                     f"${sum(x['funding'] for x in xs):+,.1f}, fees ${-sum(x['fees'] for x in xs):,.1f}")
    reasons = {}
    for x in log:
        r = reasons.setdefault(x["why"], [0, 0.0])
        r[0] += 1
        r[1] += x["net"]
    lines.append("Exit reasons: " + "; ".join(f"{k} {v[0]} (${v[1]:+,.1f})" for k, v in
                                              sorted(reasons.items(), key=lambda kv: -kv[1][0])))
    big = sorted(closed, key=lambda x: -x["net"])
    top10 = sum(x["net"] for x in big[:10])
    lines.append(f"Biggest 10 winners = ${top10:+,.1f} of the ${sum(x['net'] for x in log):+,.1f} total "
                 f"(trend systems live on a few big trends)")
    by = {}
    for x in log:
        by[x["sym"][:-4]] = by.get(x["sym"][:-4], 0.0) + x["net"]
    srt = sorted(by.items(), key=lambda kv: kv[1])
    lines.append("Best coins: " + ", ".join(f"{k} ${v:+.1f}" for k, v in srt[::-1][:5]))
    lines.append("Worst coins: " + ", ".join(f"{k} ${v:+.1f}" for k, v in srt[:5]))
    return lines


def main():
    t0 = time.time()
    now = time.time() * 1000
    start = now - YEARS * 365.25 * DAY
    hours = int(YEARS * 365.25 * 24) + 140 * 24
    num = r"\d+(?:\.\d+)?"
    bal = re.findall(num, os.getenv("TREND_BALANCE", "").replace(",", ""))
    S.START_BAL = float(bal[0]) if bal and float(bal[0]) > 0 else 300.0
    risks = [float(x) for x in re.findall(num, os.getenv("TREND_RISKS", "")) if float(x) > 0]
    if len(risks) < 2:
        risks = [1.0, 1.5]
    print(f"start ${S.START_BAL:g}, risks {risks}", flush=True)
    limits, lim_src = S.live_limits()
    top, n_syms, failed = T.universe(start)
    top30 = {k: set(v[:30]) for k, v in top.items()}
    need = sorted({s for v in top30.values() for s in v})
    print(f"{len(need)} coins were in the top 30 at some point", flush=True)
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
    base = build(data, in_top30, end_t)
    emerg = build(data, in_top30, end_t, EMERGENCY)
    yb = [start + i * 365.25 * DAY for i in range(YEARS + 1)]
    yname = [f"{datetime.fromtimestamp(yb[i] / 1000, timezone.utc):%b %y}" for i in range(YEARS)]
    scen = [("Baseline (backtest as before)", base, 0.0),
            ("Slippage 0.05% per side", base, 0.0005),
            ("Slippage 0.10% per side", base, 0.001),
            ("Slippage 0.20% per side", base, 0.002),
            (f"Emergency stop (trail ± {EMERGENCY:g} ATR, wick hits)", emerg, 0.0),
            ("Realistic: emergency stop + 0.10% slippage", emerg, 0.001)]
    out = [f"## T2 trend ROBUSTNESS - Top 30 (no BTC/ETH), ${S.START_BAL:,.0f}, {YEARS} years, no rule tuned\n",
           f"Futures + real funding, 0.10% fees, no compounding, max 12 open, size <= 3x equity. Size limits: {lim_src}.\n",
           "| Scenario | Risk | " + " | ".join(f"Year from {y}" for y in yname)
           + " | Final | Return | Max DD | Worst month | Months up | Taken | Skipped small / cap |",
           "|---|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|"]
    tg = [f"🧪 <b>T2 trend ROBUSTNESS</b> — Top 30 (no BTC/ETH), ${S.START_BAL:,.0f}, {YEARS} years, "
          f"no rule tuned. Risk per trade: " + ", ".join(f"${r:g}" for r in risks)]
    logs = {}
    for name, trades, slip in scen:
        lines = [f"<b>{bot.esc(name)}</b>"]
        for risk_usd in risks:
            r = simulate(trades, closes, fund, times, risk_usd, limits, slip)
            logs[(name, risk_usd)] = r["log"]
            y = yearly(r["curve"], yb)
            w, up, nm = worst_month(r["curve"])
            ret = (r["final"] / S.START_BAL - 1) * 100
            out.append(f"| {name} | ${risk_usd:g} | " + " | ".join(f"${v:+,.1f}" for v in y)
                       + f" | ${r['final']:,.1f} | {ret:+.0f}% | −{r['mdd'] * 100:.1f}% | ${w:+,.1f} | {up}/{nm} | "
                         f"{r['taken']} | {r['sk_small']} / {r['sk_cap']} |")
            lines.append(f"   ${risk_usd:g}: ${S.START_BAL:,.0f} → <b>${r['final']:,.1f}</b> ({ret:+.0f}%), "
                         f"DD −{r['mdd'] * 100:.1f}%, worst month ${w:+,.1f}\n      years: "
                         + " · ".join(f"${v:+,.1f}" for v in y))
        tg.append("\n".join(lines))
    # signal order: 20 shuffles of the baseline
    out.append(f"\n### Signal order - baseline with {SEEDS} random orders (instead of alphabetical)\n")
    out.append("| Risk | Final min / median / max | Max DD worst / median | Alphabetical (baseline) |")
    out.append("|---|---|---|---|")
    lines = [f"🎲 <b>Signal order</b> ({SEEDS} random orders instead of alphabetical, baseline rules)"]
    for risk_usd in risks:
        rs = [simulate(base, closes, fund, times, risk_usd, limits, 0.0, seed) for seed in range(1, SEEDS + 1)]
        fin = sorted(x["final"] for x in rs)
        dds = sorted(x["mdd"] for x in rs)
        b = next(x for x in out if x.startswith(f"| Baseline (backtest as before) | ${risk_usd:g} |"))
        bfin = b.split(" | ")[2 + YEARS]
        out.append(f"| ${risk_usd:g} | ${fin[0]:,.1f} / ${median(fin):,.1f} / ${fin[-1]:,.1f} | "
                   f"−{dds[-1] * 100:.1f}% / −{median(dds) * 100:.1f}% | {bfin} |")
        lines.append(f"   ${risk_usd:g}: final ${fin[0]:,.0f} … ${fin[-1]:,.0f} (median ${median(fin):,.0f}; "
                     f"alphabetical {bfin}), worst DD −{dds[-1] * 100:.1f}%")
    tg.append("\n".join(lines))
    # breakdown of the baseline at the first risk
    r0 = risks[0]
    bd = breakdown(logs[("Baseline (backtest as before)", r0)], r0)
    out.append(f"\n### Baseline trade breakdown (${r0:g} risk)\n")
    out += [f"- {x}" for x in bd]
    tg.append(f"🔍 <b>Baseline breakdown</b> (${r0:g} risk)\n" + "\n".join("• " + bot.esc(x) for x in bd))
    with open(CSV_FILE, "w", newline="") as f:
        cols = ["scenario", "risk", "sym", "side", "entry_time", "exit_time", "entry", "exit", "qty", "risk_usd",
                "gross", "fees", "funding", "net", "R", "why"]
        w = csv.writer(f)
        w.writerow(cols)
        for (name, risk_usd), lg in logs.items():
            if not name.startswith(("Baseline", "Realistic")):
                continue
            for x in lg:
                ts = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else ""
                w.writerow([name, risk_usd, x["sym"], x["side"], ts(x["t_in"]), ts(x["t_out"]),
                            f"{x['entry']:.8g}", f"{x['exit']:.8g}", f"{x['qty']:.8g}", f"{x['risk_usd']:.4f}",
                            f"{x['gross']:.4f}", f"{x['fees']:.4f}", f"{x['funding']:.4f}", f"{x['net']:.4f}",
                            f"{x['R']:.4f}", x["why"]])
    if failed:
        notes.append(f"{failed} volume file(s) failed to download")
    if notes:
        out.append("\nNotes: " + "; ".join(notes))
        tg.append("Notes: " + bot.esc("; ".join(notes[:15])))
    out.append("\nThe 4-year window ends at the newest published candle, so numbers can differ by a little from "
               "earlier runs. Past results do not guarantee future results.")
    tg.append(f"Report only, nothing changed live. Trade list: Actions → this run → Artifacts → trend-robust-trades. "
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
