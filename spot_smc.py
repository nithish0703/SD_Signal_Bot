"""Top-50 SPOT backtest of the live SMC/ICT bot (LONG only, spot fees). Report only - changes nothing live.

Same code as the live bot: bot.find_smc_setups (FVG entry from smc_filters.json), liquidity TP,
the validated quality filters, cooldown / breaker / max-per-candle rules (bot.tag_sequence).
Spot differences: LONG only (no shorting on spot), spot fees 0.20% round trip (0.15% with BNB).

Universe: point-in-time. Candidates = today's top-N Binance spot USDT pairs by 24h quote volume;
a coin is only traded on days when it was inside the top 50 by the previous 7 days' quote volume
(computed from the candles themselves), so a coin that became big later is not traded before that.
"""
import bisect
import os
import re
import sys
import time
from collections import defaultdict

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

START = "2022-01-01"
CANDIDATES = int(os.getenv("SPOT_CANDIDATES", "80"))
TOP_N = int(os.getenv("SPOT_TOP_N", "50"))
FEES = (0.20, 0.15)
EX = "Prev high/low (liquidity)"
SPOT_TICKER = "https://data-api.binance.vision/api/v3/ticker/24hr"
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "EUR", "EURI", "AEUR", "USDE", "USD1", "XUSD",
          "PAXG", "WBTC", "WBETH", "BFUSD", "RLUSD", "USDS", "PYUSD", "GBP", "TRY", "BRL"}


def spot_candidates(n):
    r = requests.get(SPOT_TICKER, timeout=30)
    r.raise_for_status()
    rows = []
    for x in r.json():
        sym = x.get("symbol", "")
        if not re.fullmatch(r"[A-Z0-9]{2,20}USDT", sym):
            continue
        base = sym[:-4]
        if base in STABLE or base in getattr(bot, "EXCLUDE_BASES", set()) or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        try:
            qv, last, cnt = float(x["quoteVolume"]), float(x["lastPrice"]), int(x.get("count", 1))
        except (KeyError, TypeError, ValueError):
            continue
        if last > 0 and cnt > 0:
            rows.append((qv, sym))
    rows.sort(reverse=True)
    return [s for _, s in rows[:n]]


def hours_since(date):
    t0 = time.mktime(time.strptime(date, "%Y-%m-%d")) - time.timezone
    return int((time.time() - t0) // 3600) + 10


def daily_volume(C):
    """UTC day (ms) -> quote volume (close x base volume)."""
    out = defaultdict(float)
    for x in C:
        out[x["t"] // 86400000 * 86400000] += x["c"] * x["v"]
    return out


def pit_members(vols, top_n):
    """day -> set of coins in the top_n by the PREVIOUS 7 days' quote volume (no look-ahead)."""
    days = sorted({d for v in vols.values() for d in v})
    members = {}
    for i, d in enumerate(days):
        prev = days[max(0, i - 7):i]
        if not prev:
            continue
        score = [(sum(v.get(p, 0.0) for p in prev), s) for s, v in vols.items()]
        score = [x for x in score if x[0] > 0]
        score.sort(reverse=True)
        members[d] = {s for _, s in score[:top_n]}
    return members


def stats(trades, fee):
    rs = [t["res"][EX] - fee / t["slp"] for t in trades]
    if not rs:
        return None
    eq = peak = dd = 0.0
    for t, r in sorted(zip([t["time"] for t in trades], rs)):
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    by_year = defaultdict(float)
    for t, r in zip(trades, rs):
        by_year[time.gmtime(t["time"] / 1000).tm_year] += r
    return {"n": len(rs), "win": sum(1 for t in trades if t["res"][EX] > 0) / len(rs) * 100,
            "gross": sum(t["res"][EX] for t in trades) / len(rs), "net": sum(rs) / len(rs),
            "total": sum(rs), "dd": dd, "years": dict(sorted(by_year.items()))}


def line(name, s):
    if not s:
        return f"{name}: no trades"
    yrs = " ".join(f"{y % 100}:{v:+.0f}" for y, v in s["years"].items())
    return (f"{name}: {s['n']} trades, win {s['win']:.0f}%, gross {s['gross']:+.2f}R, net {s['net']:+.2f}R/trade, "
            f"total {s['total']:+.0f}R, max DD {s['dd']:.0f}R | per year (R): {yrs}")


def main():
    t_start = time.time()
    cands = spot_candidates(CANDIDATES)
    if "BTCUSDT" not in cands:
        cands.insert(0, "BTCUSDT")
    bot.SYMBOLS[:] = cands
    total = hours_since(START)
    print(f"{len(cands)} candidates, {total} x 1h candles each (max)", flush=True)

    C, _ = bot.fetch_history("BTCUSDT", "1h", total, source="spot-vision")
    btc_fn = bot.btc_trend_fn(bot._aggregate(C, 4))
    ex_fn = bot._smc(bot._smc_liq)
    setups, vols, got = [], {}, []
    for sym in cands:
        try:
            if sym != "BTCUSDT":
                C, _ = bot.fetch_history(sym, "1h", total, source="spot-vision")
        except Exception as e:
            print(sym, "data error", e, flush=True)
            continue
        if len(C) < 500:
            continue
        got.append(sym)
        vols[sym] = daily_volume(C)
        H = bot._aggregate(C, 4)
        n0 = len(setups)
        for st in bot.tag_btc(bot.find_smc_setups(C, H, bot.SMC_ENTRY), sym, btc_fn):
            if st["side"] != "LONG" or bot.sl_pct(st) < bot.MIN_SL_PCT:
                continue
            st["sym"], st["slp"] = sym, bot.sl_pct(st)
            st["res"] = {EX: ex_fn(C, st)}
            if st["res"][EX] is None:
                continue
            _, ci = bot._exit_detail(C, st, "LONG", "price", None, st["sl"])
            st["close_ct"] = C[ci]["ct"] if ci is not None else None
            setups.append(st)
        print(f"{sym}: {len(C)} candles from {time.strftime('%Y-%m-%d', time.gmtime(C[0]['t'] / 1000))}, "
              f"{len(setups) - n0} long setups", flush=True)
        del C, H

    members = pit_members(vols, TOP_N)
    for st in setups:
        day = st["time"] // 86400000 * 86400000
        st["pit"] = st["sym"] in members.get(day, set())
    # cooldown / breaker / max-per-candle act on the signals the live bot would actually send
    # sequence limits only where live uses them (active in smc_filters.json), like live_sequence_ok / live_bar_ok
    on = [f for v, f in (("SMC_COOLDOWN", "cool_ok"), ("SMC_BREAKER", "breaker_ok"),
                         ("SMC_MAX_PER_BAR", "bar_ok"), ("SMC_MAX_SAME_DIR", "dir_ok")) if v in bot.SMC_ACTIVE]
    seq_ok = lambda t: all(t.get(f, True) for f in on)
    nofilt = [t for t in setups if t["pit"]]
    today = [dict(t) for t in setups if bot.smc_quality_ok(t)]
    bot.tag_sequence(today, EX)
    today = [t for t in today if seq_ok(t)]
    live = [t for t in setups if t["pit"] and bot.smc_quality_ok(t)]
    bot.tag_sequence(live, EX)
    live = [t for t in live if seq_ok(t)]

    main_s = stats(live, FEES[0])
    early = stats([t for t in live if time.gmtime(t["time"] / 1000).tm_year <= 2023], FEES[0])
    late = stats([t for t in live if time.gmtime(t["time"] / 1000).tm_year >= 2024], FEES[0])
    yrs_pos = sum(1 for v in (main_s or {}).get("years", {}).values() if v > 0)
    ok = bool(main_s and main_s["net"] >= 0.10 and yrs_pos >= 4 and early and late and early["net"] > 0 and late["net"] > 0)

    per_coin = defaultdict(float)
    for t in live:
        per_coin[t["sym"]] += t["res"][EX] - FEES[0] / t["slp"]
    ranked = sorted(per_coin.items(), key=lambda x: x[1])
    coins_pos = sum(1 for _, v in ranked if v > 0)

    lines = [
        f"🧪 <b>SPOT top-{TOP_N} SMC backtest</b> (live bot code, LONG only, 1h, {START} → today)",
        f"{len(got)} coins with data (candidates = today's top {CANDIDATES}); traded only while in the top {TOP_N} "
        f"(point-in-time, 7-day volume). Filters: {bot.smc_quality_txt()}. Entry: {bot.entry_name()}. Exit: liquidity TP.",
        "",
        line(f"✅ Main (spot fee {FEES[0]:.2f}%)", main_s),
        line(f"BNB fee {FEES[1]:.2f}%", stats(live, FEES[1])),
        line("2022–23 only (before the filters were picked)", early),
        line("2024–26 only", late),
        line("Reference: no quality filters", stats(nofilt, FEES[0])),
        line("Reference: today's list for all years (survivorship bias)", stats(today, FEES[0])),
        f"Coins net positive: {coins_pos}/{len(ranked)}. Best: " + ", ".join(f"{s[:-4]} {v:+.0f}R" for s, v in ranked[::-1][:5])
        + " | Worst: " + ", ".join(f"{s[:-4]} {v:+.0f}R" for s, v in ranked[:5]),
        "",
        f"Pass rule (set before the run): net ≥ +0.10R/trade at {FEES[0]:.2f}% fees, ≥4 positive years, "
        f"2022–23 and 2024–26 both positive → {'✅ PASS' if ok else '❌ FAIL'}",
        f"1R = your risk per trade (1% risk → total R = % of account). Slippage not included. Not financial advice. "
        f"Run time {(time.time() - t_start) / 60:.0f} min.",
    ]
    msg = "\n".join(lines)
    print(msg)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write("## Spot top-50 SMC backtest\n\n" + re.sub(r"</?b>", "**", msg).replace("\n", "  \n") + "\n")
    for i in range(0, len(msg), 3900):
        bot.tg(msg[i:i + 3900])


if __name__ == "__main__":
    main()
