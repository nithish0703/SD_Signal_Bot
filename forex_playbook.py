"""'The Swing Playbook' on FOREX (the market it was written for) + T2 trend on the same data - report only.

Exactly the rules of swing_playbook.py (fixed before the crypto run, unchanged here) - only the market differs:
  Data      Dukascopy BID 1h candles (free public datafeed), 12 pairs, 10 years (+ ~13 months warm-up for weekly
            zones). Days end at 22:00 UTC (the usual forex 'New York close'), so there are no weekend candles,
            weeks start Sunday 22:00 UTC and 4h candles start 22/02/06/10/14/18 UTC. DST is ignored (1 h in summer).
  Costs     spread per pair (typical retail values below, in pips) paid once per trade (on the entry) + 0.5 pip
            slippage on every stop / intrabar exit. No commission, no swap. Data is BID only, so stops / targets
            of shorts trigger on the bid (in reality on the ask, ~1-2 pips earlier) - small next to 30-100 pip stops.
  Account   $300 start, $1 risk per trade (all results are also R), max 12 open, one trade per pair, notional
            <= 30x equity (normal forex leverage). Position size is not rounded to lots: a real $300 account
            could not open most of these trades (0.01 lot = $0.10/pip), so read the $ as R.
  Rows      T2 trend (EMA50/200 cross + 3 ATR trail + emergency stop, like crypto), Playbook A (as written:
            target next zone, breakeven at 75%), Playbook B (Playbook entry + T2 trail exit).
Pre-registered verdict (same as crypto): profitable = both halves (years 1-5 and 6-10) positive; 'beats T2' = also
>= $5 more than T2 in both halves with max DD at most 1 point worse.
Trade lists -> forex_playbook_trades.csv (workflow artifact).
"""
import csv
import lzma
import os
import re
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402
import swing_playbook as P  # noqa: E402
import trend_diag as D  # noqa: E402
import trend_robust as R  # noqa: E402
import trend_small as S  # noqa: E402

DAY, H4, H1 = P.DAY, P.H4, 3600 * 1000
SHIFT = 2 * H1                     # 22:00 UTC -> 00:00 (all grouping is done on shifted times)
YEARS = 10
WARMUP_DAYS = 400
RISK = 1.0
MIN_GAIN = 5.0
STOP_SLIP_PIPS = 0.5
PAIRS = {  # pair: spread in pips (typical retail)
    "EURUSD": 1.0, "GBPUSD": 1.4, "USDJPY": 1.1, "AUDUSD": 1.2, "USDCAD": 1.6, "USDCHF": 1.6,
    "NZDUSD": 1.8, "EURJPY": 1.6, "GBPJPY": 2.6, "EURGBP": 1.4, "AUDJPY": 1.9, "EURCHF": 1.9}
URL = "https://datafeed.dukascopy.com/datafeed/{p}/{y}/{m:02d}/BID_candles_hour_1.bi5"   # month is 0-based
CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forex_playbook_trades.csv")
_S = requests.Session()
_S.headers["User-Agent"] = "Mozilla/5.0 (research backtest)"


def pip(pair):
    return 0.01 if pair.endswith("JPY") else 0.0001


def fetch_month(pair, y, m):
    """-> (raw bytes or None, error)."""
    url = URL.format(p=pair, y=y, m=m - 1)
    err = None
    for k in range(4):
        try:
            r = _S.get(url, timeout=30)
            if r.status_code == 404:
                return None, None
            if r.status_code == 200:
                return r.content, None
            err = f"HTTP {r.status_code}"
        except Exception as e:
            err = type(e).__name__
        time.sleep(1.5 * (k + 1))
    return None, err


ORDERS = ("o c l h", "o h l c")    # order of the 4 price fields (Dukascopy candles: open, close, low, high)


def decode(raw, month_ms, point, order):
    """bi5 candle file -> 1h candles. Records: >I (seconds from month start), 4 x >I prices, >f volume."""
    data = lzma.decompress(raw) if raw else b""
    out = []
    for i in range(0, len(data) - 23, 24):
        ts, a, b, c, d, vol = struct.unpack(">IIIIIf", data[i:i + 24])
        if order == "o c l h":
            o, cl, lo, hi = a, b, c, d
        else:
            o, hi, lo, cl = a, b, c, d
        if vol <= 0 and o == hi == lo == cl:
            continue                                   # closed market (flat, no volume)
        out.append({"t": month_ms + ts * 1000, "o": o * point, "h": hi * point, "l": lo * point, "c": cl * point})
    return out


def check_order(raw, month_ms, point):
    """Pick the field order whose candles are consistent (high >= open/close >= low); None if neither is."""
    best = None
    for order in ORDERS:
        c = decode(raw, month_ms, point, order)
        if not c:
            continue
        good = sum(1 for x in c if x["h"] >= max(x["o"], x["c"]) - 1e-12 and x["l"] <= min(x["o"], x["c"]) + 1e-12)
        ts = [x["t"] for x in c]                       # hourly, increasing, inside this month
        times_ok = (all(b > a for a, b in zip(ts, ts[1:])) and all((b - a) % H1 == 0 for a, b in zip(ts, ts[1:]))
                    and ts[-1] - month_ms < 32 * DAY)
        if times_ok and good / len(c) > 0.99 and (best is None or good > best[1]):
            best = (order, good)
    return best[0] if best else None


def load_pair(pair, months, notes):
    """-> 4h candles on the shifted time axis (forex days), or None."""
    with ThreadPoolExecutor(6) as ex:
        raws = list(ex.map(lambda ym: (ym, fetch_month(pair, *ym)), months))
    errs = [e for (_, (_, e)) in raws if e]
    point = None
    order = None
    rows = []
    for (y, m), (raw, _) in raws:
        if not raw:
            continue
        month_ms = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
        if order is None:
            for pt in (1e-5, 1e-3):                    # JPY pairs are quoted with 3 decimals
                o = check_order(raw, month_ms, pt)
                if o:
                    c = decode(raw, month_ms, pt, o)
                    med = sorted(x["c"] for x in c)[len(c) // 2]
                    ok_range = (50 < med < 250) if pair.endswith("JPY") else (0.3 < med < 3)
                    if ok_range:
                        point, order = pt, o
                        break
            if order is None:
                notes.append(f"{pair}: data format not recognised - pair skipped")
                return None
        rows += decode(raw, month_ms, point, order)
    if errs:
        notes.append(f"{pair}: {len(errs)} month file(s) failed ({errs[0]})")
    if not rows:
        return None
    rows.sort(key=lambda x: x["t"])
    out = []
    for x in rows:                                     # 1h -> 4h on the shifted axis
        ts = x["t"] + SHIFT
        k = ts // H4 * H4
        if out and out[-1]["t"] == k:
            c = out[-1]
            c["h"], c["l"], c["c"] = max(c["h"], x["h"]), min(c["l"], x["l"]), x["c"]
        else:
            out.append({"t": k, "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"]})
    return out


def apply_costs(trades, pair_of):
    """spread on the entry (bid data: buys pay the ask), stop slippage on intrabar exits."""
    for tr in trades:
        pr = pair_of(tr)
        s, sp = tr["side"], PAIRS[pr] * pip(pr)
        tr["entry"] += s * sp
        if "stop" in tr:                               # playbook: risk = fill -> stop
            tr["risk"] = s * (tr["entry"] - tr["stop"])
        if tr.get("exit") is not None and tr.get("intrabar"):
            tr["exit"] -= s * STOP_SLIP_PIPS * pip(pr)
    return [tr for tr in trades if tr["risk"] > 0]


def main():
    t0 = time.time()
    now = datetime.now(timezone.utc)
    start = (now.timestamp() * 1000 - YEARS * 365.25 * DAY) + SHIFT
    first = datetime.fromtimestamp((start - SHIFT) / 1000 - WARMUP_DAYS * 86400, timezone.utc)
    months, y, m = [], first.year, first.month
    while (y, m) <= (now.year, now.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    notes, data = [], {}
    for pair in PAIRS:
        H = load_pair(pair, months, notes)
        if H:
            data[pair] = H
            print(f"{pair}: {len(H)} 4h candles {datetime.fromtimestamp((H[0]['t'] - SHIFT) / 1000, timezone.utc):%Y-%m-%d}"
                  f" .. {datetime.fromtimestamp((H[-1]['t'] - SHIFT) / 1000, timezone.utc):%Y-%m-%d}, "
                  f"last close {H[-1]['c']:.5g}", flush=True)
    if len(data) < 6:
        msg = (f"💱 <b>Forex Swing Playbook</b>: not enough forex data from Dukascopy ({len(data)}/{len(PAIRS)} pairs)."
               f"\n" + bot.esc("; ".join(notes[:12])))
        print(re.sub(r"</?b>", "", msg))
        bot.tg(msg)
        return
    S.START_BAL, S.TAKER, S.MAX_LEV = 300.0, 0.0, 30.0
    closes = {s: {x["t"]: x["c"] for x in H} for s, H in data.items()}
    times = sorted({t for s in closes for t in closes[s] if t >= start})
    end_t = times[-1]
    allowed = lambda sym, t: True
    yb = [start + i * 365.25 * DAY for i in range(YEARS + 1)]
    yname = [f"{datetime.fromtimestamp((yb[i] - SHIFT) / 1000, timezone.utc):%b %y}" for i in range(YEARS)]
    half = YEARS // 2
    keys = ["breaks", "expired_no_retest", "expired_retested", "dead", "star_no_ema", "gap_past_stop",
            "target_fallback", "target_too_close", "entries", "skipped_universe"]
    rows = [("T2 trend (EMA50/200 + trail)", None), ("A. Swing Playbook as written", "A"),
            ("B. Playbook entry + T2 trail exit", "B")]
    res, cnts = {}, {}
    for name, ver in rows:
        if ver is None:
            trades = D.build(data, allowed, end_t, emergency=R.EMERGENCY)
        else:
            cnts[ver] = dict.fromkeys(keys, 0)
            trades = P.build(data, allowed, end_t, ver, cnts[ver], start)
        trades = apply_costs([x for x in trades if x["t_in"] >= start], lambda tr: tr["sym"])
        r = R.simulate(trades, closes, {}, times, RISK, None, 0.0)
        y = R.yearly(r["curve"], yb)
        w, up, nm = R.worst_month(r["curve"])
        closed = [x for x in r["log"] if x["why"] != "still open"]
        hold = (sum(x["t_out"] - x["t_in"] for x in closed) / len(closed) / DAY) if closed else 0.0
        why = {}
        for x in closed:
            why[x["why"]] = why.get(x["why"], 0) + 1
        per = {}
        for x in r["log"]:
            per[x["sym"]] = per.get(x["sym"], 0.0) + x["net"]
        res[name] = dict(r=r, y=y, h1=sum(y[:half]), h2=sum(y[half:]), w=w, up=up, nm=nm, st=D.stats(r["log"]),
                         hold=hold, why=why, per=per, best=max((x["net"] for x in closed), default=0.0),
                         long=sum(x["net"] for x in r["log"] if x["side"] == "LONG"),
                         short=sum(x["net"] for x in r["log"] if x["side"] == "SHORT"))
        print(name, round(r["final"], 2), flush=True)
    ref = res[rows[0][0]]

    def verdict(name):
        v = res[name]
        prof = v["h1"] > 0 and v["h2"] > 0
        if name == rows[0][0]:
            return "reference (" + ("profitable in both halves" if prof else "NOT profitable in both halves") + ")"
        beats = (v["h1"] >= ref["h1"] + MIN_GAIN and v["h2"] >= ref["h2"] + MIN_GAIN
                 and v["r"]["mdd"] <= ref["r"]["mdd"] + 0.01)
        return ("profitable, BEATS T2" if prof and beats else "profitable, does not beat T2" if prof
                else "NOT profitable in both halves")
    out = [f"## The Swing Playbook on FOREX - {len(data)} pairs, $300, ${RISK:g} risk (= 1R), {YEARS} years\n",
           "Costs: spread per pair (" + ", ".join(f"{p} {s:g}" for p, s in PAIRS.items() if p in data)
           + f" pips) + {STOP_SLIP_PIPS:g} pip stop slippage, no swap. Max {S.MAX_OPEN} open, one trade per pair. "
             f"Rules identical to the crypto run (swing_playbook.py). Verdict: profitable = both halves (years 1-{half},"
             f" {half + 1}-{YEARS}) positive; beats T2 = also >= ${MIN_GAIN:g} more than T2 in both halves, DD at most 1 "
             "point worse. Position sizes are not rounded to lots, so read $ as R.\n",
           "| Strategy | " + " | ".join(yname) + " | Half 1 | Half 2 | Final | Max DD | Worst month | Months up | Trades "
           "| Win % | PF | Avg R | Avg hold | Best trade | Long | Short | Exits | Verdict |",
           "|---|" + "---|" * YEARS + "---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tg = [f"💱 <b>Swing Playbook on FOREX</b> ({len(data)} pairs, $300, ${RISK:g} = 1R, {YEARS} years, spread + "
          f"slippage)\nRule fixed before running: profitable = both halves (yrs 1-{half}, {half + 1}-{YEARS}) positive; "
          f"beats T2 = also ≥${MIN_GAIN:g} more than T2 in both halves and DD ≤1 pt worse."]
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
        lines.append(f"{'✅' if 'BEATS' in verdict(name) else '▫️'} <b>{bot.esc(name)}</b>: <b>${v['r']['final']:,.0f}</b>"
                     f" ({v['r']['final'] - 300:+,.0f}R), DD −{v['r']['mdd'] * 100:.1f}%, halves ${v['h1']:+,.0f} / "
                     f"${v['h2']:+,.0f}, {st['n']} trades, win {st['win']:.0f}%, PF {st['pf']:.2f}, avg hold "
                     f"{v['hold']:.1f} d → {bot.esc(verdict(name))}")
    tg.append("\n".join(lines))
    out.append("\n**P&L per pair ($ = R)**\n\n| Strategy | " + " | ".join(data) + " |\n|---|" + "---|" * len(data))
    for name, _ in rows:
        out.append(f"| {name} | " + " | ".join(f"{res[name]['per'].get(p, 0):+.1f}" for p in data) + " |")
    fl = []
    for ver, c in cnts.items():
        txt = (f"{ver}: breaks {c['breaks']} → entry signals {c['entries']}. No retest within 10 days (price ran "
               f"away): {c['expired_no_retest']}; retested but no star+EMA: {c['expired_retested']}; dead (closed back "
               f"through the zone): {c['dead']}; star but wrong side of EMA50: {c['star_no_ema']}")
        if ver == "A":
            txt += f"; target < 1R skipped: {c['target_too_close']}; no next level (3R used): {c['target_fallback']}"
        fl.append(txt)
    out.append("\n**Setup funnel**\n\n" + "\n\n".join(fl))
    tg.append("<b>Setup funnel</b>\n" + bot.esc("\n".join(fl)))
    tg.append(f"Result: A (as written) → <b>{bot.esc(verdict(rows[1][0]))}</b>; B (trail exit) → "
              f"<b>{bot.esc(verdict(rows[2][0]))}</b>; T2 on forex → {bot.esc(verdict(rows[0][0]))}.")
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "pair", "side", "entry_time_utc", "exit_time_utc", "entry", "exit", "units", "net_usd_R",
                    "why"])
        tf = lambda ms: datetime.fromtimestamp((ms - SHIFT) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else ""
        for name, _ in rows:
            for x in res[name]["r"]["log"]:
                w.writerow([name, x["sym"], x["side"], tf(x["t_in"]), tf(x["t_out"]), f"{x['entry']:.6g}",
                            f"{x['exit']:.6g}", f"{x['qty']:.6g}", f"{x['net']:.4f}", x["why"]])
    if notes:
        out.append("\nNotes: " + "; ".join(notes))
        tg.append("Notes: " + bot.esc("; ".join(notes[:12])))
    out.append("\nPast results do not guarantee future results. Report only - India: forex only via INR / exchange-"
               "listed pairs on NSE/BSE (RBI/FEMA); offshore forex platforms are not permitted.")
    tg.append(f"Report only. Trades: Actions → this run → Artifacts → forex-playbook-trades. "
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
