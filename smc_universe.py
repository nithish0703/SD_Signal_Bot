"""Coin-universe study (report only, changes nothing live): which way of picking the 50 coins works best?

The live A+B+C strategy is unchanged. Only the coin list differs. Every rule is rebuilt EVERY DAY using only
data up to the end of the previous day (no look-ahead), from a candidate pool of today's top COIN_POOL futures
coins by volume (the same pool for every rule):
  A  Top 50 by 24h volume (the live rule)
  B  Top 50 by 7-day median daily volume
  C  Top 50 by 30-day median daily volume
  D  Volume + volatility: average of the 30-day median volume rank and the 14-day ATR% rank (higher = better)
  A0 Reference: TODAY's top 50 used for the whole period (how the earlier backtests were run)
A trade counts for a rule if its coin was in that rule's list on the day of the signal.
Last UNI_MONTHS months (default 12), 1h futures, $1 risk -> R = $. Volume = sum of (base volume x close) per hour.
"""
import os
import statistics
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import smc_wf as wf  # noqa: E402
from smc_expd import curve  # noqa: E402

MONTHS = int(os.getenv("UNI_MONTHS", "12"))
POOL = int(os.getenv("COIN_POOL_STUDY", "150"))
N = 50
DAY = 86400000


def coin_data(sym, total):
    """1h candles + per-day metrics known at the START of each UTC day (from the previous days only)."""
    C, _ = bot.fetch_history(sym, "1h", total)
    H = bot._aggregate(C, 4)                                     # same 4h candles, no second download
    vol = defaultdict(float)
    cnt = defaultdict(int)
    for x in C:
        d = x["t"] // DAY
        vol[d] += x["v"] * x["c"]
        cnt[d] += 1
    days = sorted(d for d in vol if cnt[d] == 24)               # complete days only
    D1 = bot._aggregate(C, 24)
    A = bot.atr([x["h"] for x in D1], [x["l"] for x in D1], [x["c"] for x in D1], 14)
    atrp = {x["t"] // DAY: A[i] / x["c"] * 100 for i, x in enumerate(D1) if A[i] and x["c"]}
    m = {}
    for i, d in enumerate(days):
        nxt = d + 1                                              # metrics usable on the next day
        last7 = [vol[k] for k in days[max(0, i - 6):i + 1]]
        last30 = [vol[k] for k in days[max(0, i - 29):i + 1]]
        m[nxt] = {"v1": vol[d],
                  "v7": statistics.median(last7) if len(last7) == 7 else None,
                  "v30": statistics.median(last30) if len(last30) == 30 else None,
                  "atr": atrp.get(d)}
    return C, H, m


def trades_for(sym, C, H, start):
    out = []
    for st in bot.find_smc_setups(C, H, bot.SMC_ENTRY):
        if st["time"] < start or bot.sl_pct(st) < bot.MIN_SL_PCT:
            continue
        if bot.SMC_REQUIRE_DISCOUNT and not st["discount"]:
            continue
        if bot.SMC_REQUIRE_TREND and not st["trend"]:
            continue
        if not bot.passes_filters(st):
            continue
        st["sym"], st["slp"] = sym, bot.sl_pct(st)
        r = bot._smc(bot._smc_liq)(C, st)
        if r is None:
            continue
        st["res"] = {wf.EX: r}
        _, ci = bot._exit_detail(C, st, st["side"], "price", None, st["sl"])
        st["close_ct"] = C[ci]["ct"] if ci is not None else None
        sig = st["e"] - (st["fill_wait"] or 0) - 1               # candle on which the signal was sent
        st["sig_day"] = C[max(0, sig)]["ct"] // DAY
        out.append(st)
    return out


def at(m, day):
    """Metrics for a day; if that day is missing (data gap), the most recent of the 3 days before."""
    for d in range(day, day - 4, -1):
        if d in m:
            return m[d]
    return None


def pick(metrics, day, key, n):
    rows = [(at(m, day)[key], s) for s, m in metrics.items() if at(m, day) and at(m, day)[key] is not None]
    return {s for _, s in sorted(rows, reverse=True)[:n]}


def pick_d(metrics, day, n):
    rows = [(s, at(m, day)["v30"], at(m, day)["atr"]) for s, m in metrics.items()
            if at(m, day) and at(m, day)["v30"] is not None and at(m, day)["atr"] is not None]
    if not rows:
        return set()
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        for k, i in enumerate(order):
            r[i] = k / max(1, len(vals) - 1)
        return r
    rv, ra = ranks([v for _, v, _ in rows]), ranks([a for _, _, a in rows])
    score = sorted(((rv[i] + ra[i]) / 2, rows[i][0]) for i in range(len(rows)))
    return {s for _, s in score[::-1][:n]}


def stats(ts, mid, months):
    n, avg, tot, win = wf.agg(ts)
    dd, ls = curve(ts)
    h = [wf.agg([t for t in ts if (t["time"] < mid) == first]) for first in (True, False)]
    mo = defaultdict(float)
    for t in ts:
        mo[time.strftime("%Y-%m", time.gmtime(t["time"] / 1000))] += wf.net(t)
    return dict(n=n, avg=avg, tot=tot, win=win, dd=dd, ls=ls, h=h, rpm=tot / months,
                pm=f"{sum(1 for v in mo.values() if v > 0)}/{len(mo)}")


def main():
    t0 = time.time()
    bot.DATA_SOURCE = "futures"
    pool, src = bot.volume_top_symbols(POOL)
    if not pool:
        raise SystemExit("could not get the coin list")
    total = int(MONTHS * 730.5) + 300 + 24 * 40
    end = time.time() * 1000
    start = end - MONTHS * wf.MONTH_MS
    mid = start + (end - start) / 2
    metrics, trades = {}, []
    for sym in pool:
        try:
            C, H, m = coin_data(sym, total)
            if len(C) < 24 * 40:
                print(sym, "too little history", flush=True)
                continue
            metrics[sym] = m
            trades += trades_for(sym, C, H, start)
        except Exception as e:
            print(sym, "data error", e, flush=True)
        bot._FUT_CACHE.pop(bot.FUTURES_NAME.get(sym, sym), None)
    live = wf.live_filter(trades)
    first, last = int(start // DAY), int(end // DAY)
    lists = {"A": {}, "B": {}, "C": {}, "D": {}}
    for d in range(first - 1, last + 1):                       # -1: a fill just after start may have its signal the day before
        lists["A"][d] = pick(metrics, d, "v1", N)
        lists["B"][d] = pick(metrics, d, "v7", N)
        lists["C"][d] = pick(metrics, d, "v30", N)
        lists["D"][d] = pick_d(metrics, d, N)
    static = set(pool[:N])
    rows = [("A0 Today's top 50 all year (old backtest way)", [t for t in live if t["sym"] in static], None)]
    names = {"A": "A Top 50 by 24h volume (live rule)", "B": "B Top 50 by 7-day median volume",
             "C": "C Top 50 by 30-day median volume", "D": "D Volume + volatility rank"}
    for k in ("A", "B", "C", "D"):
        sel = [t for t in live if t["sym"] in lists[k].get(t["sig_day"], ())]
        churn = [len(lists[k][d] - lists[k][d - 1]) for d in range(first + 1, last + 1)
                 if lists[k].get(d) and lists[k].get(d - 1)]
        rows.append((names[k], sel, sum(churn) / len(churn) if churn else 0))

    out = [f"## Coin-universe study — live A+B+C rules, {MONTHS} months, pool = today's top {len(metrics)} "
           f"futures coins ({src}), lists rebuilt daily, $1 risk → R = $\n",
           "| Rule | Trades | Win | Avg R | Total R | R/month | Max DD | Loss streak | Half 1 | Half 2 | "
           "Profitable months | Coins changed/day |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"🪙 <b>Coin-universe study</b> — live rules, {MONTHS} months, lists rebuilt daily from today's top "
          f"{len(metrics)} ($1 risk)"]
    for name, ts, ch in rows:
        v = stats(ts, mid, MONTHS)
        chs = "–" if ch is None else f"{ch:.1f}"
        out.append(f"| {name} | {v['n']} | {v['win']:.0f}% | {v['avg']:+.2f} | {v['tot']:+.1f} | {v['rpm']:+.2f} | "
                   f"{v['dd']:.1f} | {v['ls']} | {v['h'][0][0]} / {v['h'][0][2]:+.1f} | "
                   f"{v['h'][1][0]} / {v['h'][1][2]:+.1f} | {v['pm']} | {chs} |")
        tg.append(f"{bot.esc(name)}: {v['n']} trades, win {v['win']:.0f}%, {v['avg']:+.2f}R, total {v['tot']:+.0f}R, "
                  f"DD {v['dd']:.0f}R, streak {v['ls']}, H1 {v['h'][0][2]:+.0f} · H2 {v['h'][1][2]:+.0f}"
                  + ("" if ch is None else f", {ch:.1f} coins changed/day"))
    out.append("\nCaveat: the pool is today's top coins, so coins that dropped out or were delisted during the year "
               "are missing for every rule alike. A0 vs A shows how optimistic 'today's list for the whole period' is.")
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
