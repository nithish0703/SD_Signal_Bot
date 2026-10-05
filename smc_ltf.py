"""15m entry study (report only, changes nothing live).

Everything from the live 1h strategy stays the same: 1h sweep -> MSS -> FVG setup, the A+B+C quality filters,
the 1h SL and the 1h liquidity TP, SL% 1-4 % and the target >= 1R rule. ONLY the entry changes:
the SAME pattern on 15m (15m sweep -> 15m MSS -> 15m FVG, limit at the 15m FVG 50%) that belongs to the same
1h move (15m sweep at or after the 1h sweep) and whose limit fills AFTER the 1h signal is the entry. It must fill
before price reaches the 1h SL or 1h TP. No look-ahead: the order only fills after the 1h signal is known.
50 futures coins, last 24 months, $1 risk -> R = $.

Variants
  1 Live: 1h limit at FVG 50%, filled within 3 candles (as today)
  2 15m entry, must fill within 4h of the 1h signal (same time allowed as live)
  3 15m entry, must fill within 12h of the 1h signal
  4 Hybrid: both orders working (1h limit as live + 15m entry 4h) - whichever fills FIRST is the trade
    (both in the same 15m candle -> the 1h order, conservative)
Notes: 15m trades must pass the A+B+C rules with the 1h entry AND SL% 1-4 / target >= 1R from the 15m entry.
One trade per 1h move: duplicate 1h setups (same side + sweep candle) are merged and a 15m fill is used once.
Pass rule (fixed before running): versus live, higher avg R in BOTH years, max drawdown not larger, higher total R.
"""
import bisect
import csv
import inspect
import io
import os
import sys
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import smc_wf as wf  # noqa: E402
from smc_expd import curve  # noqa: E402

H4 = 4 * 3600000
URL15 = "https://data.binance.vision/data/futures/um/{period}/klines/{s}/15m/{s}-15m-{d}.zip"


# ---------------------------------------------------------------- 1h setups incl. the ones whose 1h limit never filled
def _patched_long():
    src = inspect.getsource(bot._smc_long).replace("def _smc_long(", "def _smc_long_all(", 1)
    old = '''                })
            continue
        used.add(s)'''
    new = '''                })
            if not pending and sig + 2 < n:
                out.append({"missed": True, "pending": False, "sweep_t": C[s]["t"], "sig_i": sig, "e": sig,
                            "entry": entry, "sl": sl, "atr": A[sig] or a, "time": C[sig]["ct"], "score": 6,
                            "liq": prev_high if prev_high > entry + 0.5 * risk else None, "fill_loss": False,
                            "trend": False, "hadx": 0.0, "adx": ADX[sig] or 0.0,
                            "chop": CH[sig] if CH[sig] is not None else 100.0,
                            "discount": entry <= (sweep_low + leg_high) / 2, "fill_wait": None, **q})
            continue
        used.add(s)'''
    old2 = '''            "pending": False, "sweep_t": C[s]["t"],
            "e": f,'''
    new2 = '''            "pending": False, "sweep_t": C[s]["t"], "sig_i": start - 1,
            "e": f,'''
    assert src.count(old) == 1 and src.count(old2) == 1, "bot._smc_long changed - update smc_ltf.py"
    ns = {}
    exec(src.replace(old, new).replace(old2, new2), bot.__dict__, ns)
    return ns["_smc_long_all"]


def setups_all(C, H, fn):
    """bot.find_smc_setups with the patched _smc_long (also unfilled setups), so TP / tgt_r are exactly live."""
    orig = bot._smc_long
    bot._smc_long = fn
    try:
        return bot.find_smc_setups(C, H, bot.SMC_ENTRY)
    finally:
        bot._smc_long = orig


# ---------------------------------------------------------------- 15m futures candles
def _rows15(url):
    """Rows of one 15m zip. [] = file not published (404), None = download kept failing (after 3 tries)."""
    rows = None
    for attempt in range(3):
        try:
            r = bot.requests.get(url, timeout=30)
            if r.status_code == 404:
                return []
            if r.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    with z.open(z.namelist()[0]) as fh:
                        rows = list(csv.reader(io.TextIOWrapper(fh, "utf-8")))
                break
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    if rows is None:
        return None
    out = []
    for r in rows or []:
        if not r or not r[0].strip().isdigit():
            continue
        t = int(r[0])
        t = t // 1000 if t > 10 ** 14 else t
        out.append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                    "v": float(r[5]), "ct": t + 900000 - 1, "tb": float(r[9]) if len(r) > 9 else None})
    return out


def fetch15(sym, start_ms):
    fut = bot.FUTURES_NAME.get(sym, sym)
    now = datetime.now(timezone.utc)
    st = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    urls, m = [], datetime(st.year, st.month, 1, tzinfo=timezone.utc)
    this_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    while m < this_month:
        urls.append(URL15.format(period="monthly", s=fut, d=m.strftime("%Y-%m")))
        m = datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=timezone.utc)
    d = max(this_month - timedelta(days=45), st).date()
    while d < now.date():
        urls.append(URL15.format(period="daily", s=fut, d=d.isoformat()))
        d += timedelta(days=1)
    with ThreadPoolExecutor(8) as ex:
        parts = list(ex.map(_rows15, urls))
    failed = sum(1 for p in parts if p is None)
    if failed:
        print(f"{sym}: {failed}/{len(urls)} 15m files failed to download (setups in gaps are skipped)", flush=True)
    rows = {x["t"]: x for part in parts if part for x in part}
    return [rows[t] for t in sorted(rows)]


# ---------------------------------------------------------------- trade simulation (LONG terms, SHORT mirrored)
def run(C, i, side, E, S, TP, check_fill=True):
    """From fill candle i: SL in the fill candle = loss; then SL before TP. -> (gross R, close_ct) or None."""
    P = lambda v: bot._px(v, side)
    E, S, TP = P(E), P(S), P(TP)
    hl = lambda x: (x["h"], x["l"]) if side == "LONG" else (-x["l"], -x["h"])
    if check_fill and hl(C[i])[1] <= S:
        return -1.0, C[i]["ct"]
    for x in C[i + 1:]:
        hi, lo = hl(x)
        if lo <= S:
            return -1.0, x["ct"]
        if hi >= TP:
            return (TP - E) / (E - S), x["ct"]
    return None


def risk_ok(side, E, S, TP):
    slp = abs(E - S) / abs(E) * 100
    tr = (bot._px(TP, side) - bot._px(E, side)) / (bot._px(E, side) - bot._px(S, side)) \
        if bot._px(E, side) > bot._px(S, side) else -1
    return bot.MIN_SL_PCT <= slp <= 4.0 and tr >= 1.0, slp


def ltf_entry(X, C15, S15, idx15, window, taken):
    """First 15m setup (same side, 15m sweep at/after the 1h sweep) whose limit fills after the 1h signal and
    inside the window, before price reaches the 1h SL / TP. -> trade dict or None."""
    T = X["sig_ct"]
    side, SL, TP = X["side"], X["sl"], X["tp"]
    P = lambda v: bot._px(v, side)
    hl = lambda x: (x["h"], x["l"]) if side == "LONG" else (-x["l"], -x["h"])
    for Y in S15:
        if Y["side"] != side or Y["sweep_t"] < X["sweep_t"] or Y["time"] <= T or Y["time"] > T + window:
            continue
        if (side, Y["time"], Y["entry"]) in taken:
            continue
        ok, slp = risk_ok(side, Y["entry"], SL, TP)
        if not ok or not (P(SL) < P(Y["entry"]) < P(TP)):
            continue
        f = Y["e"]
        k0 = idx15.get(T + 1)                         # first 15m candle after the 1h signal
        if k0 is None:
            return None
        if any(hl(C15[k])[1] <= P(SL) or hl(C15[k])[0] >= P(TP) for k in range(k0, f)):
            return None                               # 1h setup already finished before the 15m fill
        r = run(C15, f, side, Y["entry"], SL, TP)
        if r is None:
            return None
        return dict(X, entry=Y["entry"], slp=slp, res={wf.EX: r[0]}, close_ct=r[1], time=Y["time"],
                    fill_t=C15[f]["t"], key15=(side, Y["time"], Y["entry"]))
    return None


def fill_t_1h(X, C, C15, idx15):
    """Open time of the 15m candle in which the 1h limit filled (fallback: the 1h fill candle open)."""
    side, E = X["side"], bot._px(X["entry"], X["side"])
    t0 = C[X["e"]]["t"]
    k = idx15.get(t0)
    if k is not None:
        for j in range(k, min(len(C15), k + 4)):
            lo = C15[j]["l"] if side == "LONG" else -C15[j]["h"]
            if lo <= E:
                return C15[j]["t"]
    return t0


def stats(ts, mid, folds):
    n, avg, tot, win = wf.agg(ts)
    dd, ls = curve(ts)
    yrs = [wf.agg([t for t in ts if (t["time"] < mid) == first]) for first in (True, False)]
    months = defaultdict(float)
    for t in ts:
        months[time.strftime("%Y-%m", time.gmtime(t["time"] / 1000))] += wf.net(t)
    fp = sum(1 for a, b in folds if wf.agg([t for t in ts if a <= t["time"] < b])[2] > 0)
    rs = [wf.net(t) for t in ts]
    loss = -sum(r for r in rs if r < 0)
    pf = "–" if not rs else ("∞" if not loss else f"{sum(r for r in rs if r > 0) / loss:.2f}")
    return dict(n=n, avg=avg, tot=tot, win=win, dd=dd, ls=ls, pf=pf, yrs=yrs,
                pm=f"{sum(1 for x in months.values() if x > 0)}/{len(months)}", wf=f"{fp}/{len(folds)}")


def main():
    t0 = time.time()
    bot.DATA_SOURCE = "futures"
    bot.TOP_N = int(os.getenv("WF_COINS", "50"))
    bot.resolve_symbols()
    total = int(wf.MONTHS * 730.5) + 300
    end = time.time() * 1000
    start = end - wf.MONTHS * wf.MONTH_MS
    mid = start + (end - start) / 2
    folds, f = [], start + wf.TRAIN_M * wf.MONTH_MS
    while f + 0.5 * wf.TEST_M * wf.MONTH_MS <= end:
        folds.append((f, min(end, f + wf.TEST_M * wf.MONTH_MS)))
        f += wf.TEST_M * wf.MONTH_MS
    long_all = _patched_long()
    act = dict(bot.SMC_ACTIVE)
    nofill = {k: v for k, v in act.items() if k != "SMC_MAX_FILL"}
    V = {k: [] for k in ("live", "m4", "m12", "hyb")}
    cnt = defaultdict(int)
    for sym in bot.SYMBOLS:
        try:
            C, _ = bot.fetch_history(sym, "1h", total)
            H, _ = bot.fetch_history(sym, bot.HTF, total // 4 + bot.EMA_LEN + 50)
            C15 = fetch15(sym, start - 86400000)
        except Exception as e:
            print(sym, "data error", e, flush=True)
            bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)
            continue
        if len(C15) < 1000:
            print(sym, "no 15m data - coin skipped in all variants", flush=True)
            bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)
            cnt["coins skipped (no 15m data)"] += 1
            continue
        t15 = [x["t"] for x in C15]
        idx15 = {x["t"]: i for i, x in enumerate(C15)}
        S15 = sorted([y for y in bot.find_smc_setups(C15, C, bot.SMC_ENTRY)], key=lambda y: y["time"])
        n0 = sum(len(v) for v in V.values())
        seen, xs = set(), []
        for X in sorted(setups_all(C, H, long_all),
                        key=lambda x: (x.get("missed", False), x["fill_wait"] if x["fill_wait"] is not None else 99)):
            if (X["side"], X["sweep_t"]) not in seen:          # one setup per 1h move
                seen.add((X["side"], X["sweep_t"]))
                xs.append(X)
        xs.sort(key=lambda x: x["time"])
        taken = {k: set() for k in ("m4", "m12", "hyb")}
        for X in xs:
            if X["time"] < start or not X["discount"] or not bot.passes_filters(X):
                continue
            X["sym"], X["slp"] = sym, bot.sl_pct(X)
            X["sig_ct"] = C[X["sig_i"]]["ct"]
            if X["slp"] < bot.MIN_SL_PCT:
                live_ok = False
            else:
                live_ok = (not X.get("missed")) and bot.smc_quality_ok(X)
            bot.SMC_ACTIVE.clear(); bot.SMC_ACTIVE.update(nofill)
            q_ok = bot.smc_quality_ok(X)
            bot.SMC_ACTIVE.clear(); bot.SMC_ACTIVE.update(act)
            if not q_ok:
                continue                                  # not an A+B+C setup at all
            a, b = X["sig_ct"] + 1, X["sig_ct"] + 12 * 3600000
            if bisect.bisect_right(t15, b) - bisect.bisect_left(t15, a) != 48:
                cnt["skipped in all variants (15m data gap)"] += 1
                continue                                  # 15m data incomplete after the signal: fair comparison only
            cnt["1h quality setups"] += 1
            lt = None
            if live_ok:
                r = bot._smc(bot._smc_liq)(C, X)
                if r is not None:
                    _, ci = bot._exit_detail(C, X, X["side"], "price", None, X["sl"])
                    lt = dict(X, res={wf.EX: r}, close_ct=C[ci]["ct"] if ci is not None else None)
                    V["live"].append(lt)
            # 15m entries (the 15m search only looks at candles after the 1h signal, so it never needs the future)
            s15 = [y for y in S15 if X["sig_ct"] < y["time"] <= X["sig_ct"] + 12 * 3600000]
            m4 = ltf_entry(X, C15, s15, idx15, H4, taken["m4"])
            m12 = ltf_entry(X, C15, s15, idx15, 3 * H4, taken["m12"])
            if m4:
                V["m4"].append(m4)
                taken["m4"].add(m4["key15"])
            if m12:
                V["m12"].append(m12)
                taken["m12"].add(m12["key15"])
            mh = ltf_entry(X, C15, s15, idx15, H4, taken["hyb"])
            if lt is not None and mh is not None:           # both working: the first fill wins (tie -> 1h)
                hy = mh if mh["fill_t"] < fill_t_1h(X, C, C15, idx15) else lt
            else:
                hy = lt if lt is not None else mh
            if hy:
                V["hyb"].append(hy)
                if hy is mh:
                    taken["hyb"].add(mh["key15"])
                    cnt["hybrid: 15m filled first"] += 1
            cnt["1h limit filled (live)"] += lt is not None
            cnt["15m entry found (4h)"] += m4 is not None
            cnt["15m entry found (12h)"] += m12 is not None
            cnt["no live 1h trade but 15m fill (4h)"] += lt is None and m4 is not None
        print(f"{sym}: 1h {len(C)} / 15m {len(C15)} candles, +{sum(len(v) for v in V.values()) - n0} trades",
              flush=True)
        del C15, S15, idx15, t15
        bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)

    names = [("live", "1 Live: 1h limit at FVG 50% (fill ≤3 candles)"),
             ("m4", "2 15m entry, fill within 4h"),
             ("m12", "3 15m entry, fill within 12h"),
             ("hyb", "4 Hybrid: 1h limit + 15m entry (4h), first fill wins")]
    res = {k: stats(V[k], mid, folds) for k, _ in names}
    base = res["live"]
    out = [f"## 15m entry study — 1h setup / SL / TP / A+B+C unchanged ({len(bot.SYMBOLS)} futures coins, "
           f"{wf.MONTHS} months, $1 risk → R = $)\n",
           "Setups: " + " · ".join(f"{k}: {v}" for k, v in cnt.items()) + "\n",
           "| Variant | Trades | Win | Avg R | Total R | Max DD | Profit factor | Loss streak | Year 1 | Year 2 | "
           "Profitable months | WF folds + | Pass? |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"⏱️ <b>15m entry study</b> — 1h setup/SL/TP/A+B+C unchanged ({len(bot.SYMBOLS)} coins, "
          f"{wf.MONTHS} months, $1 risk)", "Setups: " + " · ".join(f"{k}: {v}" for k, v in cnt.items())]
    for k, name in names:
        v = res[k]
        ok = "–" if v is base else ("✅" if all(v["yrs"][i][0] > 0 and v["yrs"][i][1] > base["yrs"][i][1]
                                                for i in (0, 1)) and v["dd"] >= base["dd"]
                                    and v["tot"] > base["tot"] else "❌")
        out.append(f"| {name} | {v['n']} | {v['win']:.0f}% | {v['avg']:+.2f} | {v['tot']:+.1f} | {v['dd']:.1f} | "
                   f"{v['pf']} | {v['ls']} | {v['yrs'][0][0]} / {v['yrs'][0][2]:+.1f} | "
                   f"{v['yrs'][1][0]} / {v['yrs'][1][2]:+.1f} | {v['pm']} | {v['wf']} | {ok} |")
        tg.append(f"{'' if ok == '–' else ok + ' '}{bot.esc(name)}: {v['n']} trades, win {v['win']:.0f}%, "
                  f"{v['avg']:+.2f}R, total {v['tot']:+.0f}R, PF {v['pf']}, DD {v['dd']:.0f}R, streak {v['ls']}, "
                  f"Y1 {v['yrs'][0][2]:+.0f} · Y2 {v['yrs'][1][2]:+.0f}, WF {v['wf']}")
    tg.append(f"Report only, nothing changed live. Not financial advice. {(time.time() - t0) / 60:.0f} min. "
              "Full table: Actions → run summary.")
    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as fh:
            fh.write(report + "\n")
    msg = "\n\n".join(tg)
    print(msg)
    bot.tg(msg[:3900])


if __name__ == "__main__":
    main()
