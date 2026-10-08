"""ICT engine - three textbook ICT models, tested on 4 years (report only - changes nothing live).

Every rule below is fixed BEFORE running (no tuning), so all 4 years are out-of-sample.

Clock     New York time (DST aware). An ICT day runs 00:00-24:00 New York.
Levels    PDH / PDL = previous NY day high / low. Asian range = 20:00-24:00 NY of the previous evening.
          London range = 02:00-05:00 NY.
Bias      Daily bias from yesterday: close above the day-before's high -> bullish, close below its low ->
          bearish, otherwise no bias. Each model runs twice: "bias" (trade only with the bias) and "any"
          (no bias; the side comes from which liquidity was swept).
Chart     15m candles, 1h-equivalent ATR not used; ATR14 on 15m. Swing = 2-candle fractal, confirmed 2 candles later.
Setup     (long; short is the mirror)
          1. inside the model window price trades BELOW the liquidity level (sweep of sell-side liquidity)
          2. within 12 candles a candle CLOSES above the last confirmed swing high formed before the sweep (MSS)
          3. the displacement leg (sweep -> MSS, or the candle right after the MSS) left a bullish FVG;
             entry = limit at its 50% (consequent encroachment), placed after that candle closes
          4. SL = sweep low - 0.1 ATR. Skip if SL < 0.3% (fees) or > 3%.
          5. TP = the model's draw on liquidity; skip if it is < 1.5R away.
          6. Order valid 2 hours after the window ends; cancelled if TP trades first. Open trades close at
             16:00 NY (time stop). SL and TP in the same candle = SL (conservative). One trade per model/coin/day/side.
          The liquidity level must still be untouched when the window opens (a fresh sweep).
Models    M1 London Judas : window 02:00-05:00, sweep the Asian range, target PDH / PDL
          M2 NY AM        : window 07:00-10:00, sweep the London range, target PDH / PDL
          M3 Silver Bullet: window 10:00-11:00, no sweep needed: first FVG formed in the window,
                            SL beyond the FVG's first candle, target the day's high/low so far, else PDH / PDL
Costs     0.10% round trip, R is net of fees (1R = $1 at $1 risk).
Pass      total R > 0, profitable in at least 3 of the 4 years, at least 100 trades.
Coins     20 large coins that traded on Binance spot for all 4 years (avoids survivorship bias).
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

NY = ZoneInfo("America/New_York")
YEARS = 4
COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
         "DOTUSDT", "LTCUSDT", "BCHUSDT", "TRXUSDT", "XLMUSDT", "ETCUSDT", "ATOMUSDT", "FILUSDT", "NEARUSDT",
         "UNIUSDT", "AAVEUSDT"]
FEE = 0.10
MIN_SL, MAX_SL, MIN_RR = 0.3, 3.0, 1.5
MSS_BARS = 12
MODELS = [  # name, window start/end (minutes after NY midnight), liquidity range to sweep, target
    ("M1 London Judas", 2 * 60, 5 * 60, "asia", "pd"),
    ("M2 NY AM", 7 * 60, 10 * 60, "london", "pd"),
    ("M3 Silver Bullet", 10 * 60, 11 * 60, None, "day"),
]


def atr14(C):
    out, s, trs = [], 0.0, []
    for i, x in enumerate(C):
        tr = x["h"] - x["l"] if i == 0 else max(x["h"], C[i - 1]["c"]) - min(x["l"], C[i - 1]["c"])
        trs.append(tr)
        s += tr
        if i >= 14:
            s -= trs[i - 14]
        out.append(s / min(i + 1, 14))
    return out


def swings(C):
    """sh[i] / sl[i] = True if candle i is a 2-candle fractal high / low (known only at i+2)."""
    n = len(C)
    sh, sl = [False] * n, [False] * n
    for i in range(2, n - 2):
        h, l = C[i]["h"], C[i]["l"]
        sh[i] = h > C[i - 1]["h"] and h > C[i - 2]["h"] and h >= C[i + 1]["h"] and h >= C[i + 2]["h"]
        sl[i] = l < C[i - 1]["l"] and l < C[i - 2]["l"] and l <= C[i + 1]["l"] and l <= C[i + 2]["l"]
    return sh, sl


def mirror(C):
    """Price-flipped copy so short setups can reuse the long logic (high <-> -low)."""
    return [dict(x, o=-x["o"], h=-x["l"], l=-x["h"], c=-x["c"]) for x in C]


def simulate(C, start, expiry, stop_i, entry, sl, tp):
    """Long-side fill + exit on candles start.. -> (R before fees) or None if never filled."""
    risk = entry - sl
    n = len(C)
    i = start
    while i < n and i <= expiry:
        x = C[i]
        if x["l"] <= entry:
            break
        if x["h"] >= tp:
            return None                                        # target traded first: cancelled
        i += 1
    else:
        return None
    if C[i]["l"] <= sl:
        return -1.0
    for j in range(i + 1, min(n, stop_i + 1)):
        x = C[j]
        if x["l"] <= sl:
            return -1.0
        if x["h"] >= tp:
            return (tp - entry) / risk
    last = C[min(n - 1, stop_i)]["c"]
    return (last - entry) / risk


def long_setups(C, A, SH, days, model, bias_mode, side_bias):
    """All long trades for one model on one coin. C may be a mirrored series (then they are shorts)."""
    name, w0, w1, liq, tgt = model
    out = []
    for d in days:
        if bias_mode == "bias" and d["bias"] != side_bias:
            continue
        idx = d["idx"]                                         # candle indices of this NY day (in order)
        win = [i for i in idx if w0 <= d["mod"][i] < w1]
        if not win:
            continue
        stop_i = next((i for i in idx if d["mod"][i] >= 16 * 60), idx[-1])
        expiry = next((i for i in idx if d["mod"][i] >= w1 + 120), stop_i)
        if tgt == "pd":
            target = d["pdh"]
        else:
            target = None
        if liq is not None:
            level = d[liq + "_low"]
            if level is None:
                continue
            ready = 0 if liq == "asia" else 5 * 60                 # the level must still be untouched when the window opens
            if any(C[i]["l"] < level for i in idx if ready <= d["mod"][i] < w0):
                continue
            sweep = next((i for i in win if C[i]["l"] < level), None)
            if sweep is None:
                continue
            ref = next((k for k in range(sweep - 3, max(0, sweep - 48), -1) if SH[k]), None)  # confirmed by sweep-1
            if ref is None:
                continue
            mss = next((m for m in range(sweep, min(len(C), sweep + MSS_BARS + 1))
                        if C[m]["c"] > C[ref]["h"]), None)
            if mss is None:
                continue
            low = min(C[k]["l"] for k in range(sweep, mss + 1))
            fvg = None
            last = min(mss + 1, len(C) - 1)                    # the gap may complete on the candle after the MSS
            for i in range(last, sweep + 1, -1):               # latest bullish FVG inside the displacement leg
                if C[i]["l"] > C[i - 2]["h"] and i - 2 >= sweep:
                    fvg = i
                    break
            if fvg is None:
                continue
            entry = (C[fvg]["l"] + C[fvg - 2]["h"]) / 2
            sl = low - 0.1 * A[mss]
            go = max(mss, fvg) + 1
        else:                                                  # Silver Bullet: first bullish FVG formed in the window
            fvg = next((i for i in win if i >= 2 and C[i]["l"] > C[i - 2]["h"]), None)
            if fvg is None:
                continue
            entry = (C[fvg]["l"] + C[fvg - 2]["h"]) / 2
            sl = C[fvg - 2]["l"] - 0.1 * A[fvg]
            go = fvg + 1
            so_far = max(C[k]["h"] for k in idx if k <= fvg)
            target = so_far if so_far - entry >= MIN_RR * (entry - sl) else d["pdh"]
        risk = entry - sl
        if risk <= 0 or target is None:
            continue
        slp = risk / abs(entry) * 100
        if not MIN_SL <= slp <= MAX_SL or target - entry < MIN_RR * risk:
            continue
        r = simulate(C, go, expiry, stop_i, entry, sl, target)
        if r is None:
            continue
        out.append({"t": C[go - 1]["ct"], "r": r - FEE / slp, "rr": (target - entry) / risk})
    return out


def day_table(C):
    """Per NY day: candle indices, minute-of-day per index, PDH/PDL, Asian/London lows, bias (+1/-1/0).
    On a mirrored (price-flipped) series the same code gives the short-side values: PDH = -PDL, bias +1 = bearish."""
    by, mod = {}, {}
    for i, x in enumerate(C):
        ny = datetime.fromtimestamp(x["t"] / 1000, NY)
        by.setdefault(ny.date(), []).append(i)
        mod[i] = ny.hour * 60 + ny.minute
    dates = sorted(by)
    hl = {d: (max(C[i]["h"] for i in by[d]), min(C[i]["l"] for i in by[d]), C[by[d][-1]]["c"]) for d in dates}
    days = []
    for k in range(2, len(dates)):
        d, d1, d2 = dates[k], dates[k - 1], dates[k - 2]
        if len(by[d]) < 80 or len(by[d1]) < 80:                # skip days with missing data
            continue
        h1, l1, c1 = hl[d1]
        h2, l2, _ = hl[d2]
        bias = 1 if c1 > h2 else -1 if c1 < l2 else 0
        asia = [i for i in by[d1] if mod[i] >= 20 * 60]
        london = [i for i in by[d] if 2 * 60 <= mod[i] < 5 * 60]
        days.append({"date": d, "idx": by[d], "mod": mod, "pdh": h1, "bias": bias,
                     "asia_low": min(C[i]["l"] for i in asia) if len(asia) >= 12 else None,
                     "london_low": min(C[i]["l"] for i in london) if len(london) >= 10 else None})
    return days


def run_coin(C):
    A = atr14(C)
    res = {}
    for side, CC in (("LONG", C), ("SHORT", mirror(C))):
        SH, _ = swings(CC)
        AA = A                                                 # ATR is the same for the mirrored series
        days = day_table(CC)
        for model in MODELS:
            for bm in ("bias", "any"):
                ts = long_setups(CC, AA, SH, days, model, bm, 1)
                for t in ts:
                    t["side"] = side
                res.setdefault((model[0], bm), []).extend(ts)
    return res


def stats(ts):
    ts = sorted(ts, key=lambda t: t["t"])
    n = len(ts)
    tot = sum(t["r"] for t in ts)
    eq = peak = dd = 0.0
    streak = worst = 0
    for t in ts:
        eq += t["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if t["r"] < 0 else 0
        worst = max(worst, streak)
    win = sum(1 for t in ts if t["r"] > 0)
    return n, tot, (win / n * 100 if n else 0.0), dd, worst


def main():
    t0 = time.time()
    bot.DATA_SOURCE = "spot"
    now = time.time() * 1000
    start = now - YEARS * 365.25 * 86400000
    total = int(YEARS * 365.25 * 96) + 200
    allres, used = {}, []
    for sym in COINS:
        try:
            C, _ = bot.fetch_history(sym, "15m", total)
        except Exception as e:
            print(sym, "data error", e, flush=True)
            continue
        if not C or C[0]["t"] > start + 30 * 86400000:
            print(sym, "skipped: history shorter than 4 years", flush=True)
            continue
        used.append(sym)
        for k, ts in run_coin(C).items():
            allres.setdefault(k, []).extend(t for t in ts if t["t"] >= start)
        print(f"{sym}: {len(C)} candles, " + ", ".join(f"{k[0][:2]}/{k[1]} {len(v)}" for k, v in allres.items()), flush=True)
    yb = [start + i * 365.25 * 86400000 for i in range(YEARS + 1)]
    yname = [f"{datetime.fromtimestamp(yb[i] / 1000, timezone.utc):%b %y}" for i in range(YEARS)]
    out = [f"## ICT engine - 3 textbook models, {len(used)} coins, 15m spot, last {YEARS} years (rules fixed, no tuning)\n",
           f"Coins: {', '.join(s[:-4] for s in used)}\n",
           "| Model | Bias | " + " | ".join(f"Year from {y}" for y in yname) + " | Total | Trades | Win % | Avg R | Max DD | Streak | Pass? |",
           "|---|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|"]
    tg = [f"🧠 <b>ICT engine test</b> ({len(used)} coins, 15m, {YEARS} years, rules fixed - no tuning)",
          "Pass = total &gt; 0, profitable in ≥3 of 4 years, ≥100 trades"]
    for (name, bm), ts in sorted(allres.items()):
        ys = [[t for t in ts if yb[i] <= t["t"] < yb[i + 1]] for i in range(YEARS)]
        yr = [sum(t["r"] for t in y) for y in ys]
        n, tot, win, dd, streak = stats(ts)
        ok = tot > 0 and sum(1 for r in yr if r > 0) >= 3 and n >= 100
        longs = sum(t["r"] for t in ts if t["side"] == "LONG")
        out.append(f"| {name} | {bm} | " + " | ".join(f"{len(y)} / {r:+.1f}R" for y, r in zip(ys, yr))
                   + f" | {tot:+.1f}R | {n} | {win:.0f}% | {tot / n if n else 0:+.2f} | −{dd:.1f}R | {streak} | {'✅' if ok else '❌'} |")
        tg.append(f"{'✅' if ok else '❌'} <b>{name}</b> ({bm}): {n} trades, total {tot:+.1f}R, win {win:.0f}%, "
                  f"DD −{dd:.0f}R\n   years: " + " · ".join(f"{r:+.0f}" for r in yr)
                  + f" | longs {longs:+.0f}R, shorts {tot - longs:+.0f}R")
    out.append("\nCaveats: spot prices (futures differ slightly); funding/slippage not modelled; limit fills assume "
               "price touching the level fills the order.")
    tg.append(f"Report only, nothing changed live. {(time.time() - t0) / 60:.0f} min. Full table: Actions → run summary.")
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
