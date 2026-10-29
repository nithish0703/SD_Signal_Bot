"""SMC hybrid backtest (report only - changes nothing live): SMC signal -> MARKET entry -> trend-style trailing exit.

Why: live SMC signals often never fill (limit at the FVG, price runs away). Idea (variant E): enter at market as soon
as the signal is known, keep the SMC stop, no fixed TP, exit with the trend strategy's chandelier trail, and only take
signals that agree with the 4h EMA50/200 trend.

Signals  = exactly the live bot's SMC setups (main branch: 1h candles, 4h HTF, FVG 50% entry, discount rule,
           SL >= 1%). The bot drops setups whose limit order never fills; `_signals_long` below is a verbatim copy of
           bot._smc_long that keeps them (checked at run time: its filled setups must equal the bot's, coin by coin).
Coins    = point-in-time top 50 by the previous day's $ volume (pool: today's top 150), 4 years of 1h SPOT candles
           (same data as smc_oos.py).
Filters  = LIVE: main's smc_filters.json (killzone, London/NY, target >= 1R, SL <= 4%, sweep <= 1 ATR, sweep candle
           closes back, MSS <= 6 candles, fill <= 3 candles). Market variants use the same signal filters except the
           fill wait (no limit order) and measure SL % from the market entry. CORE: no quality filters.
Variants (only one thing changes at a time):
  A  live today: limit at FVG 50%, SL, liquidity TP (else 2R)                 - bot's own simulator, 0.10% fees
  B  market entry at the next candle open, same SL, same TP
  C  market entry, SL, NO TP: chandelier trail = best 1h close - 3 x ATR14(1h), exit on a 1h close beyond it
     (next open); exchange stop = SL, moved to trail - 1.5 ATR when the trail improves >= 0.5 ATR (as the trend bot)
  E  C + only signals with the 4h EMA50/200 trend (long: EMA50 > EMA200, short: below) at the signal candle
Costs    market variants: 0.10% fees + 0.05% slippage on every market / stop fill. One position per coin at a time.
Rule fixed BEFORE running: E counts as better than A only if E is profitable in BOTH halves (years 1-2, 3-4) and
beats A by >= 5R in BOTH halves. Checked for LIVE and CORE filters.
"""
import bisect
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
from bot import (ATR_LEN, EMA_LEN, PIVOT_LEN, SMC_FILL_MAX, SMC_MSS_MAX, SMC_SL_BUFFER_ATR,  # noqa: E402,F401
                 SMC_SWEEP_MAX, adx, atr, chop, ema, pivots)

YEARS = 4
POOL = int(os.getenv("OOS_POOL", "150"))
TOP = 50
DAY = 86400000
YEAR_MS = 365.25 * DAY
SLIP = 0.0005                  # 0.05% per market / stop fill
TRAIL_ATR, EM_ATR, MOVE_ATR = 3.0, 1.5, 0.5
MIN_GAIN = 5.0
LIVE = {"SMC_KILLZONE": 1, "SMC_LDN_NY": 1, "SMC_TGT_1R": 1, "SMC_MAX_SL": 4, "SMC_MAX_DEPTH": 1,
        "SMC_SWEEP_REJECT": 1, "SMC_MSS_FAST": 6, "SMC_MAX_FILL": 3}       # main's smc_filters.json (Oct 2026)
QFN = {var: fn for _, var, _, fn in bot.SMC_QUALITY}


def _signals_long(C, H, entry_mode):
    """VERBATIM copy of bot._smc_long (main branch) up to the limit-order fill check; only the end differs:
    it returns EVERY setup (filled or not) with its signal candle `sig`. Checked at run time against the bot."""
    n = len(C)
    h = [x["h"] for x in C]; l = [x["l"] for x in C]; cl = [x["c"] for x in C]
    A = atr(h, l, cl, ATR_LEN)
    ADX = adx(h, l, cl)
    CH = chop(h, l, cl)
    hc = [x["c"] for x in H]
    he = ema(hc, EMA_LEN)
    hct = [x["ct"] for x in H]
    HADX = adx([x["h"] for x in H], [x["l"] for x in H], hc)
    o = [x["o"] for x in C]
    vol = [x.get("v", 0.0) for x in C]
    tbv = [x.get("tb") for x in C]
    atrp = [(A[i] / cl[i]) if A[i] and cl[i] else None for i in range(n)]
    hl = [x["l"] for x in H]
    HA = atr([x["h"] for x in H], hl, hc, ATR_LEN)
    hpl = pivots(hl, PIVOT_LEN, False)                    # 4h swing lows (for the HTF POI check)
    lows = pivots(l, PIVOT_LEN, False)
    dlow = {}                                             # UTC day -> lowest low (previous-day liquidity)
    for x in C:
        dd = x["t"] // 86400000
        dlow[dd] = min(dlow.get(dd, x["l"]), x["l"])
    t_idx = {x["t"]: i for i, x in enumerate(C)}
    out, used = [], set()
    for p, L in lows:
        # 1. sweep: first candle that trades below the swing low
        s = next((k for k in range(p + PIVOT_LEN + 1, min(n, p + SMC_SWEEP_MAX)) if l[k] < L), None)
        if s is None or s in used:
            continue
        sweep_low, rec = l[s], None
        for k in range(s, min(n, s + 3)):                 # reclaim within 3 candles
            sweep_low = min(sweep_low, l[k])
            if cl[k] > L:
                rec = k
                break
        if rec is None:
            continue
        # 2. MSS: close above the high between the swing low and the sweep
        level = max(h[p + 1:s])
        m = None
        for k in range(rec, min(n, s + SMC_MSS_MAX)):
            if k > rec and l[k] < sweep_low:
                break                                     # new low: setup failed
            if cl[k] > level:
                m = k
                break
        if m is None:
            continue
        # 3. FVG inside the displacement leg (latest one up to the MSS candle)
        fv = next((k for k in range(m, s, -1) if k + 1 < n and l[k + 1] > h[k - 1]), None)
        if fv is None:
            continue
        zb, zt = h[fv - 1], l[fv + 1]
        leg_high = max(h[s:m + 1])
        rng_leg = leg_high - sweep_low
        ote_lo, ote_hi = leg_high - 0.79 * rng_leg, leg_high - 0.62 * rng_leg   # ICT OTE zone (62-79%)
        ob = next((k for k in range(m - 1, s - 1, -1) if cl[k] < o[k]), None)  # last down candle before MSS
        if entry_mode == "top":
            entry = zt
        elif entry_mode == "ote":
            entry = leg_high - 0.705 * rng_leg
        elif entry_mode == "ob":
            if ob is None:
                continue
            entry = (o[ob] + cl[ob]) / 2                  # order block mean threshold (body 50%)
        else:
            entry = (zb + zt) / 2
        a = A[m] or A[s]
        if not a:
            continue
        sl = sweep_low - SMC_SL_BUFFER_ATR * a
        risk = entry - sl
        if risk <= 0:
            continue
        # limit order fill (cancel if price runs 2R away first or the order expires)
        start = max(m, fv + 1) + 1
        f, cancelled = None, False
        for k in range(start, min(n, start + SMC_FILL_MAX)):
            if l[k] <= entry:
                f = k
                break
            if h[k] >= entry + 2 * risk:
                cancelled = True
                break
        prev_high = max(h[max(0, s - 48):s])              # external liquidity above
        # ---- quality features (all known at signal time)
        q = {
            "disp": abs(cl[m] - o[m]) / a,                               # MSS candle body in ATRs
            "fvg_atr": (zt - zb) / a,                                    # FVG size in ATRs
            "eq": any(abs(v2 - L) <= 0.15 * a for i2, v2 in lows if p - 48 <= i2 < p),   # equal lows swept
            "session": 7 <= (C[s]["t"] // 3600000) % 24 < 20,            # London / New York hours (UTC)
            "vol": (vol[s] / (sum(vol[s - 20:s]) / 20)) if s >= 20 and sum(vol[s - 20:s]) > 0 else 0.0,
        }
        rng = h[m] - l[m]
        q["mss_close"] = (cl[m] - l[m]) / rng if rng > 0 else 0.0         # 1 = MSS closed at its high
        q["mss_margin"] = (cl[m] - level) / a                            # how far the close broke the level
        q["sweep_depth"] = (L - sweep_low) / a                           # how deep the sweep went (ATRs)
        q["sweep_rej"] = rec == s                                        # sweep candle itself closed back above
        q["mss_vol"] = (vol[m] / (sum(vol[m - 20:m]) / 20)) if m >= 20 and sum(vol[m - 20:m]) > 0 else 0.0
        q["mss_bars"] = m - s                                            # candles from sweep to MSS
        q["reclaim_bars"] = rec - s                                      # 0 = the sweep candle itself closed back in
        sg = max(m, fv + 1)                                              # signal candle (setup known)
        q["adx_sig"] = ADX[sg] or 0.0
        q["chop_sig"] = CH[sg] if CH[sg] is not None else 100.0
        k5 = bisect.bisect_right(hct, C[sg]["ct"]) - 1
        # order flow (taker volume): who was aggressive during the sweep and on the MSS candle
        sv = sum(vol[s:rec + 1])
        stb = [tbv[i] for i in range(s, rec + 1)]
        q["sweep_sell"] = (1 - sum(stb) / sv) if sv > 0 and None not in stb else None
        q["mss_buy"] = (tbv[m] / vol[m]) if vol[m] > 0 and tbv[m] is not None else None
        q["sweep_hour"] = (C[s]["t"] // 3600000) % 24
        q["sig_hour"] = (C[sg]["t"] // 3600000) % 24                     # hour the signal is sent (UTC)
        # ---- ICT features
        pdl = dlow.get(C[s]["t"] // 86400000 - 1)
        q["pdl"] = pdl is not None and sweep_low < pdl < cl[rec]         # took previous day's low and reclaimed it
        q["killzone"] = q["sweep_hour"] in (6, 7, 8, 12, 13, 14)          # London 02-05 / NY AM 08-11 New York time
        mon = datetime.fromtimestamp(C[sg]["t"] / 1000, timezone.utc).month
        mh = 4 if 3 <= mon <= 10 else 5                                  # New York midnight in UTC (EDT / EST)
        mt = (C[sg]["t"] // 86400000) * 86400000 + mh * 3600000
        if C[sg]["t"] < mt:
            mt -= 86400000
        mi = t_idx.get(mt)
        q["below_mo"] = mi is None or entry < o[mi]                      # long below NY midnight open = discount of the day
        q["ote_fvg"] = zb <= ote_hi and zt >= ote_lo                     # FVG sits inside the OTE zone
        q["has_ob"] = ob is not None
        q["ob_fvg"] = ob is not None and l[ob] <= zt and h[ob] >= zb       # order block overlaps the FVG (confluence)
        q["weekend"] = datetime.fromtimestamp(C[sg]["ct"] / 1000, timezone.utc).weekday() >= 5
        win = [v2 for v2 in atrp[max(0, sg - 720):sg + 1] if v2 is not None]   # ~30 days of 1h
        q["atr_pct"] = (sum(1 for v2 in win if v2 <= atrp[sg]) / len(win) * 100) if win and atrp[sg] else 50.0
        q["trend_sig"] = k5 >= 0 and he[k5] is not None and hc[k5] > he[k5]
        k4 = bisect.bisect_right(hct, C[s]["ct"]) - 1
        ha = HA[k4] if k4 >= 0 and HA[k4] else a
        q["htf_poi"] = any(abs(v2 - sweep_low) <= 0.5 * ha for i2, v2 in hpl
                           if k4 - 60 <= i2 and i2 + PIVOT_LEN <= k4)    # sweep at a confirmed 4h swing low
        # ---- CHANGED vs bot._smc_long: every setup is returned, filled or not (the bot drops unfilled ones).
        # `used` is updated exactly as in the bot (only filled setups), so the filled ones stay identical.
        kk = bisect.bisect_right(hct, C[f]["ct"]) - 1 if f is not None else bisect.bisect_right(hct, C[sg]["ct"]) - 1
        rec_ = {
            "pending": False, "sweep_t": C[s]["t"], "sweep_i": s, "sig": sg, "f": f, "cancelled": cancelled,
            "e": f, "entry": entry, "sl": sl, "atr": (A[f] or a) if f is not None else (A[sg] or a),
            "atr_sig": A[sg] or a, "time": C[f]["ct"] if f is not None else C[sg]["ct"], "sig_time": C[sg]["ct"],
            "score": 6, "liq": prev_high if prev_high > entry + 0.5 * risk else None,
            "fill_loss": (l[f] <= sl) if f is not None else False,
            "trend": kk >= 0 and he[kk] is not None and hc[kk] > he[kk],
            "hadx": HADX[kk] if kk >= 0 and HADX[kk] is not None else 0.0,
            "adx": (ADX[f] or 0.0) if f is not None else (ADX[sg] or 0.0),
            "chop": (CH[f] if CH[f] is not None else 100.0) if f is not None else (CH[sg] if CH[sg] is not None else 100.0),
            "discount": entry <= (sweep_low + leg_high) / 2, "fill_wait": (f - start) if f is not None else None, **q,
        }
        ks = bisect.bisect_right(hct, C[sg]["ct"]) - 1             # signal-time values for the market variants
        rec_["at_sig"] = {"adx": ADX[sg] or 0.0, "chop": CH[sg] if CH[sg] is not None else 100.0,
                          "hadx": HADX[ks] if ks >= 0 and HADX[ks] is not None else 0.0,
                          "trend": ks >= 0 and he[ks] is not None and hc[ks] > he[ks],
                          "atr": A[sg] or a, "time": C[sg]["ct"], "e": sg}
        if f is not None:
            used.add(s)
        out.append(rec_)
    return out


def all_signals(C, H, mode):
    """Both sides, same post-processing as bot.find_smc_setups (target = liquidity, else 2R)."""
    longs = [dict(x, side="LONG") for x in _signals_long(C, H, mode)]
    shorts = []
    for x in _signals_long(bot.flip(C), bot.flip(H), mode):
        x = dict(x, side="SHORT", entry=-x["entry"], sl=-x["sl"])
        x["liq"] = -x["liq"] if x["liq"] is not None else None
        shorts.append(x)
    for x in longs + shorts:
        risk = abs(x["entry"] - x["sl"])
        x["tp"] = x["liq"] if x["liq"] is not None else (
            x["entry"] + 2 * risk if x["side"] == "LONG" else x["entry"] - 2 * risk)
        x["tgt_r"] = abs(x["tp"] - x["entry"]) / risk
        x.setdefault("btc_ok", True)
    return longs + shorts


def detector_check(C, H, mode, sigs):
    """The filled signals must be exactly the bot's setups. -> (bot count, mismatches)."""
    key = lambda x: (x["side"], x["e"], round(x["entry"], 12), round(x["sl"], 12), x["time"])
    ref = sorted(key(x) for x in bot.find_smc_setups(C, H, mode))
    mine = sorted(key(x) for x in sigs if x["f"] is not None)
    return len(ref), (0 if ref == mine else max(1, len(set(ref) ^ set(mine))))


def quality_ok(t, active, skip=()):
    return all(QFN[var](t, val) for var, val in active.items() if var not in skip)


def market_trade(C, A1, t, variant, e50=None, e200=None, hct=None):
    """Market entry at the open after the signal candle. -> dict or None (not tradable)."""
    i0 = t["sig"] + 1
    if i0 >= len(C):
        return None
    s = 1 if t["side"] == "LONG" else -1
    entry = C[i0]["o"] * (1 + s * SLIP)
    sl = t["sl"]
    risk = s * (entry - sl)
    if risk <= 0:
        return None
    if variant == "E":
        k4 = bisect.bisect_right(hct, C[t["sig"]]["ct"]) - 1
        if k4 < 0 or e50[k4] is None or e200[k4] is None or (e50[k4] - e200[k4]) * s <= 0:
            return None
    ex, xi, why = None, None, None
    if variant == "B":
        tp = t["tp"]
        if s * (tp - entry) <= 0:
            return None
        for k in range(i0, len(C)):
            x = C[k]
            if (s > 0 and x["l"] <= sl) or (s < 0 and x["h"] >= sl):
                ex, xi, why = (min(x["o"], sl) if s > 0 else max(x["o"], sl)) * (1 - s * SLIP), k, "SL"
                break
            if (s > 0 and x["h"] >= tp) or (s < 0 and x["l"] <= tp):
                ex, xi, why = tp, k, "TP"
                break
    else:
        a0 = A1[t["sig"]] or t["atr_sig"]
        best, note, em = C[i0]["o"], C[i0]["o"] - s * TRAIL_ATR * a0, sl
        for k in range(i0, len(C)):
            x = C[k]
            if (s > 0 and x["l"] <= em) or (s < 0 and x["h"] >= em):
                ex, xi, why = (min(x["o"], em) if s > 0 else max(x["o"], em)) * (1 - s * SLIP), k, \
                    ("SL" if em == sl else "emergency stop")
                break
            a = A1[k] or a0
            best = max(best, x["c"]) if s > 0 else min(best, x["c"])
            trail = best - s * TRAIL_ATR * a
            if ((s > 0 and x["c"] < trail) or (s < 0 and x["c"] > trail)) and k + 1 < len(C):
                ex, xi, why = C[k + 1]["o"] * (1 - s * SLIP), k + 1, "trail"
                break
            if (trail - note) * s >= MOVE_ATR * a:
                note = trail
                lvl = trail - s * EM_ATR * a
                em = max(em, lvl) if s > 0 else min(em, lvl)
    if ex is None:                                          # still open: value at the last close
        ex, xi, why = C[-1]["c"], len(C) - 1, "open"
    slp = risk / entry * 100
    r = s * (ex - entry) / risk - bot.FEE_PCT / slp
    return {"r": r, "time": C[i0]["t"], "exit_i": xi, "hold": xi - i0, "why": why, "slp": slp, "side": t["side"]}


def build(total, start_ms):
    bot.DATA_SOURCE = "spot"
    pool, src = bot.volume_top_symbols(POOL)
    try:
        HB, _ = bot.fetch_history("BTCUSDT", bot.HTF, total // 4 + bot.EMA_LEN + 50)
        btc_fn = bot.btc_trend_fn(HB)
    except Exception as e:
        print("BTC data error", e)
        btc_fn = None
    rows = {k: [] for k in ("A_live", "A_core", "B_live", "B_core", "C_live", "C_core", "E_live", "E_core")}
    dvol, coins, check = {}, 0, [0, 0, 0]
    sig_count = {"live": [0, 0], "core": [0, 0]}           # [signals, filled]
    for sym in pool:
        try:
            C, _ = bot.fetch_history(sym, "1h", total)
            H, _ = bot.fetch_history(sym, bot.HTF, total // 4 + 260)
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
        mode = bot.SMC_ENTRY
        sigs = all_signals(C, H, mode)
        nref, bad = detector_check(C, H, mode, sigs)
        sigs = bot.tag_btc([dict(x, time=x["sig_time"]) for x in sigs], sym, btc_fn)   # BTC trend at signal time
        check[0] += 1
        check[1] += nref
        check[2] += bad
        A1 = atr([x["h"] for x in C], [x["l"] for x in C], [x["c"] for x in C], ATR_LEN)
        hc = [x["c"] for x in H]
        e50, e200, hct = ema(hc, 50), ema(hc, 200), [x["ct"] for x in H]
        # A: the bot's own filled setups and simulator (exactly smc_oos.py's "live" path)
        for st in bot.tag_btc(bot.find_smc_setups(C, H, mode), sym, btc_fn):
            if st["time"] < start_ms or bot.sl_pct(st) < bot.MIN_SL_PCT or not st["discount"]:
                continue
            if not bot.passes_filters(st):
                continue
            r = bot._smc(bot._smc_liq)(C, st)
            if r is None:
                continue
            row = {"sym": sym, "time": st["time"], "r": r - bot.FEE_PCT / bot.sl_pct(st), "hold": None, "why": "A"}
            rows["A_core"].append(row)
            if quality_ok(st, LIVE):
                rows["A_live"].append(row)
        # B / C / E: every signal, market entry, one position per coin, no duplicate sweeps
        for fset in ("live", "core"):
            pick = []
            for t in sorted(sigs, key=lambda x: x["sig"]):
                t = dict(t, **t["at_sig"])                 # no look-ahead: features as known at the signal candle
                if C[t["sig"]]["ct"] < start_ms or not t["discount"] or not bot.passes_filters(t):
                    continue
                if fset == "live" and not quality_ok(t, LIVE, skip=("SMC_MAX_FILL", "SMC_MAX_SL")):
                    continue
                pick.append(t)
                sig_count[fset][0] += 1
                sig_count[fset][1] += t["f"] is not None
            for v in ("B", "C", "E"):
                busy, seen = -1, set()
                for t in pick:
                    if t["sig"] + 1 <= busy or (t["side"], t["sweep_i"]) in seen:
                        continue
                    tr = market_trade(C, A1, t, v, e50, e200, hct)
                    if tr is None or tr["slp"] < bot.MIN_SL_PCT or (fset == "live" and tr["slp"] > LIVE["SMC_MAX_SL"]):
                        continue
                    seen.add((t["side"], t["sweep_i"]))
                    busy = tr["exit_i"]
                    rows[f"{v}_{fset}"].append(dict(tr, sym=sym))
        print(f"{sym}: {len(C)} candles, {len(sigs)} signals, detector check {'OK' if not bad else 'MISMATCH'}",
              flush=True)
        time.sleep(0.05)
    lists = {}

    def day_list(day):
        if day not in lists:
            rr = []
            for s, v in dvol.items():
                val = next((v[d] for d in range(day - 1, day - 5, -1) if d in v), None)
                if val is not None:
                    rr.append((val, s))
            lists[day] = {s for _, s in sorted(rr, reverse=True)[:TOP]}
        return lists[day]
    for k in rows:
        rows[k] = [t for t in rows[k] if t["sym"] in day_list(int(t["time"] // DAY))]
    return rows, coins, check, sig_count, src


def stats(ts):
    ts = sorted(ts, key=lambda t: t["time"])
    n = len(ts)
    tot = sum(t["r"] for t in ts)
    win = sum(1 for t in ts if t["r"] > 0)
    gw = sum(t["r"] for t in ts if t["r"] > 0)
    gl = -sum(t["r"] for t in ts if t["r"] <= 0)
    eq = peak = dd = 0.0
    for t in ts:
        eq += t["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    holds = [t["hold"] for t in ts if t.get("hold") is not None]
    big = sorted((t["r"] for t in ts), reverse=True)[:5]
    return {"n": n, "tot": tot, "win": win / n * 100 if n else 0.0, "avg": tot / n if n else 0.0,
            "pf": gw / gl if gl else float("inf"), "dd": dd, "hold": sum(holds) / len(holds) if holds else None,
            "top5": sum(big)}


def main():
    t0 = time.time()
    now = time.time() * 1000
    start = now - YEARS * YEAR_MS
    total = int(YEARS * 365.25 * 24) + 400
    bot.TOP_N = TOP
    rows, coins, check, sig_count, src = build(total, start)
    yrs = [(start + i * YEAR_MS, start + (i + 1) * YEAR_MS) for i in range(YEARS)]
    yname = [f"{datetime.fromtimestamp(a / 1000, timezone.utc):%b %y}" for a, _ in yrs]
    names = {"A": "A live today (limit + TP)", "B": "B market + same TP", "C": "C market + 1h trail",
             "E": "E market + 1h trail + 4h trend"}
    res = {}
    for k, ts in rows.items():
        ys = [[t for t in ts if a <= t["time"] < b] for a, b in yrs]
        res[k] = {"all": stats(ts), "y": [sum(t["r"] for t in y) for y in ys], "yn": [len(y) for y in ys]}
        res[k]["h1"], res[k]["h2"] = res[k]["y"][0] + res[k]["y"][1], res[k]["y"][2] + res[k]["y"][3]

    def verdict(fset):
        e, a = res[f"E_{fset}"], res[f"A_{fset}"]
        ok = e["h1"] > 0 and e["h2"] > 0 and e["h1"] >= a["h1"] + MIN_GAIN and e["h2"] >= a["h2"] + MIN_GAIN
        return ok
    out = [f"## SMC hybrid - market entry + trend trailing exit ({coins} coins pool, PIT top {TOP}, 1h spot, {YEARS} years)\n",
           f"Detector check: {check[0]} coins, {check[1]} bot setups, mismatches {check[2]} "
           f"({'OK' if not check[2] else 'PROBLEM - results not valid'}). Signals LIVE: {sig_count['live'][0]} "
           f"({sig_count['live'][1]} would have filled as a limit). CORE: {sig_count['core'][0]} ({sig_count['core'][1]} filled).\n",
           "R per trade, $1 risk = $1 per R. Market variants: 0.10% fees + 0.05% slippage per fill.\n",
           "| Variant | Filters | " + " | ".join(f"Year from {y}" for y in yname)
           + " | Half 1 | Half 2 | Total | Trades | Win % | Avg R | PF | Max DD | Avg hold | Top 5 trades |",
           "|---|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🧪 <b>SMC hybrid test</b> — signal → market entry → trend trail ({YEARS}y, PIT top {TOP}, 1h)",
          f"Detector check: {'OK ✅' if not check[2] else 'MISMATCH ❌ (results not valid)'} ({check[1]} bot setups). "
          f"Live-filter signals {sig_count['live'][0]}, of which {sig_count['live'][1]} fill as a limit "
          f"({sig_count['live'][1] / max(1, sig_count['live'][0]) * 100:.0f}%).",
          f"Rule (fixed before running): E must be profitable in both halves AND beat A by ≥{MIN_GAIN:g}R in both halves."]
    for fset in ("live", "core"):
        lines = [f"<b>{'LIVE filters' if fset == 'live' else 'CORE (no quality filters)'}</b>"]
        for v in ("A", "B", "C", "E"):
            r = res[f"{v}_{fset}"]
            s = r["all"]
            hold = f"{s['hold']:.0f}h" if s["hold"] is not None else "-"
            out.append(f"| {names[v]} | {fset} | " + " | ".join(f"{y:+.1f}R ({n})" for y, n in zip(r["y"], r["yn"]))
                       + f" | {r['h1']:+.1f}R | {r['h2']:+.1f}R | {s['tot']:+.1f}R | {s['n']} | {s['win']:.0f}% | "
                         f"{s['avg']:+.2f} | {s['pf']:.2f} | −{s['dd']:.1f}R | {hold} | {s['top5']:+.1f}R |")
            lines.append(f"{v}: <b>{s['tot']:+.1f}R</b> ({s['n']} trades, win {s['win']:.0f}%, PF {s['pf']:.2f}, "
                         f"DD −{s['dd']:.1f}R) · halves {r['h1']:+.1f} / {r['h2']:+.1f}"
                         + (f" · hold {hold}" if s["hold"] is not None else ""))
        lines.append(f"→ E vs A: <b>{'PASS ✅' if verdict(fset) else 'FAIL ❌'}</b>")
        tg.append("\n".join(lines))
    out.append(f"\n**Verdict (E vs A, rule fixed in advance): LIVE {'PASS' if verdict('live') else 'FAIL'}, "
               f"CORE {'PASS' if verdict('core') else 'FAIL'}.**\n")
    out.append("Caveats: coin pool = today's top 150 (dead coins missing); spot prices; funding not modelled "
               "(trail trades are held longer); no limit on open positions across coins.")
    tg.append(f"Report only, nothing changed live. {(time.time() - t0) / 60:.0f} min. Full table: Actions → run summary.")
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
