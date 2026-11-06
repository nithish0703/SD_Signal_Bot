"""'The Swing Playbook' (Mark - Wait - Enter) on crypto - report only, changes nothing live (paper trader untouched).

Every subjective step of the guide is turned into a fixed rule BEFORE any result is seen:

MARK (weekly + daily, built from the 4h candles, UTC days, weeks start Monday 00:00 UTC)
  Swing point   a candle whose high (low) is strictly above (below) the 3 candles on each side. It is only known
                when the 3rd candle after it has closed (no look-ahead).
  Trend         last 2 known swing highs AND last 2 known swing lows: both higher = UP, both lower = DOWN, else none.
  Bias          weekly trend == daily trend (UP -> buys only, DOWN -> sells only, disagree/none -> no trade).
  AOI zone      every known weekly and daily swing point of the last 365 days. Swing high: body top -> wick high,
                swing low: wick low -> body bottom. "Clean only": zones wider than 1.5 x daily ATR14 are dropped.
WAIT (4h)
  Break         a 4h candle CLOSES beyond the zone (buy: previous close <= zone top, this close > top) while the bias
                agrees. A new break on a zone overlapping an already waiting one is ignored.
  Dead setup    a 4h close back through the zone (buy: close < zone bottom), or no entry within 60 x 4h (10 days).
ENTER (4h)
  Pattern       morning star (buy) / evening star (sell), all 3 candles after the break candle:
                c1 against the trade with a body >= the average body of the 10 candles before it,
                c2 body <= half of c1's body, c3 in the trade direction closing beyond the middle of c1's body.
  At the AOI    the pattern's extreme wick reaches the zone (buy: lowest low <= zone top) = the retest.
  EMA           c3 closes beyond the 4h EMA50 (above for buys, below for sells); bias still agrees at c3.
  Entry         next 4h open (+0.10% slippage, 0.10% fees, real funding). Stop = the pattern's extreme wick.
  Target        A (the guide): the near edge of the next clean zone beyond c3's close; no zone there -> 3R; target
                closer than 1R -> trade skipped (decided at c3's close, before the order). Stop moves to breakeven once price has covered 75% of the way to the target.
                Same-candle stop + target -> stop first (worst case).
                B (guide entry, trend exit): no target, no breakeven; hard stop at the wick + the T2 trail
                (best 4h close -/+ 3 x ATR14, a 4h close beyond it exits at the next open).
Account (same as the T2 'Realistic' reference): Top 30 by last month's futures volume, no BTC/ETH, $300, $1 fixed
risk, max 12 open, size <= 3x equity, real Binance minimum sizes, one trade per coin at a time (signals outside the
Top 30 are not traded and do not block the coin).

Pre-registered verdict: a version is 'profitable' only if BOTH halves (years 1-2 and 3-4) are positive, and it
'beats T2' only if it also earns >= $5 more than T2 in both halves with max DD at most 1 point worse.
Trade lists -> swing_playbook_trades.csv (workflow artifact).
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
WEEK = 7 * DAY
RISK = 1.0
MIN_GAIN = 5.0
SLIP = D.SLIP
SWING_N = 3            # candles on each side of a swing point
ZONE_DAYS = 365        # zones from swing points of the last year
ZONE_MAX_ATR = 1.5     # "clean": zone width <= 1.5 x daily ATR14
WAIT_MAX = 60          # 4h candles (10 days) from the break to the entry pattern
BODY_AVG = 10          # c1 must be at least the average body of the 10 candles before it
BE_AT = 0.75           # breakeven once 75% of the way to the target
FALLBACK_R = 3.0       # no zone beyond the entry -> target 3R
MIN_TARGET_R = 1.0     # target closer than 1R -> no trade
TRAIL_ATR = 3.0
CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swing_playbook_trades.csv")


def agg(H, key):
    """Group 4h candles into daily / weekly candles. 'end' = time the candle is complete."""
    out = []
    for x in H:
        k = key(x["t"])
        if out and out[-1]["k"] == k:
            c = out[-1]
            c["h"], c["l"], c["c"] = max(c["h"], x["h"]), min(c["l"], x["l"]), x["c"]
        else:
            out.append({"k": k, "t": x["t"], "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"]})
    return out


def day_key(t):
    return t // DAY


def week_key(t):
    return (t // DAY + 3) // 7               # 1970-01-01 was a Thursday -> weeks start Monday 00:00 UTC


def swings(C, end_of):
    """[(known_at, 'H'|'L', index)] - swing points, known when the 3rd candle after them is complete."""
    out, n = [], SWING_N
    for j in range(n, len(C) - n):
        hi, lo = C[j]["h"], C[j]["l"]
        side = C[j - n:j] + C[j + 1:j + n + 1]
        if all(hi > x["h"] for x in side):
            out.append((end_of(C[j + n]), "H", j))
        if all(lo < x["l"] for x in side):
            out.append((end_of(C[j + n]), "L", j))
    out.sort()
    return out


class TF:
    """Weekly or daily structure, advanced in time: trend + zones known at a moment."""

    def __init__(self, C, end_of, atr_d=None):
        self.C, self.sw, self.k = C, swings(C, end_of), 0
        self.hs, self.ls = [], []
        self.atr_d = atr_d

    def advance(self, now, zones, src):
        while self.k < len(self.sw) and self.sw[self.k][0] <= now:
            _, kind, j = self.sw[self.k]
            x = self.C[j]
            (self.hs if kind == "H" else self.ls).append(x["h"] if kind == "H" else x["l"])
            if kind == "H":
                z = (max(x["o"], x["c"]), x["h"])
            else:
                z = (x["l"], min(x["o"], x["c"]))
            zones.append({"bot": z[0], "top": z[1], "t": x["t"], "src": src, "kind": kind, "j": j})
            self.k += 1

    def trend(self):
        if len(self.hs) < 2 or len(self.ls) < 2:
            return 0
        if self.hs[-1] > self.hs[-2] and self.ls[-1] > self.ls[-2]:
            return 1
        if self.hs[-1] < self.hs[-2] and self.ls[-1] < self.ls[-2]:
            return -1
        return 0


def daily_atr_at(Dc, atr_d, now):
    """ATR14 of the last COMPLETE daily candle at time 'now' (a daily candle k is complete at (k+1) x DAY)."""
    lo, hi = 0, len(Dc)
    while lo < hi:
        mid = (lo + hi) // 2
        if (Dc[mid]["k"] + 1) * DAY <= now:
            lo = mid + 1
        else:
            hi = mid
    return atr_d[lo - 1] if lo > 0 else None


def is_star(H, t, side, avgb):
    """morning (side 1) / evening (side -1) star ending at candle t."""
    c1, c2, c3 = H[t - 2], H[t - 1], H[t]
    b1 = c1["c"] - c1["o"]
    b3 = c3["c"] - c3["o"]
    if side * b1 >= 0 or side * b3 <= 0:
        return False
    if abs(b1) < avgb or abs(c2["c"] - c2["o"]) > 0.5 * abs(b1):
        return False
    mid1 = (c1["o"] + c1["c"]) / 2
    return side * (c3["c"] - mid1) > 0


def scan(H, version, cnt, ok=None):
    """One coin, one continuous 4h series -> trades (dicts for trend_robust.simulate).
    ok(t_in) False (coin not in that month's Top 30 / too new) -> the signal is not traded and does not block the coin."""
    cl = [x["c"] for x in H]
    e50, A = TA.ema(cl, 50), TA.atr(H, 14)
    Dc = agg(H, day_key)
    Wc = agg(H, week_key)
    atr_d = TA.atr(Dc, 14)
    d_end = lambda x: (x["k"] + 1) * DAY
    w_end = lambda x: (x["k"] + 1) * WEEK - 3 * DAY
    dtf, wtf = TF(Dc, d_end), TF(Wc, w_end)
    zones, pend, out = [], [], []
    busy = -1
    bodies = [abs(x["c"] - x["o"]) for x in H]
    for t in range(max(BODY_AVG + 3, 51), len(H) - 1):
        now = H[t]["t"] + H4                 # this 4h candle has just closed
        wtf.advance(now, zones, "W")
        dtf.advance(now, zones, "D")
        if t % 6 == 0:                       # forget zones older than a year
            zones = [z for z in zones if z["t"] >= now - ZONE_DAYS * DAY]
        atr_now = daily_atr_at(Dc, atr_d, now)
        bias = wtf.trend() if wtf.trend() == dtf.trend() else 0
        x, prev = H[t], H[t - 1]
        # waiting setups: expiry / dead / retest seen
        keep = []
        for p in pend:
            s = p["side"]
            if t - p["tb"] > WAIT_MAX:
                cnt["expired_retested" if p["retest"] else "expired_no_retest"] += 1
                continue
            if (s > 0 and x["c"] < p["bot"]) or (s < 0 and x["c"] > p["top"]):
                cnt["dead"] += 1
                continue
            if t > p["tb"] and ((s > 0 and x["l"] <= p["top"]) or (s < 0 and x["h"] >= p["bot"])):
                p["retest"] = True
            keep.append(p)
        pend = keep
        # entry: star at the zone, EMA, bias, coin free
        if bias and t + 1 > busy:
            avgb = sum(bodies[t - 2 - BODY_AVG:t - 2]) / BODY_AVG
            for p in sorted(pend, key=lambda q: -q["tb"]):
                s = p["side"]
                if s != bias or t - 2 <= p["tb"] or not A[t]:
                    continue
                lo3 = min(H[k]["l"] for k in (t - 2, t - 1, t))
                hi3 = max(H[k]["h"] for k in (t - 2, t - 1, t))
                at_zone = lo3 <= p["top"] if s > 0 else hi3 >= p["bot"]
                if not at_zone or not is_star(H, t, s, avgb):
                    continue
                if s * (x["c"] - e50[t]) <= 0:
                    cnt["star_no_ema"] += 1
                    continue
                if ok is not None and not ok(H[t + 1]["t"]):
                    cnt["skipped_universe"] += 1
                    continue
                entry = H[t + 1]["o"]
                stop = lo3 if s > 0 else hi3
                risk = s * (entry - stop)
                if risk <= 0:
                    cnt["gap_past_stop"] += 1
                    continue
                tr = dict(side=s, t_in=H[t + 1]["t"], entry=entry, risk=risk, stop=stop, zone=(p["bot"], p["top"]),
                          t_break=H[p["tb"]]["t"])
                if version == "A":                 # target decided at c3's close (known before the order)
                    ref, risk_ref = x["c"], s * (x["c"] - stop)
                    beyond = [z["bot"] if s > 0 else z["top"] for z in zones          # clean zones only
                              if z["t"] >= now - ZONE_DAYS * DAY and atr_now
                              and z["top"] - z["bot"] <= ZONE_MAX_ATR * atr_now
                              and s * ((z["bot"] if s > 0 else z["top"]) - ref) > 0]
                    if beyond:
                        target = min(beyond) if s > 0 else max(beyond)
                    else:
                        target = ref + s * FALLBACK_R * risk_ref
                        cnt["target_fallback"] += 1
                    if s * (target - ref) < MIN_TARGET_R * risk_ref:
                        cnt["target_too_close"] += 1
                        pend.remove(p)
                        break
                    tr["target"] = target
                    k = exit_a(H, t + 1, tr)
                else:
                    k = exit_b(H, t + 1, tr, A)
                cnt["entries"] += 1
                pend.remove(p)
                out.append(tr)
                busy = k if k is not None else len(H)
                break
        # new breaks (after the entry check: the break candle itself is never part of the pattern)
        if bias and atr_now:
            for z in zones:
                if z["t"] < now - ZONE_DAYS * DAY or z["top"] - z["bot"] > ZONE_MAX_ATR * atr_now:
                    continue
                if bias > 0 and not (prev["c"] <= z["top"] < x["c"]):
                    continue
                if bias < 0 and not (prev["c"] >= z["bot"] > x["c"]):
                    continue
                if any(p["side"] == bias and p["bot"] <= z["top"] and z["bot"] <= p["top"] for p in pend):
                    continue
                pend.append({"side": bias, "bot": z["bot"], "top": z["top"], "tb": t, "retest": False})
                cnt["breaks"] += 1
    return out


def exit_a(H, i0, tr):
    """guide exit: stop / target, stop -> breakeven at 75% of the way. Returns the exit candle index."""
    s, stop, tgt, entry = tr["side"], tr["stop"], tr["target"], tr["entry"]
    be_lvl, be = entry + s * BE_AT * (tgt - entry), False
    for k in range(i0, len(H)):
        x = H[k]
        if (s > 0 and x["l"] <= stop) or (s < 0 and x["h"] >= stop):
            px = min(x["o"], stop) if s > 0 else max(x["o"], stop)
            tr.update(t_out=x["t"], exit=px, why="breakeven" if be else "stop", intrabar=True)
            return k
        if (s > 0 and x["h"] >= tgt) or (s < 0 and x["l"] <= tgt):
            px = max(x["o"], tgt) if s > 0 else min(x["o"], tgt)
            tr.update(t_out=x["t"], exit=px, why="target", intrabar=True)
            return k
        if not be and ((s > 0 and x["h"] >= be_lvl) or (s < 0 and x["l"] <= be_lvl)):
            be, stop = True, entry                         # active from the next candle
    tr.update(t_out=None, exit=None, why="open", intrabar=False)
    return None


def exit_b(H, i0, tr, A):
    """guide entry + T2 trail: hard stop at the wick, 4h close beyond best close -/+ 3 ATR exits at the next open."""
    s, stop, best = tr["side"], tr["stop"], tr["entry"]
    for k in range(i0, len(H)):
        x = H[k]
        if (s > 0 and x["l"] <= stop) or (s < 0 and x["h"] >= stop):
            px = min(x["o"], stop) if s > 0 else max(x["o"], stop)
            tr.update(t_out=x["t"], exit=px, why="stop", intrabar=True)
            return k
        best = max(best, x["c"]) if s > 0 else min(best, x["c"])
        if A[k] and k + 1 < len(H):
            trail = best - s * TRAIL_ATR * A[k]
            if (s > 0 and x["c"] < trail) or (s < 0 and x["c"] > trail):
                tr.update(t_out=H[k + 1]["t"], exit=H[k + 1]["o"], why="trail", intrabar=False)
                return k + 1
    tr.update(t_out=None, exit=None, why="open", intrabar=False)
    return None


def build(data, allowed, end_t, version, cnt, start=0):
    trades = []
    for sym, full in data.items():
        cuts = [0] + [i + 1 for i in range(len(full) - 1) if full[i + 1]["t"] - full[i]["t"] > 3 * DAY] + [len(full)]
        for a, b in zip(cuts, cuts[1:]):
            H = full[a:b]
            if len(H) < 260:
                continue
            ok = lambda t_in, H=H, sym=sym: t_in >= start and allowed(sym, t_in) and t_in - H[0]["t"] >= T.MIN_HIST
            for tr in scan(H, version, cnt, ok):
                if tr["t_out"] is None and H[-1]["t"] < end_t - 3 * DAY:
                    tr.update(t_out=H[-1]["t"] + H4, exit=H[-1]["c"], why="delisted / data stopped", intrabar=False)
                tr.update(sym=sym, model=version)
                trades.append(tr)
    return trades


def main():
    t0 = time.time()
    now = time.time() * 1000
    start = now - YEARS * 365.25 * DAY
    hours = int(YEARS * 365.25 * 24) + 400 * 24          # extra year: weekly swings / zones need history
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
    keys = ["breaks", "expired_no_retest", "expired_retested", "dead", "star_no_ema", "gap_past_stop",
            "target_fallback", "target_too_close", "entries", "skipped_universe"]
    rows, res, cnts = [("T2 trend (reference, realistic)", None), ("A. Swing Playbook as written", "A"),
                       ("B. Playbook entry + T2 trail exit", "B")], {}, {}
    for name, ver in rows:
        if ver is None:
            trades = D.build(data, in_top30, end_t, emergency=E)
        else:
            cnts[ver] = dict.fromkeys(keys, 0)
            trades = build(data, in_top30, end_t, ver, cnts[ver], start)
            trades = [x for x in trades if x["t_in"] >= start]
        r = R.simulate(trades, closes, fund, times, RISK, limits, SLIP)
        y = R.yearly(r["curve"], yb)
        w, up, nm = R.worst_month(r["curve"])
        lg = [x for x in r["log"]]
        closed = [x for x in lg if x["why"] != "still open"]
        hold = (sum((x["t_out"] - x["t_in"]) for x in closed if x["t_out"]) / len(closed) / DAY) if closed else 0
        why = {}
        for x in closed:
            why[x["why"]] = why.get(x["why"], 0) + 1
        res[name] = dict(r=r, y=y, h1=y[0] + y[1], h2=y[2] + y[3], w=w, up=up, nm=nm, st=D.stats(lg), hold=hold,
                         why=why, best=max((x["net"] for x in closed), default=0.0),
                         long=sum(x["net"] for x in lg if x["side"] == "LONG"),
                         short=sum(x["net"] for x in lg if x["side"] == "SHORT"))
        print(name, round(r["final"], 2), flush=True)
    ref = res[rows[0][0]]

    def verdict(name):
        v = res[name]
        if name == rows[0][0]:
            return "reference"
        prof = v["h1"] > 0 and v["h2"] > 0
        beats = (v["h1"] >= ref["h1"] + MIN_GAIN and v["h2"] >= ref["h2"] + MIN_GAIN
                 and v["r"]["mdd"] <= ref["r"]["mdd"] + 0.01)
        return ("profitable, BEATS T2" if prof and beats else "profitable, does not beat T2" if prof
                else "NOT profitable in both halves")
    out = [f"## The Swing Playbook on crypto - Top 30 (no BTC/ETH), $300, ${RISK:g} risk, {YEARS} years\n",
           f"Realistic costs for all rows: {SLIP * 100:.2f}% slippage per side, 0.10% fees, real funding, real Binance "
           f"minimum sizes ({lim_src}), max {S.MAX_OPEN} open, one trade per coin. Rules: see swing_playbook.py "
           f"docstring (fixed before running). Verdict: profitable = both halves positive; beats T2 = also >= "
           f"${MIN_GAIN:g} more than T2 in both halves with DD at most 1 point worse.\n",
           "| Strategy | " + " | ".join(f"Year from {y}" for y in yname)
           + " | Half 1 | Half 2 | Final | Max DD | Worst month | Months up | Trades | Win % | PF | Avg R | Avg hold"
             " | Best trade | Long | Short | Exits | Verdict |",
           "|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"📘 <b>Swing Playbook on crypto</b> (Top 30, $300, ${RISK:g} risk, {YEARS} years, realistic costs)\n"
          f"Rule fixed before running: profitable = both halves (yrs 1-2, 3-4) positive; beats T2 = also ≥${MIN_GAIN:g}"
          f" more than T2 in both halves and DD ≤1 pt worse."]
    lines = []
    for name, _ in rows:
        v = res[name]
        st = v["st"]
        ex = ", ".join(f"{k} {n}" for k, n in sorted(v["why"].items(), key=lambda z: -z[1]))
        out.append(f"| {name} | " + " | ".join(f"${x:+,.1f}" for x in v["y"])
                   + f" | ${v['h1']:+,.1f} | ${v['h2']:+,.1f} | ${v['r']['final']:,.1f} | −{v['r']['mdd'] * 100:.1f}% | "
                     f"${v['w']:+,.1f} | {v['up']}/{v['nm']} | {st['n']} | {st['win']:.0f}% | {st['pf']:.2f} | "
                     f"{st['avgR']:+.2f}R | {v['hold']:.1f} d | ${v['best']:+,.1f} | ${v['long']:+,.1f} | "
                     f"${v['short']:+,.1f} | {ex} | {verdict(name)} |")
        lines.append(f"{'✅' if 'BEATS' in verdict(name) else '▫️'} <b>{bot.esc(name)}</b>: <b>${v['r']['final']:,.0f}</b>, "
                     f"DD −{v['r']['mdd'] * 100:.1f}%, years " + " · ".join(f"${x:+,.0f}" for x in v["y"])
                     + f", {st['n']} trades, win {st['win']:.0f}%, PF {st['pf']:.2f}, avg hold {v['hold']:.1f} d"
                     + f" → {bot.esc(verdict(name))}")
    tg.append("\n".join(lines))
    fl = []
    for ver, c in cnts.items():
        txt = (f"{ver}: breaks {c['breaks']} → entry signals {c['entries']} + {c['skipped_universe']} not taken (coin not in "
               f"that month's Top 30 / too new / before the start); the account limits come after. "
               f"No retest within 10 days (price ran away): {c['expired_no_retest']}; retested but no star+EMA: "
               f"{c['expired_retested']}; dead (closed back through the zone): {c['dead']}; star but wrong side of "
               f"EMA50: {c['star_no_ema']}")
        if ver == "A":
            txt += f"; target < 1R skipped: {c['target_too_close']}; no next level (3R used): {c['target_fallback']}"
        fl.append(txt)
    out.append("\n**Setup funnel** (all coins / times, before the Top 30 and account limits)\n\n" + "\n\n".join(fl))
    tg.append("<b>Setup funnel</b>\n" + bot.esc("\n".join(fl)))
    a, b = verdict(rows[1][0]), verdict(rows[2][0])
    tg.append(f"Result: A (as written) → <b>{bot.esc(a)}</b>; B (trail exit) → <b>{bot.esc(b)}</b>.")
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "sym", "side", "entry_time", "exit_time", "entry_fill", "exit_fill", "qty", "risk_usd",
                    "gross", "fees", "funding", "net", "R", "why"])
        tf = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else ""
        for name, _ in rows:
            for x in res[name]["r"]["log"]:
                w.writerow([name, x["sym"], x["side"], tf(x["t_in"]), tf(x["t_out"]), f"{x['entry']:.8g}",
                            f"{x['exit']:.8g}", f"{x['qty']:.8g}", f"{x['risk_usd']:.4f}", f"{x['gross']:.4f}",
                            f"{x['fees']:.4f}", f"{x['funding']:.4f}", f"{x['net']:.4f}", f"{x['R']:.4f}", x["why"]])
    if failed:
        notes.append(f"{failed} volume file(s) failed to download")
    if notes:
        out.append("\nNotes: " + "; ".join(notes))
        tg.append("Notes: " + bot.esc("; ".join(notes[:15])))
    out.append("\nPast results do not guarantee future results. The guide's subjective steps (clean zones, patterns, "
               "next level) are fixed rules here; a human trader may draw them differently.")
    tg.append(f"Report only, nothing changed live. Trades: Actions → this run → Artifacts → swing-playbook-trades. "
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
