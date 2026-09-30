"""Trend lab: backtest 4 research strategies side by side (report only, nothing live changes).

1. Donchian trend ensemble (Zarattini et al., "Catching Crypto Trends", 2025):
   daily closes, lookbacks 5/10/20/30/60/90/150/250/360 days, enter when the close breaks the
   highest close of the lookback, trailing stop = max(previous stop, midpoint of max/min close),
   exit on a close below the stop, equal-weight ensemble of lookbacks, each coin sized to 25%
   annualised volatility (90-day), leverage cap 2x, coin must have 1 year of history. Long-only (paper)
   plus a long/short variant.
2. Multi-timeframe trend (Quantpedia, "simple multi-timeframe trend strategy on Bitcoin"):
   daily MACD(12,26,9) above signal = uptrend; enter on a 1h MACD cross up; exit on the 1h cross down
   ("D1H1") or at the close of the first negative 1h candle ("D1H1 + stop"). Long-only (paper).
   Plus a practitioner variant: daily close vs EMA50 trend, 1h pullback to EMA20, chandelier 3 ATR exit.
3. Regime switching: BTC daily ADX(14) >= 25 = trend, else range. SMC sweep trades only in range,
   trend strategy only in trend; combined equity.
4. Intraday 1h trend (Concretum, "Seasonality in Bitcoin intraday trend trading" - their model is
   not public, so a transparent 1h time-series momentum ensemble is used): position = majority sign of
   the 6/12/24/48/96-hour returns; variant that only trades the "Monday Asia open" window
   (Sunday 19:00 -> Monday 19:00 New York time).

Costs: 0.05% taker per side; longs pay 0.01% funding per 8h (shorts get nothing = conservative).
All coins = today's top-N futures coins (survivorship bias: coins that died are not included).
"""
import argparse
import bisect
import math
import os
import re
import time
from datetime import datetime, timezone

import bot

FEE_SIDE = 0.0005           # taker, one side
FUND_8H = 0.0001            # longs pay 0.01% per 8h
DAY = 86400000
HOUR = 3600000
DONCHIAN_N = [5, 10, 20, 30, 60, 90, 150, 250, 360]


# ---------------------------------------------------------------- helpers
def ema(vals, n):
    out, k, s = [], 2 / (n + 1), None
    for v in vals:
        s = v if s is None else v * k + s * (1 - k)
        out.append(s)
    return out


def macd(vals):
    m = [a - b for a, b in zip(ema(vals, 12), ema(vals, 26))]
    return m, ema(m, 9)


def nyc_offset(ms):
    mon = datetime.fromtimestamp(ms / 1000, timezone.utc).month
    return 4 if 3 < mon < 11 else 5                   # EDT approx Mar-Oct, else EST


def stats(daily, start_ms):
    """daily: {day_ms: return}. Compounded metrics over the test period."""
    days = sorted(d for d in daily if d >= start_ms)
    if not days:
        return None
    rets = [daily[d] for d in days]
    eq, peak, mdd, curve = 1.0, 1.0, 0.0, []
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        curve.append(eq)
    n = len(rets)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, n - 1))
    years = {}
    for d, r in zip(days, rets):
        y = datetime.fromtimestamp(d / 1000, timezone.utc).year
        years[y] = years.get(y, 1.0) * (1 + r)
    half = n // 2
    h1 = math.prod(1 + r for r in rets[:half]) - 1
    h2 = math.prod(1 + r for r in rets[half:]) - 1
    return {"total": eq - 1, "cagr": eq ** (365 / n) - 1 if eq > 0 else -1.0,
            "sharpe": mean / sd * math.sqrt(365) if sd > 0 else 0.0, "mdd": mdd,
            "years": {y: v - 1 for y, v in sorted(years.items())}, "h1": h1, "h2": h2}


def book(daily, t_ms, r):
    d = t_ms // DAY * DAY
    daily[d] = daily.get(d, 0.0) + r


def trades_to_daily(trades, weight):
    """trades: (exit_ms, return). P&L booked on the exit day, notional = weight of equity."""
    daily = {}
    for t, r in trades:
        book(daily, t, weight * r)
    return daily


def trade_ret(side, entry, exit_, hours):
    r = (exit_ / entry - 1) * side - 2 * FEE_SIDE
    if side > 0:
        r -= FUND_8H * hours / 8
    return r


def fill_days(daily, start_ms, end_ms):
    d = start_ms // DAY * DAY
    while d <= end_ms:
        daily.setdefault(d, 0.0)
        d += DAY
    return daily


def add(*dailies):
    out = {}
    for dd in dailies:
        for k, v in dd.items():
            out[k] = out.get(k, 0.0) + v
    return out


# ---------------------------------------------------------------- 1. Donchian ensemble
def donchian_positions(D, both_sides):
    """Per day i: ensemble position in [-1, 1] held from close i to close i+1."""
    cl = [x["c"] for x in D]
    n = len(cl)
    ens = [0.0] * n
    for N in DONCHIAN_N:
        for side in ((1, -1) if both_sides else (1,)):
            inpos, stop = False, None
            for i in range(N, n):
                win = cl[i - N:i]                      # prior N closes
                hi, lo = max(win), min(win)
                w2 = cl[i - N + 1:i + 1]
                mid = (max(w2) + min(w2)) / 2
                if side > 0:
                    if not inpos and cl[i] > hi:
                        inpos, stop = True, mid
                    elif inpos:
                        stop = max(stop, mid)
                        if cl[i] < stop:
                            inpos = False
                else:
                    if not inpos and cl[i] < lo:
                        inpos, stop = True, mid
                    elif inpos:
                        stop = min(stop, mid)
                        if cl[i] > stop:
                            inpos = False
                if inpos:
                    ens[i] += side / len(DONCHIAN_N)
    return ens


def donchian_daily(coins, start_ms, both_sides=False, gate=None):
    """coins: {sym: daily candles}. Returns ({day: portfolio return}, avg gross exposure).
    gate(day_ms) -> False forces flat (regime switch)."""
    per = {}
    for sym, D in coins.items():
        if len(D) < 400:
            continue
        ens = donchian_positions(D, both_sides)
        cl = [x["c"] for x in D]
        rets = [0.0] + [cl[i] / cl[i - 1] - 1 for i in range(1, len(cl))]
        w = []
        for i in range(len(D)):
            if i < 365 or i < 90:
                w.append(0.0)
                continue
            win = rets[i - 89:i + 1]
            m = sum(win) / 90
            sd = math.sqrt(sum((r - m) ** 2 for r in win) / 89) * math.sqrt(365)
            scale = min(0.25 / sd, 2.0) if sd > 0 else 0.0
            pos = ens[i] * scale
            if gate is not None and not gate(D[i]["t"]):
                pos = 0.0
            w.append(pos)
        per[sym] = (D, w, rets)
    daily, expo = {}, []
    all_days = sorted({x["t"] for D, _, _ in per.values() for x in D})
    idx = {sym: {x["t"]: i for i, x in enumerate(D)} for sym, (D, _, _) in per.items()}
    for d in all_days:
        if d < start_ms - DAY:
            continue
        active = [s for s in per if d in idx[s] and idx[s][d] >= 365]
        if not active:
            continue
        k = 1 / len(active)
        tot, gross = 0.0, 0.0
        for s in active:
            D, w, rets = per[s]
            i = idx[s][d]
            if i + 1 >= len(D):
                continue
            wi, wprev = w[i] * k, (w[i - 1] * k if i > 0 else 0.0)
            tot += wi * rets[i + 1] - abs(wi - wprev) * FEE_SIDE
            if wi > 0:
                tot -= wi * FUND_8H * 3                     # 3 funding payments a day on longs
            gross += abs(wi)
        daily[d + DAY] = tot                                 # return earned on the next day
        expo.append(gross)
    return daily, (sum(expo) / len(expo) if expo else 0.0)


# ---------------------------------------------------------------- 2. multi-timeframe trend
def daily_index(C_daily):
    return [x["t"] for x in C_daily]


def last_closed_day(dts, t_ms):
    """Index of the last daily candle fully closed before hour t_ms."""
    return bisect.bisect_right(dts, t_ms - DAY) - 1


def mtf_macd_trades(C, D, variant, both_sides=False):
    """Quantpedia D1/H1 MACD. variant: 'pure', 'd1h1', 'd1h1_stop'."""
    cl = [x["c"] for x in C]
    m, s = macd(cl)
    dcl = [x["c"] for x in D]
    dm, ds = macd(dcl)
    dts = daily_index(D)
    out, pos, entry, et = [], 0, 0.0, 0
    for i in range(35, len(C) - 1):
        up_x = m[i] > s[i] and m[i - 1] <= s[i - 1]
        dn_x = m[i] < s[i] and m[i - 1] >= s[i - 1]
        k = last_closed_day(dts, C[i]["t"])
        d_up = k >= 35 and dm[k] > ds[k]
        d_dn = k >= 35 and dm[k] < ds[k]
        if pos != 0:
            exit_now = False
            if variant == "d1h1_stop":
                red = C[i]["c"] < C[i]["o"] if pos > 0 else C[i]["c"] > C[i]["o"]
                exit_now = red and C[i]["t"] > et
            else:
                exit_now = dn_x if pos > 0 else up_x
            if exit_now:
                out.append((C[i]["ct"], trade_ret(pos, entry, cl[i], (C[i]["ct"] - et) / HOUR)))
                pos = 0
        if pos == 0:
            if up_x and (variant == "pure" or d_up):
                pos, entry, et = 1, cl[i], C[i]["ct"]
            elif both_sides and dn_x and (variant == "pure" or d_dn):
                pos, entry, et = -1, cl[i], C[i]["ct"]
    return out


def pullback_trades(C, D):
    """Practitioner variant: daily close vs EMA50 = trend; 1h EMA20>EMA50 and a pullback candle that
    touches EMA20 and closes back beyond it; stop = 5-candle extreme -0.1 ATR, chandelier 3 ATR trail."""
    h = [x["h"] for x in C]; l = [x["l"] for x in C]; cl = [x["c"] for x in C]; o = [x["o"] for x in C]
    e20, e50 = ema(cl, 20), ema(cl, 50)
    A = bot.atr(h, l, cl, 14)
    dcl = [x["c"] for x in D]
    de50 = ema(dcl, 50)
    dts = daily_index(D)
    out, pos, stop, best, entry, et = [], 0, 0.0, 0.0, 0.0, 0
    for i in range(60, len(C)):
        a = A[i] or 0.0
        if pos != 0:
            hit = l[i] <= stop if pos > 0 else h[i] >= stop
            if hit:
                px = min(o[i], stop) if pos > 0 else max(o[i], stop)
                out.append((C[i]["ct"], trade_ret(pos, entry, px, (C[i]["ct"] - et) / HOUR)))
                pos = 0
            else:
                if pos > 0:
                    best = max(best, h[i]); stop = max(stop, best - 3 * a)
                else:
                    best = min(best, l[i]); stop = min(stop, best + 3 * a)
                continue
        k = last_closed_day(dts, C[i]["t"])
        if k < 50 or not a:
            continue
        if dcl[k] > de50[k] and e20[i] > e50[i] and l[i] <= e20[i] < cl[i]:
            pos, entry, et, best = 1, cl[i], C[i]["ct"], h[i]
            stop = min(l[i - 4:i + 1]) - 0.1 * a
        elif dcl[k] < de50[k] and e20[i] < e50[i] and h[i] >= e20[i] > cl[i]:
            pos, entry, et, best = -1, cl[i], C[i]["ct"], l[i]
            stop = max(h[i - 4:i + 1]) + 0.1 * a
    return out


# ---------------------------------------------------------------- 4. intraday 1h trend
def monday_asia(t_ms):
    """Sunday 19:00 -> Monday 19:00 New York time."""
    ny = datetime.fromtimestamp((t_ms - nyc_offset(t_ms) * HOUR) / 1000, timezone.utc)
    wd, hr = ny.weekday(), ny.hour
    return (wd == 6 and hr >= 19) or (wd == 0 and hr < 19)


def intraday_trades(C, both_sides=True, window=None):
    cl = [x["c"] for x in C]
    L = [6, 12, 24, 48, 96]
    out, pos, entry, et = [], 0, 0.0, 0
    for i in range(96, len(C)):
        sgn = sum((1 if cl[i] > cl[i - k] else -1 if cl[i] < cl[i - k] else 0) for k in L)
        want = 1 if sgn >= 3 else (-1 if sgn <= -3 and both_sides else 0)
        if window is not None and not window(C[i]["t"] + HOUR):   # position is held during the next hour
            want = 0
        if want != pos:
            if pos != 0:
                out.append((C[i]["ct"], trade_ret(pos, entry, cl[i], (C[i]["ct"] - et) / HOUR)))
            pos = want
            if pos != 0:
                entry, et = cl[i], C[i]["ct"]
    return out


# ---------------------------------------------------------------- SMC trades (current bot)
def smc_trades(C, H, sym, start_ms):
    out = []
    for x in bot.tag_btc(bot.find_smc_setups(C, H, bot.SMC_ENTRY), sym, None):
        if x["time"] < start_ms or not bot.smc_ok(x):
            continue
        side = x["side"]
        E = bot._px(x["entry"], side)
        risk = E - bot._px(x["sl"], side)
        if risk <= 0:
            continue
        px, idx = bot._exit_detail(C, x, side, "price", None, x["sl"])
        if px is None:
            continue
        fee_r = abs(x["entry"]) * bot.FEE_PCT / 100 / risk
        out.append((x["time"], C[idx]["ct"], (bot._px(px, side) - E) / risk - fee_r))
    return out


# ---------------------------------------------------------------- report
def fmt(name, st, extra=""):
    if st is None:
        return f"<b>{name}</b>: no data"
    yrs = " ".join(f"{y}:{v * 100:+.0f}%" for y, v in st["years"].items())
    return (f"<b>{name}</b>{extra}\n"
            f"CAGR {st['cagr'] * 100:+.1f}% | Sharpe {st['sharpe']:.2f} | max DD {st['mdd'] * 100:.1f}% | "
            f"1st half {st['h1'] * 100:+.1f}% / 2nd half {st['h2'] * 100:+.1f}%\n{yrs}")


def trade_line(tr):
    if not tr:
        return " (0 trades)"
    w = sum(1 for _, r in tr if r > 0)
    return f" ({len(tr)} trades, win {w / len(tr) * 100:.0f}%, avg {sum(r for _, r in tr) / len(tr) * 100:+.2f}%)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1000)
    ap.add_argument("--top-n", type=int, default=50)
    args = ap.parse_args()
    bot.DATA_SOURCE = "futures"
    bot.TOP_N = min(max(args.top_n, 1), 150)
    bot.resolve_symbols()
    days = max(args.days, 120)
    now = int(time.time() * 1000)
    start_ms = (now - days * DAY) // DAY * DAY
    need_h = (days + 420) * 24
    H1, D1, H4 = {}, {}, {}
    for sym in bot.SYMBOLS:
        try:
            C, src = bot.fetch_history(sym, "1h", need_h)
            H, _ = bot.fetch_history(sym, "4h", need_h // 4, source=src)
        except Exception as e:
            print("ERROR", sym, e)
            continue
        if len(C) < 24 * 60:
            continue
        H1[sym], H4[sym] = C, H
        D1[sym] = bot._aggregate(C, 24)
        print(f"{sym}: {len(C)} h, {len(D1[sym])} d")
    if "BTCUSDT" not in D1:
        bot.tg("⚠️ Trend lab: no BTC data")
        return
    n = len(H1)
    btc = {"BTCUSDT": D1["BTCUSDT"]}

    # BTC regime (daily ADX 14, last closed day)
    BD = D1["BTCUSDT"]
    badx = bot.adx([x["h"] for x in BD], [x["l"] for x in BD], [x["c"] for x in BD], 14)
    bdt = [x["t"] for x in BD]

    def trend_regime(t_ms, lvl=25):
        k = bisect.bisect_right(bdt, t_ms - DAY) - 1
        return k >= 0 and badx[k] is not None and badx[k] >= lvl

    # benchmark: BTC buy & hold
    bh = {}
    for i in range(1, len(BD)):
        bh[BD[i]["t"]] = BD[i]["c"] / BD[i - 1]["c"] - 1
    end_ms = BD[-1]["t"]
    rows = [fmt("Benchmark: BTC buy & hold", stats(fill_days(bh, start_ms, end_ms), start_ms))]

    # 1. Donchian
    d_long, ex1 = donchian_daily(D1, start_ms)
    d_ls, ex2 = donchian_daily(D1, start_ms, both_sides=True)
    d_btc, ex3 = donchian_daily(btc, start_ms)
    d_gate, ex4 = donchian_daily(D1, start_ms, gate=lambda t: trend_regime(t))
    s1 = []
    s1.append(fmt(f"1a. Donchian ensemble, long-only, top {n} (paper)", stats(fill_days(d_long, start_ms, end_ms), start_ms),
                  f" · avg exposure {ex1 * 100:.0f}%"))
    s1.append(fmt("1b. Donchian ensemble, BTC only", stats(fill_days(d_btc, start_ms, end_ms), start_ms),
                  f" · avg exposure {ex3 * 100:.0f}%"))
    s1.append(fmt(f"1c. Donchian ensemble, long + short, top {n}", stats(fill_days(d_ls, start_ms, end_ms), start_ms),
                  f" · avg exposure {ex2 * 100:.0f}%"))

    # 2. multi-timeframe (BTC per paper, then top-N)
    C, D = H1["BTCUSDT"], D1["BTCUSDT"]
    s2 = []
    for v, lbl in (("pure", "1h MACD only"), ("d1h1", "D1 filter + 1h MACD"), ("d1h1_stop", "D1 + 1h MACD + first red candle exit")):
        tr = [t for t in mtf_macd_trades(C, D, v) if t[0] >= start_ms]
        s2.append(fmt(f"2a. BTC {lbl} (paper)", stats(fill_days(trades_to_daily(tr, 1.0), start_ms, end_ms), start_ms), trade_line(tr)))
    tr_all = []
    for sym in H1:
        tr_all += [t for t in mtf_macd_trades(H1[sym], D1[sym], "d1h1_stop", both_sides=True) if t[0] >= start_ms]
    s2.append(fmt(f"2b. D1 + 1h MACD + red-candle exit, long+short, top {n}",
                  stats(fill_days(trades_to_daily(tr_all, 1 / n), start_ms, end_ms), start_ms), trade_line(tr_all)))
    pb = []
    for sym in H1:
        pb += [t for t in pullback_trades(H1[sym], D1[sym]) if t[0] >= start_ms]
    s2.append(fmt(f"2c. Daily EMA50 trend + 1h EMA20 pullback, chandelier exit, top {n}",
                  stats(fill_days(trades_to_daily(pb, 1 / n), start_ms, end_ms), start_ms), trade_line(pb)))

    # 4. intraday 1h trend
    s4 = []
    for lbl, sides, win in (("all hours, long+short", True, None), ("Monday Asia window only, long+short", True, monday_asia),
                            ("Monday Asia window only, long-only", False, monday_asia)):
        tr = [t for t in intraday_trades(C, sides, win) if t[0] >= start_ms]
        s4.append(fmt(f"4. BTC 1h trend, {lbl}", stats(fill_days(trades_to_daily(tr, 1.0), start_ms, end_ms), start_ms), trade_line(tr)))
    tr4 = []
    for sym in H1:
        tr4 += [t for t in intraday_trades(H1[sym], True, monday_asia) if t[0] >= start_ms]
    s4.append(fmt(f"4. 1h trend, Monday Asia window, long+short, top {n}",
                  stats(fill_days(trades_to_daily(tr4, 1 / n), start_ms, end_ms), start_ms), trade_line(tr4)))

    # 3. regime switching with the current SMC bot (1% risk per trade)
    smc = []
    for sym in H1:
        try:
            smc += smc_trades(H1[sym], H4[sym], sym, start_ms)
        except Exception as e:
            print("smc error", sym, e)
    rng = [(c, r) for o, c, r in smc if not trend_regime(o)]
    trd = [(c, r) for o, c, r in smc if trend_regime(o)]
    allr = [(c, r) for o, c, r in smc]

    def rline(lbl, tr):
        if not tr:
            return f"{lbl}: 0 trades"
        w = sum(1 for _, r in tr if r > 0)
        return f"{lbl}: {len(tr)} trades, win {w / len(tr) * 100:.0f}%, {sum(r for _, r in tr) / len(tr):+.2f}R/trade, total {sum(r for _, r in tr):+.1f}R"

    smc_all_d = trades_to_daily(allr, 0.01)
    smc_rng_d = trades_to_daily(rng, 0.01)
    s3 = ["<b>3. Regime switch</b> (BTC daily ADX ≥25 = trend, else range). SMC = current bot filters "
          f"({bot.smc_quality_txt()}), 1% risk per trade.",
          rline("SMC all", allr), rline("SMC in range regime", rng), rline("SMC in trend regime", trd),
          fmt("SMC alone (all regimes)", stats(fill_days(dict(smc_all_d), start_ms, end_ms), start_ms)),
          fmt("SMC only in range regime", stats(fill_days(dict(smc_rng_d), start_ms, end_ms), start_ms)),
          fmt("Donchian only in trend regime", stats(fill_days(dict(d_gate), start_ms, end_ms), start_ms),
              f" · avg exposure {ex4 * 100:.0f}%"),
          fmt("COMBO: SMC (range) + Donchian (trend)", stats(fill_days(add(smc_rng_d, d_gate), start_ms, end_ms), start_ms)),
          fmt("COMBO: SMC (all) + Donchian long-only (all)", stats(fill_days(add(smc_all_d, d_long), start_ms, end_ms), start_ms))]

    head = (f"🧪 <b>Trend lab</b> – last {days} days, {n} coins (today's top {bot.TOP_N}, futures data)\n"
            "Costs: 0.05% per side, longs pay 0.01%/8h funding. Survivorship bias: dead coins not included. "
            "Report only – live bot unchanged.")
    parts = [head, "\n\n".join(rows + s1), "\n\n".join(s2), "\n\n".join(s4), "\n\n".join(s3)]
    msg = ""
    for p in parts:
        if len(msg) + len(p) > 3800:
            bot.tg(msg)
            msg = ""
        msg += ("\n\n" if msg else "") + p
    if msg:
        bot.tg(msg)
    full = "\n\n".join(parts)
    print(full)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as fh:
            fh.write("## Trend lab\n\n" + re.sub(r"</?b>", "**", full).replace("\n", "  \n") + "\n")


if __name__ == "__main__":
    main()
