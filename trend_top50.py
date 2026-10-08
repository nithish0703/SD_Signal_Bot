"""T2 trend, small account ($100 default, TREND_BALANCE), WITHOUT BTC/ETH, on the TOP 30 and TOP 50 coins - report only, changes nothing live.

Same rules, costs and sizing as trend_small.py (EMA50/200 cross + 3 ATR chandelier on 4h, long+short, futures 1h
candles from data.binance.vision, real funding, 0.10% fees, no compounding, max 12 open, size <= 3x equity,
real Binance minimum order sizes).

Top 50 is chosen POINT-IN-TIME (no hindsight): at the start of every month, the 50 USDT-M perpetuals with the
highest futures quote volume in the PREVIOUS month, from ALL coins that existed then - including coins that were
later delisted. BTC, ETH, stablecoins and gold/stock contracts are excluded. A new trade is allowed only if the coin
is in that month's top 50 and has at least 100 days of history (EMA200 warm-up); a trade that is already open is
always managed to its normal exit, even if the coin leaves the top 50. If a coin's data stops (delisted) while a
trade is open, it is closed at the last price.

Top 30 = the first 30 of the same monthly ranking.
Compared with: the fixed 18-coin list (the 20 majors minus BTC/ETH) - picked with today's knowledge, so it is
slightly optimistic.
Scenarios: start balance = TREND_BALANCE (workflow "balance" input, default $100); $ risk per trade = TREND_RISKS
(workflow "risk" input when it is a list like 0.5,1,1.5,2; otherwise 0.5, 1, 1.5, 2).
Pass = >=3 of 4 years up, max DD < 25%, final above the start balance.
"""
import csv
import io
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import trend_account as TA  # noqa: E402
import trend_small as S  # noqa: E402

YEARS, DAY, H4 = TA.YEARS, TA.DAY, TA.H4
TOP_N = 50
MIN_HIST = 100 * DAY
LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
D1_URL = "https://data.binance.vision/data/futures/um/monthly/klines/{s}/1d/{s}-1d-{m}.zip"
EXCLUDE = {"BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "USDPUSDT", "DAIUSDT",
           "USDEUSDT", "USD1USDT", "RLUSDUSDT", "EURUSDT", "XUSDUSDT", "PYUSDUSDT", "BFUSDUSDT",
           "XAUUSDT", "XAGUSDT", "XPTUSDT", "XPDUSDT", "PAXGUSDT", "XAUTUSDT", "TSLAUSDT", "NVDAUSDT", "AAPLUSDT",
           "MSTRUSDT", "COINUSDT", "HOODUSDT", "AMZNUSDT", "GOOGLUSDT", "METAUSDT", "MSFTUSDT", "WMTUSDT", "SPYUSDT",
           "QQQUSDT", "CRCLUSDT", "INTCUSDT", "AMDUSDT", "NFLXUSDT", "PLTRUSDT", "ORCLUSDT", "BABAUSDT",
           "BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT", "BLUEBIRDUSDT", "ALL1USDT"}          # index contracts
FIXED18 = [s for s in TA.COINS if s not in ("BTCUSDT", "ETHUSDT")]


def all_symbols():
    """Every USDT-M symbol that ever had monthly kline files (delisted ones too)."""
    out, marker = [], ""
    for _ in range(20):
        r = requests.get(LIST_URL, params={"delimiter": "/", "prefix": "data/futures/um/monthly/klines/",
                                           "marker": marker}, timeout=30)
        r.raise_for_status()
        pre = re.findall(r"<Prefix>data/futures/um/monthly/klines/([^/<]+)/</Prefix>", r.text)
        out += pre
        if "<IsTruncated>true</IsTruncated>" not in r.text or not pre:
            break
        marker = f"data/futures/um/monthly/klines/{pre[-1]}/"
    return sorted(set(out))


def month_list(start_ms):
    m = datetime.fromtimestamp(start_ms / 1000, timezone.utc).replace(day=1, hour=0, minute=0, second=0,
                                                                       microsecond=0)
    now = datetime.now(timezone.utc)
    out = []
    while (m.year, m.month) <= (now.year, now.month):
        out.append(m)
        m = m.replace(year=m.year + (m.month == 12), month=m.month % 12 + 1)
    return out


def monthly_volume(sym, months):
    """{'YYYY-MM': (quote volume, days)} from the monthly 1d files (None if a download failed)."""
    res, failed = {}, 0
    for m in months:
        key = m.strftime("%Y-%m")
        rows = None
        for attempt in range(3):
            try:
                r = requests.get(D1_URL.format(s=sym, m=key), timeout=20)
                if r.status_code == 404:
                    rows = []
                    break
                if r.status_code == 200:
                    with zipfile.ZipFile(io.BytesIO(r.content)) as z, z.open(z.namelist()[0]) as f:
                        rows = list(csv.reader(io.TextIOWrapper(f, "utf-8")))
                    break
            except Exception:
                time.sleep(1 + attempt)
        if rows is None:
            failed += 1
            continue
        qv = [float(x[7]) for x in rows if x and x[0].strip().isdigit()]
        if qv:
            res[key] = (sum(qv), len(qv))
    return sym, res, failed


def universe(start_ms):
    """-> ({'YYYY-MM': [top symbols]}, notes). Ranking for month M uses month M-1's volume."""
    syms = [s for s in all_symbols() if s.endswith("USDT") and "_" not in s and s not in EXCLUDE]
    months = month_list(start_ms - 40 * DAY)
    print(f"ranking {len(syms)} symbols over {len(months)} months", flush=True)
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(lambda s: monthly_volume(s, months), syms))
    vol = {s: v for s, v, _ in res}
    failed = sum(f for _, _, f in res)
    top = {}
    for prev, cur in zip(months, months[1:]):
        pk = prev.strftime("%Y-%m")
        cand = [(vol[s][pk][0], s) for s in vol if pk in vol[s] and vol[s][pk][1] >= 20]
        ranked = [s for _, s in sorted(cand, reverse=True)[:TOP_N]]
        if len(ranked) < TOP_N // 2 and top:                         # previous month's file not published yet
            ranked = top[max(top)]
        top[cur.strftime("%Y-%m")] = ranked
    return top, len(syms), failed


def load(sym, hours, start):
    """4h candles + funding for one coin (partial history allowed). None if no data."""
    try:
        c1 = bot.fetch_futures_1h(sym, hours)
    except Exception as e:
        print(sym, "data error", e, flush=True)
        return None
    bot._FUT_CACHE.pop(sym, None)
    H = bot._aggregate(c1, 4)
    gaps = sum(1 for a, b in zip(c1, c1[1:]) if b["t"] - a["t"] > 24 * 3600000)
    del c1
    fl, miss = TA.funding(sym, max(start, H[0]["t"]) - 5 * DAY)
    return H, fl, gaps, miss


def build_trades(data, allowed, end_t):
    """T2 trades; entries only when allowed(sym, t_in) is True. Delisted while open -> close at the last price."""
    trades = []
    for sym, full in data.items():
        cuts = [0] + [i + 1 for i in range(len(full) - 1) if full[i + 1]["t"] - full[i]["t"] > 3 * DAY] + [len(full)]
        for a, b in zip(cuts, cuts[1:]):                             # each unbroken stretch of data on its own
            H = full[a:b]
            if len(H) < 260:
                continue
            for side, t_in, entry, risk, t_out, ex in TA.t2_trades(H, False):
                if not allowed(sym, t_in) or t_in - H[0]["t"] < MIN_HIST:
                    continue
                if t_out is None and H[-1]["t"] < end_t - 3 * DAY:      # data stopped = delisted (or a gap)
                    t_out, ex = H[-1]["t"] + H4, H[-1]["c"]
                trades.append({"sym": sym, "side": side, "t_in": t_in, "entry": entry, "risk": risk,
                               "t_out": t_out, "exit": ex, "model": "T2"})
    return trades


def report_rows(name, r, yb):
    def eq_at(ms):
        v = S.START_BAL
        for t, e in r["curve"]:
            if t > ms:
                break
            v = e
        return v
    ychg = [eq_at(yb[i + 1]) - eq_at(yb[i]) for i in range(YEARS)]
    months = {}
    for t, e in r["curve"]:
        months[datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m")] = e
    mchg, prev = [], S.START_BAL
    for k in sorted(months):
        mchg.append(months[k] - prev)
        prev = months[k]
    worst = min(mchg) if mchg else 0.0
    pos_m = sum(1 for x in mchg if x > 0)
    ok = sum(1 for x in ychg if x > 0) >= 3 and r["mdd"] < 0.25 and r["final"] > S.START_BAL
    md = (f"| {name} | " + " | ".join(f"${x:+,.1f}" for x in ychg)
          + f" | ${r['final']:,.1f} | {(r['final'] / S.START_BAL - 1) * 100:+.0f}% | −{r['mdd'] * 100:.1f}% | "
            f"${worst:+,.1f} | {pos_m}/{len(mchg)} | {r['taken']} | {r['sk_small']} | {r['sk_cap']} | "
            f"{r['max_open']} | {'✅' if ok else '❌'} |")
    tgl = (f"{'✅' if ok else '❌'} <b>{bot.esc(name)}</b>: ${S.START_BAL:,.0f} → <b>${r['final']:,.1f}</b> "
           f"({(r['final'] / S.START_BAL - 1) * 100:+.0f}%), DD −{r['mdd'] * 100:.1f}%, worst month ${worst:+,.1f}, "
           f"{pos_m}/{len(mchg)} months up\n   years: " + " · ".join(f"${x:+,.1f}" for x in ychg)
           + f"\n   trades {r['taken']}, skipped: too small {r['sk_small']}, cap {r['sk_cap']}; max {r['max_open']} open")
    return md, tgl


def main():
    t0 = time.time()
    now = time.time() * 1000
    start = now - YEARS * 365.25 * DAY
    hours = int(YEARS * 365.25 * 24) + 140 * 24
    S.START_BAL = float(os.getenv("TREND_BALANCE", "") or 100)
    rs = os.getenv("TREND_RISKS", "")
    risks = [float(x) for x in rs.split(",") if x.strip()] if "," in rs else [0.5, 1.0, 1.5, 2.0]
    print(f"start ${S.START_BAL:g}, risks {risks}", flush=True)
    limits, lim_src = S.live_limits()
    top, n_syms, failed = universe(start)
    need = sorted(set(FIXED18) | {s for v in top.values() for s in v})
    print(f"{len(need)} coins needed (top {TOP_N} union + fixed 18)", flush=True)
    data, fund, notes, gaps_total = {}, {}, [], 0
    for i, sym in enumerate(need):
        x = load(sym, hours, start)
        if not x:
            notes.append(f"{sym[:-4]} no data")
            continue
        H, fl, gaps, miss = x
        data[sym], fund[sym] = H, fl
        gaps_total += gaps
        if miss:
            notes.append(f"{sym[:-4]}: {miss} funding month(s) missing")
        print(f"[{i + 1}/{len(need)}] {sym}: {len(H)} 4h candles from "
              f"{datetime.fromtimestamp(H[0]['t'] / 1000, timezone.utc):%Y-%m-%d}, gaps {gaps}", flush=True)
    closes = {s: {x["t"]: x["c"] for x in H} for s, H in data.items()}
    times = sorted({t for s in closes for t in closes[s] if t >= start})
    end_t = times[-1]
    top30 = {k: set(v[:30]) for k, v in top.items()}               # the list is ranked, so top 30 = first 30
    top50 = {k: set(v) for k, v in top.items()}

    def month(t):
        return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m")

    def in_top30(sym, t):
        return sym in top30.get(month(t), ())

    def in_top50(sym, t):
        return sym in top50.get(month(t), ())

    def in_fixed(sym, t):
        return sym in FIXED18
    uni = [("18 fixed coins (no BTC/ETH)", {s: data[s] for s in FIXED18 if s in data}, in_fixed),
           ("Top 30 each month (no BTC/ETH)", data, in_top30),
           (f"Top {TOP_N} each month (no BTC/ETH)", data, in_top50)]
    yb = [start + i * 365.25 * DAY for i in range(YEARS + 1)]
    yname = [f"{datetime.fromtimestamp(yb[i] / 1000, timezone.utc):%b %y}" for i in range(YEARS)]
    used = {s for v in top.values() for s in v}
    dead = sorted(s[:-4] for s in used if s in data and data[s][-1]["t"] < end_t - 3 * DAY)
    out = [f"## T2 trend - ${S.START_BAL:,.0f} account, no BTC/ETH, top 30 and top {TOP_N} point-in-time vs fixed 18 coins, {YEARS} years\n",
           f"{n_syms} symbols ranked every month; {len(used)} different coins were in the top {TOP_N} at some point "
           f"({len(dead)} of them later delisted or stopped). Futures + real funding, 0.10% fees, no compounding, "
           f"max 12 open, size <= 3x equity. Size limits: {lim_src} (other coins: $5 minimum).\n",
           "| Scenario | " + " | ".join(f"Year from {y}" for y in yname)
           + " | Final | Return | Max DD | Worst month | Months up | Taken | Skipped small | Skipped cap | Max open | Pass? |",
           "|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"💼 <b>T2 trend — ${S.START_BAL:,.0f}, no BTC/ETH, top 30 / top {TOP_N} coins</b> ({YEARS} years, futures + real funding)",
          f"Top 30 / {TOP_N} picked every month from that time's volume ({len(used)} coins in total, {len(dead)} later "
          f"delisted - included). No compounding. Pass = ≥3 of 4 years up, DD &lt; 25%, final above ${S.START_BAL:,.0f}. "
          f"Risk per trade: " + ", ".join(f"${x:g} ({x / S.START_BAL * 100:.2g}%)" for x in risks)]
    for uname, udata, allowed in uni:
        trades = build_trades(udata, allowed, end_t)
        for risk_usd in risks:
            r = S.simulate(trades, closes, fund, times, risk_usd, limits)
            md, tgl = report_rows(f"{uname}, ${risk_usd:g} risk", r, yb)
            out.append(md)
            tg.append(tgl)
    if dead:
        out.append("\nCoins that stopped trading (included): " + ", ".join(dead))
    out.append("\nTop list per month: " + "; ".join(f"{k}: " + ", ".join(s[:-4] for s in v[:10]) + " ..."
                                                    for k, v in sorted(top.items())[-3:]))
    if failed or gaps_total:
        notes.append(f"{failed} volume file(s) failed to download, {gaps_total} candle gap(s) over 24h")
    if notes:
        out.append("\nNotes: " + "; ".join(notes))
        tg.append("Notes: " + bot.esc("; ".join(notes[:15])))
    out.append("\nCaveats: fills at the candle open (no slippage - small coins can slip more); no compounding; "
               "past results do not guarantee future results.")
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
