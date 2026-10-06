"""Timeframe-combination study (report only, changes nothing live): LTF chart x HTF bias.

The same live strategy (sweep -> MSS -> FVG 50% limit, liquidity TP, A+B+C quality filters, SL% 1-4, target >= 1R)
runs on each LTF chart with its HTF:
  1h / 4h  (live)   ·   1h / Daily   ·   15m / 4h   ·   5m / 1h
and for each, three HTF rules:
  none  - HTF not used (like live today)
  bias  - trade only WITH the HTF trend (HTF close vs EMA50 on the last HTF candle closed before the signal)
  poi   - the sweep happened at a confirmed HTF swing (within 0.5 HTF ATR)
Last 12 months (5m data is large), today's top 50 futures coins, $1 risk -> R = $. A coin is used only if all
three LTF data sets (1h, 15m, 5m) are at least 90% complete, so every combination sees the same coins.
Pass rule (fixed before running): versus 1h/4h live, higher avg R in BOTH halves, max DD not larger, higher total R.
Sanity check: "1h / Daily none" must equal "1h / 4h none" (the HTF is not used without a rule).
"""
import csv
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

MONTHS = int(os.getenv("MTF_MONTHS", "12"))
URL = "https://data.binance.vision/data/futures/um/{period}/klines/{s}/{iv}/{s}-{iv}-{d}.zip"
IV_MS = {"15m": 900000, "5m": 300000}


def _rows(url, ms):
    """[] = not published (404), None = kept failing."""
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
    for r in rows:
        if not r or not r[0].strip().isdigit():
            continue
        t = int(r[0])
        t = t // 1000 if t > 10 ** 14 else t
        out.append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                    "v": float(r[5]), "ct": t + ms - 1, "tb": float(r[9]) if len(r) > 9 else None})
    return out


def fetch_ltf(sym, iv, start_ms):
    fut = bot.FUTURES_NAME.get(sym, sym)
    now = datetime.now(timezone.utc)
    st = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    urls, m = [], datetime(st.year, st.month, 1, tzinfo=timezone.utc)
    this_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    while m < this_month:
        urls.append(URL.format(period="monthly", s=fut, iv=iv, d=m.strftime("%Y-%m")))
        m = datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=timezone.utc)
    d = max(this_month - timedelta(days=45), st).date()
    while d < now.date():
        urls.append(URL.format(period="daily", s=fut, iv=iv, d=d.isoformat()))
        d += timedelta(days=1)
    with ThreadPoolExecutor(8) as ex:
        parts = list(ex.map(lambda u: _rows(u, IV_MS[iv]), urls))
    failed = sum(1 for p in parts if p is None)
    rows = {x["t"]: x for p in parts if p for x in p}
    return [rows[t] for t in sorted(rows) if t >= start_ms], failed


def coverage(C, start_ms, end_ms, ms):
    n = sum(1 for x in C if start_ms <= x["t"] < end_ms)
    return n / max(1, (end_ms - start_ms) // ms)


def trades_on(C, H, sym, start):
    out = []
    for st in bot.find_smc_setups(C, H, bot.SMC_ENTRY):
        if st["time"] < start or bot.sl_pct(st) < bot.MIN_SL_PCT:
            continue
        if bot.SMC_REQUIRE_DISCOUNT and not st["discount"]:
            continue
        if bot.SMC_REQUIRE_TREND and not st["trend"]:
            continue
        if not bot.passes_filters(st) or not bot.smc_quality_ok(st):
            continue
        st["sym"], st["slp"] = sym, bot.sl_pct(st)
        r = bot._smc(bot._smc_liq)(C, st)
        if r is None:
            continue
        st["res"] = {wf.EX: r}
        _, ci = bot._exit_detail(C, st, st["side"], "price", None, st["sl"])
        st["close_ct"] = C[ci]["ct"] if ci is not None else None
        out.append({k: st[k] for k in ("time", "sym", "side", "slp", "res", "close_ct", "trend_sig", "htf_poi")})
    return out


def stats(ts, mid):
    n, avg, tot, win = wf.agg(ts)
    dd, ls = curve(ts)
    h = [wf.agg([t for t in ts if (t["time"] < mid) == first]) for first in (True, False)]
    mo = defaultdict(float)
    for t in ts:
        mo[time.strftime("%Y-%m", time.gmtime(t["time"] / 1000))] += wf.net(t)
    return dict(n=n, avg=avg, tot=tot, win=win, dd=dd, ls=ls, h=h,
                pm=f"{sum(1 for v in mo.values() if v > 0)}/{len(mo)}")


def main():
    t0 = time.time()
    bot.DATA_SOURCE = "futures"
    bot.TOP_N = int(os.getenv("WF_COINS", "50"))
    bot.resolve_symbols()
    end = time.time() * 1000
    start = end - MONTHS * wf.MONTH_MS
    mid = start + (end - start) / 2
    total_h = int(MONTHS * 730.5) + 24 * 80                    # + warm-up for the daily EMA50
    combos = ["1h / 4h (live)", "1h / Daily", "15m / 4h", "5m / 1h"]
    T = {c: [] for c in combos}
    used, skipped = 0, []
    for sym in bot.SYMBOLS:
        try:
            C1, _ = bot.fetch_history(sym, "1h", total_h)
            C15, f15 = fetch_ltf(sym, "15m", int(start) - 10 * 86400000)
            C5, f5 = fetch_ltf(sym, "5m", int(start) - 5 * 86400000)
        except Exception as e:
            skipped.append(sym)
            print(sym, "data error", e, flush=True)
            bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)
            continue
        cov = [coverage(C1, start, end - 2 * 86400000, 3600000), coverage(C15, start, end - 2 * 86400000, 900000),
               coverage(C5, start, end - 2 * 86400000, 300000)]
        if f15 or f5:
            skipped.append(sym)
            print(sym, f"skipped in all combos: {f15} 15m / {f5} 5m files failed to download", flush=True)
        elif min(cov) < 0.9:
            skipped.append(sym)
            print(sym, "skipped in all combos, data coverage 1h/15m/5m:", [f"{c:.0%}" for c in cov], flush=True)
        else:
            used += 1
            H4, D1 = bot._aggregate(C1, 4), bot._aggregate(C1, 24)
            try:
                T["1h / 4h (live)"] += trades_on(C1, H4, sym, start)
                T["1h / Daily"] += trades_on(C1, D1, sym, start)
                T["15m / 4h"] += trades_on(C15, H4, sym, start)
                T["5m / 1h"] += trades_on(C5, C1, sym, start)
            except Exception as e:
                print(sym, "error", e, flush=True)
            print(f"{sym}: " + " · ".join(f"{c} {sum(1 for t in T[c] if t['sym'] == sym)}" for c in combos), flush=True)
        del C15, C5
        bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)

    rules = [("none", lambda t: True), ("bias", lambda t: t["trend_sig"]), ("poi", lambda t: t["htf_poi"])]
    res = {(c, r): stats([t for t in T[c] if f(t)], mid) for c in combos for r, f in rules}
    base = res[("1h / 4h (live)", "none")]
    same = res[("1h / Daily", "none")]["n"] == base["n"] and abs(res[("1h / Daily", "none")]["tot"] - base["tot"]) < 1e-6

    def ok(v):
        if v is base:
            return "–"
        good = all(v["h"][i][0] > 0 and v["h"][i][1] > base["h"][i][1] for i in (0, 1)) \
            and v["dd"] >= base["dd"] and v["tot"] > base["tot"]
        return "✅" if good else "❌"

    out = [f"## Timeframe combinations — live A+B+C rules, {MONTHS} months, {used} futures coins "
           f"({len(skipped)} skipped for data gaps), $1 risk → R = $\n",
           "| LTF / HTF | HTF rule | Trades | Win | Avg R | Total R | Max DD | Loss streak | Half 1 | Half 2 | "
           "Profitable months | Pass? |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🧭 <b>Timeframe combinations</b> — live rules, {MONTHS} months, {used} coins, $1 risk",
          "HTF rule: none = HTF not used · bias = only with HTF trend · poi = sweep at an HTF swing"]
    for c in combos:
        for r, _ in rules:
            v = res[(c, r)]
            mark = ok(v)
            out.append(f"| {c} | {r} | {v['n']} | {v['win']:.0f}% | {v['avg']:+.2f} | {v['tot']:+.1f} | {v['dd']:.1f} | "
                       f"{v['ls']} | {v['h'][0][0]} / {v['h'][0][2]:+.1f} | {v['h'][1][0]} / {v['h'][1][2]:+.1f} | "
                       f"{v['pm']} | {mark} |")
            tg.append(f"{'' if mark == '–' else mark + ' '}{bot.esc(c)} · {r}: {v['n']} trades, win {v['win']:.0f}%, "
                      f"{v['avg']:+.2f}R, total {v['tot']:+.0f}R, DD {v['dd']:.0f}R, streak {v['ls']}, "
                      f"H1 {v['h'][0][2]:+.0f} · H2 {v['h'][1][2]:+.0f}")
    out.append(f"\nSanity check (1h/Daily none == 1h/4h none): {'OK' if same else 'MISMATCH'}. "
               "Same coins for every combination; 5m/15m data from data.binance.vision futures files.")
    tg.append(f"Sanity check: {'OK' if same else 'MISMATCH'}. Report only, nothing changed live. Not financial advice. "
              f"{(time.time() - t0) / 60:.0f} min. Full table: Actions → run summary.")
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
